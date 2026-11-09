from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from omniagent import __version__
from omniagent.config import LLMConfig
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.llm.base import LLMAdapter
from omniagent.models import LLMResponse, utc_now


class OpenAICompatibleAdapter(LLMAdapter):
    """Unified LLM adapter targeting the OpenAI Chat Completions API.

    Works with any provider that exposes an OpenAI-compatible
    ``/v1/chat/completions`` endpoint: OpenAI, Deepseek, Qwen,
    Volcengine Ark, Ollama, vLLM, LiteLLM, etc.

    Structured output uses a three-tier fallback:
      1. ``response_format`` with ``json_schema`` (OpenAI / Deepseek)
      2. ``response_format`` with ``json_object`` + schema in system prompt
      3. Schema instructions in system prompt only (universal)
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.model = config.model.strip()
        if config.provider.lower() == "openrouter" and not self.model:
            self.model = "openrouter/free"
        if config.provider.lower() == "opencode" and not self.model:
            self.model = "deepseek-v4-flash-free"
        if (
            config.provider.lower() == "openrouter"
            and config.openrouter_free_only
            and not self._is_openrouter_free_model(self.model)
        ):
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                "OpenRouter free-only 模式只允许 openrouter/free 或 :free 模型",
                details={
                    "provider": config.provider,
                    "model": self.model,
                    "openrouter_free_only": True,
                },
            )
        if (
            config.provider.lower() == "opencode"
            and config.opencode_free_only
            and not self._is_opencode_free_model(self.model)
        ):
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                "OpenCode Zen free-only 模式只允许以 -free 结尾的模型",
                details={
                    "provider": config.provider,
                    "model": self.model,
                    "opencode_free_only": True,
                },
            )
        self.endpoint = self._resolve_endpoint(config.api_base, provider=config.provider)
        if (
            not config.api_key
            and not self._is_local_endpoint(self.endpoint)
            and not self._allows_anonymous_opencode_request()
        ):
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                f"缺少 LLM API Key (provider={config.provider})",
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_health(self) -> dict[str, Any]:
        response = self.generate_structured(
            prompt_name="health_check",
            system_prompt='只返回一个 JSON 对象，格式为 {"ok": true}。',
            request_payload={"ping": "health_check"},
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        return {
            "healthy": bool(response.structured_output.get("ok", False)),
            "provider": self.config.provider,
            "model": response.model,
            "requested_model": self.model,
            "endpoint": self.endpoint,
            "request_id": response.request_id,
        }

    def generate_structured(
        self,
        *,
        prompt_name: str,
        system_prompt: str,
        request_payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> LLMResponse:
        request_id = f"llm_{uuid.uuid4().hex[:10]}"
        headers = self._headers()

        schema_instruction = self._schema_instruction(schema)
        user_content = json.dumps(request_payload, ensure_ascii=False, indent=2)

        # ISSUE-001: capture wall-clock latency and which fallback tier
        # ultimately produced the usable response so every downstream
        # consumer (workspace log, gateway accumulator, CLI stats) can read
        # the same numbers without re-timing anything.
        started_at = utc_now()
        perf_start = time.perf_counter()
        deadline_seconds = self._payload_timeout_seconds(request_payload)
        deadline_at = time.monotonic() + deadline_seconds if deadline_seconds is not None else None
        total_attempts = 0
        tier_used: str | None = None

        # Tier 1: response_format with json_schema (strict)
        body_t1 = self._build_body_tier1(
            system_prompt,
            user_content,
            schema,
            prompt_name,
        )
        raw, err, status, err_body, attempts = self._request_with_retries(
            body_t1,
            headers,
            deadline_at=deadline_at,
            deadline_seconds=deadline_seconds,
        )
        total_attempts += attempts
        if raw is not None:
            tier_used = "tier1_json_schema"

        # Tier 2: response_format json_object + schema in system prompt
        # 触发条件：HTTP 400（provider 不支持 json_schema）或超时/网络错误
        # （strict json_schema 通常比 json_object 慢，降级有助于避免再次超时）
        if raw is None and (status == 400 or self._is_timeout_error(err)):
            body_t2 = self._build_body_tier2(
                system_prompt,
                schema_instruction,
                user_content,
            )
            raw, err, status, err_body, attempts = self._request_with_retries(
                body_t2,
                headers,
                deadline_at=deadline_at,
                deadline_seconds=deadline_seconds,
            )
            total_attempts += attempts
            if raw is not None:
                tier_used = "tier2_json_object"

        # Tier 3: no response_format, schema instructions only (universal)
        if raw is None and (status == 400 or self._is_timeout_error(err)):
            body_t3 = self._build_body_tier3(
                system_prompt,
                schema_instruction,
                user_content,
            )
            raw, err, status, err_body, attempts = self._request_with_retries(
                body_t3,
                headers,
                deadline_at=deadline_at,
                deadline_seconds=deadline_seconds,
            )
            total_attempts += attempts
            if raw is not None:
                tier_used = "tier3_prompt_only"

        if raw is None:
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                f"LLM 调用失败 ({self.config.provider}): {err}",
                details={
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "endpoint": self.endpoint,
                    "http_status": status,
                    "error_body": err_body,
                    "attempts": total_attempts,
                    "latency_ms": round((time.perf_counter() - perf_start) * 1000, 2),
                },
            )

        raw_output = self._extract_text(raw)
        structured_output = self._parse_structured_output(raw_output)
        latency_ms = round((time.perf_counter() - perf_start) * 1000, 2)
        return LLMResponse(
            provider=self.config.provider,
            model=str(raw.get("model") or self.model),
            prompt_name=prompt_name,
            request_id=str(raw.get("id", request_id)),
            raw_output=raw_output,
            structured_output=structured_output,
            usage=dict(raw.get("usage", {})),
            latency_ms=latency_ms,
            tier=tier_used,
            attempts=total_attempts,
            started_at=started_at,
        )

    # ------------------------------------------------------------------
    # Request body builders
    # ------------------------------------------------------------------

    def _build_body_tier1(
        self,
        system_prompt: str,
        user_content: str,
        schema: dict[str, Any],
        prompt_name: str,
    ) -> dict[str, Any]:
        """Tier 1: ``response_format`` with ``json_schema``."""
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", prompt_name)
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": safe_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "temperature": self.config.temperature,
        }

    def _build_body_tier2(
        self,
        system_prompt: str,
        schema_instruction: str,
        user_content: str,
    ) -> dict[str, Any]:
        """Tier 2: ``response_format`` with ``json_object`` + schema in prompt."""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": schema_instruction},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.config.temperature,
        }

    def _build_body_tier3(
        self,
        system_prompt: str,
        schema_instruction: str,
        user_content: str,
    ) -> dict[str, Any]:
        """Tier 3: no ``response_format``, schema in prompt only (universal)."""
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": schema_instruction},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.config.temperature,
        }

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def _request_with_retries(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        deadline_at: float | None = None,
        deadline_seconds: float | None = None,
    ) -> tuple[dict[str, Any] | None, Exception | None, int | None, str | None, int]:
        last_error: Exception | None = None
        status_code: int | None = None
        error_body: str | None = None
        attempts = 0
        retry_count = 1 if deadline_seconds is not None else self.config.max_retries + 1

        for attempt in range(retry_count):
            timeout = self._attempt_timeout(
                deadline_at=deadline_at,
                deadline_seconds=deadline_seconds,
                retry_count=retry_count,
            )
            if timeout <= 0:
                last_error = TimeoutError("LLM call deadline exceeded")
                break
            attempts = attempt + 1
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                return raw, None, None, None, attempts
            except urllib.error.HTTPError as exc:
                last_error = exc
                status_code = exc.code
                error_body = self._read_error_body(exc)
                if exc.code == 400:
                    return None, last_error, status_code, error_body, attempts
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 4))
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 4))

        return None, last_error, status_code, error_body, attempts

    def _attempt_timeout(
        self,
        *,
        deadline_at: float | None,
        deadline_seconds: float | None,
        retry_count: int,
    ) -> float:
        timeout = float(self.config.timeout_seconds)
        if deadline_seconds is not None:
            timeout = min(timeout, max(1.0, deadline_seconds))
        if deadline_at is not None:
            timeout = min(timeout, max(0.0, deadline_at - time.monotonic()))
        return timeout

    @staticmethod
    def _payload_timeout_seconds(payload: Any) -> float | None:
        if isinstance(payload, dict):
            constraints = payload.get("constraints")
            if isinstance(constraints, dict):
                timeout = constraints.get("timeout_seconds")
                if isinstance(timeout, (int, float)) and timeout > 0:
                    return float(timeout)
            for value in payload.values():
                found = OpenAICompatibleAdapter._payload_timeout_seconds(value)
                if found is not None:
                    return found
        if isinstance(payload, list):
            for value in payload:
                found = OpenAICompatibleAdapter._payload_timeout_seconds(value)
                if found is not None:
                    return found
        return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _extract_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = [item.get("text", "") for item in content if isinstance(item, dict)]
                joined = "".join(parts).strip()
                if joined:
                    return joined
        raise OmniAgentError(
            ErrorCode.LLM_ERROR,
            f"LLM 响应中未找到可解析文本 ({self.config.provider})",
            details=payload,
        )

    def _parse_structured_output(self, raw_output: str) -> dict[str, Any]:
        normalized = raw_output.strip()
        if normalized.startswith("```"):
            lines = normalized.splitlines()
            if len(lines) >= 3:
                normalized = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            start = normalized.find("{")
            end = normalized.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise OmniAgentError(
                    ErrorCode.LLM_ERROR,
                    f"LLM 结构化输出解析失败 ({self.config.provider}): 未找到 JSON 对象",
                    details={"raw_output": raw_output},
                )
            try:
                parsed = json.loads(normalized[start : end + 1])
            except json.JSONDecodeError as exc:
                raise OmniAgentError(
                    ErrorCode.LLM_ERROR,
                    f"LLM 结构化输出解析失败 ({self.config.provider}): {exc}",
                    details={"raw_output": raw_output},
                ) from exc
        if not isinstance(parsed, dict):
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                f"LLM 结构化输出解析失败 ({self.config.provider}): 顶层结果不是对象",
                details={"raw_output": raw_output},
            )
        return parsed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_timeout_error(err: Exception | None) -> bool:
        if err is None:
            return False
        if isinstance(err, (socket.timeout, TimeoutError)):
            return True
        if isinstance(err, urllib.error.URLError):
            reason = getattr(err, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return True
            return "timed out" in str(reason).lower()
        return "timed out" in str(err).lower()

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "User-Agent": f"OmniAgent/{__version__}",
        }
        if self.config.api_key:
            h["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.provider.lower() == "openrouter":
            if self.config.openrouter_site_url:
                h["HTTP-Referer"] = self.config.openrouter_site_url
            if self.config.openrouter_app_title:
                h["X-OpenRouter-Title"] = self.config.openrouter_app_title
        return h

    def _schema_instruction(self, schema: dict[str, Any]) -> str:
        return (
            "你必须只返回一个合法 JSON 对象，不要输出解释、Markdown、代码块或额外文本。\n"
            "返回结果必须满足以下 JSON Schema:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )

    def _resolve_endpoint(self, api_base: str | None, *, provider: str = "") -> str:
        if not api_base:
            if provider.lower() == "openrouter":
                return "https://openrouter.ai/api/v1/chat/completions"
            if provider.lower() == "opencode":
                return "https://opencode.ai/zen/v1/chat/completions"
            return "https://api.openai.com/v1/chat/completions"
        if api_base.rstrip("/").endswith("/chat/completions"):
            return api_base
        return f"{api_base.rstrip('/')}/chat/completions"

    @staticmethod
    def _is_local_endpoint(endpoint: str) -> bool:
        return any(host in endpoint for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))

    @staticmethod
    def _is_openrouter_free_model(model: str) -> bool:
        normalized = model.strip().lower()
        return normalized == "openrouter/free" or normalized.endswith(":free")

    @staticmethod
    def _is_opencode_free_model(model: str) -> bool:
        return model.strip().lower().endswith("-free")

    def _allows_anonymous_opencode_request(self) -> bool:
        return (
            self.config.provider.lower() == "opencode"
            and self.config.opencode_allow_anonymous
            and self.config.opencode_free_only
            and self._is_opencode_free_model(self.model)
        )

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str | None:
        try:
            body = exc.read()
        except Exception:
            return None
        if not body:
            return None
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return str(body)

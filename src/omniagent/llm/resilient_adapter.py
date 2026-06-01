from __future__ import annotations

from typing import Any

from omniagent.errors import OmniAgentError
from omniagent.llm.base import LLMAdapter
from omniagent.models import LLMResponse


class ResilientLLMAdapter(LLMAdapter):
    def __init__(self, primary: LLMAdapter, fallback: LLMAdapter | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.degraded_to_fallback = False
        self.last_error: OmniAgentError | None = None

    def generate_structured(
        self,
        *,
        prompt_name: str,
        system_prompt: str,
        request_payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> LLMResponse:
        if not self.degraded_to_fallback:
            try:
                return self.primary.generate_structured(
                    prompt_name=prompt_name,
                    system_prompt=system_prompt,
                    request_payload=request_payload,
                    schema=schema,
                )
            except OmniAgentError as exc:
                self.last_error = exc
                if self.fallback is None:
                    raise
                self.degraded_to_fallback = True
        if self.fallback is None:
            raise self.last_error or RuntimeError("LLM primary failed without fallback")
        return self.fallback.generate_structured(
            prompt_name=prompt_name,
            system_prompt=system_prompt,
            request_payload=request_payload,
            schema=schema,
        )

    def check_health(self) -> dict[str, Any]:
        try:
            primary = self.primary.check_health()
            primary_healthy = bool(primary.get("healthy", False))
        except OmniAgentError as exc:
            self.last_error = exc
            primary = {"healthy": False, "error": exc.to_dict()}
            primary_healthy = False

        fallback = self.fallback.check_health() if self.fallback else None
        if not primary_healthy and fallback is not None:
            self.degraded_to_fallback = True
        payload = {
            "healthy": primary_healthy or bool(fallback and fallback.get("healthy", False)),
            "degraded_to_fallback": self.degraded_to_fallback,
            "primary": primary,
            "fallback": fallback,
        }
        if self.last_error is not None:
            payload["last_error"] = self.last_error.to_dict()
        return payload

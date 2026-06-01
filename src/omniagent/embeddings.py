from __future__ import annotations

import json
import math
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from omniagent.config import EmbeddingConfig
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.logging_utils import get_logger
from omniagent.vector_recall import build_embedding

LOGGER = get_logger(__name__)

LOCAL_HASH_PROVIDER = "local_hash"
LOCAL_HASH_MODEL = "local-hash-v1"
LOCAL_HASH_VERSION = "local_hash_v1"
OPENAI_COMPATIBLE_EMBEDDING_VERSION = "openai_compatible_v1"
LOCAL_MODEL_EMBEDDING_VERSION = "local_model_onnx_v1"


@dataclass(slots=True)
class EmbeddingResult:
    embedding: list[float]
    provider: str
    model: str
    dimensions: int
    version: str
    latency_ms: float = 0.0
    fallback_used: bool = False
    error: str | None = None
    requested_provider: str | None = None
    requested_model: str | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_dimensions": self.dimensions,
            "embedding_version": self.version,
            "embedding_latency_ms": self.latency_ms,
            "embedding_fallback_used": self.fallback_used,
        }
        if self.error:
            payload["embedding_error"] = self.error
        if self.requested_provider:
            payload["embedding_requested_provider"] = self.requested_provider
        if self.requested_model:
            payload["embedding_requested_model"] = self.requested_model
        payload.update(self.extra_metadata)
        return payload


class EmbeddingProvider(Protocol):
    provider: str
    model: str
    version: str

    def embed(self, text: str) -> EmbeddingResult:
        raise NotImplementedError


class LocalHashEmbeddingProvider:
    provider = LOCAL_HASH_PROVIDER
    version = LOCAL_HASH_VERSION

    def __init__(self, config: EmbeddingConfig | None = None) -> None:
        self.dimensions = (config.dimensions if config and config.dimensions else 96) or 96
        self.model = LOCAL_HASH_MODEL

    def embed(self, text: str) -> EmbeddingResult:
        started = time.perf_counter()
        vector = build_embedding(text, dimensions=self.dimensions)
        return EmbeddingResult(
            embedding=vector,
            provider=self.provider,
            model=self.model,
            dimensions=len(vector),
            version=self.version,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )


class OpenAICompatibleEmbeddingProvider:
    """Embedding provider for OpenAI-compatible ``/v1/embeddings`` endpoints."""

    version = OPENAI_COMPATIBLE_EMBEDDING_VERSION

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.provider = config.provider.lower()
        self.model = config.model
        self.endpoint = self._resolve_endpoint(config.api_base)

    def embed(self, text: str) -> EmbeddingResult:
        if not self.model:
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                f"缺少 embedding model (provider={self.provider})",
            )
        if not self.config.api_key and not self._is_local_endpoint(self.endpoint):
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                f"缺少 embedding API Key (provider={self.provider})",
            )

        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
        }
        if self.config.dimensions:
            body["dimensions"] = self.config.dimensions

        headers = self._headers()
        started = time.perf_counter()
        raw = self._request_with_retries(body, headers)
        vector = self._extract_embedding(raw)
        return EmbeddingResult(
            embedding=vector,
            provider=self.provider,
            model=str(raw.get("model") or self.model),
            dimensions=len(vector),
            version=self.version,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def _request_with_retries(
        self, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        status_code: int | None = None
        error_body: str | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                status_code = exc.code
                error_body = self._read_error_body(exc)
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 4))
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(min(2**attempt, 4))
        raise OmniAgentError(
            ErrorCode.LLM_ERROR,
            f"Embedding 调用失败 ({self.provider}): {last_error}",
            details={
                "provider": self.provider,
                "model": self.model,
                "endpoint": self.endpoint,
                "http_status": status_code,
                "error_body": error_body,
            },
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    @staticmethod
    def _extract_embedding(payload: dict[str, Any]) -> list[float]:
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                "Embedding 响应中未找到 data[0].embedding",
                details=payload,
            )
        first = data[0]
        if not isinstance(first, dict):
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                "Embedding 响应 data[0] 不是对象",
                details=payload,
            )
        embedding = first.get("embedding")
        if not isinstance(embedding, list):
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                "Embedding 响应 data[0].embedding 不是数组",
                details=payload,
            )
        return [float(value) for value in embedding]

    @staticmethod
    def _resolve_endpoint(api_base: str | None) -> str:
        if not api_base:
            return "https://api.openai.com/v1/embeddings"
        normalized = api_base.rstrip("/")
        if normalized.endswith("/embeddings"):
            return normalized
        return f"{normalized}/embeddings"

    @staticmethod
    def _is_local_endpoint(endpoint: str) -> bool:
        return any(host in endpoint for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))

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


class LocalModelEmbeddingProvider:
    """In-process ONNX embedding provider.

    The provider intentionally lazy-loads ``onnxruntime``, ``tokenizers`` and
    ``numpy`` only when ``provider=local_model`` is used. Default OmniAgent
    installs and tests therefore remain dependency-free.
    """

    provider = "local_model"
    version = LOCAL_MODEL_EMBEDDING_VERSION

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.model = config.model
        self.model_file = self._resolve_model_file(config)
        self.tokenizer_file = self._resolve_tokenizer_file(config, self.model_file)
        if not self.model:
            if self.model_file:
                self.model = self.model_file.parent.name
            elif config.model_path:
                self.model = config.model_path.name
            else:
                self.model = "local-model"
        self.pooling = config.pooling.lower()
        self.normalize = config.normalize
        self.max_tokens = config.max_tokens
        self._runtime: dict[str, Any] | None = None
        self._fingerprint: str | None = None
        self._tokenizer_version: str | None = None

    def embed(self, text: str) -> EmbeddingResult:
        started = time.perf_counter()
        runtime = self._ensure_runtime()
        tokenizer = runtime["tokenizer"]
        session = runtime["session"]
        np = runtime["numpy"]

        encoding = tokenizer.encode(text)
        input_ids = _truncate_sequence(list(encoding.ids), self.max_tokens)
        attention_mask = _truncate_sequence(list(encoding.attention_mask), self.max_tokens)
        type_ids = _truncate_sequence(list(encoding.type_ids), self.max_tokens)
        if not input_ids:
            input_ids = [0]
            attention_mask = [0]
            type_ids = [0]

        inputs: dict[str, Any] = {}
        for model_input in session.get_inputs():
            name = str(model_input.name)
            if name == "input_ids":
                inputs[name] = np.asarray([input_ids], dtype=np.int64)
            elif name == "attention_mask":
                inputs[name] = np.asarray([attention_mask], dtype=np.int64)
            elif name in {"token_type_ids", "type_ids"}:
                inputs[name] = np.asarray([type_ids], dtype=np.int64)

        outputs = session.run(None, inputs)
        vector = self._pool(outputs, attention_mask)
        if self.normalize:
            vector = _normalize_vector(vector)
        return EmbeddingResult(
            embedding=vector,
            provider=self.provider,
            model=self.model,
            dimensions=len(vector),
            version=self.version,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            extra_metadata={
                "embedding_model_path": str(self.model_file) if self.model_file else "",
                "embedding_tokenizer_path": str(self.tokenizer_file)
                if self.tokenizer_file
                else "",
                "embedding_model_fingerprint": self._fingerprint or "",
                "embedding_tokenizer_version": self._tokenizer_version or "",
                "embedding_pooling": self.pooling,
                "embedding_normalize": self.normalize,
                "embedding_max_tokens": self.max_tokens,
            },
        )

    def _ensure_runtime(self) -> dict[str, Any]:
        if self._runtime is not None:
            return self._runtime
        if self.model_file is None:
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                "缺少本地 embedding 模型路径，请配置 OMNIAGENT_EMBEDDING_MODEL_PATH",
            )
        if self.tokenizer_file is None:
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                "缺少本地 embedding tokenizer，请配置 OMNIAGENT_EMBEDDING_TOKENIZER_PATH "
                "或在模型目录放置 tokenizer.json",
            )
        if not self.model_file.is_file():
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                f"本地 embedding 模型文件不存在: {self.model_file}",
            )
        if not self.tokenizer_file.is_file():
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                f"本地 embedding tokenizer 文件不存在: {self.tokenizer_file}",
            )
        try:
            import numpy as np  # type: ignore[import-not-found]
            import onnxruntime as ort  # type: ignore[import-not-found]
            import tokenizers  # type: ignore[import-not-found]
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                "local_model embedding 需要可选依赖: onnxruntime、tokenizers、numpy",
                details={"missing_dependency": str(exc)},
            ) from exc

        tokenizer = Tokenizer.from_file(str(self.tokenizer_file))
        if hasattr(tokenizer, "enable_truncation"):
            tokenizer.enable_truncation(max_length=self.max_tokens)
        session = ort.InferenceSession(str(self.model_file), providers=["CPUExecutionProvider"])
        self._fingerprint = self._build_fingerprint(self.model_file, self.tokenizer_file)
        self._tokenizer_version = str(getattr(tokenizers, "__version__", "unknown"))
        self._runtime = {
            "numpy": np,
            "tokenizer": tokenizer,
            "session": session,
        }
        return self._runtime

    def _pool(self, outputs: Any, attention_mask: list[int]) -> list[float]:
        if not isinstance(outputs, (list, tuple)) or not outputs:
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                "本地 embedding 模型没有输出",
            )
        output = _to_list(outputs[0])
        if _is_vector(output):
            return [float(value) for value in output]
        if _is_matrix(output):
            return [float(value) for value in output[0]]
        if not _is_tensor3(output):
            raise OmniAgentError(
                ErrorCode.LLM_ERROR,
                "本地 embedding 模型输出形状不支持",
                details={"shape": _shape_of(output)},
            )

        token_vectors = output[0]
        if self.pooling == "cls":
            return [float(value) for value in token_vectors[0]]
        if self.pooling != "mean":
            raise OmniAgentError(
                ErrorCode.CONFIG_INVALID,
                f"不支持的 embedding pooling: {self.pooling}",
            )
        return _mean_pool(token_vectors, attention_mask)

    @staticmethod
    def _resolve_model_file(config: EmbeddingConfig) -> Path | None:
        if config.model_path is None:
            return None
        path = config.model_path.expanduser().resolve()
        if path.suffix == ".onnx":
            return path
        return path / "model.onnx"

    @staticmethod
    def _resolve_tokenizer_file(
        config: EmbeddingConfig,
        model_file: Path | None,
    ) -> Path | None:
        if config.tokenizer_path is not None:
            return config.tokenizer_path.expanduser().resolve()
        if model_file is None:
            return None
        return model_file.parent / "tokenizer.json"

    @staticmethod
    def _build_fingerprint(model_file: Path, tokenizer_file: Path) -> str:
        digest = sha256()
        for path in (model_file, tokenizer_file):
            digest.update(str(path.name).encode("utf-8"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        return digest.hexdigest()[:16]


class UnavailableEmbeddingProvider:
    version = "unavailable"

    def __init__(self, config: EmbeddingConfig, reason: str) -> None:
        self.provider = config.provider.lower()
        self.model = config.model
        self.reason = reason

    def embed(self, text: str) -> EmbeddingResult:
        raise OmniAgentError(
            ErrorCode.CONFIG_INVALID,
            f"Embedding provider 不可用 ({self.provider}): {self.reason}",
        )


class FallbackEmbeddingProvider:
    def __init__(
        self,
        primary: EmbeddingProvider,
        fallback: LocalHashEmbeddingProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.provider = primary.provider
        self.model = primary.model
        self.version = getattr(primary, "version", "")

    def embed(self, text: str) -> EmbeddingResult:
        try:
            return self.primary.embed(text)
        except Exception as exc:
            LOGGER.warning(
                "embedding primary failed; using local fallback",
                extra={
                    "embedding_provider": self.primary.provider,
                    "embedding_model": self.primary.model,
                    "error": _format_error(exc),
                },
            )
            fallback_result = self.fallback.embed(text)
            fallback_result.fallback_used = True
            fallback_result.error = _format_error(exc)
            fallback_result.requested_provider = self.primary.provider
            fallback_result.requested_model = self.primary.model
            return fallback_result


def build_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    fallback = LocalHashEmbeddingProvider(config)
    provider = config.provider.lower()
    if not config.enabled or provider in {"local", "local_hash", "hash"}:
        return fallback
    if provider in {"local_model", "onnx", "onnx_local"}:
        return FallbackEmbeddingProvider(
            LocalModelEmbeddingProvider(config),
            fallback,
        )
    if provider in {"openai", "openai_compatible", "deepseek", "ark", "qwen", "ollama", "vllm", "litellm"}:
        return FallbackEmbeddingProvider(
            OpenAICompatibleEmbeddingProvider(config),
            fallback,
        )
    return FallbackEmbeddingProvider(
        UnavailableEmbeddingProvider(config, "unsupported provider"),
        fallback,
    )


def _format_error(exc: Exception) -> str:
    if isinstance(exc, OmniAgentError):
        return exc.message
    return str(exc)


def _truncate_sequence(values: list[int], max_tokens: int) -> list[int]:
    if max_tokens <= 0:
        return values
    return values[:max_tokens]


def _to_list(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _is_vector(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_number(item) for item in value)


def _is_matrix(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_vector(item) for item in value)


def _is_tensor3(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_matrix(item) for item in value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float))


def _mean_pool(token_vectors: list[list[float]], attention_mask: list[int]) -> list[float]:
    active_vectors = [
        vector for vector, active in zip(token_vectors, attention_mask, strict=False) if active
    ]
    if not active_vectors:
        active_vectors = token_vectors[:1]
    dimensions = len(active_vectors[0]) if active_vectors else 0
    if dimensions == 0:
        return []
    pooled: list[float] = []
    for index in range(dimensions):
        pooled.append(sum(float(vector[index]) for vector in active_vectors) / len(active_vectors))
    return pooled


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def _shape_of(value: Any) -> list[int]:
    shape: list[int] = []
    current = value
    while isinstance(current, list):
        shape.append(len(current))
        if not current:
            break
        current = current[0]
    return shape

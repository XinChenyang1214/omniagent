from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ErrorCode(StrEnum):
    CONFIG_INVALID = "config_invalid"
    VALIDATION_ERROR = "validation_error"
    STORAGE_ERROR = "storage_error"
    WORKSPACE_ERROR = "workspace_error"
    LLM_ERROR = "llm_error"
    PLANNER_ERROR = "planner_error"
    CRITIC_ERROR = "critic_error"
    PERMISSION_DENIED = "permission_denied"
    EXECUTION_ERROR = "execution_error"
    SKILL_ERROR = "skill_error"
    NOT_FOUND = "not_found"


class OmniAgentError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(slots=True)
class Result(Generic[T]):
    ok: bool
    value: T | None = None
    error: OmniAgentError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, value: T, **metadata: Any) -> "Result[T]":
        return cls(ok=True, value=value, metadata=metadata)

    @classmethod
    def failure(cls, error: OmniAgentError, **metadata: Any) -> "Result[T]":
        return cls(ok=False, error=error, metadata=metadata)

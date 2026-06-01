from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omniagent.models import LLMResponse


class LLMAdapter(ABC):
    @abstractmethod
    def generate_structured(
        self,
        *,
        prompt_name: str,
        system_prompt: str,
        request_payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> LLMResponse:
        raise NotImplementedError

    def check_health(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "provider": self.__class__.__name__,
            "mode": "basic",
        }

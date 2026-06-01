from __future__ import annotations

import unittest

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.llm.base import LLMAdapter
from omniagent.llm.resilient_adapter import ResilientLLMAdapter
from omniagent.models import LLMResponse
from omniagent.config import LLMConfig
from tests.llm_test_helper import StubLLMAdapter


class _FailingAdapter(LLMAdapter):
    def generate_structured(
        self,
        *,
        prompt_name: str,
        system_prompt: str,
        request_payload: dict,
        schema: dict,
    ) -> LLMResponse:
        raise OmniAgentError(ErrorCode.LLM_ERROR, "primary down")

    def check_health(self) -> dict:
        raise OmniAgentError(ErrorCode.LLM_ERROR, "health failed")


class LLMResilienceTest(unittest.TestCase):
    def test_resilient_adapter_falls_back_to_test_provider(self) -> None:
        adapter = ResilientLLMAdapter(
            primary=_FailingAdapter(),
            fallback=StubLLMAdapter(LLMConfig(provider="test", model="test-fallback")),
        )

        response = adapter.generate_structured(
            prompt_name="router_v1",
            system_prompt="route",
            request_payload={"message": "写一个文件"},
            schema={"type": "object"},
        )

        self.assertTrue(adapter.degraded_to_fallback)
        self.assertEqual(response.provider, "test")
        self.assertIn(
            response.structured_output["input_type"],
            {
                "new_task",
                "task_update",
                "approval_response",
                "status_query",
                "failure_query",
                "replan_request",
                "knowledge_query",
                "system_request",
            },
        )

    def test_resilient_adapter_health_check_reports_degradation(self) -> None:
        adapter = ResilientLLMAdapter(
            primary=_FailingAdapter(),
            fallback=StubLLMAdapter(LLMConfig(provider="test", model="test-fallback")),
        )

        health = adapter.check_health()

        self.assertTrue(health["healthy"])
        self.assertTrue(health["degraded_to_fallback"])
        self.assertEqual(health["fallback"]["provider"], "test")
        self.assertEqual(health["last_error"]["code"], ErrorCode.LLM_ERROR)


if __name__ == "__main__":
    unittest.main()

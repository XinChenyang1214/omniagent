from __future__ import annotations

import unittest
from pathlib import Path

from omniagent.config import LLMConfig
from omniagent.interaction import InteractionRouter
from omniagent.models import ConversationThread, UserInputEnvelope, UserInputType
from omniagent.prompting import PromptManager
from tests.llm_test_helper import StubLLMAdapter


class InteractionRouterRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        prompts = PromptManager(
            LLMConfig(prompt_dir=Path(__file__).resolve().parents[1] / "src/omniagent/prompts")
        )
        self.router = InteractionRouter(StubLLMAdapter(), prompts)
        self.thread = ConversationThread(title="test")

    def _route(self, message: str):
        return self.router._rule_based(  # noqa: SLF001
            UserInputEnvelope(message=message),
            thread=self.thread,
            active_task=None,
            pending_checkpoint=None,
        )

    def test_status_field_assignment_is_not_status_query(self) -> None:
        route = self._route(
            "Create notes/project.md with exactly these two lines: title: Apollo and status: draft."
        )

        self.assertIsNotNone(route)
        self.assertEqual(route.input_type, UserInputType.NEW_TASK)
        self.assertEqual(route.task_action, "create")

    def test_status_line_update_is_not_status_query(self) -> None:
        route = self._route(
            "Now update that file so the status line says status: reviewed instead of status: draft."
        )

        self.assertIsNotNone(route)
        self.assertEqual(route.input_type, UserInputType.NEW_TASK)

    def test_summarize_task_mentioning_status_question_is_not_status_query(self) -> None:
        route = self._route(
            "Now summarize the file I modified before that status question into "
            "reports/latest_modified_service.md with selected_service: alpha."
        )

        self.assertIsNotNone(route)
        self.assertEqual(route.input_type, UserInputType.NEW_TASK)

    def test_explicit_status_query_still_routes_to_status(self) -> None:
        route = self._route("当前状态如何")

        self.assertIsNotNone(route)
        self.assertEqual(route.input_type, UserInputType.STATUS_QUERY)
        self.assertEqual(route.task_action, "status")


if __name__ == "__main__":
    unittest.main()

"""ISSUE-005 — Responder hallucination detection + Python-side validation.

Ensures that the ResponseGenerator rejects LLM replies that contradict
the actual execution results, and that intent-mismatch warnings are injected
into the payload when user intent diverges from what was executed.

Coverage map:
- _extract_intent_verbs        → IntentVerbExtractionTest (5)
- _detect_intent_mismatch      → IntentMismatchDetectionTest (5)
- _validate_reply (post-LLM)   → ReplyPostValidationTest (7)
- generate() integration       → ResponderIntegrationTest (4)
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from omniagent.models import (
    CompletionEvaluation,
    ExecutionResult,
    LLMResponse,
    RouteDecision,
    Task,
    TaskExecutionReport,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskStatus,
    UserInputType,
)
from omniagent.response_generator import (
    ResponseContext,
    ResponseGenerator,
    _detect_intent_mismatch,
    _extract_intent_verbs,
    _validate_reply,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_route(thread_id: str = "th1") -> RouteDecision:
    return RouteDecision(
        thread_id=thread_id,
        input_type=UserInputType.NEW_TASK,
        reasoning="test",
        confidence=1.0,
        task_action="submit",
    )


def _make_task(*, goal: str = "test goal", status: TaskStatus = TaskStatus.COMPLETED) -> Task:
    return Task(
        task_id="t_test",
        goal=goal,
        mode="sync",
        source="cli",
        status=status,
        executor="file",
        skill_name="builtin.file.basic",
        payload={},
        workspace_path="/tmp/ws",
    )


def _make_result(*, success: bool = True, message: str = "ok") -> ExecutionResult:
    return ExecutionResult(
        success=success,
        status="completed" if success else "failed",
        message=message,
    )


def _make_report(
    *,
    task_goal: str = "读取文件",
    success: bool = True,
    node_goals: list[str] | None = None,
    completion_evaluation: CompletionEvaluation | None = None,
) -> TaskExecutionReport:
    task = _make_task(
        goal=task_goal,
        status=TaskStatus.COMPLETED if success else TaskStatus.FAILED,
    )
    result = _make_result(success=success, message="done" if success else "error")
    nodes = []
    for i, ng in enumerate(node_goals or [task_goal]):
        nodes.append(
            TaskNode(
                node_id=f"n{i}",
                type=TaskNodeType.ACTION,
                goal=ng,
                status="completed" if success else "failed",
            )
        )
    graph = TaskGraph(goal=task_goal, summary="test", nodes=nodes, dependencies=[])
    return TaskExecutionReport(
        task=task,
        result=result,
        events=[],
        task_graph=graph,
        completion_evaluation=completion_evaluation,
    )


def _make_context(
    *,
    user_message: str = "读取文件",
    report: TaskExecutionReport | None = None,
    fallback: str = "fallback-msg",
) -> ResponseContext:
    return ResponseContext(
        route=_make_route(),
        user_message=user_message,
        task_report=report,
        fallback_message=fallback,
    )


# ---------------------------------------------------------------------------
# _extract_intent_verbs unit tests
# ---------------------------------------------------------------------------


class IntentVerbExtractionTest(unittest.TestCase):
    """Verifies Chinese/English verb extraction from text."""

    def test_chinese_rename_verb(self) -> None:
        verbs = _extract_intent_verbs("把 a.txt 改名为 b.txt")
        self.assertIn("rename", verbs)

    def test_chinese_delete_verb(self) -> None:
        verbs = _extract_intent_verbs("删除 old.log 文件")
        self.assertIn("delete", verbs)

    def test_english_verb(self) -> None:
        verbs = _extract_intent_verbs("delete the old file")
        self.assertIn("delete", verbs)

    def test_multiple_verbs(self) -> None:
        verbs = _extract_intent_verbs("读取文件内容并写入新文件")
        self.assertIn("read", verbs)
        self.assertIn("write", verbs)

    def test_no_verbs(self) -> None:
        verbs = _extract_intent_verbs("你好，请帮帮忙")
        self.assertEqual(verbs, set())


# ---------------------------------------------------------------------------
# _detect_intent_mismatch unit tests
# ---------------------------------------------------------------------------


class IntentMismatchDetectionTest(unittest.TestCase):
    """Verifies intent-mismatch detection between user message and node goals."""

    def test_no_mismatch(self) -> None:
        result = _detect_intent_mismatch(
            "读取 a.txt",
            [{"goal": "读取 a.txt 文件内容"}],
        )
        self.assertIsNone(result)

    def test_rename_vs_read_mismatch(self) -> None:
        result = _detect_intent_mismatch(
            "把 a.txt 改名为 b.txt",
            [{"goal": "读取 a.txt 文件内容"}],
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["intent_mismatch"])
        self.assertIn("rename", result["unmatched_intents"])

    def test_delete_vs_list_mismatch(self) -> None:
        result = _detect_intent_mismatch(
            "删除所有临时文件",
            [{"goal": "列出工作区文件"}],
        )
        self.assertIsNotNone(result)
        self.assertIn("delete", result["unmatched_intents"])

    def test_no_user_verbs_returns_none(self) -> None:
        """When user message has no recognizable verbs, no mismatch."""
        result = _detect_intent_mismatch(
            "你好",
            [{"goal": "读取文件"}],
        )
        self.assertIsNone(result)

    def test_no_node_verbs_returns_none(self) -> None:
        """When nodes have no recognizable verbs, no mismatch."""
        result = _detect_intent_mismatch(
            "删除文件",
            [{"goal": "处理任务"}],
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _validate_reply post-LLM validation tests
# ---------------------------------------------------------------------------


class ReplyPostValidationTest(unittest.TestCase):
    """Verifies that _validate_reply rejects hallucinated replies."""

    def test_valid_reply_passes_through(self) -> None:
        report = _make_report(task_goal="读取文件", success=True, node_goals=["读取文件 a.txt"])
        ctx = _make_context(user_message="读取文件", report=report)
        result = _validate_reply("已读取文件 a.txt，内容如下...", ctx)
        self.assertIsNotNone(result)

    def test_failed_task_with_success_claim_rejected(self) -> None:
        """Task failed but LLM reply says '已完成' → reject."""
        report = _make_report(task_goal="读取文件", success=False)
        ctx = _make_context(user_message="读取文件", report=report)
        result = _validate_reply("已成功读取文件内容", ctx)
        self.assertIsNone(result)

    def test_failed_task_honest_reply_passes(self) -> None:
        """Task failed and LLM reply honestly says so → pass."""
        report = _make_report(task_goal="读取文件", success=False)
        ctx = _make_context(user_message="读取文件", report=report)
        result = _validate_reply("文件读取失败：路径不存在", ctx)
        self.assertIsNotNone(result)

    def test_fabricated_action_rejected(self) -> None:
        """User asked to rename, system only read, reply claims rename done → reject."""
        report = _make_report(
            task_goal="读取 a.txt",
            success=True,
            node_goals=["读取 a.txt 文件内容"],
        )
        ctx = _make_context(user_message="把 a.txt 改名为 b.txt", report=report)
        result = _validate_reply("已改名为 b.txt", ctx)
        self.assertIsNone(result)

    def test_fabricated_action_not_in_user_intent_passes(self) -> None:
        """Reply mentions an action not in node goals, but user didn't ask for
        it either → not a dangerous fabrication, should pass."""
        report = _make_report(task_goal="读取文件", success=True, node_goals=["读取 a.txt"])
        ctx = _make_context(user_message="读取 a.txt", report=report)
        # Reply casually mentions "写入日志" but user never asked to write
        result = _validate_reply("已读取文件内容，下次可以写入日志保存", ctx)
        self.assertIsNotNone(result)

    def test_no_report_passes_through(self) -> None:
        """When there is no task_report, validation is skipped."""
        ctx = _make_context(user_message="你好", report=None)
        result = _validate_reply("你好！有什么可以帮你的？", ctx)
        self.assertIsNotNone(result)

    def test_failed_task_with_chinese_success_phrase(self) -> None:
        """Covers additional success claim phrases."""
        report = _make_report(task_goal="写入文件", success=False)
        ctx = _make_context(user_message="写入文件", report=report)
        result = _validate_reply("已写入文件到 output.txt", ctx)
        self.assertIsNone(result)

    def test_unsatisfied_completion_with_success_claim_rejected(self) -> None:
        """Completion evaluation can reject a technically successful task."""
        report = _make_report(
            task_goal="搜索最新天气",
            success=True,
            node_goals=["生成天气攻略"],
            completion_evaluation=CompletionEvaluation(
                task_id="t_test",
                task_satisfied=False,
                completion_score=0.4,
                summary="缺少实时天气证据",
                missing_requirements=["实时天气"],
                primary_failure_layer="tool_or_skill",
                confidence=0.9,
            ),
        )
        ctx = _make_context(user_message="搜索最新天气", report=report)
        result = _validate_reply("已生成本周末天气攻略", ctx)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Full generate() integration — mocks LLM, checks payload injection + validation
# ---------------------------------------------------------------------------


class ResponderIntegrationTest(unittest.TestCase):
    """End-to-end ResponseGenerator.generate() with mocked LLM."""

    def setUp(self) -> None:
        self.llm = MagicMock()
        self.prompts = MagicMock()
        self.prompts.load.return_value = "system prompt"
        self.store = MagicMock()
        self.generator = ResponseGenerator(self.llm, self.prompts, self.store)

    def _set_llm_reply(self, reply: str, actions: list[str] | None = None) -> None:
        self.llm.generate_structured.return_value = LLMResponse(
            provider="test",
            model="test-model",
            prompt_name="responder_v1",
            request_id="req_test",
            raw_output=f'{{"reply":"{reply}","suggested_actions":[]}}',
            structured_output={
                "reply": reply,
                "suggested_actions": actions or [],
            },
        )

    def test_mismatch_injected_into_payload(self) -> None:
        """When user asks to rename but node reads, payload.task_summary
        must contain intent_mismatch."""
        self._set_llm_reply("系统实际执行了读取操作")
        report = _make_report(
            task_goal="读取 a.txt",
            success=True,
            node_goals=["读取 a.txt 文件内容"],
        )
        ctx = _make_context(user_message="把 a.txt 改名为 b.txt", report=report)
        self.generator.generate(context=ctx)
        call_kwargs = self.llm.generate_structured.call_args
        payload = call_kwargs.kwargs.get("request_payload") or call_kwargs[1].get("request_payload")
        task_summary = payload["task_summary"]
        self.assertIn("intent_mismatch", task_summary)
        self.assertTrue(task_summary["intent_mismatch"]["intent_mismatch"])
        self.assertIn("rename", task_summary["intent_mismatch"]["unmatched_intents"])

    def test_hallucinated_success_falls_back(self) -> None:
        """LLM claims success on a failed task → should return fallback."""
        self._set_llm_reply("已成功完成文件写入")
        report = _make_report(task_goal="写入文件", success=False)
        ctx = _make_context(
            user_message="写入文件",
            report=report,
            fallback="任务执行失败: 磁盘空间不足",
        )
        result = self.generator.generate(context=ctx)
        self.assertEqual(result, "任务执行失败: 磁盘空间不足")

    def test_honest_reply_passes_through(self) -> None:
        """LLM gives an honest reply about a successful task → should pass."""
        self._set_llm_reply("已读取文件 a.txt 的内容")
        report = _make_report(
            task_goal="读取 a.txt",
            success=True,
            node_goals=["读取 a.txt 文件内容"],
        )
        ctx = _make_context(user_message="读取 a.txt", report=report)
        result = self.generator.generate(context=ctx)
        self.assertIn("已读取文件", result)

    def test_no_mismatch_when_intent_matches(self) -> None:
        """User asks to read and system reads → no mismatch in payload."""
        self._set_llm_reply("已读取文件内容")
        report = _make_report(
            task_goal="读取文件",
            success=True,
            node_goals=["读取 config.json"],
        )
        ctx = _make_context(user_message="读取 config.json", report=report)
        self.generator.generate(context=ctx)
        call_kwargs = self.llm.generate_structured.call_args
        payload = call_kwargs.kwargs.get("request_payload") or call_kwargs[1].get("request_payload")
        task_summary = payload["task_summary"]
        self.assertNotIn("intent_mismatch", task_summary)

    def test_completion_evaluation_injected_into_payload(self) -> None:
        self._set_llm_reply("没有取得实时天气证据，任务未满足")
        evaluation = CompletionEvaluation(
            task_id="t_test",
            task_satisfied=False,
            completion_score=0.4,
            summary="缺少实时天气证据",
            missing_requirements=["实时天气"],
            primary_failure_layer="tool_or_skill",
            confidence=0.9,
        )
        report = _make_report(
            task_goal="搜索最新天气",
            success=True,
            node_goals=["生成天气攻略"],
            completion_evaluation=evaluation,
        )
        ctx = _make_context(user_message="搜索最新天气", report=report)
        self.generator.generate(context=ctx)
        call_kwargs = self.llm.generate_structured.call_args
        payload = call_kwargs.kwargs.get("request_payload") or call_kwargs[1].get("request_payload")
        task_summary = payload["task_summary"]
        self.assertFalse(task_summary["completion_evaluation"]["task_satisfied"])
        self.assertEqual(task_summary["completion_evaluation"]["missing_requirements"], ["实时天气"])

    def test_learning_feedback_injected_and_appended_to_reply(self) -> None:
        self._set_llm_reply("任务已完成")
        report = _make_report(
            task_goal="维护每日反馈审计记录",
            success=True,
            node_goals=["写入反馈审计记录"],
        )
        report.learning_feedback = {
            "schema_version": "learning-feedback/v1",
            "summary_lines": [
                "经验记录：已写入 exp_1，validation=passed，可作为学习输入。",
                "Procedure：已生成 draft proc_1，需要 review。",
                "复用判断：本次任务生成了新的 procedure draft。",
                "需要你做：新 procedure draft 需要用户审核；审核前不会自动复用。",
            ],
            "review": {
                "commands": [
                    "omniagent procedure review proc_1 --decision approved --evidence-ref tasks/t_test/completion/evaluation.json"
                ]
            },
        }
        ctx = _make_context(user_message="维护每日反馈审计记录", report=report)

        result = self.generator.generate(context=ctx)

        call_kwargs = self.llm.generate_structured.call_args
        payload = call_kwargs.kwargs.get("request_payload") or call_kwargs[1].get("request_payload")
        self.assertEqual(
            payload["task_summary"]["learning_feedback"]["schema_version"],
            "learning-feedback/v1",
        )
        self.assertIn("任务已完成", result)
        self.assertIn("学习反馈:", result)
        self.assertIn("经验记录：已写入 exp_1", result)
        self.assertIn("omniagent procedure review proc_1", result)


if __name__ == "__main__":
    unittest.main()

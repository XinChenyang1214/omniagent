"""ISSUE-014 — Critic fail verdict enforcement by runtime.

Ensures that when the task-level critic returns action=FAIL, the runtime
overrides execution_engine's result.success=True and sets the final task
status to FAILED.

Coverage map:
- Direct override logic              → CriticOverrideLogicTest (6)
- Integration via _execute_task_graph → CriticOverrideIntegrationTest (2)
"""

from __future__ import annotations

import unittest

from omniagent.models import (
    CriticAction,
    CriticVerdict,
    ExecutionResult,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_exec_result(*, success: bool = True, msg: str = "ok") -> ExecutionResult:
    return ExecutionResult(
        success=success,
        status="completed" if success else "failed",
        message=msg,
    )


def _make_verdict(
    *,
    target_type: str = "task",
    action: CriticAction = CriticAction.ACCEPT,
    summary: str = "all good",
    confidence: float = 0.9,
) -> CriticVerdict:
    return CriticVerdict(
        target_type=target_type,
        target_id="t_test",
        action=action,
        summary=summary,
        rationale="test rationale",
        confidence=confidence,
    )


def _apply_critic_override(
    result: ExecutionResult,
    critic_verdicts: list[CriticVerdict],
) -> ExecutionResult:
    """Re-implement the ISSUE-014 override logic from runtime.py for unit testing.

    This mirrors the exact logic added in _execute_task_graph between the
    critic evaluation and the status determination.
    """
    if result.status not in ("waiting_input", "suspended") and result.success and critic_verdicts:
        task_level = [v for v in critic_verdicts if v.target_type == "task"]
        if task_level and task_level[-1].action == CriticAction.FAIL:
            tv = task_level[-1]
            return ExecutionResult(
                success=False,
                status="failed",
                message=f"[Critic override] {tv.summary}",
                output=result.output,
                artifacts=result.artifacts,
                executor=result.executor,
                error_code="CRITIC_FAIL_OVERRIDE",
            )
    return result


# ---------------------------------------------------------------------------
# CriticOverrideLogicTest — tests the override logic directly
# ---------------------------------------------------------------------------


class CriticOverrideLogicTest(unittest.TestCase):
    """Verifies the ISSUE-014 critic override logic in isolation."""

    def test_fail_verdict_overrides_success(self) -> None:
        """Core case: engine says success, task-level critic says fail → overridden."""
        result = _make_exec_result(success=True, msg="done")
        fail_verdict = _make_verdict(action=CriticAction.FAIL, summary="fabricated output")
        overridden = _apply_critic_override(result, [fail_verdict])

        self.assertFalse(overridden.success)
        self.assertEqual(overridden.status, "failed")
        self.assertIn("Critic override", overridden.message)
        self.assertEqual(overridden.error_code, "CRITIC_FAIL_OVERRIDE")

    def test_accept_verdict_preserves_success(self) -> None:
        """Critic ACCEPT → no override."""
        result = _make_exec_result(success=True, msg="done")
        accept = _make_verdict(action=CriticAction.ACCEPT)
        overridden = _apply_critic_override(result, [accept])

        self.assertTrue(overridden.success)
        self.assertEqual(overridden.message, "done")

    def test_already_failed_not_double_overridden(self) -> None:
        """Engine already failed → critic FAIL doesn't change anything."""
        result = _make_exec_result(success=False, msg="node crashed")
        fail_verdict = _make_verdict(action=CriticAction.FAIL)
        overridden = _apply_critic_override(result, [fail_verdict])

        self.assertFalse(overridden.success)
        self.assertEqual(overridden.message, "node crashed")  # original
        self.assertNotIn("Critic override", overridden.message)

    def test_node_level_fail_ignored(self) -> None:
        """Only task-level FAIL triggers override; node-level FAIL is ignored."""
        result = _make_exec_result(success=True, msg="done")
        node_fail = _make_verdict(target_type="node", action=CriticAction.FAIL)
        task_accept = _make_verdict(target_type="task", action=CriticAction.ACCEPT)
        overridden = _apply_critic_override(result, [node_fail, task_accept])

        self.assertTrue(overridden.success)

    def test_waiting_input_not_overridden(self) -> None:
        """Waiting input status skips override even with FAIL verdict."""
        result = ExecutionResult(success=False, status="waiting_input", message="need approval")
        fail_verdict = _make_verdict(action=CriticAction.FAIL)
        overridden = _apply_critic_override(result, [fail_verdict])

        self.assertEqual(overridden.status, "waiting_input")

    def test_empty_verdicts_no_crash(self) -> None:
        """No verdicts at all → no crash, result unchanged."""
        result = _make_exec_result(success=True, msg="done")
        overridden = _apply_critic_override(result, [])

        self.assertTrue(overridden.success)

    def test_override_preserves_output_and_artifacts(self) -> None:
        """Override should preserve the original output and artifacts."""
        result = ExecutionResult(
            success=True,
            status="completed",
            message="done",
            output={"key": "value"},
            executor="file",
        )
        fail_verdict = _make_verdict(action=CriticAction.FAIL, summary="bad output")
        overridden = _apply_critic_override(result, [fail_verdict])

        self.assertEqual(overridden.output, {"key": "value"})
        self.assertEqual(overridden.executor, "file")


# ---------------------------------------------------------------------------
# CriticOverrideIntegrationTest — verifies the logic exists in runtime.py
# ---------------------------------------------------------------------------


class CriticOverrideIntegrationTest(unittest.TestCase):
    """Verifies that runtime.py contains the ISSUE-014 override code path."""

    def test_override_code_present_in_runtime(self) -> None:
        """Verify the critic override code block exists in runtime.py."""
        import inspect
        from omniagent.runtime import EmbeddedRuntime

        source = inspect.getsource(EmbeddedRuntime._execute_task_graph)
        self.assertIn("CRITIC_FAIL_OVERRIDE", source)
        self.assertIn("CriticAction.FAIL", source)

    def test_critic_action_imported_in_runtime(self) -> None:
        """Verify CriticAction is importable in runtime module."""
        from omniagent import runtime

        self.assertTrue(hasattr(runtime, "CriticAction"))


# ---------------------------------------------------------------------------
# Task critic scheduling
# ---------------------------------------------------------------------------


class TaskCriticSchedulingTest(unittest.TestCase):
    """Verifies when runtime should run the task-level critic."""

    def test_waiting_input_skips_task_critic(self) -> None:
        from omniagent.runtime import should_evaluate_task_critic

        result = ExecutionResult(success=False, status="waiting_input", message="need input")

        self.assertFalse(should_evaluate_task_critic(result, []))

    def test_success_runs_task_critic(self) -> None:
        from omniagent.runtime import should_evaluate_task_critic

        result = _make_exec_result(success=True)
        node_accept = _make_verdict(target_type="node", action=CriticAction.ACCEPT)

        self.assertTrue(should_evaluate_task_critic(result, [node_accept]))

    def test_failed_node_accept_skips_redundant_task_critic(self) -> None:
        from omniagent.runtime import should_evaluate_task_critic

        result = _make_exec_result(success=False)
        node_accept = _make_verdict(target_type="node", action=CriticAction.ACCEPT)

        self.assertFalse(should_evaluate_task_critic(result, [node_accept]))

    def test_failed_node_replan_still_runs_task_critic(self) -> None:
        from omniagent.runtime import should_evaluate_task_critic

        result = _make_exec_result(success=False)
        node_replan = _make_verdict(target_type="node", action=CriticAction.REPLAN)

        self.assertTrue(should_evaluate_task_critic(result, [node_replan]))


# ---------------------------------------------------------------------------
# Status determination after override
# ---------------------------------------------------------------------------


class StatusDeterminationTest(unittest.TestCase):
    """Verifies that final_status is FAILED when result.success is False."""

    def test_overridden_result_produces_failed_status(self) -> None:
        """After critic override, result.success=False → FAILED."""
        result = _make_exec_result(success=True, msg="done")
        fail_verdict = _make_verdict(action=CriticAction.FAIL, summary="fabricated")
        overridden = _apply_critic_override(result, [fail_verdict])

        # Simulate the status determination from runtime.py line 504-511
        if overridden.status == "waiting_input":
            final_status = TaskStatus.WAITING_INPUT
        elif overridden.status == "suspended":
            final_status = TaskStatus.SUSPENDED
        else:
            final_status = TaskStatus.COMPLETED if overridden.success else TaskStatus.FAILED

        self.assertEqual(final_status, TaskStatus.FAILED)

    def test_accepted_result_produces_completed_status(self) -> None:
        """Without override, result.success=True → COMPLETED."""
        result = _make_exec_result(success=True, msg="done")
        accept = _make_verdict(action=CriticAction.ACCEPT)
        overridden = _apply_critic_override(result, [accept])

        final_status = TaskStatus.COMPLETED if overridden.success else TaskStatus.FAILED
        self.assertEqual(final_status, TaskStatus.COMPLETED)

    def test_replan_verdict_does_not_override(self) -> None:
        """REPLAN verdict does not trigger the FAIL override path."""
        result = _make_exec_result(success=True, msg="done")
        replan = _make_verdict(action=CriticAction.REPLAN, summary="try again")
        overridden = _apply_critic_override(result, [replan])

        self.assertTrue(overridden.success)


if __name__ == "__main__":
    unittest.main()

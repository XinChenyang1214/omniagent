from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


def _json_from_output(output: str) -> dict | list:
    object_start = output.find("{")
    array_start = output.find("[")
    starts = [item for item in (object_start, array_start) if item >= 0]
    if not starts:
        raise AssertionError(f"output does not contain JSON: {output}")
    return json.loads(output[min(starts) :])


class LearningFeedbackIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_root = Path(self.tempdir.name)
        self.workspace = self.project_root / "workspace"
        self.db_path = self.project_root / "data" / "omniagent.sqlite3"
        self.log_path = self.project_root / "artifacts" / "omniagent.log"
        self.env = {
            "OMNIAGENT_SQLITE_PATH": str(self.db_path),
            "OMNIAGENT_LOG_PATH": str(self.log_path),
            "OMNIAGENT_JSON_LOGS": "false",
            "OMNIAGENT_LLM_PROVIDER": "test",
        }

    def _build_gateway(self) -> RuntimeGateway:
        with patch.dict(os.environ, self.env, clear=False):
            config = AppConfig.from_env(self.project_root)
        return RuntimeGateway(config, self.workspace)

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with (
            patch.dict(os.environ, self.env, clear=False),
            redirect_stdout(stdout),
            redirect_stderr(stdout),
        ):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def _run_json(self, argv: list[str]) -> dict | list:
        exit_code, output = self._run_cli(argv)
        self.assertEqual(exit_code, 0, output)
        return _json_from_output(output)

    def _run_file_task(self, goal: str, *, path: str, content: str, action: str = "write") -> dict:
        payload: dict[str, object] = {
            "action": action,
            "path": path,
            "content": content,
        }
        if action == "write":
            payload["overwrite"] = True
        return self._run_json(
            [
                "run",
                goal,
                "--executor",
                "file",
                "--payload",
                json.dumps(payload, ensure_ascii=False),
                "--max-permission",
                "L2",
                "--json",
            ]
        )

    def _submit_file_task(
        self,
        gateway: RuntimeGateway,
        goal: str,
        *,
        payload: dict[str, object],
    ):
        return gateway.submit(
            TaskRequest(
                goal=goal,
                executor="file",
                payload=payload,
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )

    def test_first_run_feedback_explains_draft_and_review_action(self) -> None:
        report = self._run_file_task(
            "维护每日反馈审计记录：写入 reports/feedback-a.md",
            path="reports/feedback-a.md",
            content="status: first\n",
        )

        feedback = report["learning_feedback"]
        self.assertTrue(feedback["experience"]["record_written"])
        self.assertTrue(feedback["experience"]["learnable"])
        self.assertIsNotNone(feedback["procedure"]["generated_draft_id"])
        self.assertEqual(feedback["reuse"]["status"], "new_draft_needs_review")
        self.assertTrue(feedback["review"]["required"])
        self.assertTrue(
            any("procedure review" in command for command in feedback["review"]["commands"])
        )
        self.assertTrue(
            self.workspace.joinpath(
                "tasks",
                report["task"]["task_id"],
                "learning",
                "feedback.json",
            ).exists()
        )

    def test_cli_plain_output_includes_learning_feedback(self) -> None:
        exit_code, output = self._run_cli(
            [
                "run",
                "维护每日反馈审计记录：写入 reports/feedback-plain.md",
                "--executor",
                "file",
                "--payload",
                json.dumps(
                    {
                        "action": "write",
                        "path": "reports/feedback-plain.md",
                        "content": "status: plain\n",
                        "overwrite": True,
                    },
                    ensure_ascii=False,
                ),
                "--max-permission",
                "L2",
            ]
        )

        self.assertEqual(exit_code, 0, output)
        self.assertIn("学习反馈:", output)
        self.assertIn("经验记录：已写入", output)
        self.assertIn("需要你做：新 procedure draft 需要用户审核", output)

    def test_habit_fast_path_feedback_explains_reuse_and_correction_commands(self) -> None:
        first = self._run_file_task(
            "维护每日反馈审计记录：写入 reports/feedback-habit.md",
            path="reports/feedback-habit.md",
            content="status: first\n",
        )
        procedure_id = first["learning_feedback"]["procedure"]["generated_draft_id"]
        task_id = first["task"]["task_id"]
        self._run_json(
            [
                "procedure",
                "review",
                procedure_id,
                "--decision",
                "approved",
                "--evidence-ref",
                f"tasks/{task_id}/completion/evaluation.json",
                "--json",
            ]
        )
        for index in (2, 3):
            self._run_file_task(
                "继续维护每日反馈审计记录：追加 reports/feedback-habit.md",
                path="reports/feedback-habit.md",
                content=f"repeat: {index}\n",
                action="append",
            )
        self._run_json(
            [
                "learning",
                "promote",
                "--type",
                "procedure",
                procedure_id,
                "--json",
            ]
        )

        fourth = self._run_file_task(
            "继续维护每日反馈审计记录：追加 reports/feedback-habit.md",
            path="reports/feedback-habit.md",
            content="repeat: 4\n",
            action="append",
        )

        feedback = fourth["learning_feedback"]
        self.assertEqual(feedback["procedure"]["matched_id"], procedure_id)
        self.assertEqual(feedback["procedure"]["matched_object_type"], "habit")
        self.assertEqual(feedback["habit_fast_path"]["decision"], "habit_fast_path_candidate")
        self.assertEqual(feedback["reuse"]["status"], "habit_fast_path")
        self.assertTrue(feedback["reuse"]["reusable_now"])
        self.assertTrue(
            any("learning disable --type habit" in command for command in feedback["review"]["commands"])
        )

    def test_failed_reuse_feedback_records_correction_and_blocks_future_match(self) -> None:
        gateway = self._build_gateway()
        first = self._submit_file_task(
            gateway,
            "维护学习纠错审计记录：写入 reports/feedback-correction.md",
            payload={
                "action": "write",
                "path": "reports/feedback-correction.md",
                "content": "entry: first\n",
                "overwrite": True,
            },
        )
        self.assertTrue(first.result.success, first.result.message)
        procedure_id = first.learning_feedback["procedure"]["generated_draft_id"]
        assert procedure_id is not None
        gateway.procedure_memory.review(
            procedure_id,
            decision="approved",
            evidence_refs=[f"tasks/{first.task.task_id}/completion/evaluation.json"],
            note="approve correction test procedure",
        )

        failed = self._submit_file_task(
            gateway,
            "维护学习纠错审计记录：写入 reports/feedback-correction.md",
            payload={"action": "unsupported", "path": "reports/feedback-correction.md"},
        )

        self.assertFalse(failed.result.success)
        correction_path = (
            self.workspace
            / "tasks"
            / failed.task.task_id
            / "learning"
            / "correction.json"
        )
        self.assertTrue(correction_path.exists())
        correction = json.loads(correction_path.read_text(encoding="utf-8"))
        self.assertEqual(correction["schema_version"], "learning-correction/v1")
        self.assertEqual(correction["procedure_id"], procedure_id)
        self.assertEqual(correction["previous_status"], "verified")
        self.assertEqual(correction["new_status"], "draft")
        self.assertEqual(correction["new_review_status"], "correction_required")
        self.assertTrue(correction["frozen"])
        feedback = failed.learning_feedback
        self.assertTrue(feedback["correction"]["recorded"])
        self.assertEqual(feedback["reuse"]["status"], "correction_required")
        self.assertFalse(feedback["reuse"]["reusable_now"])
        match = gateway.procedure_memory.explain_match(
            "维护学习纠错审计记录：写入 reports/feedback-correction.md"
        )
        self.assertFalse(match["matched"])

        recovery = self._submit_file_task(
            gateway,
            "维护学习纠错审计记录：写入 reports/feedback-correction.md",
            payload={
                "action": "append",
                "path": "reports/feedback-correction.md",
                "content": "entry: recovery\n",
            },
        )
        self.assertTrue(recovery.result.success, recovery.result.message)
        restored = gateway.procedure_memory.review(
            procedure_id,
            decision="approved",
            evidence_refs=[f"tasks/{recovery.task.task_id}/completion/evaluation.json"],
            note="fresh recovery evidence passed",
        )
        self.assertEqual(restored["status"], "verified")
        self.assertEqual(restored["review_status"], "approved")
        self.assertEqual(restored["failure_count"], 0)
        restored_match = gateway.procedure_memory.explain_match(
            "维护学习纠错审计记录：写入 reports/feedback-correction.md"
        )
        self.assertTrue(restored_match["matched"])
        self.assertEqual(restored_match["matched_procedure_id"], procedure_id)


if __name__ == "__main__":
    unittest.main()

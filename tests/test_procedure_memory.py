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
from omniagent.procedure_memory import ProcedureMemoryError, ProcedureMemoryStore


def _passed_experience(
    *,
    experience_id: str = "exp_source",
    task_id: str = "task_source",
    user_goal: str = "write reusable report",
) -> dict:
    return {
        "schema_version": "experience-record/v1",
        "experience_id": experience_id,
        "task_id": task_id,
        "thread_id": None,
        "goal_id": "goal_1",
        "created_at": "2026-06-12T00:00:00+00:00",
        "status": "succeeded",
        "user_goal": user_goal,
        "definition_of_done": ["report exists"],
        "context_refs": ["tasks/task_source/context_snapshot.json"],
        "actions": [
            {
                "node_id": "node_write",
                "tool_or_skill": "builtin.file.basic",
                "input_summary": "dict(keys=['action', 'path'], json_chars=40)",
                "output_summary": "dict(keys=['path'], json_chars=20)",
                "status": "completed",
                "artifact_refs": ["reports/reusable.txt"],
            }
        ],
        "artifacts": [
            {
                "kind": "file",
                "path": "reports/reusable.txt",
                "description": "report",
            }
        ],
        "validation": {
            "verdict": "passed",
            "method": "completion",
            "evidence_refs": ["tasks/task_source/completion/evaluation.json"],
            "notes": "passed",
        },
        "failure": None,
        "learning_candidates": [],
        "sensitive_data_policy": {"raw_payload_stored": False},
    }


class ProcedureMemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.store = ProcedureMemoryStore(self.workspace)

    def test_extracts_draft_from_passed_experience(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["source_experience_ids"], ["exp_source"])
        self.assertTrue((self.workspace / "procedures" / "drafts").exists())
        self.assertIn("write reusable report", draft["trigger"]["task_patterns"])

    def test_extract_draft_requires_passed_validation_with_evidence(self) -> None:
        for verdict in ("not_verified", "partial", "failed"):
            experience = _passed_experience(experience_id=f"exp_{verdict}")
            experience["validation"]["verdict"] = verdict

            self.assertIsNone(self.store.extract_draft_from_experience(experience))

        missing_evidence = _passed_experience(experience_id="exp_missing_evidence")
        missing_evidence["validation"]["evidence_refs"] = []

        self.assertIsNone(self.store.extract_draft_from_experience(missing_evidence))
        self.assertEqual(self.store.list(include_disabled=True), [])

    def test_draft_without_evidence_cannot_be_verified(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        with self.assertRaises(ProcedureMemoryError):
            self.store.promote_to_verified(draft["procedure_id"], evidence_refs=[])

    def test_similar_task_matches_procedure(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        match = self.store.explain_match("write reusable report again")

        self.assertTrue(match["matched"])
        self.assertEqual(match["matched_procedure_id"], draft["procedure_id"])
        self.assertEqual(match["suggestion_mode"], "suggest_only")
        self.assertTrue(match["required_confirmation"])

    def test_validator_failure_downgrades_verified_procedure(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )

        updated = self.store.record_validator_failure(
            verified["procedure_id"],
            evidence_ref="tasks/task_new/completion/evaluation.json",
            reason="validator failed",
        )

        self.assertEqual(updated["status"], "draft")
        self.assertEqual(updated["failure_count"], 1)

    def test_consecutive_validator_failures_disable_procedure(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        downgraded = self.store.record_validator_failure(
            verified["procedure_id"],
            evidence_ref="tasks/task_new/completion/evaluation.json",
            reason="validator failed",
        )

        disabled = self.store.record_validator_failure(
            downgraded["procedure_id"],
            evidence_ref="tasks/task_newer/completion/evaluation.json",
            reason="validator failed again",
        )

        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["failure_count"], 2)

    def test_habit_promotion_requires_three_verified_successes(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )

        with self.assertRaises(ProcedureMemoryError):
            self.store.promote_to_habit(verified["procedure_id"])

    def test_verified_procedure_promotes_to_low_risk_habit(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        self.store.save(verified)

        habit = self.store.promote_to_habit(verified["procedure_id"])
        match = self.store.explain_match("write reusable report again")
        fast_path = self.store.build_habit_fast_path_decision(match)

        self.assertEqual(habit["status"], "habit")
        self.assertGreaterEqual(habit["confidence"], 0.75)
        self.assertEqual(match["suggestion_mode"], "habit_fast_path_candidate")
        self.assertFalse(match["required_confirmation"])
        self.assertEqual(fast_path["decision"], "habit_fast_path_candidate")
        self.assertEqual(fast_path["habit_id"], habit["procedure_id"])
        self.assertEqual(fast_path["skipped_stages"], ["planner"])

    def test_validator_success_increments_verified_procedure_with_lineage(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )

        updated = self.store.record_validator_success(
            verified["procedure_id"],
            evidence_refs=["tasks/task_repeat/completion/evaluation.json"],
            experience_id="exp_repeat",
            task_id="task_repeat",
        )

        self.assertEqual(updated["status"], "verified")
        self.assertEqual(updated["success_count"], 2)
        self.assertIn("exp_repeat", updated["source_experience_ids"])
        self.assertIn("task_repeat", updated["source_task_ids"])
        self.assertIn(
            "tasks/task_repeat/completion/evaluation.json",
            updated["validator"]["required_evidence"],
        )
        self.assertEqual(updated["audit_notes"][-1]["event"], "validator_succeeded")

    def test_high_risk_habit_requires_confirmation_and_no_fast_path(self) -> None:
        draft = self.store.extract_draft_from_experience(
            _passed_experience(user_goal="delete cache")
        )
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        verified["risk_level"] = "high"
        verified["success_count"] = 3
        self.store.save(verified)

        with self.assertRaises(ProcedureMemoryError):
            self.store.promote_to_habit(verified["procedure_id"])
        habit = self.store.promote_to_habit(
            verified["procedure_id"],
            require_confirmation=True,
        )
        match = self.store.explain_match("delete cache again")
        fast_path = self.store.build_habit_fast_path_decision(match)

        self.assertEqual(habit["status"], "habit")
        self.assertIn("user_confirmation", habit["required_permissions"])
        self.assertTrue(match["required_confirmation"])
        self.assertEqual(fast_path["decision"], "planner_required")
        self.assertEqual(fast_path["skipped_stages"], [])

    def test_habit_validator_failure_downgrades_to_verified(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        self.store.save(verified)
        habit = self.store.promote_to_habit(verified["procedure_id"])

        downgraded = self.store.record_validator_failure(
            habit["procedure_id"],
            evidence_ref="tasks/task_new/completion/evaluation.json",
            reason="validator failed",
        )

        self.assertEqual(downgraded["status"], "verified")
        self.assertEqual(downgraded["failure_count"], 1)

    def test_high_risk_habit_match_requires_confirmation(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience(user_goal="delete cache"))
        assert draft is not None
        draft["status"] = "habit"
        draft["risk_level"] = "high"
        self.store.save(draft)

        match = self.store.explain_match("delete cache again")

        self.assertTrue(match["matched"])
        self.assertEqual(match["suggestion_mode"], "validator_required")
        self.assertTrue(match["required_confirmation"])

    def test_edit_behavior_fields_downgrades_habit_for_review(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = self.store.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        self.store.save(verified)
        habit = self.store.promote_to_habit(verified["procedure_id"])

        edited = self.store.edit(
            habit["procedure_id"],
            patch={
                "trigger": {
                    "task_patterns": ["write audited report"],
                    "negative_patterns": ["delete"],
                }
            },
            note="tighten trigger",
            reviewer="tester",
        )

        self.assertEqual(edited["status"], "draft")
        self.assertEqual(edited["success_count"], 0)
        self.assertEqual(edited["failure_count"], 0)
        self.assertEqual(edited["review_status"], "edited_requires_review")
        self.assertEqual(edited["trigger"]["task_patterns"], ["write audited report"])
        self.assertEqual(edited["audit_notes"][-1]["event"], "edited")
        self.assertEqual(edited["audit_notes"][-1]["from_status"], "habit")

    def test_review_approved_draft_promotes_to_verified_with_evidence(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        reviewed = self.store.review(
            draft["procedure_id"],
            decision="approved",
            reviewer="tester",
            note="manual review passed",
            evidence_refs=["manual_review/proc_source.md"],
        )

        self.assertEqual(reviewed["status"], "verified")
        self.assertEqual(reviewed["review_status"], "approved")
        self.assertEqual(reviewed["reviews"][-1]["decision"], "approved")
        self.assertIn("manual_review/proc_source.md", reviewed["validator"]["required_evidence"])

    def test_review_rejected_disables_procedure(self) -> None:
        draft = self.store.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        reviewed = self.store.review(
            draft["procedure_id"],
            decision="rejected",
            reviewer="tester",
            note="bad trigger",
        )

        self.assertEqual(reviewed["status"], "disabled")
        self.assertEqual(reviewed["review_status"], "rejected")


class ProcedureMemoryRuntimeIntegrationTest(unittest.TestCase):
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

    def test_runtime_writes_procedure_match_artifact_before_planner(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        request = TaskRequest(
            goal="write reusable report again",
            executor="file",
            payload={
                "action": "write",
                "path": "reports/reusable-again.txt",
                "content": "ok",
                "overwrite": True,
            },
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        self.assertTrue(report.result.success, report.result.message)
        match_path = (
            self.workspace
            / "tasks"
            / report.task.task_id
            / "procedure"
            / "procedure_match.json"
        )
        self.assertTrue(match_path.exists())
        match = json.loads(match_path.read_text(encoding="utf-8"))
        self.assertTrue(match["matched"])
        self.assertEqual(match["matched_procedure_id"], draft["procedure_id"])
        self.assertEqual(match["retrieval_phase"], "before_planner")
        self.assertEqual(match["suggestion_mode"], "suggest_only")

    def test_runtime_updates_matched_verified_procedure_instead_of_new_draft(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        request = TaskRequest(
            goal="write reusable report again",
            executor="file",
            payload={
                "action": "write",
                "path": "reports/reusable-verified-repeat.txt",
                "content": "verified repeat ok",
                "overwrite": True,
            },
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        self.assertTrue(report.result.success, report.result.message)
        updated = gateway.procedure_memory.get(verified["procedure_id"])
        assert updated is not None
        self.assertEqual(updated["status"], "verified")
        self.assertEqual(updated["success_count"], 2)
        self.assertIn(report.task.task_id, updated["source_task_ids"])
        self.assertTrue(
            any(note.get("event") == "validator_succeeded" for note in updated["audit_notes"])
        )
        procedures = gateway.procedure_memory.list(include_disabled=True)
        self.assertEqual([item["procedure_id"] for item in procedures], [verified["procedure_id"]])

    def test_runtime_writes_accepted_habit_fast_path_artifact(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])
        request = TaskRequest(
            goal="write reusable report again",
            executor="file",
            payload={
                "action": "write",
                "path": "reports/reusable-habit.txt",
                "content": "habit ok",
                "overwrite": True,
            },
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        self.assertTrue(report.result.success, report.result.message)
        task_dir = self.workspace / "tasks" / report.task.task_id
        habit_path = task_dir / "procedure" / "habit_fast_path.json"
        policy_path = task_dir / "plan" / "policy_decision.json"
        self.assertTrue(habit_path.exists())
        habit_fast_path = json.loads(habit_path.read_text(encoding="utf-8"))
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(habit_fast_path["decision"], "habit_fast_path_candidate")
        self.assertEqual(habit_fast_path["habit_id"], habit["procedure_id"])
        self.assertEqual(habit_fast_path["skipped_stages"], ["planner"])
        self.assertEqual(habit_fast_path["policy_gate"]["result"], "accepted")
        self.assertIn(
            "tasks/task_source/completion/evaluation.json",
            habit_fast_path["validator_evidence_refs"],
        )
        self.assertEqual(
            policy["habit_fast_path"]["policy_gate"]["result"],
            "accepted",
        )

    def test_runtime_high_risk_habit_requires_confirmation_and_no_fast_path(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(user_goal="delete cache")
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )
        verified["risk_level"] = "high"
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(
            verified["procedure_id"],
            require_confirmation=True,
        )
        request = TaskRequest(
            goal="delete cache again",
            executor="file",
            payload={"action": "delete", "path": "cache.tmp"},
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        task_dir = self.workspace / "tasks" / report.task.task_id
        habit_path = task_dir / "procedure" / "habit_fast_path.json"
        policy_path = task_dir / "plan" / "policy_decision.json"
        self.assertTrue(habit_path.exists())
        self.assertTrue(policy_path.exists())
        habit_fast_path = json.loads(habit_path.read_text(encoding="utf-8"))
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(habit_fast_path["decision"], "planner_required")
        self.assertEqual(habit_fast_path["habit_id"], habit["procedure_id"])
        self.assertTrue(habit_fast_path["required_confirmation"])
        self.assertEqual(habit_fast_path["required_permissions"], ["user_confirmation"])
        self.assertEqual(habit_fast_path["skipped_stages"], [])
        self.assertEqual(policy["policy_decision"], "plan_required")
        self.assertTrue(policy["risk_flags"]["delete"])
        self.assertNotEqual(
            policy["habit_fast_path"]["policy_gate"]["result"],
            "accepted",
        )

    def test_cli_promote_habit_uses_promotion_gate(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_source/completion/evaluation.json"],
        )

        early_exit, early_output = self._run_cli(
            ["procedure", "promote-habit", verified["procedure_id"], "--json"]
        )
        self.assertEqual(early_exit, 1)
        self.assertIn("habit promotion requires", early_output)

        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        promote_exit, promote_output = self._run_cli(
            ["procedure", "promote-habit", verified["procedure_id"], "--json"]
        )

        self.assertEqual(promote_exit, 0, promote_output)
        habit = json.loads(promote_output)
        self.assertEqual(habit["status"], "habit")

    def test_cli_disable_prevents_future_matches(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        disable_exit, disable_output = self._run_cli(
            ["procedure", "disable", draft["procedure_id"], "--json"]
        )
        self.assertEqual(disable_exit, 0, disable_output)
        disabled = json.loads(disable_output)
        self.assertEqual(disabled["status"], "disabled")

        explain_exit, explain_output = self._run_cli(
            ["procedure", "explain-match", "write reusable report again", "--json"]
        )
        self.assertEqual(explain_exit, 0, explain_output)
        match = json.loads(explain_output)
        self.assertFalse(match["matched"])

    def test_cli_edit_and_review_procedure(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        edit_exit, edit_output = self._run_cli(
            [
                "procedure",
                "edit",
                draft["procedure_id"],
                "--patch",
                json.dumps({"name": "Reusable report procedure"}),
                "--reviewer",
                "tester",
                "--json",
            ]
        )
        self.assertEqual(edit_exit, 0, edit_output)
        edited = json.loads(edit_output)
        self.assertEqual(edited["name"], "Reusable report procedure")
        self.assertEqual(edited["review_status"], "edited")

        review_exit, review_output = self._run_cli(
            [
                "procedure",
                "review",
                draft["procedure_id"],
                "--decision",
                "approved",
                "--reviewer",
                "tester",
                "--evidence-ref",
                "manual_review/proc_source.md",
                "--json",
            ]
        )
        self.assertEqual(review_exit, 0, review_output)
        reviewed = json.loads(review_output)
        self.assertEqual(reviewed["status"], "verified")
        self.assertEqual(reviewed["review_status"], "approved")
        self.assertEqual(reviewed["reviews"][-1]["reviewer"], "tester")


if __name__ == "__main__":
    unittest.main()

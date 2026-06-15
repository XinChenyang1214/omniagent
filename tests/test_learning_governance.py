from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway


def _passed_experience(
    *,
    experience_id: str,
    task_id: str,
    user_goal: str,
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
        "context_refs": [f"tasks/{task_id}/context_snapshot.json"],
        "actions": [
            {
                "node_id": "node_write",
                "tool_or_skill": "builtin.file.basic",
                "input_summary": "dict(keys=['action', 'path'], json_chars=40)",
                "output_summary": "dict(keys=['path'], json_chars=20)",
                "status": "completed",
                "artifact_refs": [f"tasks/{task_id}/completion/evaluation.json"],
            }
        ],
        "artifacts": [
            {
                "kind": "file",
                "path": f"reports/{task_id}.txt",
                "description": "report",
            }
        ],
        "validation": {
            "verdict": "passed",
            "method": "completion",
            "evidence_refs": [f"tasks/{task_id}/completion/evaluation.json"],
            "notes": "passed",
        },
        "failure": None,
        "learning_candidates": [],
        "sensitive_data_policy": {"raw_payload_stored": False},
    }


def _write_experience_record(workspace: Path, record: dict) -> None:
    task_id = str(record["task_id"])
    record_path = workspace / "tasks" / task_id / "experience" / "experience_record.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = workspace / "experience" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "schema_version": record["schema_version"],
                "experience_id": record["experience_id"],
                "task_id": record["task_id"],
                "goal_id": record.get("goal_id"),
                "status": record["status"],
                "user_goal": record["user_goal"],
                "validation_verdict": record["validation"]["verdict"],
                "created_at": record["created_at"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class LearningGovernanceIntegrationTest(unittest.TestCase):
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

    def _create_external_skill(
        self,
        *,
        root: Path,
        skill_name: str,
        version: str,
        message: str,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "skill.yaml").write_text(
            json.dumps(
                {
                    "name": skill_name,
                    "version": version,
                    "description": f"Imported skill {version}",
                    "input_schema": {"text": "string"},
                    "output_schema": {"echo": "string", "version": "string"},
                    "permission_requirements": [],
                    "dependency_requirements": [],
                    "side_effects": [],
                    "sandbox_level": "workspace",
                    "tags": ["imported", "echo"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "main.py").write_text(
            textwrap.dedent(
                f"""
                def run(payload, context):
                    return {{
                        "success": True,
                        "status": "completed",
                        "message": "{message}",
                        "output": {{
                            "echo": payload.get("text", ""),
                            "version": "{version}",
                        }},
                    }}
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text(f"# {skill_name}\n", encoding="utf-8")
        tests_dir = root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_smoke.py").write_text(
            textwrap.dedent(
                """
                import unittest


                class SmokeTest(unittest.TestCase):
                    def test_smoke(self):
                        self.assertTrue(True)


                if __name__ == "__main__":
                    unittest.main()
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return root

    def test_learning_review_unifies_procedure_and_skill_audit_items(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_review_draft",
                task_id="task_review_draft",
                user_goal="write review draft report",
            )
        )
        assert draft is not None
        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_review_habit",
                task_id="task_review_habit",
                user_goal="write promoted review report",
            )
        )
        assert habit_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_review_habit/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])
        gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.learning_disable_file",
        )
        gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.learning_review_file",
        )

        payload = gateway.review_learning()

        self.assertEqual(payload["schema_version"], "learning-review/v1")
        self.assertGreaterEqual(payload["summary"]["procedures"], 1)
        self.assertGreaterEqual(payload["summary"]["habits"], 1)
        self.assertGreaterEqual(payload["summary"]["skills"], 1)
        draft_item = next(item for item in payload["items"] if item["id"] == draft["procedure_id"])
        self.assertEqual(draft_item["object_type"], "procedure")
        self.assertTrue(draft_item["requires_review"])
        self.assertIn(
            "draft_requires_review",
            {finding["code"] for finding in draft_item["findings"]},
        )
        habit_item = next(item for item in payload["items"] if item["id"] == habit["procedure_id"])
        self.assertEqual(habit_item["object_type"], "habit")
        self.assertEqual(habit_item["status"], "habit")
        self.assertEqual(habit_item["review_status"], "promotion_ready")
        self.assertTrue(habit_item["health"]["promotion_candidate"])
        skill_item = next(
            item
            for item in payload["items"]
            if item["id"] == "generated.habit.learning_review_file"
        )
        self.assertEqual(skill_item["object_type"], "skill")
        self.assertEqual(skill_item["status"], "active")
        self.assertEqual(skill_item["lineage"]["habit_id"], habit["procedure_id"])
        self.assertEqual(skill_item["lineage"]["source_experience_ids"], ["exp_review_habit"])
        self.assertTrue(skill_item["health"]["lineage_present"])

        procedure_detail = gateway.show_learning_object(
            object_type="procedure",
            object_id=draft["procedure_id"],
        )
        self.assertEqual(procedure_detail["schema_version"], "learning-object/v1")
        self.assertEqual(procedure_detail["item"]["object_type"], "procedure")
        self.assertEqual(procedure_detail["item"]["id"], draft["procedure_id"])
        self.assertEqual(procedure_detail["raw_object"]["procedure_id"], draft["procedure_id"])

        skill_detail = gateway.show_learning_object(
            object_type="skill",
            object_id="generated.habit.learning_review_file",
        )
        self.assertEqual(skill_detail["item"]["object_type"], "skill")
        self.assertEqual(skill_detail["item"]["status"], "active")
        self.assertEqual(
            skill_detail["item"]["lineage"]["habit_id"],
            habit["procedure_id"],
        )
        self.assertTrue(skill_detail["raw_object"]["lineage_records"])

        draft_only = gateway.review_learning(object_type="procedure", status="draft")
        self.assertTrue(draft_only["items"])
        self.assertTrue(all(item["object_type"] == "procedure" for item in draft_only["items"]))
        self.assertTrue(all(item["status"] == "draft" for item in draft_only["items"]))

    def test_learning_review_includes_experience_audit_items(self) -> None:
        gateway = self._build_gateway()
        passed = _passed_experience(
            experience_id="exp_review_passed",
            task_id="task_review_passed",
            user_goal="write passed experience report",
        )
        passed["learning_candidates"] = [
            {
                "kind": "procedure",
                "reason": "repeatable validated file write",
            }
        ]
        failed = _passed_experience(
            experience_id="exp_review_failed",
            task_id="task_review_failed",
            user_goal="write failed experience report",
        )
        failed["status"] = "failed"
        failed["validation"]["verdict"] = "failed"
        failed["failure"] = {
            "primary_failure_layer": "tool_or_skill",
            "summary": "tool failed",
        }
        _write_experience_record(self.workspace, passed)
        _write_experience_record(self.workspace, failed)

        payload = gateway.review_learning(object_type="experience")

        self.assertEqual(payload["summary"]["experiences"], 2)
        self.assertEqual(payload["summary"]["procedures"], 0)
        self.assertEqual(payload["summary"]["habits"], 0)
        self.assertEqual(payload["summary"]["skills"], 0)
        passed_item = next(item for item in payload["items"] if item["id"] == "exp_review_passed")
        self.assertEqual(passed_item["object_type"], "experience")
        self.assertEqual(passed_item["review_status"], "candidate_ready")
        self.assertFalse(passed_item["requires_review"])
        self.assertEqual(passed_item["lineage"]["task_id"], "task_review_passed")
        self.assertEqual(passed_item["health"]["learning_candidate_count"], 1)
        failed_item = next(item for item in payload["items"] if item["id"] == "exp_review_failed")
        self.assertTrue(failed_item["requires_review"])
        self.assertIn(
            "validation_not_passed",
            {finding["code"] for finding in failed_item["findings"]},
        )
        self.assertEqual(failed_item["health"]["failure_layer"], "tool_or_skill")

        failed_only = gateway.review_learning(object_type="experience", status="failed")
        self.assertEqual([item["id"] for item in failed_only["items"]], ["exp_review_failed"])

    def test_cli_learning_review_outputs_json(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_review",
                task_id="task_cli_review",
                user_goal="write cli review report",
            )
        )
        assert draft is not None

        exit_code, output = self._run_cli(["learning", "review", "--json"])

        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["schema_version"], "learning-review/v1")
        self.assertGreaterEqual(payload["summary"]["procedures"], 1)
        self.assertTrue(any(item["id"] == draft["procedure_id"] for item in payload["items"]))

    def test_cli_learning_review_filters_experience_items(self) -> None:
        record = _passed_experience(
            experience_id="exp_cli_experience_review",
            task_id="task_cli_experience_review",
            user_goal="write cli experience review report",
        )
        _write_experience_record(self.workspace, record)

        exit_code, output = self._run_cli(["learning", "review", "--type", "experience", "--json"])

        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["summary"]["experiences"], 1)
        self.assertEqual(payload["summary"]["procedures"], 0)
        self.assertEqual(payload["summary"]["habits"], 0)
        self.assertEqual(payload["summary"]["skills"], 0)
        self.assertEqual(payload["items"][0]["object_type"], "experience")
        self.assertEqual(payload["items"][0]["id"], "exp_cli_experience_review")

    def test_cli_learning_show_outputs_experience_detail(self) -> None:
        record = _passed_experience(
            experience_id="exp_cli_experience_show",
            task_id="task_cli_experience_show",
            user_goal="write cli experience show report",
        )
        _write_experience_record(self.workspace, record)

        exit_code, output = self._run_cli(
            [
                "learning",
                "show",
                "--type",
                "experience",
                "exp_cli_experience_show",
                "--json",
            ]
        )

        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["schema_version"], "learning-object/v1")
        self.assertEqual(payload["item"]["object_type"], "experience")
        self.assertEqual(payload["item"]["id"], "exp_cli_experience_show")
        self.assertEqual(payload["raw_object"]["task_id"], "task_cli_experience_show")

    def test_cli_learning_history_outputs_audit_events(self) -> None:
        record = _passed_experience(
            experience_id="exp_cli_experience_history",
            task_id="task_cli_experience_history",
            user_goal="write cli experience history report",
        )
        _write_experience_record(self.workspace, record)

        experience_exit, experience_output = self._run_cli(
            [
                "learning",
                "history",
                "--type",
                "experience",
                "exp_cli_experience_history",
                "--json",
            ]
        )

        self.assertEqual(experience_exit, 0, experience_output)
        experience_payload = json.loads(experience_output)
        self.assertEqual(experience_payload["schema_version"], "learning-history/v1")
        self.assertEqual(experience_payload["object_type"], "experience")
        self.assertEqual(experience_payload["summary"]["events"], 2)
        self.assertEqual(
            {event["event"] for event in experience_payload["events"]},
            {"recorded", "validated"},
        )

        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_history_habit",
                task_id="task_cli_history_habit",
                user_goal="write cli history habit report",
            )
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_cli_history_habit/completion/evaluation.json"],
            note="verified for history",
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(
            verified["procedure_id"],
            note="stable history habit",
        )
        gateway.procedure_memory.review(
            habit["procedure_id"],
            decision="approved",
            reviewer="maintainer",
            note="history review",
            evidence_refs=["manual_review/history.md"],
        )

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "history",
                "--type",
                "habit",
                habit["procedure_id"],
                "--json",
            ]
        )

        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-history/v1")
        self.assertEqual(habit_payload["object_type"], "habit")
        habit_events = {(event["source"], event["event"]) for event in habit_payload["events"]}
        self.assertIn(("procedure", "created"), habit_events)
        self.assertIn(("procedure_audit", "promoted_to_verified"), habit_events)
        self.assertIn(("procedure_audit", "promoted_to_habit"), habit_events)
        self.assertIn(("procedure_review", "reviewed"), habit_events)
        self.assertEqual(habit_payload["summary"]["by_source"]["procedure_review"], 1)

        gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.learning_history_file",
            rationale="history test",
        )

        skill_exit, skill_output = self._run_cli(
            [
                "learning",
                "history",
                "--type",
                "skill",
                "generated.habit.learning_history_file",
                "--json",
            ]
        )

        self.assertEqual(skill_exit, 0, skill_output)
        skill_payload = json.loads(skill_output)
        self.assertEqual(skill_payload["schema_version"], "learning-history/v1")
        self.assertEqual(skill_payload["object_type"], "skill")
        skill_events = {(event["source"], event["event"]) for event in skill_payload["events"]}
        self.assertIn(("skill_draft", "drafted"), skill_events)
        self.assertIn(("skill_validation", "validated"), skill_events)
        self.assertTrue(
            any(
                event["source"] == "skill_registration" and event["event"] == "activate"
                for event in skill_payload["events"]
            )
        )
        self.assertIn(("skill_lineage", "lineage_recorded"), skill_events)
        self.assertEqual(skill_payload["summary"]["by_source"]["skill_lineage"], 1)

    def test_cli_learning_review_filters_habit_items(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_habit_review",
                task_id="task_cli_habit_review",
                user_goal="write cli habit review report",
            )
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_cli_habit_review/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

        exit_code, output = self._run_cli(["learning", "review", "--type", "habit", "--json"])

        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["summary"]["experiences"], 0)
        self.assertEqual(payload["summary"]["procedures"], 0)
        self.assertEqual(payload["summary"]["habits"], 1)
        self.assertEqual(payload["summary"]["skills"], 0)
        self.assertEqual(payload["items"][0]["object_type"], "habit")
        self.assertEqual(payload["items"][0]["id"], habit["procedure_id"])
        self.assertTrue(payload["items"][0]["health"]["promotion_candidate"])

    def test_cli_learning_explain_match_outputs_habit_audit_context(self) -> None:
        gateway = self._build_gateway()
        disabled_draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_match_disabled",
                task_id="task_cli_match_disabled",
                user_goal="write cli match report",
            )
        )
        assert disabled_draft is not None
        gateway.procedure_memory.disable(disabled_draft["procedure_id"], note="stale match")

        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_match_habit",
                task_id="task_cli_match_habit",
                user_goal="write cli match habit report",
            )
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_cli_match_habit/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

        exit_code, output = self._run_cli(
            [
                "learning",
                "explain-match",
                "write cli match habit report again",
                "--json",
            ]
        )

        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["schema_version"], "learning-match-explanation/v1")
        self.assertTrue(payload["matched"])
        self.assertEqual(payload["match"]["matched_procedure_id"], habit["procedure_id"])
        self.assertEqual(payload["matched_item"]["object_type"], "habit")
        self.assertEqual(payload["matched_item"]["id"], habit["procedure_id"])
        self.assertEqual(payload["matched_item"]["review_status"], "promotion_ready")
        self.assertEqual(payload["summary"]["matched_object_type"], "habit")
        self.assertEqual(payload["summary"]["matched_id"], habit["procedure_id"])
        self.assertEqual(payload["summary"]["suggestion_mode"], "habit_fast_path_candidate")
        rejected = {
            item["procedure_id"]: item
            for item in payload["rejected_candidate_items"]
            if item.get("procedure_id")
        }
        self.assertIn(disabled_draft["procedure_id"], rejected)
        self.assertEqual(rejected[disabled_draft["procedure_id"]]["status"], "disabled")
        self.assertEqual(
            rejected[disabled_draft["procedure_id"]]["item"]["review_status"],
            "disabled",
        )

    def test_cli_learning_edit_controls_procedure_and_habit_objects(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_edit_draft",
                task_id="task_cli_edit_draft",
                user_goal="write cli edit draft report",
            )
        )
        assert draft is not None

        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "edit",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--patch",
                json.dumps({"name": "edited draft procedure"}),
                "--note",
                "rename draft through learning governance",
                "--json",
            ]
        )

        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "edit")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "draft")
        self.assertEqual(procedure_payload["status"], "draft")
        self.assertEqual(procedure_payload["raw_object"]["name"], "edited draft procedure")
        self.assertEqual(procedure_payload["item"]["review_status"], "edited")
        self.assertEqual(procedure_payload["audit_record"]["event"], "edited")
        self.assertEqual(procedure_payload["audit_record"]["changed_fields"], ["name"])

        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_edit_habit",
                task_id="task_cli_edit_habit",
                user_goal="write cli edit habit report",
            )
        )
        assert habit_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_edit_habit/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "edit",
                "--type",
                "habit",
                habit["procedure_id"],
                "--patch",
                json.dumps({"trigger": {"task_patterns": ["write corrected habit report"]}}),
                "--reviewer",
                "maintainer",
                "--note",
                "narrow habit trigger",
                "--json",
            ]
        )

        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "edit")
        self.assertEqual(habit_payload["object_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "habit")
        self.assertEqual(habit_payload["status"], "draft")
        self.assertEqual(habit_payload["item"]["object_type"], "procedure")
        self.assertEqual(habit_payload["item"]["review_status"], "edited_requires_review")
        self.assertTrue(habit_payload["item"]["requires_review"])
        self.assertEqual(habit_payload["raw_object"]["success_count"], 0)
        self.assertEqual(habit_payload["raw_object"]["failure_count"], 0)
        self.assertEqual(habit_payload["audit_record"]["event"], "edited")
        self.assertEqual(habit_payload["audit_record"]["changed_fields"], ["trigger"])
        self.assertEqual(habit_payload["audit_record"]["from_status"], "habit")

        habit_review = self._build_gateway().review_learning(object_type="habit")
        self.assertEqual(habit_review["summary"]["habits"], 0)

    def test_cli_learning_decay_controls_procedure_and_habit_objects(self) -> None:
        gateway = self._build_gateway()
        procedure_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_decay_procedure",
                task_id="task_cli_decay_procedure",
                user_goal="write cli decay procedure report",
            )
        )
        assert procedure_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            procedure_source["procedure_id"],
            evidence_refs=["tasks/task_cli_decay_procedure/completion/evaluation.json"],
        )

        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "decay",
                "--type",
                "procedure",
                verified["procedure_id"],
                "--reviewer",
                "maintainer",
                "--note",
                "procedure evidence is stale",
                "--json",
            ]
        )

        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "decay")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "verified")
        self.assertEqual(procedure_payload["status"], "draft")
        self.assertEqual(procedure_payload["item"]["review_status"], "decayed_requires_review")
        self.assertTrue(procedure_payload["item"]["requires_review"])
        self.assertEqual(procedure_payload["raw_object"]["success_count"], 0)
        self.assertEqual(procedure_payload["raw_object"]["failure_count"], 0)
        self.assertEqual(procedure_payload["audit_record"]["event"], "decayed")
        self.assertEqual(procedure_payload["audit_record"]["from_status"], "verified")
        self.assertEqual(procedure_payload["audit_record"]["to_status"], "draft")
        self.assertEqual(procedure_payload["audit_record"]["reviewer"], "maintainer")

        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_decay_habit",
                task_id="task_cli_decay_habit",
                user_goal="write cli decay habit report",
            )
        )
        assert habit_source is not None
        habit_verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_decay_habit/completion/evaluation.json"],
        )
        habit_verified["success_count"] = 3
        gateway.procedure_memory.save(habit_verified)
        habit = gateway.procedure_memory.promote_to_habit(habit_verified["procedure_id"])

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "decay",
                "--type",
                "habit",
                habit["procedure_id"],
                "--note",
                "habit evidence is stale",
                "--json",
            ]
        )

        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "decay")
        self.assertEqual(habit_payload["object_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "habit")
        self.assertEqual(habit_payload["status"], "verified")
        self.assertEqual(habit_payload["item"]["object_type"], "procedure")
        self.assertEqual(habit_payload["item"]["review_status"], "decayed_requires_review")
        self.assertTrue(habit_payload["item"]["requires_review"])
        self.assertEqual(habit_payload["raw_object"]["success_count"], 0)
        self.assertEqual(habit_payload["audit_record"]["from_status"], "habit")
        self.assertEqual(habit_payload["audit_record"]["to_status"], "verified")

        habit_review = self._build_gateway().review_learning(object_type="habit")
        self.assertEqual(habit_review["summary"]["habits"], 0)
        procedure_review = self._build_gateway().review_learning(object_type="procedure")
        decayed_item = next(
            item for item in procedure_review["items"] if item["id"] == habit["procedure_id"]
        )
        self.assertEqual(decayed_item["status"], "verified")
        self.assertEqual(decayed_item["review_status"], "decayed_requires_review")
        self.assertIn(
            "decayed_requires_review",
            {finding["code"] for finding in decayed_item["findings"]},
        )

        history_exit, history_output = self._run_cli(
            [
                "learning",
                "history",
                "--type",
                "procedure",
                habit["procedure_id"],
                "--json",
            ]
        )
        self.assertEqual(history_exit, 0, history_output)
        history_payload = json.loads(history_output)
        self.assertIn(
            ("procedure_audit", "decayed"),
            {(event["source"], event["event"]) for event in history_payload["events"]},
        )

        approve_without_evidence_exit, approve_without_evidence_output = self._run_cli(
            [
                "learning",
                "approve",
                "--type",
                "procedure",
                habit["procedure_id"],
                "--json",
            ]
        )
        self.assertEqual(approve_without_evidence_exit, 1)
        self.assertIn("requires evidence refs", approve_without_evidence_output)

        approve_exit, approve_output = self._run_cli(
            [
                "learning",
                "approve",
                "--type",
                "procedure",
                habit["procedure_id"],
                "--evidence-ref",
                "tasks/task_cli_decay_habit/fresh-validation.json",
                "--note",
                "fresh evidence restored procedure reuse",
                "--json",
            ]
        )
        self.assertEqual(approve_exit, 0, approve_output)
        approve_payload = json.loads(approve_output)
        self.assertEqual(approve_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(approve_payload["operation"], "approve")
        self.assertEqual(approve_payload["previous_status"], "verified")
        self.assertEqual(approve_payload["status"], "verified")
        self.assertEqual(approve_payload["item"]["review_status"], "approved")
        self.assertFalse(approve_payload["item"]["requires_review"])
        self.assertEqual(approve_payload["review_record"]["decision"], "approved")
        self.assertEqual(
            approve_payload["review_record"]["evidence_refs"],
            ["tasks/task_cli_decay_habit/fresh-validation.json"],
        )

    def test_cli_learning_promote_controls_procedure_and_habit_objects(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_promote_source",
                task_id="task_cli_promote_source",
                user_goal="write cli promote report",
            )
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_cli_promote_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "promote",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--note",
                "stable enough for habit",
                "--json",
            ]
        )

        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "promote")
        self.assertEqual(habit_payload["object_type"], "procedure")
        self.assertEqual(habit_payload["target_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "verified")
        self.assertEqual(habit_payload["status"], "habit")
        self.assertEqual(habit_payload["target_id"], draft["procedure_id"])
        self.assertEqual(habit_payload["item"]["object_type"], "habit")
        self.assertEqual(habit_payload["audit_record"]["event"], "promoted_to_habit")

        skill_exit, skill_output = self._run_cli(
            [
                "learning",
                "promote",
                "--type",
                "habit",
                draft["procedure_id"],
                "--skill-name",
                "generated.habit.learning_promote_file",
                "--approver",
                "maintainer",
                "--rationale",
                "promote through learning governance",
                "--json",
            ]
        )

        self.assertEqual(skill_exit, 0, skill_output)
        skill_payload = json.loads(skill_output)
        self.assertEqual(skill_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(skill_payload["operation"], "promote")
        self.assertEqual(skill_payload["object_type"], "habit")
        self.assertEqual(skill_payload["target_type"], "skill")
        self.assertEqual(skill_payload["previous_status"], "habit")
        self.assertEqual(skill_payload["target_id"], "generated.habit.learning_promote_file")
        self.assertEqual(skill_payload["status"], "active")
        self.assertEqual(skill_payload["item"]["object_type"], "skill")
        self.assertEqual(skill_payload["item"]["lineage"]["habit_id"], draft["procedure_id"])
        self.assertTrue(skill_payload["validation_report"]["passed"])
        self.assertEqual(skill_payload["approval_record"]["status"], "not_required")
        self.assertEqual(skill_payload["registration_record"]["status"], "active")

    def test_cli_learning_approve_controls_procedure_and_skill_objects(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_approve_draft",
                task_id="task_cli_approve_draft",
                user_goal="write cli approve draft report",
            )
        )
        assert draft is not None
        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "approve",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--evidence-ref",
                "tasks/task_cli_approve_draft/completion/evaluation.json",
                "--note",
                "approve validated draft",
                "--json",
            ]
        )

        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "approve")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "draft")
        self.assertEqual(procedure_payload["status"], "verified")
        self.assertEqual(procedure_payload["item"]["status"], "verified")
        self.assertEqual(procedure_payload["review_record"]["decision"], "approved")

        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_approve_skill",
                task_id="task_cli_approve_skill",
                user_goal="write cli approve skill report",
            )
        )
        assert habit_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_approve_skill/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        verified["risk_level"] = "medium"
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])
        gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.learning_approve_file",
        )

        skill_before = gateway.show_learning_object(
            object_type="skill",
            object_id="generated.habit.learning_approve_file",
        )
        self.assertEqual(skill_before["item"]["status"], "validated")
        self.assertEqual(skill_before["item"]["review_status"], "missing")

        skill_exit, skill_output = self._run_cli(
            [
                "learning",
                "approve",
                "--type",
                "skill",
                "generated.habit.learning_approve_file",
                "--reviewer",
                "maintainer",
                "--note",
                "manual approval through learning governance",
                "--json",
            ]
        )

        self.assertEqual(skill_exit, 0, skill_output)
        skill_payload = json.loads(skill_output)
        self.assertEqual(skill_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(skill_payload["operation"], "approve")
        self.assertEqual(skill_payload["object_type"], "skill")
        self.assertEqual(skill_payload["previous_status"], "validated")
        self.assertEqual(skill_payload["status"], "approved")
        self.assertEqual(skill_payload["item"]["review_status"], "approved")
        self.assertEqual(skill_payload["approval_record"]["status"], "approved")
        self.assertEqual(skill_payload["approval_record"]["approver"], "maintainer")

    def test_cli_learning_request_changes_controls_procedure_and_habit_objects(self) -> None:
        gateway = self._build_gateway()
        procedure_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_request_changes_procedure",
                task_id="task_cli_request_changes_procedure",
                user_goal="write cli request changes procedure report",
            )
        )
        assert procedure_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            procedure_source["procedure_id"],
            evidence_refs=[
                "tasks/task_cli_request_changes_procedure/completion/evaluation.json"
            ],
        )

        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "request-changes",
                "--type",
                "procedure",
                verified["procedure_id"],
                "--reviewer",
                "maintainer",
                "--note",
                "procedure trigger is too broad",
                "--evidence-ref",
                "reviews/request-changes/procedure.md",
                "--json",
            ]
        )

        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "request_changes")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "verified")
        self.assertEqual(procedure_payload["status"], "draft")
        self.assertEqual(procedure_payload["item"]["status"], "draft")
        self.assertEqual(procedure_payload["item"]["review_status"], "needs_changes")
        self.assertTrue(procedure_payload["item"]["requires_review"])
        self.assertEqual(procedure_payload["raw_object"]["success_count"], 0)
        self.assertEqual(procedure_payload["review_record"]["decision"], "needs_changes")
        self.assertEqual(procedure_payload["review_record"]["reviewer"], "maintainer")
        self.assertEqual(
            procedure_payload["review_record"]["evidence_refs"],
            ["reviews/request-changes/procedure.md"],
        )

        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_request_changes_habit",
                task_id="task_cli_request_changes_habit",
                user_goal="write cli request changes habit report",
            )
        )
        assert habit_source is not None
        habit_verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_request_changes_habit/completion/evaluation.json"],
        )
        habit_verified["success_count"] = 3
        gateway.procedure_memory.save(habit_verified)
        habit = gateway.procedure_memory.promote_to_habit(habit_verified["procedure_id"])

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "request-changes",
                "--type",
                "habit",
                habit["procedure_id"],
                "--note",
                "habit validator is too weak",
                "--json",
            ]
        )

        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "request_changes")
        self.assertEqual(habit_payload["object_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "habit")
        self.assertEqual(habit_payload["status"], "draft")
        self.assertEqual(habit_payload["item"]["object_type"], "procedure")
        self.assertEqual(habit_payload["item"]["review_status"], "needs_changes")
        self.assertTrue(habit_payload["item"]["requires_review"])
        self.assertEqual(habit_payload["raw_object"]["success_count"], 0)
        self.assertEqual(habit_payload["review_record"]["decision"], "needs_changes")

        habit_review = self._build_gateway().review_learning(object_type="habit")
        self.assertEqual(habit_review["summary"]["habits"], 0)
        procedure_review = self._build_gateway().review_learning(object_type="procedure")
        changed_item = next(
            item for item in procedure_review["items"] if item["id"] == habit["procedure_id"]
        )
        self.assertEqual(changed_item["status"], "draft")
        self.assertEqual(changed_item["review_status"], "needs_changes")
        self.assertTrue(changed_item["requires_review"])

        history_exit, history_output = self._run_cli(
            [
                "learning",
                "history",
                "--type",
                "procedure",
                habit["procedure_id"],
                "--json",
            ]
        )
        self.assertEqual(history_exit, 0, history_output)
        history_payload = json.loads(history_output)
        self.assertIn(
            ("procedure_review", "reviewed", "needs_changes"),
            {
                (event["source"], event["event"], event.get("status"))
                for event in history_payload["events"]
            },
        )

    def test_cli_learning_reject_controls_procedure_habit_and_skill_objects(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_reject_draft",
                task_id="task_cli_reject_draft",
                user_goal="write cli reject draft report",
            )
        )
        assert draft is not None
        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "reject",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--note",
                "not reusable",
                "--json",
            ]
        )

        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "reject")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "draft")
        self.assertEqual(procedure_payload["status"], "disabled")
        self.assertEqual(procedure_payload["item"]["review_status"], "disabled")
        self.assertEqual(procedure_payload["review_record"]["decision"], "rejected")

        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_reject_habit",
                task_id="task_cli_reject_habit",
                user_goal="write cli reject habit report",
            )
        )
        assert habit_source is not None
        verified_habit = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_reject_habit/completion/evaluation.json"],
        )
        verified_habit["success_count"] = 3
        gateway.procedure_memory.save(verified_habit)
        habit = gateway.procedure_memory.promote_to_habit(verified_habit["procedure_id"])

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "reject",
                "--type",
                "habit",
                habit["procedure_id"],
                "--note",
                "habit is too broad",
                "--json",
            ]
        )

        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "reject")
        self.assertEqual(habit_payload["object_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "habit")
        self.assertEqual(habit_payload["status"], "disabled")
        self.assertEqual(habit_payload["item"]["object_type"], "habit")
        self.assertEqual(habit_payload["item"]["review_status"], "disabled")
        self.assertEqual(habit_payload["review_record"]["decision"], "rejected")

        disabled_habit_review = self._build_gateway().review_learning(
            object_type="habit",
            include_disabled=True,
        )
        self.assertEqual(disabled_habit_review["summary"]["habits"], 1)
        self.assertEqual(disabled_habit_review["items"][0]["id"], habit["procedure_id"])
        self.assertEqual(disabled_habit_review["items"][0]["status"], "disabled")

        skill_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_reject_skill",
                task_id="task_cli_reject_skill",
                user_goal="write cli reject skill report",
            )
        )
        assert skill_source is not None
        verified_skill = gateway.procedure_memory.promote_to_verified(
            skill_source["procedure_id"],
            evidence_refs=["tasks/task_cli_reject_skill/completion/evaluation.json"],
        )
        verified_skill["success_count"] = 3
        verified_skill["risk_level"] = "medium"
        gateway.procedure_memory.save(verified_skill)
        skill_habit = gateway.procedure_memory.promote_to_habit(verified_skill["procedure_id"])
        gateway.promote_habit_to_skill(
            skill_habit["procedure_id"],
            skill_name="generated.habit.learning_reject_file",
        )

        skill_exit, skill_output = self._run_cli(
            [
                "learning",
                "reject",
                "--type",
                "skill",
                "generated.habit.learning_reject_file",
                "--reviewer",
                "maintainer",
                "--note",
                "too risky for automatic reuse",
                "--json",
            ]
        )

        self.assertEqual(skill_exit, 0, skill_output)
        skill_payload = json.loads(skill_output)
        self.assertEqual(skill_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(skill_payload["operation"], "reject")
        self.assertEqual(skill_payload["object_type"], "skill")
        self.assertEqual(skill_payload["previous_status"], "validated")
        self.assertEqual(skill_payload["status"], "validated")
        self.assertEqual(skill_payload["item"]["review_status"], "rejected")
        self.assertEqual(skill_payload["approval_record"]["status"], "rejected")
        self.assertEqual(skill_payload["approval_record"]["approver"], "maintainer")

    def test_cli_learning_disable_and_enable_controls_learning_objects(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_disable_draft",
                task_id="task_cli_disable_draft",
                user_goal="write cli disable draft report",
            )
        )
        assert draft is not None
        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_disable_habit",
                task_id="task_cli_disable_habit",
                user_goal="write cli disable habit report",
            )
        )
        assert habit_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_disable_habit/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])
        gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.learning_disable_file",
        )

        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "disable",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--note",
                "stale draft",
                "--json",
            ]
        )
        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "disable")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "draft")
        self.assertEqual(procedure_payload["status"], "disabled")
        self.assertEqual(procedure_payload["item"]["review_status"], "disabled")
        self.assertEqual(
            procedure_payload["raw_object"]["audit_notes"][-1]["previous_status"],
            "draft",
        )

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "disable",
                "--type",
                "habit",
                habit["procedure_id"],
                "--note",
                "stale habit",
                "--json",
            ]
        )
        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "disable")
        self.assertEqual(habit_payload["object_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "habit")
        self.assertEqual(habit_payload["status"], "disabled")
        self.assertEqual(habit_payload["item"]["object_type"], "habit")
        self.assertEqual(habit_payload["item"]["review_status"], "disabled")
        self.assertFalse(habit_payload["item"]["requires_review"])
        self.assertFalse(habit_payload["item"]["health"]["promotion_candidate"])
        self.assertEqual(
            habit_payload["raw_object"]["audit_notes"][-1]["previous_status"],
            "habit",
        )

        review = self._build_gateway().review_learning(object_type="habit")
        self.assertEqual(review["summary"]["habits"], 0)
        disabled_review = self._build_gateway().review_learning(
            object_type="habit",
            include_disabled=True,
        )
        self.assertEqual(disabled_review["summary"]["habits"], 1)
        disabled_item = disabled_review["items"][0]
        self.assertEqual(disabled_item["id"], habit["procedure_id"])
        self.assertEqual(disabled_item["status"], "disabled")
        self.assertEqual(disabled_item["review_status"], "disabled")

        skill_exit, skill_output = self._run_cli(
            [
                "learning",
                "disable",
                "--type",
                "skill",
                "generated.habit.learning_disable_file",
                "--note",
                "stale skill",
                "--json",
            ]
        )
        self.assertEqual(skill_exit, 0, skill_output)
        skill_payload = json.loads(skill_output)
        self.assertEqual(skill_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(skill_payload["operation"], "disable")
        self.assertEqual(skill_payload["object_type"], "skill")
        self.assertEqual(skill_payload["previous_status"], "active")
        self.assertEqual(skill_payload["status"], "deprecated")
        self.assertEqual(skill_payload["item"]["object_type"], "skill")
        self.assertEqual(skill_payload["item"]["status"], "deprecated")
        self.assertEqual(
            skill_payload["registration_record"]["metadata"]["note"],
            "stale skill",
        )
        self.assertEqual(
            skill_payload["raw_object"]["registry_entry"]["lifecycle_status"],
            "deprecated",
        )

        procedure_enable_exit, procedure_enable_output = self._run_cli(
            [
                "learning",
                "enable",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--note",
                "restore draft",
                "--json",
            ]
        )
        self.assertEqual(procedure_enable_exit, 0, procedure_enable_output)
        procedure_enable_payload = json.loads(procedure_enable_output)
        self.assertEqual(procedure_enable_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_enable_payload["operation"], "enable")
        self.assertEqual(procedure_enable_payload["object_type"], "procedure")
        self.assertEqual(procedure_enable_payload["previous_status"], "disabled")
        self.assertEqual(procedure_enable_payload["status"], "draft")
        self.assertEqual(
            procedure_enable_payload["raw_object"]["audit_notes"][-1]["restored_status"],
            "draft",
        )

        habit_enable_exit, habit_enable_output = self._run_cli(
            [
                "learning",
                "enable",
                "--type",
                "habit",
                habit["procedure_id"],
                "--note",
                "restore habit",
                "--json",
            ]
        )
        self.assertEqual(habit_enable_exit, 0, habit_enable_output)
        habit_enable_payload = json.loads(habit_enable_output)
        self.assertEqual(habit_enable_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_enable_payload["operation"], "enable")
        self.assertEqual(habit_enable_payload["object_type"], "habit")
        self.assertEqual(habit_enable_payload["previous_status"], "disabled")
        self.assertEqual(habit_enable_payload["status"], "habit")
        self.assertEqual(habit_enable_payload["item"]["object_type"], "habit")
        self.assertTrue(habit_enable_payload["item"]["health"]["promotion_candidate"])
        self.assertEqual(
            habit_enable_payload["raw_object"]["audit_notes"][-1]["restored_status"],
            "habit",
        )

        restored_review = self._build_gateway().review_learning(object_type="habit")
        self.assertEqual(restored_review["summary"]["habits"], 1)
        self.assertEqual(restored_review["items"][0]["id"], habit["procedure_id"])
        self.assertEqual(restored_review["items"][0]["status"], "habit")

        skill_enable_exit, skill_enable_output = self._run_cli(
            [
                "learning",
                "enable",
                "--type",
                "skill",
                "generated.habit.learning_disable_file",
                "--note",
                "restore skill",
                "--json",
            ]
        )
        self.assertEqual(skill_enable_exit, 0, skill_enable_output)
        skill_enable_payload = json.loads(skill_enable_output)
        self.assertEqual(skill_enable_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(skill_enable_payload["operation"], "enable")
        self.assertEqual(skill_enable_payload["object_type"], "skill")
        self.assertEqual(skill_enable_payload["previous_status"], "deprecated")
        self.assertEqual(skill_enable_payload["status"], "active")
        self.assertEqual(skill_enable_payload["item"]["status"], "active")
        self.assertEqual(
            skill_enable_payload["registration_record"]["metadata"]["note"],
            "restore skill",
        )
        self.assertEqual(
            skill_enable_payload["raw_object"]["registry_entry"]["lifecycle_status"],
            "active",
        )

    def test_cli_learning_validate_controls_procedure_habit_and_skill_objects(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_validate_draft",
                task_id="task_cli_validate_draft",
                user_goal="write cli validate draft report",
            )
        )
        assert draft is not None

        procedure_exit, procedure_output = self._run_cli(
            [
                "learning",
                "validate",
                "--type",
                "procedure",
                draft["procedure_id"],
                "--evidence-ref",
                "tasks/task_cli_validate_draft/completion/evaluation.json",
                "--reviewer",
                "validator",
                "--note",
                "validate draft through learning governance",
                "--json",
            ]
        )
        self.assertEqual(procedure_exit, 0, procedure_output)
        procedure_payload = json.loads(procedure_output)
        self.assertEqual(procedure_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(procedure_payload["operation"], "validate")
        self.assertEqual(procedure_payload["object_type"], "procedure")
        self.assertEqual(procedure_payload["previous_status"], "draft")
        self.assertEqual(procedure_payload["status"], "verified")
        self.assertEqual(procedure_payload["item"]["status"], "verified")
        self.assertEqual(procedure_payload["item"]["review_status"], "approved")
        self.assertEqual(procedure_payload["review_record"]["decision"], "approved")
        self.assertEqual(procedure_payload["review_record"]["reviewer"], "validator")
        self.assertEqual(
            procedure_payload["review_record"]["evidence_refs"],
            ["tasks/task_cli_validate_draft/completion/evaluation.json"],
        )

        gateway = self._build_gateway()
        habit_source = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_cli_validate_habit",
                task_id="task_cli_validate_habit",
                user_goal="write cli validate habit report",
            )
        )
        assert habit_source is not None
        verified = gateway.procedure_memory.promote_to_verified(
            habit_source["procedure_id"],
            evidence_refs=["tasks/task_cli_validate_habit/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        habit = gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

        habit_exit, habit_output = self._run_cli(
            [
                "learning",
                "validate",
                "--type",
                "habit",
                habit["procedure_id"],
                "--evidence-ref",
                "manual/habit-validation.md",
                "--reviewer",
                "validator",
                "--note",
                "validate habit through learning governance",
                "--json",
            ]
        )
        self.assertEqual(habit_exit, 0, habit_output)
        habit_payload = json.loads(habit_output)
        self.assertEqual(habit_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(habit_payload["operation"], "validate")
        self.assertEqual(habit_payload["object_type"], "habit")
        self.assertEqual(habit_payload["previous_status"], "habit")
        self.assertEqual(habit_payload["status"], "habit")
        self.assertEqual(habit_payload["item"]["object_type"], "habit")
        self.assertEqual(habit_payload["item"]["review_status"], "promotion_ready")
        self.assertEqual(habit_payload["raw_object"]["review_status"], "approved")
        self.assertEqual(habit_payload["review_record"]["decision"], "approved")
        self.assertEqual(
            habit_payload["review_record"]["evidence_refs"],
            ["manual/habit-validation.md"],
        )

        gateway = self._build_gateway()
        skill_dir = self._create_external_skill(
            root=self.project_root / "imports" / "learning_validate_echo",
            skill_name="imported.learning_validate_echo",
            version="0.1.0",
            message="learning validate echo",
        )
        gateway.import_skill(str(skill_dir))

        validate_exit, validate_output = self._run_cli(
            [
                "learning",
                "validate",
                "--type",
                "skill",
                "imported.learning_validate_echo",
                "--version",
                "0.1.0",
                "--json",
            ]
        )

        self.assertEqual(validate_exit, 0, validate_output)
        validate_payload = json.loads(validate_output)
        self.assertEqual(validate_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(validate_payload["operation"], "validate")
        self.assertEqual(validate_payload["object_type"], "skill")
        self.assertEqual(validate_payload["id"], "imported.learning_validate_echo")
        self.assertEqual(validate_payload["previous_status"], "draft")
        self.assertEqual(validate_payload["previous_version"], "0.1.0")
        self.assertEqual(validate_payload["target_version"], "0.1.0")
        self.assertEqual(validate_payload["status"], "approved")
        self.assertEqual(validate_payload["item"]["status"], "approved")
        self.assertTrue(validate_payload["validation_report"]["passed"])
        self.assertEqual(validate_payload["approval_record"]["status"], "not_required")
        self.assertFalse(validate_payload["approval_policy"]["approval_required"])
        self.assertEqual(
            validate_payload["raw_object"]["registry_entry"]["lifecycle_status"],
            "approved",
        )
        self.assertEqual(
            validate_payload["raw_object"]["validation_reports"][0]["version"],
            "0.1.0",
        )

        history = self._build_gateway().history_learning_object(
            object_type="skill",
            object_id="imported.learning_validate_echo",
        )
        self.assertIn(
            ("skill_validation", "passed"),
            {(event["source"], event["status"]) for event in history["events"]},
        )

    def test_cli_learning_rollback_controls_skill_object(self) -> None:
        gateway = self._build_gateway()
        v1_dir = self._create_external_skill(
            root=self.project_root / "imports" / "learning_echo_v1",
            skill_name="imported.learning_echo",
            version="0.1.0",
            message="learning echo v1",
        )
        gateway.import_skill(str(v1_dir))
        gateway.validate_skill("imported.learning_echo", version="0.1.0")
        gateway.activate_skill("imported.learning_echo", version="0.1.0")

        v2_dir = self._create_external_skill(
            root=self.project_root / "imports" / "learning_echo_v2",
            skill_name="imported.learning_echo",
            version="0.2.0",
            message="learning echo v2",
        )
        gateway.import_skill(str(v2_dir))
        gateway.validate_skill("imported.learning_echo", version="0.2.0")
        gateway.activate_skill("imported.learning_echo", version="0.2.0")

        rollback_exit, rollback_output = self._run_cli(
            [
                "learning",
                "rollback",
                "--type",
                "skill",
                "imported.learning_echo",
                "--to-version",
                "0.1.0",
                "--json",
            ]
        )

        self.assertEqual(rollback_exit, 0, rollback_output)
        rollback_payload = json.loads(rollback_output)
        self.assertEqual(rollback_payload["schema_version"], "learning-operation/v1")
        self.assertEqual(rollback_payload["operation"], "rollback")
        self.assertEqual(rollback_payload["object_type"], "skill")
        self.assertEqual(rollback_payload["previous_status"], "active")
        self.assertEqual(rollback_payload["previous_version"], "0.2.0")
        self.assertEqual(rollback_payload["target_version"], "0.1.0")
        self.assertEqual(rollback_payload["status"], "active")
        self.assertEqual(rollback_payload["item"]["version"], "0.1.0")
        self.assertEqual(rollback_payload["registration_record"]["action"], "rollback")
        self.assertEqual(rollback_payload["registration_record"]["previous_version"], "0.2.0")
        self.assertEqual(rollback_payload["registration_record"]["version"], "0.1.0")
        self.assertEqual(
            rollback_payload["raw_object"]["registry_entry"]["version"],
            "0.1.0",
        )

        detail = self._build_gateway().show_learning_object(
            object_type="skill",
            object_id="imported.learning_echo",
        )
        self.assertEqual(detail["item"]["status"], "active")
        self.assertEqual(detail["item"]["version"], "0.1.0")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


class AutoSkillGenerationIntegrationTest(unittest.TestCase):
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

    def test_missing_file_skill_can_be_generated_activated_and_executed(self) -> None:
        gateway = self._build_gateway()
        report = gateway.submit(
            TaskRequest(
                goal="运行一个缺失的 file skill",
                executor="file",
                skill_name="generated.file.helper",
                payload={
                    "action": "write",
                    "path": "reports/output.txt",
                    "content": "alpha",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertEqual(str(report.task.status), "completed")

        payload = gateway.show_task(report.task.task_id)
        action_node = next(node for node in payload["task_nodes"] if node.type.value == "action")
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.file.helper")
        self.assertEqual(
            action_node.metadata.get("skill_selection", {}).get("source"), "generated_skill"
        )
        self.assertTrue((self.workspace / "reports" / "output.txt").exists())

        skill_payload = gateway.show_skill("generated.file.helper")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "active")
        self.assertTrue(skill_payload["validation_reports"][0].passed)
        self.assertEqual(skill_payload["registration_records"][0].status, "active")
        draft = skill_payload["drafts"][0]
        self.assertEqual(draft.source_kind, "generated")
        self.assertTrue(Path(draft.metadata["generation_report_path"]).exists())

    def test_missing_python_skill_waits_for_approval_and_resumes(self) -> None:
        gateway = self._build_gateway()
        initial = gateway.send_message(
            "执行一个 python 任务",
            request_options={
                "executor": "python",
                "skill_name": "generated.python.helper",
                "payload": {"code": "result = {'ok': True}\nprint('python ok')"},
                "constraints": TaskConstraints(max_permission=PermissionLevel.L3),
            },
        )
        self.assertIsNotNone(initial.task_report)
        self.assertEqual(str(initial.task_report.task.status), "waiting_input")
        self.assertIsNotNone(initial.checkpoint)
        self.assertIn("自动生成 Skill generated.python.helper", initial.task_report.result.message)

        skill_payload = gateway.show_skill("generated.python.helper")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")
        self.assertTrue(skill_payload["validation_reports"][0].passed)
        self.assertEqual(skill_payload["approval_records"], [])

        followup = gateway.send_message("同意继续执行", thread_id=initial.thread.thread_id)
        self.assertIsNotNone(followup.task_report)
        self.assertEqual(str(followup.task_report.task.status), "completed")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        activation_permission = next(
            item for item in task_payload["permissions"] if item["permission"] == "skill.activate"
        )
        self.assertEqual(activation_permission["status"], "granted")
        self.assertEqual(activation_permission["scope"], "node")
        self.assertEqual(activation_permission["skill_name"], "generated.python.helper")

        final_skill_payload = gateway.show_skill("generated.python.helper")
        self.assertEqual(final_skill_payload["registry_entry"].lifecycle_status.value, "active")
        self.assertEqual(final_skill_payload["approval_records"][0].status, "approved")
        self.assertEqual(final_skill_payload["registration_records"][0].status, "active")

    def test_manual_api_generation_creates_validated_draft_with_audit_artifacts(self) -> None:
        gateway = self._build_gateway()
        payload = gateway.generate_skill(
            skill_name="generated.api.fetcher",
            generation_type="api",
            goal="请求一个 JSON API 并返回结果",
            payload_example={"url": "https://example.com/api", "response_format": "json"},
            trigger="user_request",
        )
        self.assertEqual(payload["status"], "validated")
        self.assertIsNotNone(payload["draft"])
        self.assertTrue(payload["validation_report"]["passed"])
        self.assertIsNone(payload["approval_record"])
        self.assertIsNone(payload["registration_record"])
        self.assertTrue(Path(payload["generation_report_path"]).exists())

        entry_id = "skillgen_generated.api.fetcher_0.0.1".replace("/", "_").replace(":", "_")
        workflow_summary = self.workspace / "WORKFLOWS" / f"{entry_id}.json"
        self.assertTrue(workflow_summary.exists())

    def test_incompatible_existing_skill_can_trigger_generated_replacement(self) -> None:
        skill_root = self.workspace / "skills" / "broken_file_skill"
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "skill.yaml").write_text(
            json.dumps(
                {
                    "name": "workspace.broken.file",
                    "version": "0.1.0",
                    "description": "Always fails",
                    "input_schema": {"action": "string", "path": "string", "content": "string"},
                    "output_schema": {"error": "string"},
                    "permission_requirements": ["filesystem.write"],
                    "dependency_requirements": [],
                    "side_effects": [],
                    "sandbox_level": "workspace",
                    "tags": ["file", "broken"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (skill_root / "main.py").write_text(
            "\n".join(
                [
                    "def run(payload, context):",
                    "    return {",
                    "        'success': False,",
                    "        'status': 'failed',",
                    "        'message': 'workspace broken skill failed',",
                    "        'output': {'error': 'broken'},",
                    "    }",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (skill_root / "README.md").write_text("# broken file skill\n", encoding="utf-8")

        gateway = self._build_gateway()
        report = gateway.submit(
            TaskRequest(
                goal="执行一个不适配的 file skill",
                executor="file",
                skill_name="workspace.broken.file",
                payload={
                    "action": "write",
                    "path": "reports/recovered.txt",
                    "content": "beta",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertEqual(str(report.task.status), "completed")

        payload = gateway.show_task(report.task.task_id)
        action_node = next(node for node in payload["task_nodes"] if node.type.value == "action")
        recovery = action_node.metadata.get("incompatible_skill_recovery", {})
        replacement_skill = recovery.get("replacement_skill")
        self.assertEqual(recovery.get("failed_skill"), "workspace.broken.file")
        self.assertIsNotNone(replacement_skill)
        self.assertEqual(action_node.metadata.get("selected_skill"), replacement_skill)
        self.assertTrue((self.workspace / "reports" / "recovered.txt").exists())
        self.assertTrue(
            any(event.event_type == "task.node.skill_generated" for event in payload["events"])
        )

        replacement_payload = gateway.show_skill(replacement_skill)
        self.assertEqual(replacement_payload["registry_entry"].lifecycle_status.value, "active")
        self.assertEqual(replacement_payload["drafts"][0].source_kind, "generated")
        self.assertTrue(
            Path(replacement_payload["drafts"][0].metadata["generation_report_path"]).exists()
        )

    def test_workflow_promotion_requires_habit_before_skill_generation(self) -> None:
        gateway = self._build_gateway()
        first = gateway.submit(
            TaskRequest(
                goal="写一个文件",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/a.txt",
                    "content": "alpha",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        second = gateway.submit(
            TaskRequest(
                goal="写一个文件",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/b.txt",
                    "content": "beta",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        candidate = gateway.show_task(second.task.task_id)["workflow_candidates"][0]

        promotion = gateway.promote_workflow(
            second.task.task_id,
            skill_name=candidate["skill_name"],
            approver="tester",
            rationale="把稳定 workflow 固化成受控生成能力",
        )
        self.assertIsNone(promotion["generated_skill"])
        self.assertEqual(promotion["candidate"]["solidification_result"], "blocked_requires_habit")
        self.assertIn("stable habit", promotion["candidate"]["solidification_blocker"])

        skill_payload = gateway.show_skill(candidate["skill_name"])
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "draft")
        self.assertEqual(
            {draft.source_kind for draft in skill_payload["drafts"]},
            {"workflow_compiled"},
        )
        self.assertEqual(first.task.status.value, "completed")


if __name__ == "__main__":
    unittest.main()

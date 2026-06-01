from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


class SkillLifecycleIntegrationTest(unittest.TestCase):
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

    def _create_external_skill(
        self,
        *,
        root: Path,
        skill_name: str,
        version: str,
        message: str,
        include_tests: bool = True,
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
        if include_tests:
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

    def test_workflow_candidate_stays_draft_and_is_not_selected(self) -> None:
        gateway = self._build_gateway()
        gateway.submit(
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
        task_payload = gateway.show_task(second.task.task_id)
        candidate = task_payload["workflow_candidates"][0]
        skill_payload = gateway.show_skill(candidate["skill_name"])
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "draft")

        third = gateway.submit(
            TaskRequest(
                goal="写一个文件",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/c.txt",
                    "content": "gamma",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        third_payload = gateway.show_task(third.task.task_id)
        action_node = next(
            node for node in third_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertNotEqual(action_node.metadata.get("selected_skill"), candidate["skill_name"])

    def test_explicit_inactive_skill_returns_standardized_gap_instead_of_fallback(self) -> None:
        gateway = self._build_gateway()
        gateway.submit(
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
        task_payload = gateway.show_task(second.task.task_id)
        candidate = task_payload["workflow_candidates"][0]

        failed = gateway.submit(
            TaskRequest(
                goal="显式运行草稿 workflow skill",
                executor="file",
                skill_name=candidate["skill_name"],
                payload={
                    "action": "write",
                    "path": "reports/c.txt",
                    "content": "gamma",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertEqual(str(failed.task.status), "failed")
        self.assertIn("尚不能执行", failed.result.message)

        failed_payload = gateway.show_task(failed.task.task_id)
        action_node = next(
            node for node in failed_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("skill_selection", {}).get("source"), "inactive_skill"
        )
        self.assertNotEqual(action_node.metadata.get("selected_skill"), candidate["skill_name"])
        self.assertEqual(action_node.error_message, failed.result.message)

    def test_imported_skill_can_validate_activate_and_execute(self) -> None:
        gateway = self._build_gateway()
        source_dir = self._create_external_skill(
            root=self.project_root / "imports" / "echo_v1",
            skill_name="imported.echo",
            version="0.1.0",
            message="echo skill v1",
        )
        imported = gateway.import_skill(str(source_dir))
        self.assertEqual(imported["registry_entry"].lifecycle_status.value, "draft")

        validation = gateway.validate_skill("imported.echo")
        self.assertTrue(validation["validation_report"].passed)
        self.assertEqual(validation["approval_record"].status, "not_required")
        self.assertEqual(validation["draft"].lifecycle_status.value, "approved")
        self.assertEqual(validation["validation_report"].checks["unit_tests"]["status"], "passed")

        activation = gateway.activate_skill("imported.echo")
        self.assertEqual(activation["registration_record"].action, "activate")
        managed_root = self.workspace / "skills" / "managed" / "imported.echo"
        self.assertTrue((managed_root / "skill.yaml").exists())

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行导入 Skill",
                skill_name="imported.echo",
                payload={"text": "hello imported"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L1),
            )
        )
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "imported.echo")
        self.assertEqual(action_node.output["echo"], "hello imported")
        self.assertEqual(action_node.output["version"], "0.1.0")

    def test_imported_skill_upgrade_rollback_and_deprecate(self) -> None:
        gateway = self._build_gateway()
        v1_dir = self._create_external_skill(
            root=self.project_root / "imports" / "echo_v1",
            skill_name="imported.echo",
            version="0.1.0",
            message="echo skill v1",
        )
        gateway.import_skill(str(v1_dir))
        gateway.validate_skill("imported.echo", version="0.1.0")
        gateway.activate_skill("imported.echo", version="0.1.0")

        v2_dir = self._create_external_skill(
            root=self.project_root / "imports" / "echo_v2",
            skill_name="imported.echo",
            version="0.2.0",
            message="echo skill v2",
        )
        gateway.import_skill(str(v2_dir))
        gateway.validate_skill("imported.echo", version="0.2.0")
        activation = gateway.activate_skill("imported.echo", version="0.2.0")
        self.assertEqual(activation["registration_record"].action, "upgrade")

        runtime_gateway = self._build_gateway()
        v2_report = runtime_gateway.submit(
            TaskRequest(
                goal="运行导入 Skill",
                skill_name="imported.echo",
                payload={"text": "version check"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L1),
            )
        )
        v2_payload = runtime_gateway.show_task(v2_report.task.task_id)
        v2_action = next(node for node in v2_payload["task_nodes"] if node.type.value == "action")
        self.assertEqual(v2_action.output["version"], "0.2.0")

        rollback = gateway.rollback_skill("imported.echo", to_version="0.1.0")
        self.assertEqual(rollback["registration_record"].action, "rollback")
        self.assertEqual(rollback["target_version"], "0.1.0")

        rolled_gateway = self._build_gateway()
        v1_report = rolled_gateway.submit(
            TaskRequest(
                goal="运行导入 Skill",
                skill_name="imported.echo",
                payload={"text": "rollback check"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L1),
            )
        )
        v1_payload = rolled_gateway.show_task(v1_report.task.task_id)
        v1_action = next(node for node in v1_payload["task_nodes"] if node.type.value == "action")
        self.assertEqual(v1_action.output["version"], "0.1.0")

        deprecate = gateway.deprecate_skill("imported.echo")
        self.assertEqual(deprecate["registration_record"].status, "deprecated")
        self.assertFalse((self.workspace / "skills" / "managed" / "imported.echo").exists())

        deprecated_gateway = self._build_gateway()
        skill_payload = deprecated_gateway.show_skill("imported.echo")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "deprecated")


if __name__ == "__main__":
    unittest.main()

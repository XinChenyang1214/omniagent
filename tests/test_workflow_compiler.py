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


class WorkflowCompilerIntegrationTest(unittest.TestCase):
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

    def test_single_workflow_run_does_not_promote_candidate(self) -> None:
        gateway = self._build_gateway()
        report = gateway.submit(
            TaskRequest(
                goal="写一个文件",
                executor="file",
                payload={
                    "action": "write",
                    "path": "drafts/a.txt",
                    "content": "first",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        payload = gateway.show_task(report.task.task_id)
        self.assertEqual(payload["workflow_candidates"], [])
        registry_path = self.project_root / "skills" / "registry" / "workflow_candidates.json"
        self.assertFalse(registry_path.exists())

    def test_repeated_successful_workflow_generates_skill_candidate_draft(self) -> None:
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

        payload = gateway.show_task(second.task.task_id)
        candidates = payload["workflow_candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["status"], "draft")
        self.assertEqual(
            set(candidate["source_task_ids"]), {first.task.task_id, second.task.task_id}
        )
        self.assertGreaterEqual(candidate["success_rate"], 1.0)
        self.assertGreaterEqual(candidate["parameter_count"], 2)

        manifest_path = Path(candidate["manifest_path"])
        template_path = Path(candidate["template_path"])
        self.assertTrue(manifest_path.exists())
        self.assertTrue(template_path.exists())

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], candidate["skill_name"])
        self.assertTrue(manifest["draft"])

        template = json.loads(template_path.read_text(encoding="utf-8"))
        parameter_names = {item["name"] for item in template["parameters"]}
        # Node IDs vary by planner implementation — check that path/content parameters exist
        self.assertTrue(
            any("path" in name for name in parameter_names),
            f"No path parameter in {parameter_names}",
        )
        self.assertTrue(
            any("content" in name for name in parameter_names),
            f"No content parameter in {parameter_names}",
        )

        registry_path = self.project_root / "skills" / "registry" / "workflow_candidates.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(len(registry["candidates"]), 1)
        self.assertEqual(registry["candidates"][0]["skill_name"], candidate["skill_name"])

        event_types = [event.event_type for event in payload["events"]]
        self.assertIn("workflow.candidate_generated", event_types)
        neighbor_types = {
            item["node"]["node_type"] for item in payload["graph_memory"]["neighbors"]
        }
        self.assertIn("SkillCandidate", neighbor_types)


if __name__ == "__main__":
    unittest.main()

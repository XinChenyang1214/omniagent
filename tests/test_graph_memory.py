from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


class GraphMemoryIntegrationTest(unittest.TestCase):
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

    def test_task_execution_is_written_to_graph_memory(self) -> None:
        gateway = self._build_gateway()
        report = gateway.submit(
            TaskRequest(
                goal="写一个文件然后读取这个文件",
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        graph_payload = gateway.show_task(report.task.task_id)["graph_memory"]
        self.assertEqual(graph_payload["root"]["entity_key"], f"task:{report.task.task_id}")
        neighbor_types = {item["node"]["node_type"] for item in graph_payload["neighbors"]}
        self.assertIn("Workflow", neighbor_types)
        self.assertIn("TaskNode", neighbor_types)
        self.assertTrue(gateway.graph_memory.related_workflows(report.task.task_id))

    def test_thread_interaction_is_written_to_graph_memory(self) -> None:
        gateway = self._build_gateway()
        response = gateway.send_message(
            "写一个文件",
            request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
        )
        thread_payload = gateway.show_thread(response.thread.thread_id)
        graph_payload = thread_payload["graph_memory"]
        self.assertEqual(graph_payload["root"]["entity_key"], f"thread:{response.thread.thread_id}")
        neighbor_types = {item["node"]["node_type"] for item in graph_payload["neighbors"]}
        self.assertIn("ConversationTurn", neighbor_types)
        self.assertIn("Task", neighbor_types)

    def test_memory_read_permission_decision_is_recorded(self) -> None:
        gateway = self._build_gateway()
        report = gateway.submit(
            TaskRequest(
                goal="在关闭记忆读取时执行一个简单任务",
                constraints=TaskConstraints(
                    max_permission=PermissionLevel.L2,
                    allow_memory_read=False,
                ),
            )
        )
        payload = gateway.show_task(report.task.task_id)
        memory_permission = next(
            item for item in payload["permissions"] if item["permission"] == "memory.read"
        )
        self.assertEqual(memory_permission["status"], "denied")
        self.assertEqual(memory_permission["scope"], "memory")


if __name__ == "__main__":
    unittest.main()

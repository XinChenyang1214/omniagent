from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.config import AppConfig
from omniagent.experience import ExperienceRecordWriter
from omniagent.gateway import RuntimeGateway
from omniagent.models import ExecutionResult, PermissionLevel, Task, TaskConstraints, TaskRequest, TaskStatus
from omniagent.procedure_memory import ProcedureMemoryStore


class ExperienceRecordIntegrationTest(unittest.TestCase):
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

    def _record_for(self, task_id: str) -> dict:
        path = self.workspace / "tasks" / task_id / "experience" / "experience_record.json"
        self.assertTrue(path.exists(), f"missing experience record: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _procedure_ids(self) -> list[str]:
        return [
            str(item["procedure_id"])
            for item in ProcedureMemoryStore(self.workspace).list(include_disabled=True)
        ]

    def test_success_task_writes_record_and_index_without_raw_payload_copy(self) -> None:
        gateway = self._build_gateway()
        raw_content = "payload body that should stay in the produced artifact only"
        request = TaskRequest(
            goal="Write an experience success artifact",
            executor="file",
            payload={
                "action": "write",
                "path": "reports/experience-success.txt",
                "content": raw_content,
                "overwrite": True,
                "api_key": "placeholder",
            },
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        self.assertTrue(report.result.success, report.result.message)
        record = self._record_for(report.task.task_id)
        self.assertEqual(record["schema_version"], "experience-record/v1")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["task_id"], report.task.task_id)
        self.assertTrue(record["actions"])
        self.assertTrue(record["artifacts"])
        self.assertIn("context_snapshot.json", "\n".join(record["context_refs"]))
        self.assertFalse(record["sensitive_data_policy"]["raw_payload_stored"])
        self.assertTrue(
            any(item["type"] == "procedure_candidate" for item in record["learning_candidates"])
        )
        serialized_record = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(raw_content, serialized_record)
        self.assertNotIn("api_key", serialized_record)
        self.assertNotIn("placeholder", serialized_record)

        index_path = self.workspace / "experience" / "index.jsonl"
        self.assertTrue(index_path.exists())
        self.assertIn(report.task.task_id, index_path.read_text(encoding="utf-8"))

    def test_failed_task_writes_failure_record(self) -> None:
        gateway = self._build_gateway()
        request = TaskRequest(
            goal="Read a missing file",
            executor="file",
            payload={"action": "read", "path": "missing.txt"},
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        self.assertFalse(report.result.success)
        record = self._record_for(report.task.task_id)
        self.assertEqual(record["status"], "failed")
        self.assertIsNotNone(record["failure"])
        self.assertTrue(
            any(item["type"] == "failure_pattern" for item in record["learning_candidates"])
        )
        self.assertFalse(
            any(item["type"] == "procedure_candidate" for item in record["learning_candidates"])
        )
        self.assertEqual(self._procedure_ids(), [])

    def test_permission_checkpoint_writes_permission_denied_record(self) -> None:
        gateway = self._build_gateway()
        request = TaskRequest(
            goal="Run Python without enough permission",
            skill_name="builtin.python.basic",
            payload={"code": "result = {'ok': True}"},
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )

        report = gateway.submit(request)

        self.assertEqual(str(report.task.status), "waiting_input")
        record = self._record_for(report.task.task_id)
        self.assertEqual(record["status"], "permission_denied")
        self.assertEqual(record["failure"]["failure_layer"], "permission")
        self.assertEqual(record["failure"]["permission"], "code_execution")
        self.assertFalse(
            any(item["type"] == "procedure_candidate" for item in record["learning_candidates"])
        )
        self.assertEqual(self._procedure_ids(), [])

    def test_success_without_validation_does_not_create_procedure_candidate(self) -> None:
        task = Task(
            task_id="task_unverified_success",
            goal="Write an unverified artifact",
            mode="semi_auto",
            source="cli",
            status=TaskStatus.COMPLETED,
            executor="file",
            skill_name=None,
            payload={"action": "write"},
            workspace_path=str(self.workspace),
        )
        request = TaskRequest(goal=task.goal, executor="file", payload=task.payload)
        result = ExecutionResult(
            success=True,
            status="completed",
            message="runtime completed without validation artifact",
            executor="file",
        )
        task_dir = self.workspace / "tasks" / task.task_id
        writer = ExperienceRecordWriter(self.workspace)

        record = writer.write(
            task=task,
            request=request,
            task_dir=task_dir,
            result=result,
            task_graph=None,
            critic_verdicts=[],
            completion_evaluation=None,
        )
        extracted = ProcedureMemoryStore(self.workspace).extract_draft_from_experience(record)

        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["validation"]["verdict"], "not_verified")
        self.assertFalse(
            any(item["type"] == "procedure_candidate" for item in record["learning_candidates"])
        )
        self.assertIsNone(extracted)
        self.assertEqual(self._procedure_ids(), [])


if __name__ == "__main__":
    unittest.main()

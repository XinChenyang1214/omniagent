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
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest
from omniagent.task_intake import TASK_INTAKE_SCHEMA_VERSION, assess_task_intake


class TaskIntakeAssessmentTest(unittest.TestCase):
    def test_file_organization_payload_records_low_risk_file_tool(self) -> None:
        assessment = assess_task_intake(
            TaskRequest(
                goal="整理收件箱文件：移动 inbox/raw.txt 到 archive/raw.txt",
                executor="file",
                payload={
                    "action": "move",
                    "source": "inbox/raw.txt",
                    "target": "archive/raw.txt",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[],
        )

        self.assertEqual(assessment["schema_version"], TASK_INTAKE_SCHEMA_VERSION)
        self.assertEqual(assessment["task_type"], "file_organization")
        self.assertEqual(assessment["risk_level"], "low")
        self.assertIn("builtin.file.basic", assessment["tools"])
        self.assertEqual(assessment["validation_strategy"], "artifact_file_check")
        self.assertFalse(assessment["confirmation_required"])

    def test_network_task_requires_confirmation(self) -> None:
        assessment = assess_task_intake(
            TaskRequest(
                goal="从 https://example.com 获取网页信息并生成摘要",
                skill_name="builtin.http.client",
                payload={"action": "get", "url": "https://example.com"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[],
        )

        self.assertEqual(assessment["task_type"], "web_information")
        self.assertEqual(assessment["risk_level"], "medium")
        self.assertIn("builtin.http.client", assessment["tools"])
        self.assertEqual(assessment["validation_strategy"], "retrieved_content_check")
        self.assertTrue(assessment["confirmation_required"])
        self.assertIn(
            "network_capable_tool_without_explicit_network",
            assessment["confirmation_reasons"],
        )

    def test_data_file_signal_wins_over_report_wording(self) -> None:
        assessment = assess_task_intake(
            TaskRequest(
                goal="Sum revenue from inputs/sales.csv and write reports/sales.md.",
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[],
        )

        self.assertEqual(assessment["task_type"], "data_processing")
        self.assertIn("builtin.data.csv", assessment["tools"])
        self.assertEqual(assessment["validation_strategy"], "data_result_and_artifact_check")

    def test_html_file_signal_wins_over_report_wording(self) -> None:
        assessment = assess_task_intake(
            TaskRequest(
                goal="Create an HTML report from inputs/page_a.html at reports/page_a.md.",
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[],
        )

        self.assertEqual(assessment["task_type"], "web_information")
        self.assertIn("builtin.web.html", assessment["tools"])
        self.assertEqual(assessment["validation_strategy"], "retrieved_content_check")

    def test_cli_prints_intake_event_before_task_created(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            workspace = project_root / "workspace"
            env = {
                "OMNIAGENT_SQLITE_PATH": str(project_root / "data" / "omniagent.sqlite3"),
                "OMNIAGENT_LOG_PATH": str(project_root / "artifacts" / "omniagent.log"),
                "OMNIAGENT_JSON_LOGS": "false",
                "OMNIAGENT_LLM_PROVIDER": "test",
            }
            stdout = StringIO()
            payload = {
                "action": "write",
                "path": "reports/intake-event.md",
                "content": "status: intake\n",
                "overwrite": True,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                redirect_stdout(stdout),
                redirect_stderr(stdout),
            ):
                exit_code = main(
                    [
                        "--workspace",
                        str(workspace),
                        "run",
                        "整理任务预判审计记录：写入 reports/intake-event.md",
                        "--executor",
                        "file",
                        "--payload",
                        json.dumps(payload, ensure_ascii=False),
                        "--max-permission",
                        "L2",
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0, output)
            intake_index = output.index("任务预判: type=file_organization")
            created_index = output.index("event: task.created")
            self.assertLess(intake_index, created_index)
            self.assertIn("validation=artifact_file_check", output)
            self.assertIn("confirmation_required=False", output)


if __name__ == "__main__":
    unittest.main()

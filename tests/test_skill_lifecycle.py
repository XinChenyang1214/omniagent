from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


def _passed_experience(
    *,
    experience_id: str = "exp_skill_source",
    task_id: str = "task_skill_source",
    user_goal: str = "write reusable file report",
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
        "context_refs": ["tasks/task_skill_source/context_snapshot.json"],
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
            "evidence_refs": ["tasks/task_skill_source/completion/evaluation.json"],
            "notes": "passed",
        },
        "failure": None,
        "learning_candidates": [],
        "sensitive_data_policy": {"raw_payload_stored": False},
    }


def _passed_json_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_json_source",
        task_id="task_json_source",
        user_goal="query reusable JSON profile",
    )
    experience["actions"] = [
        {
            "node_id": "node_query",
            "tool_or_skill": "builtin.data.json",
            "input_summary": "dict(keys=['action', 'text', 'query'], json_chars=80)",
            "output_summary": "dict(keys=['value'], json_chars=20)",
            "status": "completed",
            "artifact_refs": ["tasks/task_json_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "json_value",
            "path": "reports/profile_value.json",
            "description": "profile field",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_json_source/completion/evaluation.json"
    ]
    return experience


def _passed_csv_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_csv_source",
        task_id="task_csv_source",
        user_goal="aggregate reusable CSV sales",
    )
    experience["actions"] = [
        {
            "node_id": "node_aggregate",
            "tool_or_skill": "builtin.data.csv",
            "input_summary": "dict(keys=['action', 'path', 'aggregate'], json_chars=100)",
            "output_summary": "dict(keys=['result'], json_chars=40)",
            "status": "completed",
            "artifact_refs": ["tasks/task_csv_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "csv_metric",
            "path": "reports/sales_total.json",
            "description": "sales aggregate",
        }
    ]
    experience["validation"]["evidence_refs"] = ["tasks/task_csv_source/completion/evaluation.json"]
    return experience


def _passed_sqlite_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_sqlite_source",
        task_id="task_sqlite_source",
        user_goal="query reusable SQLite support database",
    )
    experience["actions"] = [
        {
            "node_id": "node_query",
            "tool_or_skill": "builtin.data.sqlite",
            "input_summary": "dict(keys=['action', 'path', 'sql'], json_chars=120)",
            "output_summary": "dict(keys=['rows'], json_chars=60)",
            "status": "completed",
            "artifact_refs": ["tasks/task_sqlite_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "sqlite_rows",
            "path": "reports/sqlite_summary.json",
            "description": "sqlite query result",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_sqlite_source/completion/evaluation.json"
    ]
    return experience


def _passed_config_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_config_source",
        task_id="task_config_source",
        user_goal="query reusable service config",
    )
    experience["actions"] = [
        {
            "node_id": "node_query",
            "tool_or_skill": "builtin.data.config",
            "input_summary": "dict(keys=['action', 'path', 'query'], json_chars=90)",
            "output_summary": "dict(keys=['value'], json_chars=30)",
            "status": "completed",
            "artifact_refs": ["tasks/task_config_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "config_value",
            "path": "reports/service_config.json",
            "description": "service config field",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_config_source/completion/evaluation.json"
    ]
    return experience


def _passed_markdown_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_markdown_source",
        task_id="task_markdown_source",
        user_goal="extract reusable markdown summary section",
    )
    experience["actions"] = [
        {
            "node_id": "node_extract",
            "tool_or_skill": "builtin.text.markdown",
            "input_summary": "dict(keys=['action', 'path', 'heading'], json_chars=90)",
            "output_summary": "dict(keys=['section'], json_chars=40)",
            "status": "completed",
            "artifact_refs": ["tasks/task_markdown_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "markdown_section",
            "path": "reports/summary_section.md",
            "description": "markdown summary section",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_markdown_source/completion/evaluation.json"
    ]
    return experience


def _passed_text_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_text_source",
        task_id="task_text_source",
        user_goal="extract reusable plain text incident lines",
    )
    experience["actions"] = [
        {
            "node_id": "node_extract",
            "tool_or_skill": "builtin.text.basic",
            "input_summary": "dict(keys=['action', 'path', 'contains'], json_chars=90)",
            "output_summary": "dict(keys=['lines', 'matches'], json_chars=70)",
            "status": "completed",
            "artifact_refs": ["tasks/task_text_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "text_lines",
            "path": "reports/incident_lines.txt",
            "description": "plain text incident lines",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_text_source/completion/evaluation.json"
    ]
    return experience


def _passed_html_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_html_source",
        task_id="task_html_source",
        user_goal="extract reusable HTML page summary",
    )
    experience["actions"] = [
        {
            "node_id": "node_report",
            "tool_or_skill": "builtin.web.html",
            "input_summary": "dict(keys=['action', 'path', 'output_path'], json_chars=110)",
            "output_summary": "dict(keys=['title', 'links', 'output_path'], json_chars=70)",
            "status": "completed",
            "artifact_refs": ["reports/page_summary.md"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "html_report",
            "path": "reports/page_summary.md",
            "description": "HTML page summary report",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_html_source/completion/evaluation.json"
    ]
    return experience


def _passed_archive_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_archive_source",
        task_id="task_archive_source",
        user_goal="create reusable archive bundle",
    )
    experience["actions"] = [
        {
            "node_id": "node_archive",
            "tool_or_skill": "builtin.archive.basic",
            "input_summary": "dict(keys=['action', 'paths', 'output_path'], json_chars=100)",
            "output_summary": "dict(keys=['entries', 'output_path'], json_chars=80)",
            "status": "completed",
            "artifact_refs": ["outputs/bundle.zip"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "archive",
            "path": "outputs/bundle.zip",
            "description": "archive bundle",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_archive_source/completion/evaluation.json"
    ]
    return experience


def _passed_datetime_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_datetime_source",
        task_id="task_datetime_source",
        user_goal="format reusable datetime value",
    )
    experience["actions"] = [
        {
            "node_id": "node_format",
            "tool_or_skill": "builtin.datetime.format",
            "input_summary": "dict(keys=['action', 'value', 'output_format'], json_chars=110)",
            "output_summary": "dict(keys=['value', 'iso'], json_chars=70)",
            "status": "completed",
            "artifact_refs": ["tasks/task_datetime_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "datetime_value",
            "path": "reports/date_value.json",
            "description": "formatted datetime value",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_datetime_source/completion/evaluation.json"
    ]
    return experience


def _passed_git_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_git_source",
        task_id="task_git_source",
        user_goal="inspect reusable git repository status",
    )
    experience["actions"] = [
        {
            "node_id": "node_status",
            "tool_or_skill": "builtin.vcs.git",
            "input_summary": "dict(keys=['action', 'cwd'], json_chars=70)",
            "output_summary": "dict(keys=['stdout', 'returncode'], json_chars=90)",
            "status": "completed",
            "artifact_refs": ["tasks/task_git_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "git_status",
            "path": "reports/git_status.json",
            "description": "read-only git status output",
        }
    ]
    experience["validation"]["evidence_refs"] = ["tasks/task_git_source/completion/evaluation.json"]
    return experience


def _passed_python_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_python_source",
        task_id="task_python_source",
        user_goal="run reusable Python calculation",
    )
    experience["actions"] = [
        {
            "node_id": "node_python",
            "tool_or_skill": "builtin.python.basic",
            "input_summary": "dict(keys=['code'], json_chars=80)",
            "output_summary": "dict(keys=['result', 'stdout'], json_chars=60)",
            "status": "completed",
            "artifact_refs": ["tasks/task_python_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "python_result",
            "path": "reports/python_result.json",
            "description": "python execution result",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_python_source/completion/evaluation.json"
    ]
    return experience


def _passed_http_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_http_source",
        task_id="task_http_source",
        user_goal="fetch reusable HTTP JSON payload",
    )
    experience["actions"] = [
        {
            "node_id": "node_get",
            "tool_or_skill": "builtin.http.client",
            "input_summary": "dict(keys=['action', 'url'], json_chars=80)",
            "output_summary": "dict(keys=['status', 'json'], json_chars=80)",
            "status": "completed",
            "artifact_refs": ["tasks/task_http_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "http_response",
            "path": "reports/http_payload.json",
            "description": "HTTP JSON response",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_http_source/completion/evaluation.json"
    ]
    return experience


def _passed_browser_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_browser_source",
        task_id="task_browser_source",
        user_goal="fetch reusable browser page content",
    )
    experience["actions"] = [
        {
            "node_id": "node_fetch",
            "tool_or_skill": "builtin.browser.basic",
            "input_summary": "dict(keys=['url'], json_chars=70)",
            "output_summary": "dict(keys=['status', 'body'], json_chars=90)",
            "status": "completed",
            "artifact_refs": ["tasks/task_browser_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "html",
            "path": "reports/browser_body.html",
            "description": "browser fetched body",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_browser_source/completion/evaluation.json"
    ]
    return experience


def _passed_shell_experience() -> dict:
    experience = _passed_experience(
        experience_id="exp_shell_source",
        task_id="task_shell_source",
        user_goal="inspect reusable shell command output",
    )
    experience["actions"] = [
        {
            "node_id": "node_wc",
            "tool_or_skill": "builtin.shell.safe",
            "input_summary": "dict(keys=['action', 'argv', 'cwd'], json_chars=90)",
            "output_summary": "dict(keys=['stdout', 'returncode'], json_chars=80)",
            "status": "completed",
            "artifact_refs": ["tasks/task_shell_source/completion/evaluation.json"],
        }
    ]
    experience["artifacts"] = [
        {
            "kind": "shell_output",
            "path": "reports/shell_wc.json",
            "description": "safe shell command output",
        }
    ]
    experience["validation"]["evidence_refs"] = [
        "tasks/task_shell_source/completion/evaluation.json"
    ]
    return experience


class _HttpFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"message": "ok", "path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


class _HttpFixtureServer:
    def __enter__(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpFixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


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

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with (
            patch.dict(os.environ, self.env, clear=False),
            redirect_stdout(stdout),
            redirect_stderr(stdout),
        ):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def _create_file_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_skill_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_json_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_json_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_json_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_jsonl_habit(self, gateway: RuntimeGateway) -> dict:
        habit = self._create_json_habit(gateway)
        habit["steps"][0]["target"] = "jsonl"
        return gateway.procedure_memory.save(habit)

    def _create_csv_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_csv_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_csv_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_tsv_csv_habit(self, gateway: RuntimeGateway) -> dict:
        habit = self._create_csv_habit(gateway)
        habit["steps"][0]["target"] = "tsv"
        return gateway.procedure_memory.save(habit)

    def _create_sqlite_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_sqlite_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_sqlite_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_config_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_config_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_config_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_yaml_config_habit(self, gateway: RuntimeGateway) -> dict:
        habit = self._create_config_habit(gateway)
        habit["steps"][0]["target"] = "yaml"
        return gateway.procedure_memory.save(habit)

    def _create_xml_config_habit(self, gateway: RuntimeGateway) -> dict:
        habit = self._create_config_habit(gateway)
        habit["steps"][0]["target"] = "xml"
        return gateway.procedure_memory.save(habit)

    def _create_dotenv_config_habit(self, gateway: RuntimeGateway) -> dict:
        habit = self._create_config_habit(gateway)
        habit["steps"][0]["target"] = "dotenv"
        return gateway.procedure_memory.save(habit)

    def _create_markdown_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_markdown_experience()
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_markdown_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_text_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_text_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_text_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_html_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_html_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_html_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_archive_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_archive_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_archive_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_tgz_archive_habit(self, gateway: RuntimeGateway) -> dict:
        habit = self._create_archive_habit(gateway)
        habit["steps"][0]["target"] = "tgz"
        return gateway.procedure_memory.save(habit)

    def _create_datetime_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_datetime_experience()
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_datetime_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_git_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_git_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_git_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_python_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_python_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_python_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_http_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_http_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_http_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_browser_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_browser_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_browser_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_shell_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_shell_experience())
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_shell_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(verified["procedure_id"])

    def _create_high_risk_file_habit(self, gateway: RuntimeGateway) -> dict:
        draft = gateway.procedure_memory.extract_draft_from_experience(
            _passed_experience(
                experience_id="exp_delete_source",
                task_id="task_delete_source",
                user_goal="delete cache file",
            )
        )
        assert draft is not None
        verified = gateway.procedure_memory.promote_to_verified(
            draft["procedure_id"],
            evidence_refs=["tasks/task_delete_source/completion/evaluation.json"],
        )
        verified["success_count"] = 3
        gateway.procedure_memory.save(verified)
        return gateway.procedure_memory.promote_to_habit(
            verified["procedure_id"],
            require_confirmation=True,
        )

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

    def test_habit_promotes_to_active_skill_with_lineage_and_contract(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_file_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.file_report",
            rationale="stable file habit should become a reusable skill",
        )

        self.assertEqual(promotion["draft"].source_kind, "habit_promoted")
        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        assert promotion["registration_record"] is not None
        self.assertEqual(promotion["registration_record"].status, "active")
        lineage_path = Path(promotion["lineage_path"])
        self.assertTrue(lineage_path.exists())
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
        self.assertEqual(lineage["habit_id"], habit["procedure_id"])
        self.assertEqual(lineage["source_experience_ids"], ["exp_skill_source"])
        self.assertIn(
            "tasks/task_skill_source/completion/evaluation.json",
            lineage["validator_evidence_refs"],
        )

        skill_payload = gateway.show_skill("generated.habit.file_report")
        self.assertEqual(skill_payload["registry_entry"].source, "habit_promotion")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "active")
        contract_actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("write", contract_actions)
        self.assertIn("replace_line", contract_actions)
        self.assertIn("copy", contract_actions)
        self.assertEqual(
            skill_payload["lineage_records"][0]["habit_id"],
            habit["procedure_id"],
        )

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 habit promoted skill",
                skill_name="generated.habit.file_report",
                payload={
                    "action": "write",
                    "path": "reports/promoted.txt",
                    "content": "promoted ok",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.file_report")

        source_path = self.workspace / "reports" / "source.txt"
        source_path.write_text("copy ok", encoding="utf-8")
        copy_report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 habit promoted skill copy",
                skill_name="generated.habit.file_report",
                payload={
                    "action": "copy",
                    "source": "reports/source.txt",
                    "target": "reports/copied.txt",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(copy_report.result.success, copy_report.result.message)
        copy_payload = runtime_gateway.show_task(copy_report.task.task_id)
        copy_node = next(
            node for node in copy_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(copy_node.metadata.get("selected_skill"), "generated.habit.file_report")
        self.assertEqual(copy_node.output["action"], "copy")
        self.assertEqual(
            (self.workspace / "reports" / "copied.txt").read_text(encoding="utf-8"),
            "copy ok",
        )

        line_path = self.workspace / "reports" / "line_target.txt"
        line_path.write_text("status: old\nowner: omniagent\n", encoding="utf-8")
        replace_report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 habit promoted skill replace line",
                skill_name="generated.habit.file_report",
                payload={
                    "action": "replace_line",
                    "path": "reports/line_target.txt",
                    "line_prefix": "status:",
                    "content": "status: new",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(replace_report.result.success, replace_report.result.message)
        replace_payload = runtime_gateway.show_task(replace_report.task.task_id)
        replace_node = next(
            node for node in replace_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            replace_node.metadata.get("selected_skill"),
            "generated.habit.file_report",
        )
        self.assertEqual(replace_node.output["line_index"], 0)
        self.assertEqual(
            line_path.read_text(encoding="utf-8"),
            "status: new\nowner: omniagent\n",
        )

    def test_habit_skill_promotion_rejects_non_habit_source(self) -> None:
        gateway = self._build_gateway()
        draft = gateway.procedure_memory.extract_draft_from_experience(_passed_experience())
        assert draft is not None

        with self.assertRaisesRegex(Exception, "stable habit"):
            gateway.promote_habit_to_skill(draft["procedure_id"])

    def test_json_habit_promotes_to_active_skill_and_executes_query(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_json_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.json_profile",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "json")
        skill_payload = gateway.show_skill("generated.habit.json_profile")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("query", actions)
        self.assertIn("report", actions)

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 JSON habit promoted skill",
                skill_name="generated.habit.json_profile",
                payload={
                    "action": "query",
                    "text": '{"user": {"name": "Ada"}}',
                    "query": "user.name",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.json_profile")
        self.assertEqual(action_node.output["value"], "Ada")

    def test_jsonl_habit_promotes_as_json_skill_and_executes_line_query(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_jsonl_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.jsonl_events",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "json")
        self.assertEqual(promotion["lineage"]["source_targets"], ["jsonl"])
        skill_payload = gateway.show_skill("generated.habit.jsonl_events")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("query_lines", actions)
        self.assertIn("report_lines", actions)

        (self.workspace / "events.jsonl").write_text(
            '{"user": {"name": "Ada"}}\n{"user": {"name": "Grace"}}\n',
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 JSONL habit promoted skill",
                skill_name="generated.habit.jsonl_events",
                payload={
                    "action": "query_lines",
                    "path": "events.jsonl",
                    "query": "user.name",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.jsonl_events",
        )
        self.assertEqual(action_node.output["value"], ["Ada", "Grace"])
        self.assertEqual(action_node.output["line_count"], 2)

        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 JSONL habit promoted skill 写报告",
                skill_name="generated.habit.jsonl_events",
                payload={
                    "action": "report_lines",
                    "path": "events.jsonl",
                    "fields": [{"label": "names", "query": "user.name"}],
                    "output_path": "reports/jsonl_names.md",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        self.assertEqual(
            (self.workspace / "reports" / "jsonl_names.md").read_text(encoding="utf-8"),
            "names: Ada, Grace\n",
        )

    def test_csv_habit_promotes_to_active_skill_and_executes_aggregate(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_csv_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.csv_sales",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "csv")
        skill_payload = gateway.show_skill("generated.habit.csv_sales")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("aggregate", actions)
        self.assertIn("report", actions)

        (self.workspace / "sales.csv").write_text(
            "region,amount\nA,10\nB,15\n",
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 CSV habit promoted skill",
                skill_name="generated.habit.csv_sales",
                payload={
                    "action": "aggregate",
                    "path": "sales.csv",
                    "aggregate": {"op": "sum", "column": "amount"},
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.csv_sales")
        self.assertEqual(action_node.output["result"]["value"], 25.0)

    def test_tsv_habit_promotes_as_csv_skill_and_executes_tab_aggregate(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_tsv_csv_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.tsv_sales",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "csv")
        self.assertEqual(promotion["lineage"]["source_targets"], ["tsv"])

        (self.workspace / "sales.tsv").write_text(
            "region\tamount\nA\t10\nB\t15\n",
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 TSV habit promoted skill",
                skill_name="generated.habit.tsv_sales",
                payload={
                    "action": "aggregate",
                    "path": "sales.tsv",
                    "delimiter": "\t",
                    "aggregate": {"op": "sum", "column": "amount"},
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.tsv_sales")
        self.assertEqual(action_node.output["result"]["value"], 25.0)

    def test_sqlite_habit_promotes_to_active_skill_and_executes_query(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_sqlite_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.sqlite_support",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "sqlite")
        skill_payload = gateway.show_skill("generated.habit.sqlite_support")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("query", actions)
        self.assertIn("export_csv", actions)
        self.assertIn("report", actions)

        db_path = self.workspace / "support.sqlite3"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("create table tickets(team text, points integer)")
            conn.executemany(
                "insert into tickets(team, points) values (?, ?)",
                [("alpha", 5), ("alpha", 7), ("beta", 3)],
            )
            conn.commit()
        finally:
            conn.close()

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 SQLite habit promoted skill",
                skill_name="generated.habit.sqlite_support",
                payload={
                    "action": "query",
                    "path": "support.sqlite3",
                    "sql": "select sum(points) as total_points from tickets",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.sqlite_support",
        )
        self.assertEqual(action_node.output["rows"][0]["total_points"], 15)

    def test_config_habit_promotes_to_active_skill_and_executes_query(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_yaml_config_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.config_service",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "config")
        skill_payload = gateway.show_skill("generated.habit.config_service")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("query", actions)
        self.assertIn("report", actions)

        (self.workspace / "service.yaml").write_text(
            "service:\n  name: billing\n  retries: 3\n",
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 config habit promoted skill",
                skill_name="generated.habit.config_service",
                payload={
                    "action": "query",
                    "path": "service.yaml",
                    "query": "service.name",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.config_service",
        )
        self.assertEqual(action_node.output["value"], "billing")
        self.assertEqual(action_node.output["format"], "yaml")

    def test_xml_config_habit_promotes_to_active_skill_and_executes_query(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_xml_config_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.config_xml",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "config")
        skill_payload = gateway.show_skill("generated.habit.config_xml")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("query", actions)
        self.assertIn("report", actions)

        (self.workspace / "service.xml").write_text(
            '<service><name>billing</name><owner team="platform">Maya</owner></service>',
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 XML config habit promoted skill",
                skill_name="generated.habit.config_xml",
                payload={
                    "action": "query",
                    "path": "service.xml",
                    "query": "service.owner.@team",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.config_xml",
        )
        self.assertEqual(action_node.output["value"], "platform")
        self.assertEqual(action_node.output["format"], "xml")

    def test_dotenv_config_habit_promotes_to_active_skill_and_executes_query(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_dotenv_config_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.config_env",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "config")
        skill_payload = gateway.show_skill("generated.habit.config_env")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("query", actions)
        self.assertIn("report", actions)

        (self.workspace / "service.env").write_text(
            "SERVICE_NAME=billing\nRETRIES=3\n",
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 dotenv config habit promoted skill",
                skill_name="generated.habit.config_env",
                payload={
                    "action": "query",
                    "path": "service.env",
                    "query": "SERVICE_NAME",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.config_env",
        )
        self.assertEqual(action_node.output["value"], "billing")
        self.assertEqual(action_node.output["format"], "keyvalue")

        (self.workspace / "service.ini").write_text(
            "[service]\nname=billing\nretries=3\n",
            encoding="utf-8",
        )
        ini_report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 INI config habit promoted skill",
                skill_name="generated.habit.config_env",
                payload={
                    "action": "query",
                    "path": "service.ini",
                    "query": "service.name",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(ini_report.result.success, ini_report.result.message)
        ini_payload = runtime_gateway.show_task(ini_report.task.task_id)
        ini_action = next(
            node for node in ini_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(ini_action.output["value"], "billing")
        self.assertEqual(ini_action.output["format"], "keyvalue")

        (self.workspace / "service.properties").write_text(
            "service.name=billing\nservice.retries=3\n",
            encoding="utf-8",
        )
        properties_report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 properties config habit promoted skill",
                skill_name="generated.habit.config_env",
                payload={
                    "action": "query",
                    "path": "service.properties",
                    "query": "service.name",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(properties_report.result.success, properties_report.result.message)
        properties_payload = runtime_gateway.show_task(properties_report.task.task_id)
        properties_action = next(
            node for node in properties_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(properties_action.output["value"], "billing")
        self.assertEqual(properties_action.output["format"], "keyvalue")

    def test_markdown_habit_promotes_to_active_skill_and_executes_section_extract(
        self,
    ) -> None:
        gateway = self._build_gateway()
        habit = self._create_markdown_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.markdown_report",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "markdown")
        skill_payload = gateway.show_skill("generated.habit.markdown_report")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("extract_section", actions)
        self.assertIn("replace_section", actions)
        self.assertIn("table", actions)

        (self.workspace / "report.md").write_text(
            "# Report\n\n## Summary\nReady for reuse.\n\n## Details\nMore context.\n",
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 markdown habit promoted skill",
                skill_name="generated.habit.markdown_report",
                payload={
                    "action": "extract_section",
                    "path": "report.md",
                    "heading": "Summary",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.markdown_report",
        )
        self.assertEqual(action_node.output["section"], "Ready for reuse.")

    def test_text_habit_promotes_to_active_skill_and_executes_line_extract(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_text_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.text_incidents",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "text")
        skill_payload = gateway.show_skill("generated.habit.text_incidents")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("stats", actions)
        self.assertIn("extract_lines", actions)
        self.assertIn("replace", actions)
        self.assertIn("report", actions)

        (self.workspace / "incidents.log").write_text(
            "INFO booted\nERROR failed\nwarning retry\nerror recovered\n",
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 text habit promoted skill",
                skill_name="generated.habit.text_incidents",
                payload={
                    "action": "extract_lines",
                    "path": "incidents.log",
                    "contains": "error",
                    "ignore_case": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.text_incidents",
        )
        self.assertEqual(action_node.output["lines"], ["ERROR failed", "error recovered"])

    def test_html_habit_promotes_to_active_skill_and_executes_report(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_html_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.html_page",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "html")
        skill_payload = gateway.show_skill("generated.habit.html_page")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("extract", actions)
        self.assertIn("links", actions)
        self.assertIn("report", actions)

        inputs_dir = self.workspace / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        (inputs_dir / "page.html").write_text(
            "<html><head><title>Service Status</title></head>"
            '<body><h1>OK</h1><a href="/docs">Docs</a></body></html>',
            encoding="utf-8",
        )
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 HTML habit promoted skill",
                skill_name="generated.habit.html_page",
                payload={
                    "action": "report",
                    "path": "inputs/page.html",
                    "output_path": "reports/page_summary.md",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.html_page",
        )
        self.assertEqual(action_node.output["title"], "Service Status")
        report_text = (self.workspace / "reports" / "page_summary.md").read_text(encoding="utf-8")
        self.assertIn("title: Service Status", report_text)
        self.assertIn("link_count: 1", report_text)

    def test_archive_habit_promotes_to_active_skill_and_executes_create(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_tgz_archive_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.archive_bundle",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "archive")
        skill_payload = gateway.show_skill("generated.habit.archive_bundle")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("list", actions)
        self.assertIn("extract", actions)
        self.assertIn("create", actions)

        inputs_dir = self.workspace / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        (inputs_dir / "a.txt").write_text("alpha\n", encoding="utf-8")
        (inputs_dir / "b.txt").write_text("beta\n", encoding="utf-8")
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 archive habit promoted skill",
                skill_name="generated.habit.archive_bundle",
                payload={
                    "action": "create",
                    "paths": ["inputs"],
                    "output_path": "outputs/bundle.tar.gz",
                    "format": "gztar",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.archive_bundle",
        )
        output_path = self.workspace / "outputs" / "bundle.tar.gz"
        self.assertTrue(output_path.exists())
        with tarfile.open(output_path, "r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {"inputs/a.txt", "inputs/b.txt"})

    def test_datetime_habit_promotes_to_active_skill_and_executes_format(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_datetime_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.datetime_format",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNotNone(promotion["approval_record"])
        self.assertIsNotNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "datetime")
        skill_payload = gateway.show_skill("generated.habit.datetime_format")
        actions = [item.action for item in skill_payload["registry_entry"].contracts]
        self.assertIn("now", actions)
        self.assertIn("parse", actions)
        self.assertIn("format", actions)
        self.assertIn("diff", actions)
        self.assertIn("add", actions)
        self.assertEqual(skill_payload["registry_entry"].permission_requirements, [])

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 datetime habit promoted skill",
                skill_name="generated.habit.datetime_format",
                payload={
                    "action": "format",
                    "value": "2026-06-12T08:30:00+00:00",
                    "timezone": "UTC",
                    "output_format": "%Y-%m-%d",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.datetime_format",
        )
        self.assertEqual(action_node.output["value"], "2026-06-12")

    def test_git_habit_promotes_to_validated_skill_and_requires_manual_approval(
        self,
    ) -> None:
        gateway = self._build_gateway()
        habit = self._create_git_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.git_status",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNone(promotion["approval_record"])
        self.assertIsNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "git")
        manifest = json.loads(Path(promotion["draft"].manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["permission_requirements"], ["system.exec"])
        self.assertEqual(manifest["dependency_requirements"], ["git"])
        self.assertEqual(manifest["side_effects"], ["process_execution"])
        self.assertEqual(manifest["sandbox_level"], "process")
        self.assertEqual(
            {item["action"] for item in manifest["contracts"]},
            {"status", "diff", "log", "show"},
        )
        skill_payload = gateway.show_skill("generated.habit.git_status")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")

        approval = gateway.approve_skill(
            "generated.habit.git_status",
            approver="tester",
            rationale="manual approval for process-backed read-only git skill",
        )
        self.assertEqual(approval["approval_record"].status, "approved")
        activation = gateway.activate_skill("generated.habit.git_status")
        self.assertEqual(activation["registration_record"].status, "active")

        repo = self.workspace / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        (repo / "tracked.txt").write_text("untracked\n", encoding="utf-8")

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 Git habit promoted skill",
                skill_name="generated.habit.git_status",
                payload={"action": "status", "cwd": "repo"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L4),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.git_status")
        self.assertIn("?? tracked.txt", action_node.output["stdout"])

    def test_python_habit_promotes_to_validated_skill_and_requires_manual_approval(
        self,
    ) -> None:
        gateway = self._build_gateway()
        habit = self._create_python_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.python_run",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNone(promotion["approval_record"])
        self.assertIsNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "python")
        manifest = json.loads(Path(promotion["draft"].manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["permission_requirements"], ["code_execution"])
        self.assertEqual(manifest["side_effects"], ["python_execution"])
        self.assertEqual(manifest["sandbox_level"], "workspace")
        self.assertEqual({item["action"] for item in manifest["contracts"]}, {"run"})
        skill_payload = gateway.show_skill("generated.habit.python_run")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")

        approval = gateway.approve_skill(
            "generated.habit.python_run",
            approver="tester",
            rationale="manual approval for code-execution-backed Python skill",
        )
        self.assertEqual(approval["approval_record"].status, "approved")
        activation = gateway.activate_skill("generated.habit.python_run")
        self.assertEqual(activation["registration_record"].status, "active")

        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 Python habit promoted skill",
                skill_name="generated.habit.python_run",
                payload={
                    "action": "run",
                    "code": "print('hello')\nresult = 6 * 7",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L3),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.python_run")
        self.assertEqual(action_node.output["result"], 42)
        self.assertIn("hello", action_node.output["stdout"])

    def test_http_habit_promotes_to_validated_skill_and_requires_manual_approval(
        self,
    ) -> None:
        gateway = self._build_gateway()
        habit = self._create_http_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.http_fetch",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNone(promotion["approval_record"])
        self.assertIsNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "http")
        manifest = json.loads(Path(promotion["draft"].manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["permission_requirements"], ["network"])
        self.assertEqual(manifest["side_effects"], ["network_read", "network_write"])
        self.assertEqual(manifest["sandbox_level"], "workspace")
        self.assertEqual(
            {item["action"] for item in manifest["contracts"]},
            {"get", "post", "put", "delete"},
        )
        skill_payload = gateway.show_skill("generated.habit.http_fetch")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")

        approval = gateway.approve_skill(
            "generated.habit.http_fetch",
            approver="tester",
            rationale="manual approval for network-backed HTTP skill",
        )
        self.assertEqual(approval["approval_record"].status, "approved")
        activation = gateway.activate_skill("generated.habit.http_fetch")
        self.assertEqual(activation["registration_record"].status, "active")

        with _HttpFixtureServer() as url:
            runtime_gateway = self._build_gateway()
            report = runtime_gateway.submit(
                TaskRequest(
                    goal="运行 HTTP habit promoted skill",
                    skill_name="generated.habit.http_fetch",
                    payload={
                        "action": "get",
                        "url": f"{url}/payload",
                        "params": {"q": "demo"},
                    },
                    constraints=TaskConstraints(
                        allow_network=True,
                        max_permission=PermissionLevel.L2,
                    ),
                )
            )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.http_fetch")
        self.assertEqual(action_node.output["status"], 200)
        self.assertEqual(action_node.output["json"]["message"], "ok")
        self.assertIn("q=demo", action_node.output["request_url"])

    def test_browser_habit_promotes_to_validated_skill_and_requires_manual_approval(
        self,
    ) -> None:
        gateway = self._build_gateway()
        habit = self._create_browser_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.browser_fetch",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNone(promotion["approval_record"])
        self.assertIsNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "browser")
        manifest = json.loads(Path(promotion["draft"].manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["permission_requirements"], ["network"])
        self.assertEqual(manifest["side_effects"], ["network_read"])
        self.assertEqual(manifest["sandbox_level"], "workspace")
        self.assertEqual({item["action"] for item in manifest["contracts"]}, {"fetch"})
        skill_payload = gateway.show_skill("generated.habit.browser_fetch")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")

        approval = gateway.approve_skill(
            "generated.habit.browser_fetch",
            approver="tester",
            rationale="manual approval for network-backed browser fetch skill",
        )
        self.assertEqual(approval["approval_record"].status, "approved")
        activation = gateway.activate_skill("generated.habit.browser_fetch")
        self.assertEqual(activation["registration_record"].status, "active")

        with _HttpFixtureServer() as url:
            runtime_gateway = self._build_gateway()
            report = runtime_gateway.submit(
                TaskRequest(
                    goal="运行 browser habit promoted skill",
                    skill_name="generated.habit.browser_fetch",
                    payload={
                        "action": "fetch",
                        "url": f"{url}/page",
                        "max_body_chars": 100,
                    },
                    constraints=TaskConstraints(
                        allow_network=True,
                        max_permission=PermissionLevel.L2,
                    ),
                )
            )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(
            action_node.metadata.get("selected_skill"),
            "generated.habit.browser_fetch",
        )
        self.assertEqual(action_node.output["status"], 200)
        self.assertIn('"message": "ok"', action_node.output["body"])
        self.assertFalse(action_node.output["blocked"])

    def test_shell_habit_promotes_to_validated_skill_and_requires_manual_approval(
        self,
    ) -> None:
        gateway = self._build_gateway()
        habit = self._create_shell_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.shell_wc",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNone(promotion["approval_record"])
        self.assertIsNone(promotion["registration_record"])
        self.assertEqual(promotion["lineage"]["generation_type"], "shell")
        manifest = json.loads(Path(promotion["draft"].manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["permission_requirements"], ["system.exec"])
        self.assertEqual(manifest["side_effects"], ["process_execution"])
        self.assertEqual(manifest["sandbox_level"], "process")
        self.assertEqual({item["action"] for item in manifest["contracts"]}, {"run"})
        skill_payload = gateway.show_skill("generated.habit.shell_wc")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")

        approval = gateway.approve_skill(
            "generated.habit.shell_wc",
            approver="tester",
            rationale="manual approval for process-backed safe shell skill",
        )
        self.assertEqual(approval["approval_record"].status, "approved")
        activation = gateway.activate_skill("generated.habit.shell_wc")
        self.assertEqual(activation["registration_record"].status, "active")

        (self.workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        runtime_gateway = self._build_gateway()
        report = runtime_gateway.submit(
            TaskRequest(
                goal="运行 shell habit promoted skill",
                skill_name="generated.habit.shell_wc",
                payload={
                    "action": "run",
                    "argv": ["wc", "-l", "notes.txt"],
                    "cwd": ".",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L4),
            )
        )
        self.assertTrue(report.result.success, report.result.message)
        task_payload = runtime_gateway.show_task(report.task.task_id)
        action_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.metadata.get("selected_skill"), "generated.habit.shell_wc")
        self.assertIn("2", action_node.output["stdout"])

    def test_high_risk_habit_promotion_requires_manual_approval_before_activation(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_high_risk_file_habit(gateway)

        promotion = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.high_risk_file",
        )

        self.assertTrue(promotion["validation_report"].passed)
        self.assertIsNone(promotion["approval_record"])
        self.assertIsNone(promotion["registration_record"])
        skill_payload = gateway.show_skill("generated.habit.high_risk_file")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")

        approval = gateway.approve_skill(
            "generated.habit.high_risk_file",
            approver="tester",
            rationale="manual approval for high risk source habit",
        )
        self.assertEqual(approval["approval_record"].status, "approved")
        activation = gateway.activate_skill("generated.habit.high_risk_file")
        self.assertEqual(activation["registration_record"].status, "active")

    def test_repeated_habit_promotion_upgrades_skill_version(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_file_habit(gateway)
        first = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.file_report",
        )

        second = gateway.promote_habit_to_skill(
            habit["procedure_id"],
            skill_name="generated.habit.file_report",
        )

        self.assertNotEqual(first["version"], second["version"])
        self.assertIsNotNone(second["registration_record"])
        assert second["registration_record"] is not None
        self.assertEqual(second["registration_record"].action, "upgrade")

    def test_cli_promotes_habit_to_skill(self) -> None:
        gateway = self._build_gateway()
        habit = self._create_file_habit(gateway)

        exit_code, output = self._run_cli(
            [
                "skill",
                "promote-habit",
                habit["procedure_id"],
                "--skill-name",
                "generated.habit.cli_report",
                "--json",
            ]
        )

        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["skill_name"], "generated.habit.cli_report")
        self.assertEqual(payload["lineage"]["habit_id"], habit["procedure_id"])
        self.assertEqual(payload["registration_record"]["status"], "active")

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

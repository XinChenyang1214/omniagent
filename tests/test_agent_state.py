from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.agent_state import AgentStateStore, AgentStateValidationError
from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


class AgentStateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_root = Path(self.tempdir.name)
        self.workspace = self.project_root / "workspace"
        self.store = AgentStateStore(self.workspace)

    def test_initializes_default_state_files(self) -> None:
        snapshot = self.store.ensure_defaults()

        self.assertEqual(snapshot.profile["schema_version"], "agent-state/v1")
        self.assertEqual(snapshot.preferences["schema_version"], "agent-state/v1")
        self.assertEqual(snapshot.project["project_root"], str(self.project_root.resolve()))
        self.assertIn(snapshot.goal["status"], {"active", "paused"})
        for filename in (
            "agent_profile.json",
            "user_preferences.json",
            "project_state.json",
            "current_goal.json",
        ):
            self.assertTrue((self.workspace / "agent_state" / filename).exists())

    def test_current_document_layout_initializes_without_active_workstream(self) -> None:
        current_docs = (
            "README.md",
            "ROADMAP.md",
            "issue.md",
            "docs/doc-registry.md",
            "docs/product-architecture.md",
            "docs/specs/README.md",
            "docs/specs/current-system.md",
            "docs/specs/task-intake.md",
            "docs/specs/agent-state.md",
        )
        for relative_path in current_docs:
            path = self.project_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        (self.project_root / "docs" / "archive").mkdir(parents=True)

        snapshot = self.store.ensure_defaults()

        self.assertEqual(snapshot.project["current_workstream"], "none")
        self.assertIn(
            "docs/specs/current-system.md",
            snapshot.project["authoritative_docs"],
        )
        self.assertNotIn(
            "docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md",
            snapshot.project["authoritative_docs"],
        )
        self.assertEqual(snapshot.project["archived_docs"], ["docs/archive"])
        self.assertEqual(
            snapshot.goal["active_phase"],
            "General Agent product validation",
        )

    def test_summary_excludes_disabled_preferences(self) -> None:
        self.store.write_section(
            "preferences",
            {
                "preferences": [
                    {
                        "id": "pref_active",
                        "scope": "project",
                        "statement": "Prefer concise Chinese updates.",
                        "source": "explicit_user",
                        "confidence": 1.0,
                        "evidence_refs": ["AGENTS.md"],
                    },
                    {
                        "id": "pref_disabled",
                        "scope": "project",
                        "statement": "This preference should not enter context.",
                        "source": "observed",
                        "confidence": 0.7,
                        "evidence_refs": ["workspace/tasks/task_old"],
                    },
                ],
                "disabled_preferences": ["pref_disabled"],
            },
        )

        summary = self.store.build_summary(task_goal="test task")

        statements = [item["statement"] for item in summary["active_preferences"]]
        self.assertIn("Prefer concise Chinese updates.", statements)
        self.assertNotIn("This preference should not enter context.", statements)
        self.assertEqual(summary["disabled_preferences"], ["pref_disabled"])

    def test_rejects_sensitive_state_values(self) -> None:
        with self.assertRaises(AgentStateValidationError):
            self.store.write_section(
                "preferences",
                {
                    "preferences": [
                        {
                            "id": "leaky",
                            "scope": "global",
                            "statement": "Store a credential",
                            "source": "explicit_user",
                            "confidence": 1.0,
                            "evidence_refs": [],
                            "api_key": "placeholder",
                        }
                    ]
                },
            )

    def test_updates_current_goal(self) -> None:
        updated = self.store.update_current_goal(
            title="Implement Agent state",
            status="active",
            active_phase="Phase 1: Agent state",
            next_actions=["Run tests"],
        )

        self.assertEqual(updated["title"], "Implement Agent state")
        self.assertEqual(self.store.load_section("goal")["active_phase"], "Phase 1: Agent state")


class AgentStateRuntimeIntegrationTest(unittest.TestCase):
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

    def test_cli_state_show_reads_workspace_state(self) -> None:
        gateway = self._build_gateway()
        gateway.update_current_goal(
            title="CLI visible goal",
            status="active",
            active_phase="Phase 1: Agent state",
        )

        stdout = StringIO()
        with patch.dict(os.environ, self.env, clear=False), redirect_stdout(stdout):
            exit_code = main(
                [
                    "--workspace",
                    str(self.workspace),
                    "state",
                    "show",
                    "--section",
                    "goal",
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["title"], "CLI visible goal")
        self.assertEqual(payload["status"], "active")

    def test_runtime_context_snapshot_contains_agent_state_summary(self) -> None:
        gateway = self._build_gateway()
        gateway.agent_state.write_section(
            "preferences",
            {
                "preferences": [
                    {
                        "id": "pref_context",
                        "scope": "project",
                        "statement": "Prefer evidence-backed changes.",
                        "source": "explicit_user",
                        "confidence": 1.0,
                        "evidence_refs": ["AGENTS.md"],
                    },
                    {
                        "id": "pref_disabled",
                        "scope": "project",
                        "statement": "Disabled preference must stay out.",
                        "source": "observed",
                        "confidence": 0.8,
                        "evidence_refs": ["workspace/tasks/task_old"],
                    },
                ],
                "disabled_preferences": ["pref_disabled"],
            },
        )
        gateway.update_current_goal(
            title="Runtime visible goal",
            status="active",
            active_phase="Phase 1: Agent state",
            definition_of_done=["Context snapshot contains state summary"],
        )

        request = TaskRequest(
            goal="Write a small state report",
            executor="file",
            payload={
                "action": "write",
                "path": "reports/state.txt",
                "content": "ok",
                "overwrite": True,
            },
            constraints=TaskConstraints(max_permission=PermissionLevel.L2),
        )
        report = gateway.submit(request)
        self.assertTrue(report.result.success, report.result.message)

        task_dir = self.workspace / "tasks" / report.task.task_id
        context_snapshot = json.loads(
            (task_dir / "context_snapshot.json").read_text(encoding="utf-8")
        )
        summary = context_snapshot["agent_state_summary"]
        self.assertEqual(summary["current_goal"]["title"], "Runtime visible goal")
        self.assertEqual(summary["task_goal"], "Write a small state report")
        statements = [item["statement"] for item in summary["active_preferences"]]
        self.assertIn("Prefer evidence-backed changes.", statements)
        self.assertNotIn("Disabled preference must stay out.", statements)
        self.assertIn("AGENT_STATE.md", context_snapshot["bootstrap_documents"])

        policy = json.loads(
            (task_dir / "plan" / "policy_decision.json").read_text(encoding="utf-8")
        )
        self.assertIn("High-risk actions", policy["agent_state_summary"]["risk_policy_summary"])


if __name__ == "__main__":
    unittest.main()

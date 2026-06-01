from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omniagent.execution_policy import ExecutionPolicy
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore


class ExecutionPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        workspace = Path(self.tempdir.name) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        loader = SkillLoader(workspace)
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(loader.load_all())
        self.entries = [entry.to_dict() for entry in registry.list_entries(active_only=True)]
        self.policy = ExecutionPolicy()

    def test_explicit_file_payload_can_direct_execute(self) -> None:
        decision = self.policy.decide(
            TaskRequest(
                goal="Create reports/policy.md with status: ok.",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/policy.md",
                    "content": "status: ok\n",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=self.entries,
        )

        self.assertEqual(decision.policy_decision, "direct_execute")
        self.assertEqual(decision.skipped_stages, ["planner"])
        self.assertEqual(decision.target_skill, "builtin.file.basic")

    def test_ambiguous_context_reference_requires_planner(self) -> None:
        decision = self.policy.decide(
            TaskRequest(
                goal="Append a line to that file.",
                executor="file",
                payload={
                    "action": "append",
                    "path": "reports/policy.md",
                    "content": "owner: qa\n",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=self.entries,
        )

        self.assertEqual(decision.policy_decision, "plan_required")
        self.assertTrue(decision.risk_flags["ambiguous_reference"])

    def test_network_skill_never_direct_executes(self) -> None:
        decision = self.policy.decide(
            TaskRequest(
                goal="Fetch https://example.com",
                skill_name="builtin.http.client",
                payload={"action": "get", "url": "https://example.com"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=self.entries,
        )

        self.assertEqual(decision.policy_decision, "plan_required")
        self.assertTrue(decision.risk_flags["network"])

    def test_planner_context_requirement_requires_planner(self) -> None:
        decision = self.policy.decide(
            TaskRequest(
                goal="Write reports/context.md using the provided payload.",
                expected_output="reports/context.md exists and Planner context is auditable.",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/context.md",
                    "content": "context: ok\n",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=self.entries,
        )

        self.assertEqual(decision.policy_decision, "plan_required")
        self.assertTrue(decision.risk_flags["planner_context_required"])


if __name__ == "__main__":
    unittest.main()

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

    def _generated_habit_entry(
        self,
        *,
        skill_name: str = "generated.habit.policy_report",
        permissions: list[str] | None = None,
        lifecycle_status: str = "active",
        side_effects: list[str] | None = None,
    ) -> dict:
        return {
            "skill_name": skill_name,
            "source": "workspace",
            "version": "0.0.1",
            "description": "Generated habit policy test skill",
            "permission_requirements": permissions or ["filesystem.write"],
            "tags": ["generated", "habit-promoted", "jsonl"],
            "location": "/tmp/generated.habit.policy_report",
            "scope": "workspace",
            "sandbox_level": "workspace",
            "lifecycle_status": lifecycle_status,
            "contracts": [
                {
                    "action": "report_lines",
                    "description": "Write a JSONL report",
                    "input_fields": [
                        {"name": "action", "type": "string", "required": True},
                        {"name": "path", "type": "string", "required": True},
                        {"name": "output_path", "type": "string", "required": True},
                    ],
                    "output_fields": [{"name": "path", "type": "string"}],
                    "side_effects": side_effects or ["filesystem.write"],
                }
            ],
        }

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

    def test_low_risk_generated_habit_skill_can_direct_execute(self) -> None:
        entry = self._generated_habit_entry()
        decision = self.policy.decide(
            TaskRequest(
                goal="Use the generated habit skill to write a JSONL report.",
                skill_name="generated.habit.policy_report",
                payload={
                    "action": "report_lines",
                    "path": "inputs/events.jsonl",
                    "output_path": "reports/events.md",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[*self.entries, entry],
        )

        self.assertEqual(decision.policy_decision, "direct_execute")
        self.assertEqual(decision.skipped_stages, ["planner"])
        self.assertEqual(decision.target_skill, "generated.habit.policy_report")

    def test_generated_habit_skill_still_respects_permission_gate(self) -> None:
        entry = self._generated_habit_entry(permissions=["network"], side_effects=["network_read"])
        decision = self.policy.decide(
            TaskRequest(
                goal="Use the generated habit skill to fetch data.",
                skill_name="generated.habit.policy_report",
                payload={
                    "action": "report_lines",
                    "path": "inputs/events.jsonl",
                    "output_path": "reports/events.md",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[*self.entries, entry],
        )

        self.assertEqual(decision.policy_decision, "plan_required")
        self.assertIn("network", decision.reason)

    def test_inactive_generated_habit_skill_requires_planner(self) -> None:
        entry = self._generated_habit_entry(lifecycle_status="validated")
        decision = self.policy.decide(
            TaskRequest(
                goal="Use the generated habit skill to write a JSONL report.",
                skill_name="generated.habit.policy_report",
                payload={
                    "action": "report_lines",
                    "path": "inputs/events.jsonl",
                    "output_path": "reports/events.md",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=[*self.entries, entry],
        )

        self.assertEqual(decision.policy_decision, "plan_required")
        self.assertIn("active generated habit skill", decision.reason)

    def test_habit_candidate_does_not_bypass_static_risk_gate(self) -> None:
        decision = self.policy.decide(
            TaskRequest(
                goal="Delete reports/policy.md using a known habit.",
                executor="file",
                payload={
                    "action": "delete",
                    "path": "reports/policy.md",
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            skill_registry_entries=self.entries,
            habit_fast_path={
                "decision": "habit_fast_path_candidate",
                "habit_id": "proc_habit",
                "skipped_stages": ["planner"],
            },
        )

        self.assertEqual(decision.policy_decision, "plan_required")
        self.assertTrue(decision.risk_flags["delete"])
        self.assertEqual(
            decision.habit_fast_path["policy_gate"]["result"],
            "rejected_by_policy",
        )
        self.assertEqual(decision.habit_fast_path["skipped_stages"], [])

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

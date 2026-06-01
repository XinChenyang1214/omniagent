from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omniagent.graph_memory import GraphMemoryStore
from omniagent.models import (
    SkillContract,
    SkillContractField,
    SkillRegistryEntry,
    Task,
    TaskConstraints,
    TaskNode,
    TaskNodeType,
    TaskRequest,
    TaskStatus,
)
from omniagent.planner import Planner
from omniagent.skill_registry import (
    SkillRegistry,
    build_planner_skill_context,
    filter_skill_registry_entries_for_planner,
)
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore


class PlannerSkillManifestFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.loader = SkillLoader(self.workspace)
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(self.loader.load_all())
        self.entries = [entry.to_dict() for entry in registry.list_entries(active_only=True)]

    def test_http_request_receives_bounded_relevant_skill_manifests(self) -> None:
        request = TaskRequest(
            goal="调用 HTTP API 获取 JSON 数据",
            payload={"action": "get", "url": "https://example.test/items"},
            constraints=TaskConstraints(allow_network=True),
        )

        filtered = filter_skill_registry_entries_for_planner(self.entries, request)
        names = {entry["skill_name"] for entry in filtered}

        self.assertLess(len(filtered), len(self.entries))
        self.assertIn("builtin.http.client", names)
        self.assertNotIn("builtin.shell.safe", names)

    def test_explicit_skill_is_preserved_even_when_permission_is_high(self) -> None:
        request = TaskRequest(
            goal="运行安全 shell 命令",
            skill_name="builtin.shell.safe",
            payload={"action": "run", "argv": ["wc", "-l", "README.md"]},
            constraints=TaskConstraints(),
        )

        filtered = filter_skill_registry_entries_for_planner(self.entries, request)
        names = {entry["skill_name"] for entry in filtered}

        self.assertIn("builtin.shell.safe", names)

    def test_planner_skill_context_splits_index_and_expanded_manifests(self) -> None:
        request = TaskRequest(
            goal="Use the JSON skill to write a small report",
            skill_name="builtin.data.json",
            payload={
                "action": "report",
                "path": "inputs/profile.json",
                "fields": [{"label": "name", "query": "user.name"}],
                "output_path": "reports/profile.md",
            },
        )
        filtered = filter_skill_registry_entries_for_planner(self.entries, request)

        context = build_planner_skill_context(filtered, request)

        self.assertEqual(len(context["skill_capability_index"]), len(filtered))
        self.assertLessEqual(len(context["expanded_skill_manifests"]), 2)
        expanded_names = {
            item["skill_name"] for item in context["expanded_skill_manifests"]
        }
        self.assertIn("builtin.data.json", expanded_names)
        first_index = context["skill_capability_index"][0]
        self.assertIn("actions", first_index)
        self.assertNotIn("contracts", first_index)
        budget = context["manifest_budget"]
        self.assertEqual(
            budget["expanded_manifest_count"], len(context["expanded_skill_manifests"])
        )

    def test_planner_payload_keeps_legacy_key_to_expanded_manifests(self) -> None:
        request = TaskRequest(
            goal="Use the CSV skill to summarize inputs/sales.csv",
            preferred_skills=["builtin.data.csv"],
            payload={
                "action": "report",
                "path": "inputs/sales.csv",
                "metrics": [{"name": "total", "op": "sum", "column": "amount"}],
                "output_path": "reports/sales.md",
            },
        )
        filtered = filter_skill_registry_entries_for_planner(self.entries, request)
        planner = Planner(llm=object(), prompts=object())  # type: ignore[arg-type]

        payload = planner.build_request_payload(
            task_request=request,
            bootstrap_documents={},
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=filtered,
        )

        self.assertIn("skill_capability_index", payload)
        self.assertIn("expanded_skill_manifests", payload)
        self.assertEqual(payload["skill_registry_entries"], payload["expanded_skill_manifests"])
        self.assertLessEqual(len(payload["expanded_skill_manifests"]), 2)
        self.assertLess(
            len(str(payload["skill_capability_index"])),
            len(str(filtered)),
        )


class RegistryScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        self.registry = SkillRegistry(self.store)

    def _entry(
        self,
        name: str,
        *,
        permissions: list[str],
        success_rate: float,
        failure_count: int,
    ) -> SkillRegistryEntry:
        return SkillRegistryEntry(
            skill_name=name,
            source="test",
            version="0.1.0",
            description="tool runner",
            permission_requirements=permissions,
            tags=["tool"],
            location="",
            scope="workspace",
            sandbox_level="workspace",
            success_rate=success_rate,
            failure_count=failure_count,
            contracts=[
                SkillContract(
                    action="run",
                    input_fields=[
                        SkillContractField(name="action", type="string", required=True)
                    ],
                )
            ],
        )

    def test_action_match_and_failure_history_affect_selection_score(self) -> None:
        risky = self._entry(
            "workspace.risky.runner",
            permissions=["system.exec"],
            success_rate=1.0,
            failure_count=10,
        )
        steady = self._entry(
            "workspace.steady.runner",
            permissions=[],
            success_rate=0.2,
            failure_count=0,
        )
        self.store.upsert_skill_registry_entry(risky)
        self.store.upsert_skill_registry_entry(steady)
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="run tool",
            required_skills=["tool"],
            input_payload={"action": "run"},
        )

        selection = self.registry.select(node)

        self.assertEqual(selection.selected_skill, "workspace.steady.runner")


class SkillSignalGraphMemoryTest(unittest.TestCase):
    def test_skill_signal_is_written_to_graph_memory(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = SQLiteStore(Path(tempdir.name) / "runtime.sqlite")
        graph = GraphMemoryStore(store)
        task = Task(
            task_id="task_signal",
            goal="run missing action",
            mode="semi_auto",
            source="cli",
            status=TaskStatus.RUNNING,
            executor="auto",
            skill_name=None,
            payload={},
            workspace_path=str(Path(tempdir.name) / "workspace"),
        )
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="bad action",
            input_payload={"action": "teleport"},
        )
        graph.record_task(task)
        graph.record_task_node(task.task_id, node)

        graph.record_skill_signal(
            task=task,
            node=node,
            signal="contract_gap",
            skill_name="builtin.file.basic",
            message="unsupported action",
            details={"requested_action": "teleport"},
        )

        signals = store.list_graph_nodes(node_type="SkillSignal")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["properties"]["signal"], "contract_gap")
        self.assertEqual(signals[0]["properties"]["skill_name"], "builtin.file.basic")


if __name__ == "__main__":
    unittest.main()

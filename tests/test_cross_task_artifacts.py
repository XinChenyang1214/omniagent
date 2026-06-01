"""META-008 — cross-task artifact pull model.

Covers three layers:

1. ``_load_recent_task_artifacts`` in runtime — builds metadata-only manifest
   for the Planner. Enforces the PULL contract: keys and paths only, no
   actual data content.
2. ``_resolve_context_refs`` + ``_load_context_ref`` in ExecutionEngine —
   resolves ``{"context_ref": {...}}`` entries in a node's input_payload by
   reading prior tasks' workspace snapshots.
3. End-to-end integration via ``_resolve_node_context_refs`` on a TaskNode,
   asserting that the original payload is preserved in ``node.metadata``
   while the live payload is rewritten in place.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.execution_engine import ExecutionEngine
from omniagent.models import (
    ArtifactRecord,
    ConversationTurn,
    ConversationTurnRole,
    Task,
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskNodeType,
    TaskStatus,
)


# ===========================================================================
# helpers
# ===========================================================================


def _make_turn(thread_id: str, task_id: str, message: str = "msg") -> ConversationTurn:
    return ConversationTurn(
        thread_id=thread_id,
        role=ConversationTurnRole.USER,
        message=message,
        task_id=task_id,
    )


def _make_task(
    task_id: str,
    goal: str = "g",
    status: TaskStatus = TaskStatus.COMPLETED,
    summary: str = "done",
) -> Task:
    return Task(
        task_id=task_id,
        goal=goal,
        mode="auto",
        source="chat",
        status=status,
        executor="file",
        skill_name=None,
        payload={},
        workspace_path="/tmp/ws",
        summary=summary,
        plan_summary="p summary",
    )


def _make_graph(task_id: str, nodes: list[TaskNode]) -> TaskGraph:
    return TaskGraph(
        graph_id=f"g_{task_id}",
        goal="g",
        summary="s",
        nodes=nodes,
    )


def _make_node(
    node_id: str,
    *,
    status: TaskNodeStatus = TaskNodeStatus.COMPLETED,
    output: dict | None = None,
    artifacts: list[ArtifactRecord] | None = None,
) -> TaskNode:
    return TaskNode(
        node_id=node_id,
        type=TaskNodeType.ACTION,
        goal="n goal",
        status=status,
        output=output or {},
        artifacts=artifacts or [],
    )


def _runtime_with_store(store) -> object:
    """Build a minimal EmbeddedRuntime-like object that only needs `store`
    and a no-op logger. We exercise only `_load_recent_task_artifacts`, so
    the other collaborators are irrelevant."""
    from omniagent.runtime import EmbeddedRuntime

    rt = EmbeddedRuntime.__new__(EmbeddedRuntime)
    rt.store = store
    rt.logger = MagicMock()
    return rt


# ===========================================================================
# 1. recent_task_artifacts loader
# ===========================================================================


class RecentTaskArtifactsLoaderTest(unittest.TestCase):
    """Layer 1 — metadata manifest construction."""

    def test_returns_empty_when_no_thread(self) -> None:
        rt = _runtime_with_store(MagicMock())
        self.assertEqual(rt._load_recent_task_artifacts(None, "cur"), [])

    def test_returns_empty_when_no_turns(self) -> None:
        store = MagicMock()
        store.list_conversation_turns.return_value = []
        rt = _runtime_with_store(store)
        self.assertEqual(rt._load_recent_task_artifacts("thr_1", "cur"), [])

    def test_excludes_current_task_id(self) -> None:
        store = MagicMock()
        store.list_conversation_turns.return_value = [
            _make_turn("thr_1", "tsk_current"),
            _make_turn("thr_1", "tsk_prior"),
        ]
        store.get_task.return_value = _make_task("tsk_prior")
        store.get_task_graph.return_value = None
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "tsk_current")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["task_id"], "tsk_prior")

    def test_skips_non_completed_tasks(self) -> None:
        store = MagicMock()
        store.list_conversation_turns.return_value = [
            _make_turn("thr_1", "tsk_failed"),
            _make_turn("thr_1", "tsk_ok"),
        ]

        def _get(tid):
            if tid == "tsk_failed":
                return _make_task(tid, status=TaskStatus.FAILED)
            return _make_task(tid, status=TaskStatus.COMPLETED)

        store.get_task.side_effect = _get
        store.get_task_graph.return_value = None
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "cur")
        self.assertEqual([t["task_id"] for t in out], ["tsk_ok"])

    def test_most_recent_first(self) -> None:
        store = MagicMock()
        # Turns ascending: oldest first. Reverse-order → tsk_3 should be first.
        store.list_conversation_turns.return_value = [
            _make_turn("thr_1", "tsk_1"),
            _make_turn("thr_1", "tsk_2"),
            _make_turn("thr_1", "tsk_3"),
        ]
        store.get_task.side_effect = lambda tid: _make_task(tid)
        store.get_task_graph.return_value = None
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "cur")
        self.assertEqual([t["task_id"] for t in out], ["tsk_3", "tsk_2", "tsk_1"])

    def test_honors_limit(self) -> None:
        store = MagicMock()
        store.list_conversation_turns.return_value = [
            _make_turn("thr_1", f"tsk_{i}") for i in range(10)
        ]
        store.get_task.side_effect = lambda tid: _make_task(tid)
        store.get_task_graph.return_value = None
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "cur", limit=3)
        self.assertEqual(len(out), 3)

    def test_emits_output_keys_not_values(self) -> None:
        """PULL contract: only keys leak to the Planner, not real data."""
        store = MagicMock()
        store.list_conversation_turns.return_value = [_make_turn("thr_1", "tsk_1")]
        store.get_task.return_value = _make_task("tsk_1")
        node = _make_node(
            "node_fetch",
            output={"body": "<html>sensitive weather data</html>", "status_code": 200},
        )
        store.get_task_graph.return_value = _make_graph("tsk_1", [node])
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "cur")
        self.assertEqual(len(out), 1)
        node_meta = out[0]["nodes"][0]
        self.assertEqual(sorted(node_meta["output_keys"]), ["body", "status_code"])
        # Critically: actual values are NOT leaked.
        serialized = json.dumps(out)
        self.assertNotIn("sensitive weather data", serialized)
        self.assertNotIn("<html>", serialized)

    def test_skips_non_completed_nodes(self) -> None:
        store = MagicMock()
        store.list_conversation_turns.return_value = [_make_turn("thr_1", "tsk_1")]
        store.get_task.return_value = _make_task("tsk_1")
        ok = _make_node("node_ok", status=TaskNodeStatus.COMPLETED, output={"a": 1})
        bad = _make_node("node_bad", status=TaskNodeStatus.FAILED, output={"a": 2})
        store.get_task_graph.return_value = _make_graph("tsk_1", [ok, bad])
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "cur")
        self.assertEqual([n["node_id"] for n in out[0]["nodes"]], ["node_ok"])

    def test_includes_artifact_metadata(self) -> None:
        store = MagicMock()
        store.list_conversation_turns.return_value = [_make_turn("thr_1", "tsk_1")]
        store.get_task.return_value = _make_task("tsk_1")
        node = _make_node(
            "node_x",
            artifacts=[ArtifactRecord(kind="file", path="workspace/a.html", description="weather")],
        )
        store.get_task_graph.return_value = _make_graph("tsk_1", [node])
        rt = _runtime_with_store(store)
        out = rt._load_recent_task_artifacts("thr_1", "cur")
        self.assertEqual(out[0]["nodes"][0]["artifacts"][0]["path"], "workspace/a.html")
        self.assertEqual(out[0]["nodes"][0]["artifacts"][0]["kind"], "file")


# ===========================================================================
# 2. context_ref resolver
# ===========================================================================


class ContextRefResolverTest(unittest.TestCase):
    """Layer 2 — resolving {"context_ref": {...}} dicts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name) / "workspace"
        self.task_id = "tsk_src"
        self.node_id = "node_fetch"
        node_dir = self.workspace_root / "tasks" / self.task_id / "nodes" / self.node_id
        node_dir.mkdir(parents=True)
        (node_dir / "output.json").write_text(
            json.dumps({"body": "weather html body", "status_code": 200}),
            encoding="utf-8",
        )
        art_dir = self.workspace_root / "tasks" / self.task_id / "artifacts"
        art_dir.mkdir(parents=True)
        (art_dir / "weather.html").write_text("<html>stored weather</html>", encoding="utf-8")
        self.engine = self._make_engine()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_engine(self) -> ExecutionEngine:
        engine = ExecutionEngine.__new__(ExecutionEngine)
        # artifact_manager.workspace_manager.root is all the resolver uses.
        engine.artifact_manager = MagicMock()
        engine.artifact_manager.workspace_manager.root = self.workspace_root
        return engine

    def test_no_refs_unchanged(self) -> None:
        payload = {"action": "list", "path": "."}
        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(out, payload)

    def test_resolves_field_ref(self) -> None:
        payload = {
            "context_ref": {
                "task_id": self.task_id,
                "node_id": self.node_id,
                "field": "body",
            }
        }
        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(out, "weather html body")

    def test_resolves_full_output_when_no_field(self) -> None:
        payload = {"context_ref": {"task_id": self.task_id, "node_id": self.node_id}}
        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(out, {"body": "weather html body", "status_code": 200})

    def test_output_field_alias_returns_content_when_present(self) -> None:
        node_dir = self.workspace_root / "tasks" / self.task_id / "nodes" / "node_generate"
        node_dir.mkdir(parents=True)
        (node_dir / "output.json").write_text(
            json.dumps({"content": "generated text", "reasoning": "ok"}),
            encoding="utf-8",
        )
        payload = {
            "context_ref": {
                "task_id": self.task_id,
                "node_id": "node_generate",
                "field": "output",
            }
        }

        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)

        self.assertEqual(out, "generated text")

    def test_resolves_artifact_path(self) -> None:
        payload = {
            "context_ref": {
                "task_id": self.task_id,
                "artifact_path": f"workspace/tasks/{self.task_id}/artifacts/weather.html",
            }
        }
        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(out, "<html>stored weather</html>")

    def test_resolves_nested_ref_inside_field(self) -> None:
        payload = {
            "action": "write",
            "path": "out.md",
            "content": {
                "context_ref": {
                    "task_id": self.task_id,
                    "node_id": self.node_id,
                    "field": "body",
                }
            },
        }
        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(
            out,
            {"action": "write", "path": "out.md", "content": "weather html body"},
        )

    def test_resolves_refs_inside_list(self) -> None:
        payload = {
            "inputs": [
                "literal",
                {
                    "context_ref": {
                        "task_id": self.task_id,
                        "node_id": self.node_id,
                        "field": "body",
                    }
                },
            ]
        }
        out = self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(out, {"inputs": ["literal", "weather html body"]})

    def test_missing_task_dir_raises(self) -> None:
        payload = {
            "context_ref": {
                "task_id": "tsk_missing",
                "node_id": "x",
                "field": "y",
            }
        }
        with self.assertRaises(OmniAgentError) as cm:
            self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertIn("tsk_missing", cm.exception.message)

    def test_missing_node_output_raises(self) -> None:
        payload = {
            "context_ref": {
                "task_id": self.task_id,
                "node_id": "node_unknown",
                "field": "body",
            }
        }
        with self.assertRaises(OmniAgentError) as cm:
            self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_missing_field_raises_with_available_keys(self) -> None:
        payload = {
            "context_ref": {
                "task_id": self.task_id,
                "node_id": self.node_id,
                "field": "ghost",
            }
        }
        with self.assertRaises(OmniAgentError) as cm:
            self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertIn("available_keys", cm.exception.details)
        self.assertIn("body", cm.exception.details["available_keys"])

    def test_missing_task_id_raises(self) -> None:
        payload = {"context_ref": {"node_id": "x", "field": "y"}}
        with self.assertRaises(OmniAgentError) as cm:
            self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertIn("task_id", cm.exception.message)

    def test_artifact_path_traversal_blocked(self) -> None:
        """Path traversal outside task_dir must fail, even if file exists."""
        # Write a file outside the task dir but inside workspace to tempt traversal.
        outside = self.workspace_root / "secrets.txt"
        outside.write_text("secret", encoding="utf-8")
        payload = {
            "context_ref": {
                "task_id": self.task_id,
                # Attempt to escape the task_id sandbox via '..'
                "artifact_path": f"workspace/tasks/{self.task_id}/../../secrets.txt",
            }
        }
        with self.assertRaises(OmniAgentError) as cm:
            self.engine._resolve_context_refs(payload, workspace_root=self.workspace_root)
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertIn("越界", cm.exception.message)


# ===========================================================================
# 3. _resolve_node_context_refs integration
# ===========================================================================


class ResolveNodeContextRefsIntegrationTest(unittest.TestCase):
    """Layer 3 — end-to-end mutation of a TaskNode before dispatch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.tmp.name) / "workspace"
        node_dir = self.workspace_root / "tasks" / "tsk_src" / "nodes" / "node_fetch"
        node_dir.mkdir(parents=True)
        (node_dir / "output.json").write_text(json.dumps({"body": "hello world"}), encoding="utf-8")
        self.engine = ExecutionEngine.__new__(ExecutionEngine)
        self.engine.artifact_manager = MagicMock()
        self.engine.artifact_manager.workspace_manager.root = self.workspace_root

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_ref_leaves_payload_and_metadata_untouched(self) -> None:
        node = TaskNode(
            node_id="node_x",
            type=TaskNodeType.ACTION,
            goal="g",
            input_payload={"action": "list", "path": "."},
        )
        self.engine._resolve_node_context_refs(node)
        self.assertEqual(node.input_payload, {"action": "list", "path": "."})
        self.assertNotIn("input_payload_original", node.metadata)

    def test_ref_is_resolved_and_original_preserved(self) -> None:
        node = TaskNode(
            node_id="node_x",
            type=TaskNodeType.ACTION,
            goal="g",
            input_payload={
                "action": "write",
                "path": "out.md",
                "content": {
                    "context_ref": {
                        "task_id": "tsk_src",
                        "node_id": "node_fetch",
                        "field": "body",
                    }
                },
            },
        )
        self.engine._resolve_node_context_refs(node)
        self.assertEqual(node.input_payload["content"], "hello world")
        # Original preserved verbatim for audit.
        self.assertIn("input_payload_original", node.metadata)
        self.assertEqual(
            node.metadata["input_payload_original"]["content"],
            {
                "context_ref": {
                    "task_id": "tsk_src",
                    "node_id": "node_fetch",
                    "field": "body",
                }
            },
        )

    def test_failure_propagates_as_OmniAgentError(self) -> None:
        node = TaskNode(
            node_id="node_x",
            type=TaskNodeType.ACTION,
            goal="g",
            input_payload={"content": {"context_ref": {"task_id": "tsk_missing", "node_id": "n"}}},
        )
        with self.assertRaises(OmniAgentError):
            self.engine._resolve_node_context_refs(node)


# ===========================================================================
# 4. same-graph dependency output injection
# ===========================================================================


class SameGraphDependencyOutputInjectionTest(unittest.TestCase):
    """Dependency edges carry both execution order and same-graph dataflow."""

    def setUp(self) -> None:
        self.engine = ExecutionEngine.__new__(ExecutionEngine)

    def test_injects_dependency_outputs_into_action_payload(self) -> None:
        fetch = TaskNode(
            node_id="node_fetch_weather",
            type=TaskNodeType.ACTION,
            goal="fetch weather",
            status=TaskNodeStatus.COMPLETED,
            output={"status": 200, "body": "郑州今日天气：晴，25°C"},
        )
        format_node = TaskNode(
            node_id="node_format_weather",
            type=TaskNodeType.ACTION,
            goal="format weather",
            dependencies=["node_fetch_weather"],
            executor_hint="llm_native",
            input_payload={"prompt": "根据上一步天气数据整理中文天气报告"},
        )
        graph = TaskGraph(
            goal="weather",
            nodes=[fetch, format_node],
        )

        self.engine._inject_dependency_outputs(format_node, graph)

        self.assertEqual(format_node.input_payload["prompt"], "根据上一步天气数据整理中文天气报告")
        self.assertEqual(
            format_node.input_payload["dependency_outputs"],
            {"node_fetch_weather": {"status": 200, "body": "郑州今日天气：晴，25°C"}},
        )
        self.assertEqual(
            format_node.metadata["input_payload_original"],
            {"prompt": "根据上一步天气数据整理中文天气报告"},
        )
        self.assertEqual(
            format_node.metadata["dependency_outputs_injected"],
            ["node_fetch_weather"],
        )

    def test_does_not_overwrite_explicit_dependency_outputs(self) -> None:
        source = TaskNode(
            node_id="node_source",
            type=TaskNodeType.ACTION,
            goal="source",
            status=TaskNodeStatus.COMPLETED,
            output={"body": "actual"},
        )
        target = TaskNode(
            node_id="node_target",
            type=TaskNodeType.ACTION,
            goal="target",
            dependencies=["node_source"],
            input_payload={"dependency_outputs": {"manual": {"body": "planner supplied"}}},
        )
        graph = TaskGraph(goal="g", nodes=[source, target])

        self.engine._inject_dependency_outputs(target, graph)

        self.assertEqual(
            target.input_payload["dependency_outputs"],
            {"manual": {"body": "planner supplied"}},
        )
        self.assertNotIn("dependency_outputs_injected", target.metadata)

    def test_injected_outputs_are_copied(self) -> None:
        source = TaskNode(
            node_id="node_source",
            type=TaskNodeType.ACTION,
            goal="source",
            status=TaskNodeStatus.COMPLETED,
            output={"nested": {"value": "original"}},
        )
        target = TaskNode(
            node_id="node_target",
            type=TaskNodeType.ACTION,
            goal="target",
            dependencies=["node_source"],
            input_payload={"prompt": "summarize"},
        )
        graph = TaskGraph(goal="g", nodes=[source, target])

        self.engine._inject_dependency_outputs(target, graph)
        target.input_payload["dependency_outputs"]["node_source"]["nested"]["value"] = "changed"

        self.assertEqual(source.output["nested"]["value"], "original")


# ===========================================================================
# 5. _payload_contains_context_ref
# ===========================================================================


class PayloadContainsContextRefTest(unittest.TestCase):
    def test_flat_ref_detected(self) -> None:
        self.assertTrue(
            ExecutionEngine._payload_contains_context_ref({"context_ref": {"task_id": "x"}})
        )

    def test_nested_ref_detected(self) -> None:
        self.assertTrue(
            ExecutionEngine._payload_contains_context_ref(
                {"action": "write", "content": {"context_ref": {"task_id": "x"}}}
            )
        )

    def test_ref_in_list_detected(self) -> None:
        self.assertTrue(
            ExecutionEngine._payload_contains_context_ref(
                {"inputs": [{"context_ref": {"task_id": "x"}}]}
            )
        )

    def test_no_ref_returns_false(self) -> None:
        self.assertFalse(
            ExecutionEngine._payload_contains_context_ref({"action": "list", "path": "."})
        )

    def test_empty_payload_returns_false(self) -> None:
        self.assertFalse(ExecutionEngine._payload_contains_context_ref({}))

    def test_context_ref_not_dict_ignored(self) -> None:
        # If "context_ref" is not a dict value, it is not a valid ref marker.
        self.assertFalse(
            ExecutionEngine._payload_contains_context_ref({"context_ref": "just a string"})
        )


# ===========================================================================
# 6. Planner payload wiring
# ===========================================================================


class PlannerPayloadWiringTest(unittest.TestCase):
    """Ensures Planner.plan forwards recent_task_artifacts into request_payload."""

    def test_recent_task_artifacts_enters_request_payload(self) -> None:
        from omniagent.planner import Planner
        from omniagent.models import (
            BootstrapContext,
            LLMResponse,
            TaskConstraints,
            TaskRequest,
            utc_now,
        )
        from omniagent.prompting import PromptManager

        captured: dict = {}

        class _CapturingLLM:
            def generate_structured(self, **kw):
                captured["request_payload"] = kw["request_payload"]
                return LLMResponse(
                    provider="test",
                    model="t",
                    prompt_name="planner_v1",
                    request_id="r1",
                    raw_output="{}",
                    structured_output={
                        "summary": "s",
                        "reasoning": "r",
                        "risk_level": "low",
                        "required_capabilities": [],
                        "decision": "decline_not_a_task",
                        "decline_reason": "test",
                    },
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    started_at=utc_now(),
                )

        prompts = MagicMock(spec=PromptManager)
        prompts.load.return_value = "sys"
        planner = Planner(_CapturingLLM(), prompts)
        request = TaskRequest(
            goal="g",
            constraints=TaskConstraints(),
        )
        bootstrap = BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[])
        artifacts = [
            {"task_id": "tsk_prior", "goal": "prev", "nodes": []},
        ]
        planner.plan(
            task_request=request,
            bootstrap=bootstrap,
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
            conversation_history=[],
            recent_task_artifacts=artifacts,
        )
        self.assertIn("recent_task_artifacts", captured["request_payload"])
        self.assertEqual(
            captured["request_payload"]["recent_task_artifacts"],
            artifacts,
        )

    def test_recent_task_artifacts_defaults_to_empty_list(self) -> None:
        from omniagent.planner import Planner
        from omniagent.models import (
            BootstrapContext,
            LLMResponse,
            TaskConstraints,
            TaskRequest,
            utc_now,
        )
        from omniagent.prompting import PromptManager

        captured: dict = {}

        class _CapturingLLM:
            def generate_structured(self, **kw):
                captured["request_payload"] = kw["request_payload"]
                return LLMResponse(
                    provider="test",
                    model="t",
                    prompt_name="planner_v1",
                    request_id="r1",
                    raw_output="{}",
                    structured_output={
                        "summary": "s",
                        "reasoning": "r",
                        "risk_level": "low",
                        "required_capabilities": [],
                        "decision": "decline_not_a_task",
                        "decline_reason": "test",
                    },
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    started_at=utc_now(),
                )

        prompts = MagicMock(spec=PromptManager)
        prompts.load.return_value = "sys"
        planner = Planner(_CapturingLLM(), prompts)
        request = TaskRequest(goal="g", constraints=TaskConstraints())
        bootstrap = BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[])
        planner.plan(
            task_request=request,
            bootstrap=bootstrap,
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
            conversation_history=[],
        )
        # Default → empty list, not None, not missing.
        self.assertEqual(captured["request_payload"]["recent_task_artifacts"], [])


if __name__ == "__main__":
    unittest.main()

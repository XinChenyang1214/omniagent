"""META-007 — TaskGraph dual-dependency representation sync.

Ensures that `node.dependencies` is always backfilled from `graph.dependencies`
(the graph-level `TaskDependency` list) at parse time, so the execution engine,
`ready_nodes()`, `_apply_replan`, and graph-memory recording all see consistent
dependency information.

Coverage map:
- TaskGraph.from_dict backfill   → DependencyBackfillTest (7)
- ready_nodes ordering           → ReadyNodesOrderingTest (3)
- Idempotency & edge cases       → BackfillEdgeCaseTest (3)
"""

from __future__ import annotations

import unittest

from omniagent.models import (
    TaskGraph,
    TaskNodeStatus,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _graph_payload(
    *,
    nodes: list[dict],
    deps: list[dict],
    goal: str = "test",
) -> dict:
    """Build a minimal TaskGraph dict as the Planner/LLM would produce."""
    return {
        "goal": goal,
        "summary": "test graph",
        "nodes": nodes,
        "dependencies": deps,
    }


def _node_dict(
    node_id: str,
    *,
    node_deps: list[str] | None = None,
    node_type: str = "action",
) -> dict:
    return {
        "node_id": node_id,
        "type": node_type,
        "goal": f"do {node_id}",
        "dependencies": node_deps or [],
    }


# ---------------------------------------------------------------------------
# DependencyBackfillTest — core backfill logic
# ---------------------------------------------------------------------------


class DependencyBackfillTest(unittest.TestCase):
    """Verifies that from_dict backfills node.dependencies from graph.dependencies."""

    def test_empty_node_deps_backfilled_from_graph_deps(self) -> None:
        """LLM writes graph deps but leaves node deps empty → backfill."""
        payload = _graph_payload(
            nodes=[
                _node_dict("a"),
                _node_dict("b"),
                _node_dict("c"),
            ],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "b", "target_node_id": "c"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("a").dependencies, [])
        self.assertEqual(graph.get_node("b").dependencies, ["a"])
        self.assertEqual(graph.get_node("c").dependencies, ["b"])

    def test_missing_node_id_gets_stable_fallback(self) -> None:
        """Planner output missing node_id should not crash graph parsing."""
        payload = _graph_payload(
            nodes=[
                {
                    "type": "action",
                    "goal": "read missing file",
                    "input_payload": {"action": "read", "path": "missing.md"},
                },
                _node_dict("node_finalize", node_type="finalize"),
            ],
            deps=[{"source_node_id": "node_1", "target_node_id": "node_finalize"}],
        )

        graph = TaskGraph.from_dict(payload)

        self.assertEqual(graph.nodes[0].node_id, "node_1")
        self.assertEqual(graph.nodes[0].goal, "read missing file")
        self.assertEqual(graph.get_node("node_finalize").dependencies, ["node_1"])

    def test_three_node_chain_order(self) -> None:
        """fetch → format → finalize (real-world weather pattern)."""
        payload = _graph_payload(
            nodes=[
                _node_dict("node_fetch"),
                _node_dict("node_format"),
                _node_dict("node_finalize", node_type="finalize"),
            ],
            deps=[
                {"source_node_id": "node_fetch", "target_node_id": "node_format"},
                {"source_node_id": "node_format", "target_node_id": "node_finalize"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertIn("node_fetch", graph.get_node("node_format").dependencies)
        self.assertIn("node_format", graph.get_node("node_finalize").dependencies)

    def test_diamond_dependency(self) -> None:
        """A → B, A → C, B → D, C → D (diamond shape)."""
        payload = _graph_payload(
            nodes=[_node_dict(x) for x in ("a", "b", "c", "d")],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "a", "target_node_id": "c"},
                {"source_node_id": "b", "target_node_id": "d"},
                {"source_node_id": "c", "target_node_id": "d"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("a").dependencies, [])
        self.assertEqual(graph.get_node("b").dependencies, ["a"])
        self.assertEqual(graph.get_node("c").dependencies, ["a"])
        self.assertCountEqual(graph.get_node("d").dependencies, ["b", "c"])

    def test_backfill_does_not_duplicate_existing(self) -> None:
        """When node already has the dep, backfill must not add it again."""
        payload = _graph_payload(
            nodes=[
                _node_dict("a"),
                _node_dict("b", node_deps=["a"]),  # already correct
            ],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("b").dependencies, ["a"])
        self.assertEqual(graph.get_node("b").dependencies.count("a"), 1)

    def test_graph_deps_empty_no_crash(self) -> None:
        """When graph has no graph-level deps, nothing happens."""
        payload = _graph_payload(
            nodes=[_node_dict("a"), _node_dict("b")],
            deps=[],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("a").dependencies, [])
        self.assertEqual(graph.get_node("b").dependencies, [])

    def test_dangling_graph_dep_filtered(self) -> None:
        """Graph-level dep referencing non-existent node → silently dropped."""
        payload = _graph_payload(
            nodes=[_node_dict("a"), _node_dict("b")],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "a", "target_node_id": "nonexistent"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("b").dependencies, ["a"])


# ---------------------------------------------------------------------------
# ReadyNodesOrderingTest — ready_nodes respects backfilled deps
# ---------------------------------------------------------------------------


class ReadyNodesOrderingTest(unittest.TestCase):
    """Verifies that ready_nodes() returns the correct subset after backfill."""

    def test_only_root_is_initially_ready(self) -> None:
        """In A → B → C, only A should be ready at start."""
        payload = _graph_payload(
            nodes=[_node_dict("a"), _node_dict("b"), _node_dict("c")],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "b", "target_node_id": "c"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        ready = graph.ready_nodes()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].node_id, "a")

    def test_completing_root_unlocks_next(self) -> None:
        """After A completes, B (not C) becomes ready."""
        payload = _graph_payload(
            nodes=[_node_dict("a"), _node_dict("b"), _node_dict("c")],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "b", "target_node_id": "c"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        graph.get_node("a").status = TaskNodeStatus.COMPLETED
        ready = graph.ready_nodes()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].node_id, "b")

    def test_diamond_parallel_after_root(self) -> None:
        """In A → {B, C} → D, B and C are both ready after A completes."""
        payload = _graph_payload(
            nodes=[_node_dict(x) for x in ("a", "b", "c", "d")],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "a", "target_node_id": "c"},
                {"source_node_id": "b", "target_node_id": "d"},
                {"source_node_id": "c", "target_node_id": "d"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        graph.get_node("a").status = TaskNodeStatus.COMPLETED
        ready = graph.ready_nodes()
        ready_ids = {n.node_id for n in ready}
        self.assertEqual(ready_ids, {"b", "c"})


# ---------------------------------------------------------------------------
# BackfillEdgeCaseTest
# ---------------------------------------------------------------------------


class BackfillEdgeCaseTest(unittest.TestCase):
    def test_single_node_no_deps(self) -> None:
        """A graph with a single node and no deps should work fine."""
        payload = _graph_payload(
            nodes=[_node_dict("only")],
            deps=[],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("only").dependencies, [])
        ready = graph.ready_nodes()
        self.assertEqual(len(ready), 1)

    def test_alias_fields_in_graph_deps(self) -> None:
        """LLM sometimes uses 'source'/'target' instead of full field names."""
        payload = _graph_payload(
            nodes=[_node_dict("a"), _node_dict("b")],
            deps=[
                {"source": "a", "target": "b"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("b").dependencies, ["a"])

    def test_mixed_node_and_graph_deps(self) -> None:
        """Some edges in node.deps, others only in graph.deps → merged."""
        payload = _graph_payload(
            nodes=[
                _node_dict("a"),
                _node_dict("b", node_deps=["a"]),  # already has A
                _node_dict("c"),  # will get B from graph deps
            ],
            deps=[
                {"source_node_id": "a", "target_node_id": "b"},
                {"source_node_id": "b", "target_node_id": "c"},
            ],
        )
        graph = TaskGraph.from_dict(payload)
        self.assertEqual(graph.get_node("b").dependencies, ["a"])
        self.assertEqual(graph.get_node("c").dependencies, ["b"])


if __name__ == "__main__":
    unittest.main()

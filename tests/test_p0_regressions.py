"""P0 regression tests — backfill coverage for Phase 1 + META-006 Phase 1 fixes.

Each fix is exercised by a focused test class so that a future refactor
breaking any one of them produces a targeted failure. These tests were
backfilled in a single pass because the original fixes shipped without
dedicated coverage (see issue.md P0 列表).

Coverage map:
- ISSUE-010           → RecallScoreThresholdTest
- ISSUE-011 子任务 #1 → WorkflowSuccessHardFilterTest
- META-006 重灾区 A   → PerTurnRecallCacheTest
- META-006 重灾区 B   → RouterFallthroughNoLLMTest
- META-006 N+1        → GraphNeighborBatchQueryTest
- META-006 SQL filter → ThreadContextPreferenceFilterTest
- ISSUE-012           → ApprovalGoalResolutionTest
- ISSUE-013           → HybridWorkflowDedupTest
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omniagent.gateway import RuntimeGateway
from omniagent.graph_memory import GraphMemoryStore
from omniagent.hybrid_memory import HybridMemoryRetriever
from omniagent.interaction import InteractionRouter
from omniagent.memory import FileMemoryManager
from omniagent.models import (
    ConversationThread,
    PendingCheckpoint,
    PendingCheckpointStatus,
    PendingCheckpointType,
    Task,
    TaskStatus,
    UserInputEnvelope,
    UserInputType,
)
from omniagent.prompting import PromptManager
from omniagent.storage import SQLiteStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(path: Path) -> SQLiteStore:
    return SQLiteStore(path)


def _seed_task_node(store: SQLiteStore, *, task_id: str, status: str) -> None:
    """Create a minimal Task graph node so _task_status_map picks it up."""
    store.upsert_graph_node(
        entity_key=f"task:{task_id}",
        node_type="Task",
        name=f"task {task_id}",
        properties={
            "task_id": task_id,
            "status": status,
            "goal": f"goal-{task_id}",
        },
        source="test",
    )


def _seed_workflow_node(store: SQLiteStore, *, task_id: str, goal: str, summary: str) -> None:
    store.upsert_graph_node(
        entity_key=f"workflow:{task_id}",
        node_type="Workflow",
        name=goal[:80],
        properties={
            "task_id": task_id,
            "graph_id": f"graph_{task_id}",
            "goal": goal,
            "summary": summary,
            "node_count": 1,
        },
        source="test",
    )


# ---------------------------------------------------------------------------
# ISSUE-011 子任务 #1 — workflow success hard-filter
# ---------------------------------------------------------------------------


class WorkflowSuccessHardFilterTest(unittest.TestCase):
    """Failed / non-completed tasks must not appear in workflow_matches.

    Before the fix, a failed task like "讲一个冷笑话" was recommended as a
    positive workflow reference because the only filter was ``score > 0``.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)

    def test_completed_task_appears_in_workflow_matches(self) -> None:
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="写一个 markdown 摘要文件",
            summary="创建一个 markdown 文件汇总结果",
        )
        results = self.graph_memory.recommend_workflows("写一个 markdown 摘要文件", min_score=0.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "t_ok")

    def test_failed_task_filtered_out(self) -> None:
        _seed_task_node(self.store, task_id="t_fail", status="failed")
        _seed_workflow_node(
            self.store,
            task_id="t_fail",
            goal="写一个 markdown 摘要文件",
            summary="创建一个 markdown 文件汇总结果",
        )
        results = self.graph_memory.recommend_workflows("写一个 markdown 摘要文件", min_score=0.0)
        self.assertEqual(results, [])

    def test_pending_task_filtered_out(self) -> None:
        _seed_task_node(self.store, task_id="t_pending", status="pending")
        _seed_workflow_node(
            self.store,
            task_id="t_pending",
            goal="写一个 markdown 摘要文件",
            summary="创建一个 markdown 文件汇总结果",
        )
        results = self.graph_memory.recommend_workflows("写一个 markdown 摘要文件", min_score=0.0)
        self.assertEqual(results, [])

    def test_mixed_success_and_failure_only_keeps_completed(self) -> None:
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_task_node(self.store, task_id="t_fail", status="failed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="写 markdown 文件",
            summary="good run",
        )
        _seed_workflow_node(
            self.store,
            task_id="t_fail",
            goal="写 markdown 文件",
            summary="bad run",
        )
        results = self.graph_memory.recommend_workflows("写 markdown 文件", min_score=0.0)
        task_ids = [item["task_id"] for item in results]
        self.assertEqual(task_ids, ["t_ok"])


# ---------------------------------------------------------------------------
# ISSUE-010 — min_score thresholds
# ---------------------------------------------------------------------------


class RecallScoreThresholdTest(unittest.TestCase):
    """recommend_* functions must drop below-threshold candidates."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)

    def test_workflows_filter_unrelated_by_default_threshold(self) -> None:
        # The workflow has completed status so it survives ISSUE-011 filter,
        # but the query is totally unrelated so the score falls below the
        # default 0.4 threshold and must be dropped.
        _seed_task_node(self.store, task_id="t_off", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_off",
            goal="plant hydrangeas in a temperate garden",
            summary="gardening tips and soil preparation notes",
        )
        results = self.graph_memory.recommend_workflows("部署一个分布式数据库集群并配置高可用副本")
        self.assertEqual(results, [])

    def test_workflows_respect_explicit_min_score(self) -> None:
        _seed_task_node(self.store, task_id="t_match", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_match",
            goal="写 markdown 文件",
            summary="创建 markdown 文件",
        )
        loose = self.graph_memory.recommend_workflows("写 markdown 文件", min_score=0.0)
        # Score is bounded to [0, 1] by SemanticScorer; using an impossible
        # threshold proves the parameter is respected without depending on
        # the exact score a perfect match lands at.
        strict = self.graph_memory.recommend_workflows("写 markdown 文件", min_score=1.01)
        self.assertEqual(len(loose), 1)
        self.assertEqual(strict, [])

    def test_failure_patterns_apply_threshold(self) -> None:
        self.store.upsert_graph_node(
            entity_key="failure:t_x:n1",
            node_type="FailurePattern",
            name="pattern x",
            properties={
                "task_id": "t_x",
                "node_id": "n1",
                "goal": "gardening in heavy clay soil",
                "error_message": "shovel snapped",
                "critic_summary": "use proper tools",
            },
            source="test",
        )
        # Far-off query: must be filtered by default (0.55) threshold.
        results = self.graph_memory.recommend_failure_patterns("部署分布式数据库集群")
        self.assertEqual(results, [])

    def test_skills_for_goal_apply_threshold(self) -> None:
        self.store.upsert_graph_node(
            entity_key="task_node:t_a:n1",
            node_type="TaskNode",
            name="gardening",
            properties={
                "task_id": "t_a",
                "node_id": "n1",
                "goal": "plant a garden of hydrangeas",
                "executor_hint": "file",
            },
            source="test",
        )
        self.store.upsert_graph_node(
            entity_key="skill:garden.tool",
            node_type="Skill",
            name="garden.tool",
            properties={
                "skill_name": "garden.tool",
                "lifecycle_status": "active",
                "success_rate": 1.0,
            },
            source="test",
        )
        self.store.upsert_graph_edge(
            source_entity_key="task_node:t_a:n1",
            target_entity_key="skill:garden.tool",
            edge_type="USES",
            source="test",
        )
        results = self.graph_memory.recommend_skills_for_goal("部署分布式数据库集群")
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# META-006 重灾区 A — per-turn recall cache shared between router + planner
# ---------------------------------------------------------------------------


class PerTurnRecallCacheTest(unittest.TestCase):
    """Router pre-pass and planner pre-pass must share one graph scan per turn."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.memory = FileMemoryManager(Path(self.tempdir.name) / "workspace")
        self.graph = MagicMock()
        self.graph.recommend_workflows.return_value = []
        self.graph.recommend_skills_for_goal.return_value = []
        self.graph.recommend_failure_patterns.return_value = []
        self.graph.thread_context.return_value = {
            "thread": None,
            "recent_turns": [],
            "preferences": [],
        }
        self.retriever = HybridMemoryRetriever(self.memory, self.graph)

    def test_thread_then_planning_share_one_scan(self) -> None:
        self.retriever.begin_turn("turn-1")
        self.retriever.recall_for_thread(thread_id="th1", message="写文件", limit=5)
        self.retriever.recall_for_planning("写文件", limit=5)
        # Both callers hit the same cache key (query="写文件", limit=5).
        # Second call reuses the first's scan.
        self.assertEqual(self.graph.recommend_workflows.call_count, 1)
        self.assertEqual(self.graph.recommend_skills_for_goal.call_count, 1)

    def test_begin_turn_with_new_key_invalidates(self) -> None:
        self.retriever.begin_turn("turn-1")
        self.retriever.recall_for_planning("写文件", limit=5)
        self.retriever.begin_turn("turn-2")
        self.retriever.recall_for_planning("写文件", limit=5)
        self.assertEqual(self.graph.recommend_workflows.call_count, 2)

    def test_begin_turn_same_key_is_no_op(self) -> None:
        self.retriever.begin_turn("turn-1")
        self.retriever.recall_for_planning("写文件", limit=5)
        self.retriever.begin_turn("turn-1")
        self.retriever.recall_for_planning("写文件", limit=5)
        self.assertEqual(self.graph.recommend_workflows.call_count, 1)

    def test_different_queries_within_same_turn_do_not_collide(self) -> None:
        self.retriever.begin_turn("turn-1")
        self.retriever.recall_for_planning("写文件", limit=5)
        self.retriever.recall_for_planning("读文件", limit=5)
        self.assertEqual(self.graph.recommend_workflows.call_count, 2)


# ---------------------------------------------------------------------------
# META-006 重灾区 B — router fall-through must not invoke LLM
# ---------------------------------------------------------------------------


class RouterFallthroughNoLLMTest(unittest.TestCase):
    """Rule-based router must fall through to NEW_TASK without calling LLM.

    Before META-003/004, unmatched messages like "把 a.txt 改名为 b.txt"
    cascaded into ``_llm_based()`` which triggered an extra Qwen roundtrip
    (5-15s on Dashscope). With Planner decision + LLMNativeExecutor both in
    place, the rule layer should go straight to NEW_TASK.
    """

    def setUp(self) -> None:
        self.llm = MagicMock()
        self.llm.generate_structured.side_effect = AssertionError(
            "LLM must not be invoked for rule-based fall-through"
        )
        self.prompts = MagicMock(spec=PromptManager)
        self.prompts.load.return_value = "stub prompt"
        self.router = InteractionRouter(self.llm, self.prompts)
        self.thread = ConversationThread(title="test thread")

    def _envelope(self, text: str) -> UserInputEnvelope:
        return UserInputEnvelope(message=text, thread_id=self.thread.thread_id)

    def test_rename_request_falls_through_without_llm(self) -> None:
        # "改名" isn't in the task-hint list, but must still route without LLM.
        decision, context = self.router.decide(
            self._envelope("把 a.txt 改名为 b.txt"),
            thread=self.thread,
            active_task=None,
            pending_checkpoint=None,
        )
        self.assertEqual(decision.input_type, UserInputType.NEW_TASK)
        self.assertEqual(decision.metadata.get("route_source"), "rule")
        self.assertEqual(decision.metadata.get("rule_path"), "fall_through")
        self.assertIsNone(context)
        self.llm.generate_structured.assert_not_called()

    def test_task_hint_matched_still_stays_in_rule_layer(self) -> None:
        decision, context = self.router.decide(
            self._envelope("写一个文件"),
            thread=self.thread,
            active_task=None,
            pending_checkpoint=None,
        )
        self.assertEqual(decision.input_type, UserInputType.NEW_TASK)
        self.assertEqual(decision.metadata.get("rule_path"), "task_hint")
        self.assertIsNone(context)
        self.llm.generate_structured.assert_not_called()

    def test_chitchat_also_falls_through_to_planner(self) -> None:
        # META-004: "讲个冷笑话" / "你怎么看" are no longer short-circuited
        # by a SMALL_TALK rule; they fall through to Planner which will either
        # plan (llm_native) or decline.
        decision, _ = self.router.decide(
            self._envelope("讲个冷笑话吧"),
            thread=self.thread,
            active_task=None,
            pending_checkpoint=None,
        )
        self.assertEqual(decision.input_type, UserInputType.NEW_TASK)
        self.assertEqual(decision.metadata.get("rule_path"), "fall_through")
        self.llm.generate_structured.assert_not_called()


# ---------------------------------------------------------------------------
# META-006 — list_graph_neighbors must not regress to N+1
# ---------------------------------------------------------------------------


class GraphNeighborBatchQueryTest(unittest.TestCase):
    """``list_graph_neighbors`` must batch the node fetch, not loop per neighbor.

    The pre-fix implementation did 1 edge query + N ``get_graph_node`` calls.
    In ``recommend_skills_for_goal`` (called per TaskNode, up to 800) this
    blew up into thousands of SQLite round-trips per user turn.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")

    def test_batched_fetch_never_calls_get_graph_node(self) -> None:
        center_key = "task_node:center"
        self.store.upsert_graph_node(
            entity_key=center_key,
            node_type="TaskNode",
            name="center",
            properties={"node_id": "center"},
            source="test",
        )
        for idx in range(5):
            neighbor_key = f"skill:s{idx}"
            self.store.upsert_graph_node(
                entity_key=neighbor_key,
                node_type="Skill",
                name=f"s{idx}",
                properties={"skill_name": f"s{idx}"},
                source="test",
            )
            self.store.upsert_graph_edge(
                source_entity_key=center_key,
                target_entity_key=neighbor_key,
                edge_type="USES",
                source="test",
            )

        with (
            patch.object(
                self.store,
                "list_graph_edges",
                wraps=self.store.list_graph_edges,
            ) as edges_spy,
            patch.object(
                self.store,
                "get_graph_node",
                wraps=self.store.get_graph_node,
            ) as get_spy,
        ):
            neighbors = self.store.list_graph_neighbors(
                center_key, edge_type="USES", node_type="Skill"
            )

        self.assertEqual(len(neighbors), 5)
        self.assertEqual(edges_spy.call_count, 1, "should do exactly one edge query")
        self.assertEqual(
            get_spy.call_count,
            0,
            "get_graph_node per neighbor = N+1 regression — must stay 0",
        )


# ---------------------------------------------------------------------------
# META-006 SQL filter — thread_context Preference scan
# ---------------------------------------------------------------------------


class ThreadContextPreferenceFilterTest(unittest.TestCase):
    """``thread_context`` must push the Preference filter to SQL, not Python."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)

    def test_list_graph_nodes_called_with_preference_filter(self) -> None:
        self.store.upsert_graph_node(
            entity_key="thread:th1",
            node_type="ConversationThread",
            name="th1",
            properties={"status": "active"},
            source="test",
        )
        recorded_calls: list[dict] = []
        real = self.store.list_graph_nodes

        def spy(*args, **kwargs):
            recorded_calls.append(dict(kwargs))
            return real(*args, **kwargs)

        with patch.object(self.store, "list_graph_nodes", side_effect=spy):
            self.graph_memory.thread_context("th1", limit=8)

        preference_calls = [c for c in recorded_calls if c.get("node_type") == "Preference"]
        self.assertEqual(
            len(preference_calls),
            1,
            f"expected exactly 1 Preference-filtered scan; got {recorded_calls}",
        )
        # Regression guard: must never do an unfiltered 500-row pull.
        unfiltered = [c for c in recorded_calls if c.get("node_type") is None]
        self.assertEqual(
            unfiltered,
            [],
            "thread_context must not pull unfiltered nodes and filter in Python",
        )


# ---------------------------------------------------------------------------
# ISSUE-012 — approval_response goal reverse-lookup
# ---------------------------------------------------------------------------


class ApprovalGoalResolutionTest(unittest.TestCase):
    """``_resolve_goal_from_approval`` must rewrite literal approval text.

    Scenario: user had a pending checkpoint; they replied "立即生成".
    The new task's goal must come from the parent task, not the literal reply.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")

    def _gateway(self) -> RuntimeGateway:
        gateway = RuntimeGateway.__new__(RuntimeGateway)
        gateway.store = self.store
        return gateway

    def _seed_parent(self, *, task_id: str, goal: str) -> None:
        self.store.create_task(
            Task(
                task_id=task_id,
                goal=goal,
                mode="task",
                source="test",
                status=TaskStatus.WAITING_INPUT,
                executor="file",
                skill_name=None,
                payload={},
                workspace_path="/tmp/workspace",
            )
        )

    def _seed_checkpoint(self, *, checkpoint_id: str, task_id: str) -> None:
        self.store.save_pending_checkpoint(
            PendingCheckpoint(
                checkpoint_id=checkpoint_id,
                thread_id="th1",
                task_id=task_id,
                checkpoint_type=PendingCheckpointType.APPROVAL,
                title="confirm",
                prompt="approve to proceed",
                status=PendingCheckpointStatus.PENDING,
            )
        )

    def test_literal_approval_resolves_to_parent_goal(self) -> None:
        self._seed_parent(task_id="t_parent", goal="写一份上海三天旅游行程")
        self._seed_checkpoint(checkpoint_id="ckpt_a", task_id="t_parent")

        resolved_goal, resolved_meta = self._gateway()._resolve_goal_from_approval(
            "立即生成",
            {"approved_checkpoint_id": "ckpt_a"},
        )

        self.assertEqual(resolved_goal, "写一份上海三天旅游行程")
        self.assertEqual(resolved_meta.get("user_message"), "立即生成")
        self.assertEqual(resolved_meta.get("parent_task_id"), "t_parent")
        self.assertEqual(resolved_meta.get("original_goal"), "写一份上海三天旅游行程")

    def test_missing_checkpoint_preserves_literal_goal(self) -> None:
        resolved_goal, resolved_meta = self._gateway()._resolve_goal_from_approval(
            "立即生成",
            {"approved_checkpoint_id": "ckpt_missing"},
        )
        self.assertEqual(resolved_goal, "立即生成")
        self.assertNotIn("parent_task_id", resolved_meta)

    def test_missing_parent_task_preserves_literal_goal(self) -> None:
        self._seed_checkpoint(checkpoint_id="ckpt_orphan", task_id="t_ghost")
        resolved_goal, _ = self._gateway()._resolve_goal_from_approval(
            "立即生成",
            {"approved_checkpoint_id": "ckpt_orphan"},
        )
        self.assertEqual(resolved_goal, "立即生成")

    def test_no_checkpoint_id_is_noop(self) -> None:
        resolved_goal, resolved_meta = self._gateway()._resolve_goal_from_approval(
            "写文件",
            {"thread_id": "th1"},
        )
        self.assertEqual(resolved_goal, "写文件")
        self.assertEqual(resolved_meta, {"thread_id": "th1"})


# ---------------------------------------------------------------------------
# ISSUE-013 — recall_for_planning dedup across sources
# ---------------------------------------------------------------------------


class HybridWorkflowDedupTest(unittest.TestCase):
    """File + graph workflow recall with the same task_id must merge."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.memory = FileMemoryManager(Path(self.tempdir.name) / "workspace")
        self.graph = MagicMock()
        self.graph.recommend_failure_patterns.return_value = []
        self.graph.recommend_skills_for_goal.return_value = []
        self.retriever = HybridMemoryRetriever(self.memory, self.graph)

    def test_same_task_id_across_sources_merges_once(self) -> None:
        self.memory.recall_workflows = MagicMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "task_id": "t_same",
                    "goal": "写 markdown",
                    "plan_summary": "file-side summary",
                    "result_message": "ok",
                    "score": 0.62,
                }
            ]
        )
        self.memory.recall_vector = MagicMock(return_value=[])  # type: ignore[method-assign]
        self.memory.recall_failure_patterns = MagicMock(return_value=[])  # type: ignore[method-assign]
        self.graph.recommend_workflows.return_value = [
            {
                "task_id": "t_same",
                "entity_key": "workflow:t_same",
                "goal": "写 markdown",
                "summary": "graph-side summary",
                "score": 0.71,
                "source": "graph",
                "explanation": {"type": "graph_workflow", "reason": "stub"},
            }
        ]

        self.retriever.begin_turn("turn-x")
        result = self.retriever.recall_for_planning("写 markdown", limit=5)

        self.assertEqual(len(result["workflow_matches"]), 1)
        merged = result["workflow_matches"][0]
        self.assertEqual(merged["task_id"], "t_same")
        self.assertEqual(sorted(merged["sources"]), ["file", "graph"])
        # Fusion keeps the stronger score.
        self.assertEqual(merged["score"], 0.71)
        self.assertEqual(
            merged["explanation"]["type"],
            "hybrid_workflow",
            "merged entries should be re-labelled as hybrid",
        )


if __name__ == "__main__":
    unittest.main()

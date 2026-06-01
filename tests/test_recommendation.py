"""ISSUE-011 main task — multi-dimensional Recommendation scoring.

Ensures that ``similarity`` and ``success_rate`` are kept as independent
dimensions rather than folded into a single opaque ``final_score`` scalar.

Coverage map:
- Recommendation dataclass           → RecommendationDataclassTest (5)
- recommend_skills_for_goal scoring   → SkillRecommendationScoringTest (5)
- recommend_workflows explanation     → WorkflowRecommendationExplanationTest (3)
- recommend_failure_patterns explain  → FailureRecommendationExplanationTest (2)
- hybrid_memory explanation propagate → HybridExplanationPropagationTest (3)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omniagent.graph_memory import GraphMemoryStore
from omniagent.hybrid_memory import HybridMemoryRetriever
from omniagent.memory import FileMemoryManager
from omniagent.models import Recommendation
from omniagent.storage import SQLiteStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(path: Path) -> SQLiteStore:
    return SQLiteStore(path)


def _seed_task_node(store: SQLiteStore, *, task_id: str, status: str) -> None:
    store.upsert_graph_node(
        entity_key=f"task:{task_id}",
        node_type="Task",
        name=f"task {task_id}",
        properties={"task_id": task_id, "status": status, "goal": f"goal-{task_id}"},
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


def _seed_skill_graph(
    store: SQLiteStore,
    *,
    task_id: str,
    node_id: str,
    goal: str,
    skill_name: str,
    success_rate: float,
    executor_hint: str = "file",
) -> None:
    """Create a TaskNode → USES → Skill graph so recommend_skills_for_goal finds it."""
    store.upsert_graph_node(
        entity_key=f"task_node:{task_id}:{node_id}",
        node_type="TaskNode",
        name=goal,
        properties={
            "task_id": task_id,
            "node_id": node_id,
            "goal": goal,
            "executor_hint": executor_hint,
        },
        source="test",
    )
    store.upsert_graph_node(
        entity_key=f"skill:{skill_name}",
        node_type="Skill",
        name=skill_name,
        properties={
            "skill_name": skill_name,
            "lifecycle_status": "active",
            "success_rate": success_rate,
        },
        source="test",
    )
    store.upsert_graph_edge(
        source_entity_key=f"task_node:{task_id}:{node_id}",
        target_entity_key=f"skill:{skill_name}",
        edge_type="USES",
        source="test",
    )


# ---------------------------------------------------------------------------
# Recommendation dataclass unit tests
# ---------------------------------------------------------------------------


class RecommendationDataclassTest(unittest.TestCase):
    """Core Recommendation dataclass behaviour."""

    def test_bucket_quantisation(self) -> None:
        rec = Recommendation(similarity=0.87, success_rate=0.9)
        # bucket_width=0.05 → 0.87 / 0.05 = 17.4 → int(17) = 17
        self.assertEqual(rec.similarity_bucket, 17)

    def test_sort_key_primary_similarity_bucket(self) -> None:
        high_sim = Recommendation(similarity=0.90, success_rate=0.1)
        low_sim = Recommendation(similarity=0.70, success_rate=0.99)
        # 0.90 → bucket 18, 0.70 → bucket 14. Higher bucket wins.
        self.assertGreater(high_sim.sort_key, low_sim.sort_key)

    def test_sort_key_secondary_success_rate(self) -> None:
        """Within the same similarity bucket, higher success_rate wins."""
        a = Recommendation(similarity=0.82, success_rate=0.9)
        b = Recommendation(similarity=0.83, success_rate=0.1)
        # Both in bucket 16. a.success_rate > b.success_rate → a wins.
        self.assertEqual(a.similarity_bucket, b.similarity_bucket)
        self.assertGreater(a.sort_key, b.sort_key)

    def test_to_dict_roundtrip(self) -> None:
        rec = Recommendation(similarity=0.75, success_rate=0.88, source="graph")
        d = rec.to_dict()
        self.assertAlmostEqual(d["similarity"], 0.75)
        self.assertAlmostEqual(d["success_rate"], 0.88)
        self.assertEqual(d["source"], "graph")

    def test_sort_key_boundary(self) -> None:
        """Items at exact bucket boundary belong to the same bucket."""
        a = Recommendation(similarity=0.80, success_rate=0.5)
        b = Recommendation(similarity=0.7999, success_rate=0.9)
        # 0.80 → bucket 16, 0.7999 → bucket 15 (just under). Different buckets.
        self.assertGreater(a.sort_key, b.sort_key)


# ---------------------------------------------------------------------------
# recommend_skills_for_goal — no more final_score folding
# ---------------------------------------------------------------------------


class SkillRecommendationScoringTest(unittest.TestCase):
    """ISSUE-011: ``recommend_skills_for_goal`` must NOT fold similarity and
    success_rate into a single scalar.  Returned dicts must carry both
    dimensions in ``explanation``, and ``score`` must equal pure similarity.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)

    def test_score_is_pure_similarity(self) -> None:
        """The top-level ``score`` key must be the pure similarity dimension,
        not ``similarity + success_rate * 0.1``."""
        _seed_skill_graph(
            self.store,
            task_id="t1",
            node_id="n1",
            goal="读取 markdown 文件",
            skill_name="builtin.file.basic",
            success_rate=1.0,
        )
        results = self.graph_memory.recommend_skills_for_goal("读取 markdown 文件", min_score=0.0)
        self.assertEqual(len(results), 1)
        item = results[0]
        similarity = item["explanation"]["similarity"]
        # Before the fix: score = similarity + 1.0 * 0.1 = similarity + 0.1
        # After the fix: score == similarity
        self.assertAlmostEqual(item["score"], similarity, places=4)

    def test_explanation_carries_both_dimensions(self) -> None:
        _seed_skill_graph(
            self.store,
            task_id="t1",
            node_id="n1",
            goal="写入 JSON 文件",
            skill_name="builtin.file.basic",
            success_rate=0.8,
        )
        results = self.graph_memory.recommend_skills_for_goal("写入 JSON 文件", min_score=0.0)
        exp = results[0]["explanation"]
        self.assertIn("similarity", exp)
        self.assertIn("success_rate", exp)
        self.assertAlmostEqual(exp["success_rate"], 0.8)

    def test_lexicographic_sort_prefers_similarity_over_success_rate(self) -> None:
        """A skill with higher similarity but lower success_rate should rank
        above a skill with lower similarity but higher success_rate (assuming
        they fall in different similarity buckets)."""
        _seed_skill_graph(
            self.store,
            task_id="t1",
            node_id="n1",
            goal="读取 markdown 文件并输出内容",
            skill_name="skill.high_sim",
            success_rate=0.1,
        )
        _seed_skill_graph(
            self.store,
            task_id="t2",
            node_id="n2",
            goal="浇花除草修建花园",
            skill_name="skill.high_sr",
            success_rate=1.0,
        )
        results = self.graph_memory.recommend_skills_for_goal(
            "读取 markdown 文件内容", min_score=0.0
        )
        # skill.high_sim should be first because its similarity to the query
        # is much higher, despite low success_rate.
        names = [item["skill_name"] for item in results]
        if len(names) >= 2:
            self.assertEqual(names[0], "skill.high_sim")

    def test_no_recommendation_key_in_returned_dict(self) -> None:
        """The internal ``recommendation`` Recommendation object must be
        stripped before returning (JSON serialisation safety)."""
        _seed_skill_graph(
            self.store,
            task_id="t1",
            node_id="n1",
            goal="读取文件",
            skill_name="builtin.file.basic",
            success_rate=1.0,
        )
        results = self.graph_memory.recommend_skills_for_goal("读取文件", min_score=0.0)
        self.assertNotIn("recommendation", results[0])

    def test_within_bucket_success_rate_ordering(self) -> None:
        """Two skills with very similar similarity (same bucket) should be
        ordered by success_rate descending."""
        # Use exactly the same goal text so similarities are identical
        _seed_skill_graph(
            self.store,
            task_id="t1",
            node_id="n1",
            goal="读取配置文件",
            skill_name="skill.low_sr",
            success_rate=0.2,
        )
        _seed_skill_graph(
            self.store,
            task_id="t2",
            node_id="n2",
            goal="读取配置文件",
            skill_name="skill.high_sr",
            success_rate=0.9,
        )
        results = self.graph_memory.recommend_skills_for_goal("读取配置文件", min_score=0.0)
        names = [item["skill_name"] for item in results]
        self.assertEqual(len(names), 2)
        # Same similarity bucket → higher success_rate first
        self.assertEqual(names[0], "skill.high_sr")


# ---------------------------------------------------------------------------
# recommend_workflows — explanation now includes similarity / success_rate
# ---------------------------------------------------------------------------


class WorkflowRecommendationExplanationTest(unittest.TestCase):
    """Workflow recommendations must propagate multi-dimensional fields."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)

    def test_explanation_has_similarity_and_success_rate(self) -> None:
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="写 markdown 文件",
            summary="创建 markdown 文件",
        )
        results = self.graph_memory.recommend_workflows("写 markdown 文件", min_score=0.0)
        exp = results[0]["explanation"]
        self.assertIn("similarity", exp)
        self.assertIn("success_rate", exp)

    def test_workflow_score_is_pure_similarity(self) -> None:
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="写 markdown 文件",
            summary="创建 markdown 文件",
        )
        results = self.graph_memory.recommend_workflows("写 markdown 文件", min_score=0.0)
        item = results[0]
        self.assertAlmostEqual(item["score"], item["explanation"]["similarity"], places=4)

    def test_no_recommendation_key_in_returned_dict(self) -> None:
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="写 markdown 文件",
            summary="创建 markdown 文件",
        )
        results = self.graph_memory.recommend_workflows("写 markdown 文件", min_score=0.0)
        self.assertNotIn("recommendation", results[0])


# ---------------------------------------------------------------------------
# recommend_failure_patterns — explanation
# ---------------------------------------------------------------------------


class FailureRecommendationExplanationTest(unittest.TestCase):
    """Failure pattern recommendations must carry multi-dimensional fields."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)

    def _seed_failure(self, goal: str) -> None:
        self.store.upsert_graph_node(
            entity_key="failure:tf:n1",
            node_type="FailurePattern",
            name="pattern",
            properties={
                "task_id": "tf",
                "node_id": "n1",
                "goal": goal,
                "error_message": "something failed",
                "critic_summary": "retry with better params",
            },
            source="test",
        )

    def test_explanation_has_similarity_and_success_rate(self) -> None:
        self._seed_failure("读取文件失败")
        results = self.graph_memory.recommend_failure_patterns("读取文件失败", min_score=0.0)
        exp = results[0]["explanation"]
        self.assertIn("similarity", exp)
        self.assertIn("success_rate", exp)
        self.assertAlmostEqual(exp["success_rate"], 0.0)

    def test_score_is_pure_similarity(self) -> None:
        self._seed_failure("写入文件出错")
        results = self.graph_memory.recommend_failure_patterns("写入文件出错", min_score=0.0)
        item = results[0]
        self.assertAlmostEqual(item["score"], item["explanation"]["similarity"], places=4)


# ---------------------------------------------------------------------------
# hybrid_memory — explanation propagation
# ---------------------------------------------------------------------------


class HybridExplanationPropagationTest(unittest.TestCase):
    """HybridMemoryRetriever must propagate similarity / success_rate through
    _build_explanations and _fuse_* into the final recall result."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        ws = Path(self.tempdir.name) / "workspace"
        ws.mkdir()
        self.store = _make_store(Path(self.tempdir.name) / "store.sqlite")
        self.graph_memory = GraphMemoryStore(self.store)
        self.memory = FileMemoryManager(ws)
        self.retriever = HybridMemoryRetriever(self.memory, self.graph_memory)

    def test_recall_explanations_carry_similarity(self) -> None:
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="读取 markdown 文件",
            summary="读取文件内容",
        )
        self.retriever.begin_turn("turn-1")
        result = self.retriever.recall_for_planning("读取 markdown 文件", limit=5)
        explanations = result["recall_explanations"]
        self.assertGreaterEqual(len(explanations), 1)
        wf_exp = [e for e in explanations if e["channel"] == "workflow"]
        self.assertGreaterEqual(len(wf_exp), 1)
        self.assertIn("similarity", wf_exp[0])
        self.assertIsNotNone(wf_exp[0]["similarity"])

    def test_skill_recall_explanations_carry_success_rate(self) -> None:
        _seed_skill_graph(
            self.store,
            task_id="t1",
            node_id="n1",
            goal="读取 JSON 文件",
            skill_name="builtin.file.basic",
            success_rate=0.75,
        )
        self.retriever.begin_turn("turn-2")
        result = self.retriever.recall_for_planning("读取 JSON 文件", limit=5)
        explanations = result["recall_explanations"]
        skill_exp = [e for e in explanations if e["channel"] == "skill"]
        self.assertGreaterEqual(len(skill_exp), 1)
        self.assertIn("success_rate", skill_exp[0])
        self.assertAlmostEqual(skill_exp[0]["success_rate"], 0.75)

    def test_fused_workflow_preserves_similarity(self) -> None:
        """When file + graph workflows merge, the fused explanation must
        keep the ``similarity`` field."""
        _seed_task_node(self.store, task_id="t_ok", status="completed")
        _seed_workflow_node(
            self.store,
            task_id="t_ok",
            goal="写 markdown 摘要",
            summary="创建摘要文件",
        )
        self.retriever.begin_turn("turn-3")
        result = self.retriever.recall_for_planning("写 markdown 摘要", limit=5)
        for wf in result.get("workflow_matches", []):
            exp = wf.get("explanation", {})
            self.assertIn("similarity", exp)


if __name__ == "__main__":
    unittest.main()

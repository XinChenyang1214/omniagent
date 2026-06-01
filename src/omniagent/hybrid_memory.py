from __future__ import annotations

from typing import Any, Callable, TypeVar

from omniagent.graph_memory import GraphMemoryStore
from omniagent.memory import FileMemoryManager

T = TypeVar("T")


def _status_token(value: Any) -> str:
    return str(value or "").strip().lower().split(".")[-1]


def _is_completed_status(value: Any) -> bool:
    return _status_token(value) == "completed"


def _task_key(item: dict[str, Any]) -> str | None:
    task_id = item.get("task_id")
    if not task_id and isinstance(item.get("metadata"), dict):
        task_id = item["metadata"].get("task_id")
    if not task_id:
        task_id = item.get("entry_id")
    if not task_id:
        return None
    return str(task_id)


class HybridMemoryRetriever:
    def __init__(self, memory_manager: FileMemoryManager, graph_memory: GraphMemoryStore) -> None:
        self.memory_manager = memory_manager
        self.graph_memory = graph_memory
        # META-006 重灾区 A+C dedupe: router pre-pass (recall_for_thread)
        # and planner pre-pass (recall_for_planning) used to independently
        # rescan the same graph tables for the same query within a single
        # user turn. The per-turn cache below lets the second caller reuse
        # the first caller's scan result — bounded scope (cleared at the
        # start of each send_message), so invalidation is trivial.
        self._turn_key: str | None = None
        self._turn_cache: dict[tuple[Any, ...], Any] = {}

    def begin_turn(self, turn_key: str | None) -> None:
        """Reset the per-turn recall cache.

        Call this at the start of each user-facing turn (e.g. Gateway
        ``send_message``). Passing ``None`` forces a full invalidation.
        Passing the same key twice is a no-op.
        """
        if turn_key != self._turn_key:
            self._turn_key = turn_key
            self._turn_cache = {}

    def _memoize(self, key: tuple[Any, ...], factory: Callable[[], T]) -> T:
        if key in self._turn_cache:
            return self._turn_cache[key]
        value = factory()
        self._turn_cache[key] = value
        return value

    def recall_for_planning(self, goal: str, *, limit: int = 5) -> dict[str, Any]:
        file_workflows = self._memoize(
            ("file_workflows", goal, limit),
            lambda: self.memory_manager.recall_workflows(goal, limit=limit),
        )
        raw_vector_matches = self._memoize(
            ("vector", goal, limit),
            lambda: self.memory_manager.recall_vector(goal, limit=limit),
        )
        file_failures = self._memoize(
            ("file_failures", goal, limit),
            lambda: self.memory_manager.recall_failure_patterns(goal, limit=limit),
        )
        graph_workflows = self._memoize(
            ("graph_workflows", goal, limit),
            lambda: self.graph_memory.recommend_workflows(goal, limit=limit),
        )
        graph_skills = self._memoize(
            ("graph_skills", goal, None, limit),
            lambda: self.graph_memory.recommend_skills_for_goal(goal, limit=limit),
        )
        graph_failures = self._memoize(
            ("graph_failures", goal, limit),
            lambda: self.graph_memory.recommend_failure_patterns(goal, limit=limit),
        )

        workflow_matches = self._fuse_workflows(file_workflows, graph_workflows, limit=limit)
        vector_matches = self._filter_vector_matches(
            raw_vector_matches,
            workflow_matches=workflow_matches,
            limit=limit,
        )
        failure_patterns = self._fuse_failures(file_failures, graph_failures, limit=limit)
        explanations = self._build_explanations(
            workflow_matches, vector_matches, graph_skills, failure_patterns
        )
        return {
            "workflow_matches": workflow_matches,
            "vector_matches": vector_matches,
            "failure_patterns": failure_patterns,
            "graph_workflow_matches": graph_workflows,
            "graph_skill_matches": graph_skills,
            "graph_failure_patterns": graph_failures,
            "recall_explanations": explanations,
        }

    def recall_for_thread(
        self,
        *,
        thread_id: str,
        message: str,
        active_task_goal: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        query = active_task_goal or message
        graph_workflows = self._memoize(
            ("graph_workflows", query, limit),
            lambda: self.graph_memory.recommend_workflows(query, limit=limit),
        )
        graph_skills = self._memoize(
            ("graph_skills", query, None, limit),
            lambda: self.graph_memory.recommend_skills_for_goal(query, limit=limit),
        )
        thread_context = self._memoize(
            ("thread_context", thread_id, limit),
            lambda: self.graph_memory.thread_context(thread_id, limit=limit),
        )
        explanations = self._build_explanations(graph_workflows, [], graph_skills, [])
        return {
            "thread_context": thread_context,
            "graph_workflow_matches": graph_workflows,
            "graph_skill_matches": graph_skills,
            "recall_explanations": explanations,
        }

    def recall_for_critic(self, goal: str, *, limit: int = 5) -> dict[str, Any]:
        file_failures = self._memoize(
            ("file_failures", goal, limit),
            lambda: self.memory_manager.recall_failure_patterns(goal, limit=limit),
        )
        graph_failures = self._memoize(
            ("graph_failures", goal, limit),
            lambda: self.graph_memory.recommend_failure_patterns(goal, limit=limit),
        )
        file_workflows = self._memoize(
            ("file_workflows", goal, limit),
            lambda: self.memory_manager.recall_workflows(goal, limit=limit),
        )
        graph_workflows = self._memoize(
            ("graph_workflows", goal, limit),
            lambda: self.graph_memory.recommend_workflows(goal, limit=limit),
        )
        workflow_matches = self._fuse_workflows(file_workflows, graph_workflows, limit=limit)
        failure_patterns = self._fuse_failures(file_failures, graph_failures, limit=limit)
        explanations = self._build_explanations(workflow_matches, [], [], failure_patterns)
        return {
            "workflow_matches": workflow_matches,
            "failure_patterns": failure_patterns,
            "graph_failure_patterns": graph_failures,
            "recall_explanations": explanations,
        }

    def _fuse_workflows(
        self,
        file_workflows: list[dict[str, Any]],
        graph_workflows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for item in file_workflows:
            key = str(item.get("task_id") or item.get("goal"))
            file_score = round(float(item.get("score", 0.55)), 4)
            merged[key] = {
                "task_id": item.get("task_id"),
                "goal": item.get("goal"),
                "summary": item.get("plan_summary"),
                "result_message": item.get("result_message"),
                "score": file_score,
                "sources": ["file"],
                "explanation": {
                    "type": "file_workflow",
                    "reason": "文件型 workflow 摘要匹配当前目标",
                    "similarity": file_score,
                    "success_rate": 1.0,
                },
            }
        for item in graph_workflows:
            key = str(item.get("task_id") or item.get("entity_key"))
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    "task_id": item.get("task_id"),
                    "goal": item.get("goal"),
                    "summary": item.get("summary"),
                    "result_message": None,
                    "score": float(item.get("score", 0)),
                    "sources": ["graph"],
                    "entity_key": item.get("entity_key"),
                    "explanation": item.get("explanation"),
                }
                continue
            existing["score"] = round(max(float(existing["score"]), float(item.get("score", 0))), 4)
            existing["sources"] = sorted(set(existing["sources"] + ["graph"]))
            existing["entity_key"] = item.get("entity_key")
            # ISSUE-011: preserve multi-dimensional breakdown on merge
            graph_sim = float(item.get("explanation", {}).get("similarity", item.get("score", 0)))
            exist_sim = float(existing["explanation"].get("similarity", existing["score"]))
            existing["explanation"] = {
                "type": "hybrid_workflow",
                "reason": "file + graph 两类 workflow recall 同时命中",
                "similarity": round(max(graph_sim, exist_sim), 4),
                "success_rate": 1.0,
            }
        items = sorted(merged.values(), key=lambda item: item["score"], reverse=True)
        return items[:limit]

    def _filter_vector_matches(
        self,
        vector_matches: list[dict[str, Any]],
        *,
        workflow_matches: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Keep vector recall as a supplemental channel.

        If the same task already appears as a positive workflow reference,
        passing it again through vector recall only duplicates context. Failed
        task vectors are also excluded from the positive vector channel; they
        should enter prompts through failure recall instead.
        """

        workflow_task_ids = {
            task_id
            for task_id in (_task_key(item) for item in workflow_matches)
            if task_id is not None
        }
        seen: set[str] = set()
        filtered: list[dict[str, Any]] = []
        for item in vector_matches:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            status = metadata.get("status") or item.get("status")
            if status is not None and not _is_completed_status(status):
                continue
            key = _task_key(item)
            if key is not None:
                if key in workflow_task_ids or key in seen:
                    continue
                seen.add(key)
            filtered.append(item)
            if len(filtered) >= limit:
                break
        return filtered

    def _fuse_failures(
        self,
        file_failures: list[dict[str, Any]],
        graph_failures: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for item in file_failures:
            key = f"{item.get('task_id')}:{item.get('node_id')}"
            file_score = round(float(item.get("score", 0.55)), 4)
            merged[key] = {
                "task_id": item.get("task_id"),
                "node_id": item.get("node_id"),
                "goal": item.get("goal"),
                "error_message": item.get("error_message"),
                "critic_action": item.get("critic_action"),
                "critic_summary": item.get("critic_summary"),
                "score": file_score,
                "sources": ["file"],
                "explanation": {
                    "type": "file_failure",
                    "reason": "文件型失败模式与当前目标相关",
                    "similarity": file_score,
                    "success_rate": 0.0,
                },
            }
        for item in graph_failures:
            key = f"{item.get('task_id')}:{item.get('node_id')}"
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    "task_id": item.get("task_id"),
                    "node_id": item.get("node_id"),
                    "goal": item.get("goal"),
                    "error_message": item.get("error_message"),
                    "critic_action": item.get("critic_action"),
                    "critic_summary": item.get("critic_summary"),
                    "score": float(item.get("score", 0)),
                    "sources": ["graph"],
                    "entity_key": item.get("entity_key"),
                    "explanation": item.get("explanation"),
                }
                continue
            existing["score"] = round(max(float(existing["score"]), float(item.get("score", 0))), 4)
            existing["sources"] = sorted(set(existing["sources"] + ["graph"]))
            existing["entity_key"] = item.get("entity_key")
            graph_sim = float(item.get("explanation", {}).get("similarity", item.get("score", 0)))
            exist_sim = float(existing["explanation"].get("similarity", existing["score"]))
            existing["explanation"] = {
                "type": "hybrid_failure",
                "reason": "file + graph 两类失败模式同时命中",
                "similarity": round(max(graph_sim, exist_sim), 4),
                "success_rate": 0.0,
            }
        items = sorted(merged.values(), key=lambda item: item["score"], reverse=True)
        return items[:limit]

    def _build_explanations(
        self,
        workflow_matches: list[dict[str, Any]],
        vector_matches: list[dict[str, Any]],
        graph_skills: list[dict[str, Any]],
        failure_patterns: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        explanations: list[dict[str, Any]] = []
        for item in workflow_matches:
            exp = item.get("explanation", {})
            explanations.append(
                {
                    "channel": "workflow",
                    "score": item.get("score", 0),
                    "similarity": exp.get("similarity", item.get("score", 0)),
                    "success_rate": exp.get("success_rate"),
                    "reason": exp.get("reason"),
                    "sources": item.get("sources", ["graph"]),
                }
            )
        for item in vector_matches:
            explanations.append(
                {
                    "channel": "vector",
                    "score": item.get("score", 0),
                    "similarity": item.get("score", 0),
                    "success_rate": None,
                    "reason": "向量召回命中相似任务文本",
                    "sources": ["vector"],
                }
            )
        for item in graph_skills:
            exp = item.get("explanation", {})
            explanations.append(
                {
                    "channel": "skill",
                    "score": item.get("score", 0),
                    "similarity": exp.get("similarity", item.get("score", 0)),
                    "success_rate": exp.get("success_rate"),
                    "reason": exp.get("reason"),
                    "sources": ["graph"],
                }
            )
        for item in failure_patterns:
            exp = item.get("explanation", {})
            explanations.append(
                {
                    "channel": "failure",
                    "score": item.get("score", 0),
                    "similarity": exp.get("similarity", item.get("score", 0)),
                    "success_rate": exp.get("success_rate"),
                    "reason": exp.get("reason"),
                    "sources": item.get("sources", ["graph"]),
                }
            )
        return explanations

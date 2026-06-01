from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omniagent.models import CriticVerdict, ExecutionResult, PlanResult, Task, TaskNode, utc_now
from omniagent.vector_recall import SemanticScorer, VectorRecallEngine


def _safe_name(value: str) -> str:
    return value.replace("/", "_").replace(".", "_").replace(":", "_")


_VECTOR_MATCH_TEXT_LIMIT = 2000
_VECTOR_PROMPT_FIELDS = {
    "entry_id",
    "text",
    "source_type",
    "metadata",
    "created_at",
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "embedding_version",
    "embedding_fallback_used",
    "embedding_requested_provider",
    "embedding_error",
}


def _status_token(value: Any) -> str:
    return str(value or "").strip().lower().split(".")[-1]


def _is_completed_status(value: Any) -> bool:
    return _status_token(value) == "completed"


def _clip_text(value: Any, *, limit: int = _VECTOR_MATCH_TEXT_LIMIT) -> Any:
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + "...<truncated>"


def _sanitize_vector_match(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the prompt-safe vector recall shape.

    Vector files are storage records and intentionally contain the raw
    embedding. Planner/Critic prompts only need the textual evidence and
    scoring metadata; including hundreds of floats per match has no planning
    value and made real prompts balloon by tens of thousands of tokens.
    """

    sanitized: dict[str, Any] = {}
    for key in _VECTOR_PROMPT_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "text":
            value = _clip_text(value)
        sanitized[key] = value
    return sanitized


class FileMemoryManager:
    def __init__(
        self,
        workspace_root: Path,
        *,
        vector_engine: VectorRecallEngine | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.memory_root = self.workspace_root / "MEMORY"
        self.workflow_root = self.workspace_root / "WORKFLOWS"
        self.vector_root = self.memory_root / "vector"
        self.failure_root = self.memory_root / "failures"
        self.vector_engine = vector_engine or VectorRecallEngine()
        for path in [self.memory_root, self.workflow_root, self.vector_root, self.failure_root]:
            path.mkdir(parents=True, exist_ok=True)

    def recall(self, goal: str, *, limit: int = 5) -> list[dict[str, Any]]:
        # ISSUE-013: successful tasks are written to BOTH short_term/ and
        # long_term/ (see ``write_task_memory`` below), so without dedup the
        # same task_id shows up twice in ``memory_summaries``. Dedup by
        # task_id (preferring the highest score) and merge sources so the
        # Planner context sees each historical task once.
        merged: dict[str, tuple[float, dict[str, Any], list[str]]] = {}
        seen_paths: list[tuple[Path, str]] = []
        for file_path in (self.memory_root / "short_term").glob("*.json"):
            seen_paths.append((file_path, "short_term"))
        for file_path in (self.memory_root / "long_term").glob("*.json"):
            seen_paths.append((file_path, "long_term"))

        # META-006: amortize the query-side feature build across every
        # candidate file instead of rebuilding it per call.
        scorer = SemanticScorer(goal)
        for file_path, tier in seen_paths:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            text = json.dumps(payload, ensure_ascii=False)
            score = scorer.score(text)
            key = str(payload.get("task_id") or file_path.stem)
            existing = merged.get(key)
            if existing is None:
                merged[key] = (score, payload, [tier])
                continue
            prev_score, prev_payload, prev_sources = existing
            updated_sources = prev_sources if tier in prev_sources else prev_sources + [tier]
            if score > prev_score:
                merged[key] = (score, payload, updated_sources)
            else:
                merged[key] = (prev_score, prev_payload, updated_sources)

        annotated: list[tuple[float, dict[str, Any]]] = []
        for score, payload, sources in merged.values():
            enriched = dict(payload)
            enriched["memory_tiers"] = sources
            annotated.append((score, enriched))

        annotated.sort(key=lambda item: (item[0], item[1].get("created_at", "")), reverse=True)
        return [payload for _, payload in annotated[:limit]]

    def recall_workflows(self, goal: str, *, limit: int = 5) -> list[dict[str, Any]]:
        # META-006: SemanticScorer amortizes the query feature build across
        # every workflow file (previously rebuilt per call).
        scorer = SemanticScorer(goal)
        items: list[tuple[float, dict[str, Any]]] = []
        for file_path in self.workflow_root.glob("*.json"):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            if payload.get("status") is not None and not _is_completed_status(
                payload.get("status")
            ):
                continue
            text = f"{payload.get('goal', '')}\n{payload.get('plan_summary', '')}\n{payload.get('result_message', '')}"
            items.append((scorer.score(text), payload))
        items.sort(key=lambda item: (item[0], item[1].get("created_at", "")), reverse=True)
        return [payload for _, payload in items[:limit]]

    def recall_failure_patterns(self, goal: str, *, limit: int = 5) -> list[dict[str, Any]]:
        # META-006: same SemanticScorer amortization as recall_workflows.
        scorer = SemanticScorer(goal)
        items: list[tuple[float, dict[str, Any]]] = []
        for file_path in self.failure_root.glob("*.json"):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            text = f"{payload.get('goal', '')}\n{payload.get('node_goal', '')}\n{payload.get('error_message', '')}"
            items.append((scorer.score(text), payload))
        items.sort(key=lambda item: (item[0], item[1].get("created_at", "")), reverse=True)
        return [payload for _, payload in items[:limit]]

    def recall_vector(self, goal: str, *, limit: int = 5) -> list[dict[str, Any]]:
        items: list[tuple[float, dict[str, Any]]] = []
        query_result = self.vector_engine.build_query(goal)
        for file_path in self.vector_root.glob("*.json"):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            score, details = self.vector_engine.score(
                goal,
                payload,
                query_result=query_result,
            )
            if score <= 0:
                continue
            match = _sanitize_vector_match(payload)
            match["score"] = round(score, 4)
            match["match_details"] = details
            items.append((score, match))
        items.sort(key=lambda item: (item[0], item[1].get("created_at", "")), reverse=True)
        return [payload for _, payload in items[:limit]]

    def write_task_memory(
        self,
        task: Task,
        result: ExecutionResult,
        *,
        capability_name: str,
    ) -> Path:
        payload = {
            "task_id": task.task_id,
            "goal": task.goal,
            "status": task.status,
            "executor": task.executor,
            "capability": capability_name,
            "success": result.success,
            "message": result.message,
            "created_at": utc_now(),
        }
        short_term_path = self.memory_root / "short_term" / f"{task.task_id}.json"
        short_term_path.parent.mkdir(parents=True, exist_ok=True)
        short_term_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if result.success:
            long_term_path = self.memory_root / "long_term" / f"{task.task_id}.json"
            long_term_path.parent.mkdir(parents=True, exist_ok=True)
            long_term_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return short_term_path

    def write_workflow_summary(
        self, task: Task, result: ExecutionResult, *, plan: PlanResult | None = None
    ) -> Path:
        payload = {
            "task_id": task.task_id,
            "goal": task.goal,
            "executor": task.executor,
            "skill_name": task.skill_name,
            "status": task.status,
            "result_message": result.message,
            "plan_summary": plan.summary if plan else task.plan_summary,
            "nodes": [node.to_dict() for node in plan.graph.nodes] if plan else [],
            "created_at": utc_now(),
        }
        workflow_path = self.workflow_root / f"{task.task_id}.json"
        workflow_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        markdown_path = self.workflow_root / f"{task.task_id}.md"
        markdown_path.write_text(
            "\n".join(
                [
                    f"# Workflow Summary {task.task_id}",
                    "",
                    f"- goal: {task.goal}",
                    f"- executor: {task.executor}",
                    f"- plan_summary: {payload['plan_summary'] or 'none'}",
                    f"- result: {result.message}",
                ]
            ),
            encoding="utf-8",
        )
        return workflow_path

    def update_skill_stats(self, capability_name: str, *, success: bool) -> Path:
        stats_path = self.memory_root / "skills" / f"{_safe_name(capability_name)}.json"
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        if stats_path.exists():
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        else:
            stats = {"capability": capability_name, "runs": 0, "successes": 0, "success_rate": 0.0}
        stats["runs"] += 1
        stats["successes"] += 1 if success else 0
        stats["success_rate"] = round(stats["successes"] / stats["runs"], 3)
        stats["last_used_at"] = utc_now()
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return stats_path

    def write_vector_entry(
        self,
        *,
        entry_id: str,
        text: str,
        source_type: str,
        metadata: dict[str, Any],
    ) -> Path:
        payload = {
            "entry_id": entry_id,
            "text": text,
            "source_type": source_type,
            "metadata": metadata,
            "created_at": utc_now(),
            **self.vector_engine.build_entry(text),
        }
        path = self.vector_root / f"{entry_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_failure_pattern(
        self,
        *,
        task: Task,
        node: TaskNode,
        result: ExecutionResult,
        verdict: CriticVerdict,
    ) -> Path:
        payload = {
            "task_id": task.task_id,
            "goal": task.goal,
            "node_id": node.node_id,
            "node_goal": node.goal,
            "error_message": result.message,
            "critic_action": verdict.action,
            "critic_summary": verdict.summary,
            "created_at": utc_now(),
        }
        path = self.failure_root / f"{task.task_id}_{node.node_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

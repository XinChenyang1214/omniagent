from __future__ import annotations

import uuid
from typing import Any

from omniagent.models import (
    ConversationThread,
    ConversationTurn,
    CriticVerdict,
    PermissionDecision,
    Recommendation,
    Task,
    TaskGraph,
    TaskNode,
)
from omniagent.storage import SQLiteStore
from omniagent.vector_recall import SemanticScorer


class GraphMemoryStore:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def sync_bootstrap_documents(self, documents: dict[str, str]) -> None:
        for filename, content in documents.items():
            entity_key = f"document:{filename}"
            self.store.upsert_graph_node(
                entity_key=entity_key,
                node_type="Document",
                name=filename,
                properties={"content_preview": content[:1000], "filename": filename},
                source="workspace_bootstrap",
                tags=["bootstrap", "document"],
            )
            if filename in {"AGENT.md", "POLICY.md", "USER.md"}:
                node_type = "Preference" if filename == "USER.md" else "Rule"
                for index, line in enumerate(content.splitlines(), start=1):
                    normalized = line.strip()
                    if not normalized.startswith("-"):
                        continue
                    rule_key = f"{node_type.lower()}:{filename}:{index}"
                    self.store.upsert_graph_node(
                        entity_key=rule_key,
                        node_type=node_type,
                        name=f"{filename}:{index}",
                        properties={"text": normalized.lstrip("- ").strip(), "document": filename},
                        source="workspace_bootstrap",
                        tags=["bootstrap", node_type.lower()],
                    )
                    self.store.upsert_graph_edge(
                        source_entity_key=entity_key,
                        target_entity_key=rule_key,
                        edge_type="CONTAINS",
                        properties={"line": index},
                        source="workspace_bootstrap",
                    )

    def record_thread(self, thread: ConversationThread) -> None:
        self.store.upsert_graph_node(
            entity_key=f"thread:{thread.thread_id}",
            node_type="ConversationThread",
            name=thread.title,
            properties={
                "status": str(thread.status),
                "active_task_id": thread.active_task_id,
                "pending_checkpoint_id": thread.pending_checkpoint_id,
                "metadata": thread.metadata,
            },
            source="interaction",
            tags=["thread", "conversation"],
        )
        if thread.active_task_id:
            self.store.upsert_graph_edge(
                source_entity_key=f"thread:{thread.thread_id}",
                target_entity_key=f"task:{thread.active_task_id}",
                edge_type="USES",
                properties={"binding": "active_task"},
                source="interaction",
            )

    def record_turn(self, turn: ConversationTurn) -> None:
        entity_key = f"turn:{turn.turn_id}"
        self.store.upsert_graph_node(
            entity_key=entity_key,
            node_type="ConversationTurn",
            name=f"{turn.role}:{turn.turn_id}",
            properties={
                "thread_id": turn.thread_id,
                "role": str(turn.role),
                "message": turn.message,
                "input_type": str(turn.input_type) if turn.input_type else None,
                "route": turn.route,
                "task_id": turn.task_id,
                "checkpoint_id": turn.checkpoint_id,
                "payload": turn.payload,
            },
            source="interaction",
            tags=["turn", str(turn.role)],
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"thread:{turn.thread_id}",
            target_entity_key=entity_key,
            edge_type="CONTAINS",
            properties={"role": str(turn.role)},
            source="interaction",
        )
        if turn.task_id:
            self.store.upsert_graph_edge(
                source_entity_key=entity_key,
                target_entity_key=f"task:{turn.task_id}",
                edge_type="USES",
                properties={"route": turn.route},
                source="interaction",
            )

    def record_task(self, task: Task) -> None:
        self.store.upsert_graph_node(
            entity_key=f"task:{task.task_id}",
            node_type="Task",
            name=task.goal[:80],
            properties={
                "task_id": task.task_id,
                "goal": task.goal,
                "status": str(task.status),
                "executor": task.executor,
                "skill_name": task.skill_name,
                "summary": task.summary,
                "plan_summary": task.plan_summary,
                "error_code": task.error_code,
                "error_message": task.error_message,
                "metadata": task.metadata,
            },
            source="runtime",
            tags=["task", str(task.status)],
        )

    def record_workflow(self, task: Task, task_graph: TaskGraph) -> None:
        workflow_key = f"workflow:{task.task_id}"
        self.store.upsert_graph_node(
            entity_key=workflow_key,
            node_type="Workflow",
            name=task_graph.summary or task.goal[:80],
            properties={
                "task_id": task.task_id,
                "graph_id": task_graph.graph_id,
                "goal": task_graph.goal,
                "summary": task_graph.summary,
                "node_count": len(task_graph.nodes),
            },
            source="planner",
            tags=["workflow", "task_graph"],
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task:{task.task_id}",
            target_entity_key=workflow_key,
            edge_type="USES",
            properties={"graph_id": task_graph.graph_id},
            source="planner",
        )
        for node in task_graph.nodes:
            self.record_task_node(task.task_id, node)
            node_key = f"task_node:{task.task_id}:{node.node_id}"
            self.store.upsert_graph_edge(
                source_entity_key=workflow_key,
                target_entity_key=node_key,
                edge_type="CONTAINS",
                properties={},
                source="planner",
            )
            for dependency in node.dependencies:
                dependency_key = f"task_node:{task.task_id}:{dependency}"
                self.store.upsert_graph_edge(
                    source_entity_key=node_key,
                    target_entity_key=dependency_key,
                    edge_type="DEPENDS_ON",
                    properties={},
                    source="planner",
                )
            selected_skill = node.metadata.get("selected_skill") or node.skill_name
            if selected_skill:
                self.store.upsert_graph_edge(
                    source_entity_key=workflow_key,
                    target_entity_key=f"skill:{selected_skill}",
                    edge_type="USES",
                    properties={"node_id": node.node_id},
                    source="runtime",
                )

    def record_task_node(self, task_id: str, node: TaskNode) -> None:
        node_key = f"task_node:{task_id}:{node.node_id}"
        self.store.upsert_graph_node(
            entity_key=node_key,
            node_type="TaskNode",
            name=node.goal[:80],
            properties={
                "task_id": task_id,
                "node_id": node.node_id,
                "goal": node.goal,
                "type": str(node.type),
                "status": str(node.status),
                "executor_hint": node.executor_hint,
                "skill_name": node.skill_name,
                "required_skills": node.required_skills,
                "error_message": node.error_message,
                "metadata": node.metadata,
            },
            source="runtime",
            tags=["task_node", str(node.type), str(node.status)],
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task:{task_id}",
            target_entity_key=node_key,
            edge_type="CONTAINS",
            properties={},
            source="runtime",
        )
        selected_skill = node.metadata.get("selected_skill")
        if selected_skill:
            self.store.upsert_graph_edge(
                source_entity_key=node_key,
                target_entity_key=f"skill:{selected_skill}",
                edge_type="USES",
                properties={"selected": True},
                source="runtime",
            )

    def record_skill(self, entry: dict[str, Any]) -> None:
        skill_key = f"skill:{entry['skill_name']}"
        self.store.upsert_graph_node(
            entity_key=skill_key,
            node_type="Skill",
            name=entry["skill_name"],
            properties=entry,
            source="skill_registry",
            tags=["skill", entry.get("scope", "workspace")],
            score=float(entry.get("success_rate", 0.0)),
        )

    def record_workflow_candidate(self, candidate: dict[str, Any]) -> None:
        entity_key = f"skill_candidate:{candidate['skill_name']}"
        self.store.upsert_graph_node(
            entity_key=entity_key,
            node_type="SkillCandidate",
            name=candidate["skill_name"],
            properties=candidate,
            source="workflow_compiler",
            tags=["skill_candidate", candidate.get("status", "draft"), "workflow_compiled"],
            score=float(candidate.get("success_rate", 0.0)),
        )
        for task_id in candidate.get("source_task_ids", []):
            self.store.upsert_graph_edge(
                source_entity_key=f"task:{task_id}",
                target_entity_key=entity_key,
                edge_type="SUGGESTS",
                properties={"version": candidate.get("version"), "source": "workflow_compiler"},
                source="workflow_compiler",
            )
            self.store.upsert_graph_edge(
                source_entity_key=f"workflow:{task_id}",
                target_entity_key=entity_key,
                edge_type="OPTIMIZED_TO",
                properties={"version": candidate.get("version"), "status": candidate.get("status")},
                source="workflow_compiler",
            )

    def record_permission_decision(self, decision: PermissionDecision) -> None:
        entity_key = f"permission:{decision.task_id}:{decision.created_at}"
        self.store.upsert_graph_node(
            entity_key=entity_key,
            node_type="PermissionDecision",
            name=decision.permission,
            properties={
                "task_id": decision.task_id,
                "permission": decision.permission,
                "level": str(decision.level),
                "status": decision.status,
                "rationale": decision.rationale,
                "scope": decision.scope,
                "node_id": decision.node_id,
                "skill_name": decision.skill_name,
                "resource_kind": decision.resource_kind,
                "resource_ref": decision.resource_ref,
                "sandbox_level": decision.sandbox_level,
                "source": decision.source,
                "policy": decision.policy,
                "requires_approval": decision.requires_approval,
                "details": decision.details,
            },
            source="permission",
            tags=["permission", decision.status, decision.scope],
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task:{decision.task_id}",
            target_entity_key=entity_key,
            edge_type="REQUIRES_PERMISSION",
            properties={"status": decision.status},
            source="permission",
        )
        if decision.node_id:
            self.store.upsert_graph_edge(
                source_entity_key=f"task_node:{decision.task_id}:{decision.node_id}",
                target_entity_key=entity_key,
                edge_type="REQUIRES_PERMISSION",
                properties={"status": decision.status},
                source="permission",
            )
        if decision.skill_name:
            self.store.upsert_graph_edge(
                source_entity_key=entity_key,
                target_entity_key=f"skill:{decision.skill_name}",
                edge_type="USES",
                properties={"status": decision.status},
                source="permission",
            )

    def record_skill_signal(
        self,
        *,
        task: Task,
        node: TaskNode,
        signal: str,
        skill_name: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized_signal = str(signal or "unknown").strip() or "unknown"
        entity_key = f"skill_signal:{task.task_id}:{node.node_id}:{normalized_signal}:{uuid.uuid4().hex[:8]}"
        self.store.upsert_graph_node(
            entity_key=entity_key,
            node_type="SkillSignal",
            name=normalized_signal,
            properties={
                "task_id": task.task_id,
                "node_id": node.node_id,
                "goal": node.goal,
                "executor_hint": node.executor_hint,
                "skill_name": skill_name,
                "signal": normalized_signal,
                "message": message,
                "details": details or {},
            },
            source="skill_selection",
            tags=["skill_signal", normalized_signal],
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task:{task.task_id}",
            target_entity_key=entity_key,
            edge_type="OBSERVED_SKILL_SIGNAL",
            properties={"signal": normalized_signal},
            source="skill_selection",
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task_node:{task.task_id}:{node.node_id}",
            target_entity_key=entity_key,
            edge_type="OBSERVED_SKILL_SIGNAL",
            properties={"signal": normalized_signal},
            source="skill_selection",
        )
        if skill_name:
            self.store.upsert_graph_edge(
                source_entity_key=entity_key,
                target_entity_key=f"skill:{skill_name}",
                edge_type="ABOUT",
                properties={"signal": normalized_signal},
                source="skill_selection",
            )

    def record_failure_pattern(
        self,
        *,
        task: Task,
        node: TaskNode,
        verdict: CriticVerdict,
        error_message: str,
    ) -> None:
        entity_key = f"failure:{task.task_id}:{node.node_id}"
        self.store.upsert_graph_node(
            entity_key=entity_key,
            node_type="FailurePattern",
            name=node.goal[:80],
            properties={
                "task_id": task.task_id,
                "node_id": node.node_id,
                "goal": node.goal,
                "error_message": error_message,
                "critic_action": str(verdict.action),
                "critic_summary": verdict.summary,
            },
            source="critic",
            tags=["failure", str(verdict.action)],
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task:{task.task_id}",
            target_entity_key=entity_key,
            edge_type="FAILED_WITH",
            properties={"node_id": node.node_id},
            source="critic",
        )
        self.store.upsert_graph_edge(
            source_entity_key=f"task_node:{task.task_id}:{node.node_id}",
            target_entity_key=entity_key,
            edge_type="FAILED_WITH",
            properties={"node_id": node.node_id},
            source="critic",
        )
        if verdict.action != "accept":
            recovery_key = f"recovery:{task.task_id}:{node.node_id}"
            self.store.upsert_graph_node(
                entity_key=recovery_key,
                node_type="Observation",
                name=f"recovery:{node.node_id}",
                properties={
                    "task_id": task.task_id,
                    "node_id": node.node_id,
                    "action": str(verdict.action),
                    "summary": verdict.summary,
                    "downgrade_target": verdict.downgrade_target,
                    "replan_goal": verdict.replan_goal,
                },
                source="critic",
                tags=["recovery", str(verdict.action)],
            )
            self.store.upsert_graph_edge(
                source_entity_key=entity_key,
                target_entity_key=recovery_key,
                edge_type="RECOVERED_BY",
                properties={"action": str(verdict.action)},
                source="critic",
            )
            if verdict.downgrade_target:
                self.store.upsert_graph_edge(
                    source_entity_key=recovery_key,
                    target_entity_key=f"skill:{verdict.downgrade_target}",
                    edge_type="USES",
                    properties={},
                    source="critic",
                )

    def record_verdict(self, task: Task, verdict: CriticVerdict) -> None:
        if verdict.target_type != "node":
            return
        target_key = f"task_node:{task.task_id}:{verdict.target_id}"
        self.store.upsert_graph_edge(
            source_entity_key=f"task:{task.task_id}",
            target_entity_key=target_key,
            edge_type="DERIVED_FROM",
            properties={"critic_action": str(verdict.action), "summary": verdict.summary},
            source="critic",
        )

    def subgraph_for_task(self, task_id: str) -> dict[str, Any]:
        task_key = f"task:{task_id}"
        return {
            "root": self.store.get_graph_node(task_key),
            "neighbors": self.store.list_graph_neighbors(task_key, limit=100),
            "edges": self.store.list_graph_edges(task_key, direction="both", limit=100),
        }

    def subgraph_for_thread(self, thread_id: str) -> dict[str, Any]:
        thread_key = f"thread:{thread_id}"
        return {
            "root": self.store.get_graph_node(thread_key),
            "neighbors": self.store.list_graph_neighbors(thread_key, limit=100),
            "edges": self.store.list_graph_edges(thread_key, direction="both", limit=100),
        }

    def related_workflows(self, task_id: str) -> list[dict[str, Any]]:
        return self.store.list_graph_neighbors(
            f"task:{task_id}", edge_type="USES", node_type="Workflow", limit=20
        )

    def related_skills(self, skill_name: str) -> list[dict[str, Any]]:
        return self.store.list_graph_neighbors(f"skill:{skill_name}", limit=20)

    def related_failures(self, task_id: str) -> list[dict[str, Any]]:
        return self.store.list_graph_neighbors(
            f"task:{task_id}", edge_type="FAILED_WITH", node_type="FailurePattern", limit=20
        )

    # Default min_score thresholds for recommend_* functions (see ISSUE-010).
    # These are starting points — raise them once empirical data allows tuning.
    _DEFAULT_WORKFLOW_MIN_SCORE = 0.4
    _DEFAULT_FAILURE_MIN_SCORE = 0.55
    _DEFAULT_SKILL_MIN_SCORE = 0.5

    def _task_status_map(self) -> dict[str, str]:
        """Build a {task_id: status} lookup for all Task nodes in the graph.

        Used by ``recommend_workflows`` to filter out non-successful tasks
        before recommending them as positive references (see ISSUE-011 #1 in
        issue.md — a failed task like "讲一个冷笑话" must not be recommended
        as a positive workflow).
        """
        mapping: dict[str, str] = {}
        for node in self.store.list_graph_nodes(node_type="Task", limit=500):
            props = node["properties"]
            task_id = props.get("task_id")
            if task_id:
                mapping[str(task_id)] = str(props.get("status", ""))
        return mapping

    def recommend_workflows(
        self,
        goal: str,
        *,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        threshold = self._DEFAULT_WORKFLOW_MIN_SCORE if min_score is None else min_score
        task_status_map = self._task_status_map()
        scorer = SemanticScorer(goal)
        items: list[dict[str, Any]] = []
        for node in self.store.list_graph_nodes(node_type="Workflow", limit=500):
            properties = node["properties"]
            task_id = properties.get("task_id")
            # ISSUE-011 #1: hard-filter on task.status == "completed". Failed
            # workflows belong in recall_failure_patterns as negative signal,
            # not in workflow_matches as positive reference.
            if task_id and task_status_map.get(str(task_id)) != "completed":
                continue
            candidate_text = (
                f"{node['name']}\n{properties.get('goal', '')}\n{properties.get('summary', '')}"
            )
            score = scorer.score(candidate_text)
            # ISSUE-010: apply min_score threshold (was: score <= 0, which
            # treated 0.18 noise matches as positive hits).
            if score < threshold:
                continue
            rec = Recommendation(
                similarity=round(score, 4),
                success_rate=1.0,  # workflows are pre-filtered to completed tasks
                source="graph",
                explanation="workflow goal/summary 与当前目标相似",
            )
            items.append(
                {
                    "entity_key": node["entity_key"],
                    "task_id": task_id,
                    "graph_id": properties.get("graph_id"),
                    "goal": properties.get("goal"),
                    "summary": properties.get("summary"),
                    "score": round(score, 4),
                    "source": "graph",
                    "recommendation": rec,
                    "explanation": {
                        "type": "graph_workflow",
                        "reason": rec.explanation,
                        "similarity": rec.similarity,
                        "success_rate": rec.success_rate,
                    },
                }
            )
        items.sort(key=lambda item: item["recommendation"].sort_key, reverse=True)
        result = items[:limit]
        for item in result:
            item.pop("recommendation", None)
        return result

    def recommend_failure_patterns(
        self,
        goal: str,
        *,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        threshold = self._DEFAULT_FAILURE_MIN_SCORE if min_score is None else min_score
        scorer = SemanticScorer(goal)
        items: list[dict[str, Any]] = []
        for node in self.store.list_graph_nodes(node_type="FailurePattern", limit=500):
            properties = node["properties"]
            candidate_text = f"{properties.get('goal', '')}\n{properties.get('error_message', '')}\n{properties.get('critic_summary', '')}"
            score = scorer.score(candidate_text)
            # ISSUE-010: apply min_score threshold.
            if score < threshold:
                continue
            rec = Recommendation(
                similarity=round(score, 4),
                success_rate=0.0,  # failure patterns are inherently unsuccessful
                source="graph",
                explanation="历史失败模式与当前目标相似",
            )
            items.append(
                {
                    "entity_key": node["entity_key"],
                    "task_id": properties.get("task_id"),
                    "node_id": properties.get("node_id"),
                    "goal": properties.get("goal"),
                    "error_message": properties.get("error_message"),
                    "critic_action": properties.get("critic_action"),
                    "critic_summary": properties.get("critic_summary"),
                    "score": round(score, 4),
                    "source": "graph",
                    "recommendation": rec,
                    "explanation": {
                        "type": "graph_failure",
                        "reason": rec.explanation,
                        "similarity": rec.similarity,
                        "success_rate": rec.success_rate,
                    },
                }
            )
        items.sort(key=lambda item: item["recommendation"].sort_key, reverse=True)
        result = items[:limit]
        for item in result:
            item.pop("recommendation", None)
        return result

    def recommend_skills_for_goal(
        self,
        goal: str,
        *,
        executor_hint: str | None = None,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        threshold = self._DEFAULT_SKILL_MIN_SCORE if min_score is None else min_score
        scorer = SemanticScorer(goal)
        aggregated: dict[str, dict[str, Any]] = {}
        for node in self.store.list_graph_nodes(node_type="TaskNode", limit=800):
            properties = node["properties"]
            historical_executor = properties.get("executor_hint")
            if executor_hint and historical_executor and historical_executor != executor_hint:
                continue
            candidate_goal = str(properties.get("goal", node["name"]))
            score = scorer.score(candidate_goal)
            # ISSUE-010: apply min_score on the similarity dimension (NOT on
            # final_score which folds in success_rate — see ISSUE-011).
            if score < threshold:
                continue
            for neighbor in self.store.list_graph_neighbors(
                node["entity_key"], edge_type="USES", node_type="Skill", limit=10
            ):
                skill_node = neighbor["node"]
                skill_name = str(skill_node["properties"].get("skill_name", skill_node["name"]))
                if str(skill_node["properties"].get("lifecycle_status", "active")) != "active":
                    continue
                current = aggregated.get(skill_name)
                success_rate = float(skill_node["properties"].get("success_rate", 0.0))
                # ISSUE-011: no longer fold similarity + success_rate into a
                # single scalar.  Each dimension stays independent so callers
                # can threshold / explain them separately.
                rec = Recommendation(
                    similarity=round(score, 4),
                    success_rate=success_rate,
                    source="graph",
                    explanation="相似 TaskNode 历史上成功使用过该 Skill",
                )
                payload = {
                    "skill_name": skill_name,
                    "score": round(score, 4),  # back-compat: now pure similarity
                    "source": "graph",
                    "matched_task_node": node["entity_key"],
                    "matched_goal": candidate_goal,
                    "skill_entry": skill_node["properties"],
                    "recommendation": rec,
                    "explanation": {
                        "type": "graph_skill",
                        "reason": rec.explanation,
                        "similarity": rec.similarity,
                        "success_rate": rec.success_rate,
                    },
                }
                if current is None or rec.sort_key > current["recommendation"].sort_key:
                    aggregated[skill_name] = payload
        items = sorted(
            aggregated.values(),
            key=lambda item: item["recommendation"].sort_key,
            reverse=True,
        )
        result = items[:limit]
        for item in result:
            item.pop("recommendation", None)
        return result

    def thread_context(self, thread_id: str, *, limit: int = 8) -> dict[str, Any]:
        turns = self.store.list_graph_neighbors(
            f"thread:{thread_id}", edge_type="CONTAINS", node_type="ConversationTurn", limit=limit
        )
        # META-006 重灾区 A: was `list_graph_nodes(limit=500)` + Python-side
        # `if node_type != "Preference"`. On a new workspace Preference is
        # nearly always empty, which wasted a 500-row scan per turn. SQL
        # now filters at source so the scan is bounded by actual Preference
        # count, not the entire graph.
        preferences = [
            {
                "entity_key": node["entity_key"],
                "text": node["properties"].get("text"),
            }
            for node in self.store.list_graph_nodes(node_type="Preference", limit=limit * 4)
        ]
        turns.sort(key=lambda item: item["node"]["updated_at"])
        return {
            "thread": self.store.get_graph_node(f"thread:{thread_id}"),
            "recent_turns": [
                {
                    "turn_id": item["node"]["properties"].get("turn_id")
                    or item["node"]["entity_key"].split(":", 1)[-1],
                    "role": item["node"]["properties"].get("role"),
                    "message": item["node"]["properties"].get("message"),
                    "route": item["node"]["properties"].get("route"),
                    "task_id": item["node"]["properties"].get("task_id"),
                }
                for item in turns[-limit:]
            ],
            "preferences": preferences[:limit],
        }

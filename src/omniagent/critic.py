from __future__ import annotations

from typing import Any

from omniagent.llm.base import LLMAdapter
from omniagent.models import CriticVerdict, ExecutionResult, TaskNode
from omniagent.prompting import PromptManager

CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "target_type": {"type": "string"},
        "target_id": {"type": "string"},
        "action": {"type": "string"},
        "summary": {"type": "string"},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
        "suggested_retry_policy": {
            "type": ["object", "null"],
            "additionalProperties": True,
            "properties": {
                "max_retries": {"type": "integer"},
                "backoff_seconds": {"type": "integer"},
                "degrade_to": {"type": "array", "items": {"type": "string"}},
                "replan_on_failure": {"type": "boolean"},
            },
        },
        "downgrade_target": {"type": ["string", "null"]},
        "replan_goal": {"type": ["string", "null"]},
    },
    "required": [
        "target_type",
        "target_id",
        "action",
        "summary",
        "rationale",
        "confidence",
        "suggested_retry_policy",
        "downgrade_target",
        "replan_goal",
    ],
}


CRITIC_TEXT_LIMIT = 1200
CRITIC_LIST_LIMIT = 30
CRITIC_DICT_LIMIT = 30
CRITIC_DEPTH_LIMIT = 3


class Critic:
    def __init__(self, llm: LLMAdapter, prompts: PromptManager) -> None:
        self.llm = llm
        self.prompts = prompts

    def evaluate_node(
        self,
        *,
        node: TaskNode,
        execution_result: ExecutionResult,
        failure_patterns: list[dict[str, Any]],
        workflow_matches: list[dict[str, Any]],
        graph_failure_patterns: list[dict[str, Any]],
        recall_explanations: list[dict[str, Any]],
        user_intent: str | None = None,
        task_goal: str | None = None,
    ) -> tuple[CriticVerdict, Any]:
        prompt = self.prompts.load("critic_v1")
        request_payload = {
            "target_type": "node",
            "target_id": node.node_id,
            "user_intent": user_intent,
            "task_goal": task_goal,
            "node": self._node_summary(node),
            "execution_result": {
                "success": execution_result.success,
                "status": execution_result.status,
                "message": execution_result.message,
                "output": self._compact(execution_result.output),
                "error_code": execution_result.error_code,
            },
            "failure_patterns": self._compact(failure_patterns),
            "workflow_matches": self._compact(workflow_matches),
            "graph_failure_patterns": self._compact(graph_failure_patterns),
            "recall_explanations": self._compact(recall_explanations),
        }
        response = self.llm.generate_structured(
            prompt_name="critic_v1",
            system_prompt=prompt,
            request_payload=request_payload,
            schema=CRITIC_SCHEMA,
        )
        verdict = CriticVerdict.from_dict(response.structured_output)
        verdict.llm_trace_id = response.request_id
        return verdict, response

    def evaluate_task(
        self,
        *,
        task_id: str,
        execution_result: ExecutionResult,
        workflow_matches: list[dict[str, Any]],
        graph_failure_patterns: list[dict[str, Any]],
        recall_explanations: list[dict[str, Any]],
        user_intent: str | None = None,
        task_goal: str | None = None,
    ) -> tuple[CriticVerdict, Any]:
        prompt = self.prompts.load("critic_v1")
        request_payload = {
            "target_type": "task",
            "target_id": task_id,
            "user_intent": user_intent,
            "task_goal": task_goal,
            "execution_result": {
                "success": execution_result.success,
                "status": execution_result.status,
                "message": execution_result.message,
                "output": self._compact(execution_result.output),
                "error_code": execution_result.error_code,
            },
            "failure_patterns": [],
            "workflow_matches": self._compact(workflow_matches),
            "graph_failure_patterns": self._compact(graph_failure_patterns),
            "recall_explanations": self._compact(recall_explanations),
        }
        response = self.llm.generate_structured(
            prompt_name="critic_v1",
            system_prompt=prompt,
            request_payload=request_payload,
            schema=CRITIC_SCHEMA,
        )
        verdict = CriticVerdict.from_dict(response.structured_output)
        verdict.llm_trace_id = response.request_id
        return verdict, response

    def _node_summary(self, node: TaskNode) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "type": str(node.type),
            "goal": node.goal,
            "dependencies": list(node.dependencies),
            "required_skills": list(node.required_skills),
            "executor_hint": node.executor_hint,
            "skill_name": node.skill_name,
            "risk_level": node.risk_level,
            "retry_policy": node.retry_policy.to_dict(),
            "status": str(node.status),
            "input_payload": self._compact(node.input_payload),
            "output": self._compact(node.output),
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "path": artifact.path,
                    "description": artifact.description,
                }
                for artifact in node.artifacts[:10]
            ],
            "error_message": node.error_message,
            "attempts": node.attempts,
            "metadata": self._compact(node.metadata),
        }

    def _compact(self, value: Any, *, depth: int = 0) -> Any:
        if depth > CRITIC_DEPTH_LIMIT:
            return "<truncated>"
        if isinstance(value, str):
            if len(value) <= CRITIC_TEXT_LIMIT:
                return value
            return value[:CRITIC_TEXT_LIMIT] + "...<truncated>"
        if isinstance(value, dict):
            items = list(value.items())[:CRITIC_DICT_LIMIT]
            return {str(key): self._compact(item, depth=depth + 1) for key, item in items}
        if isinstance(value, list):
            return [self._compact(item, depth=depth + 1) for item in value[:CRITIC_LIST_LIMIT]]
        return value

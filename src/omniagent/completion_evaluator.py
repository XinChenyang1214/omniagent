from __future__ import annotations

from typing import Any

from omniagent.llm.base import LLMAdapter
from omniagent.models import (
    CompletionEvaluation,
    CriticVerdict,
    ExecutionResult,
    Task,
    TaskGraph,
)
from omniagent.prompting import PromptManager


COMPLETION_EVALUATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_id": {"type": "string"},
        "task_satisfied": {"type": "boolean"},
        "completion_score": {"type": "number"},
        "summary": {"type": "string"},
        "missing_requirements": {"type": "array", "items": {"type": "string"}},
        "unexpected_side_effects": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "primary_failure_layer": {"type": "string"},
        "model_capability_suspected": {"type": "boolean"},
        "recommended_next_action": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "task_id",
        "task_satisfied",
        "completion_score",
        "summary",
        "missing_requirements",
        "unexpected_side_effects",
        "evidence",
        "primary_failure_layer",
        "model_capability_suspected",
        "recommended_next_action",
        "confidence",
    ],
}


class CompletionEvaluator:
    def __init__(self, llm: LLMAdapter, prompts: PromptManager) -> None:
        self.llm = llm
        self.prompts = prompts

    def evaluate_task(
        self,
        *,
        task: Task,
        task_graph: TaskGraph,
        execution_result: ExecutionResult,
        critic_verdicts: list[CriticVerdict],
        workflow_matches: list[dict[str, Any]],
        graph_failure_patterns: list[dict[str, Any]],
        recall_explanations: list[dict[str, Any]],
        user_intent: str | None = None,
    ) -> tuple[CompletionEvaluation, Any]:
        prompt = self.prompts.load("completion_evaluator_v1")
        request_payload = {
            "task": {
                "task_id": task.task_id,
                "goal": task.goal,
                "expected_output": task.expected_output,
                "status": str(task.status),
                "plan_summary": task.plan_summary,
                "error_code": task.error_code,
                "error_message": task.error_message,
            },
            "user_intent": user_intent or task.goal,
            "task_graph": {
                "graph_id": task_graph.graph_id,
                "goal": task_graph.goal,
                "summary": task_graph.summary,
                "nodes": [self._node_summary(node) for node in task_graph.nodes],
            },
            "execution_result": {
                "success": execution_result.success,
                "status": execution_result.status,
                "message": execution_result.message,
                "output": self._compact(execution_result.output),
                "error_code": execution_result.error_code,
                "artifacts": [
                    {
                        "kind": artifact.kind,
                        "path": artifact.path,
                        "description": artifact.description,
                    }
                    for artifact in execution_result.artifacts
                ],
            },
            "critic_verdicts": [verdict.to_dict() for verdict in critic_verdicts],
            "workflow_matches": workflow_matches[:5],
            "graph_failure_patterns": graph_failure_patterns[:5],
            "recall_explanations": recall_explanations[:10],
        }
        response = self.llm.generate_structured(
            prompt_name="completion_evaluator_v1",
            system_prompt=prompt,
            request_payload=request_payload,
            schema=COMPLETION_EVALUATOR_SCHEMA,
        )
        evaluation = CompletionEvaluation.from_dict(response.structured_output)
        if not evaluation.task_id:
            evaluation.task_id = task.task_id
        evaluation.llm_trace_id = response.request_id
        return evaluation, response

    def _node_summary(self, node: Any) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "type": str(node.type),
            "goal": node.goal,
            "status": str(node.status),
            "executor_hint": node.executor_hint,
            "skill_name": node.skill_name,
            "attempts": node.attempts,
            "error_message": node.error_message,
            "input_payload": self._compact(node.input_payload),
            "output": self._compact(node.output),
            "metadata": self._compact(node.metadata),
        }

    def _compact(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 3:
            return "<truncated>"
        if isinstance(value, str):
            return value if len(value) <= 1200 else value[:1200] + "...<truncated>"
        if isinstance(value, dict):
            items = list(value.items())[:30]
            return {str(key): self._compact(item, depth=depth + 1) for key, item in items}
        if isinstance(value, list):
            return [self._compact(item, depth=depth + 1) for item in value[:30]]
        return value

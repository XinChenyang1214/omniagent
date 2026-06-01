from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from omniagent.models import (
    AutonomyMode,
    BootstrapContext,
    CompletionEvaluation,
    CriticVerdict,
    ExecutionResult,
    LLMResponse,
    PermissionLevel,
    PlanResult,
    Task,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskRequest,
    TaskSource,
)

DEFAULT_BOOTSTRAP_TEMPLATES = {
    "AGENT.md": "# Workspace Agent Rules\n\n- 以任务为中心执行。\n- 优先复用已有产物和技能。\n",
    "USER.md": "# User Preferences\n\n- 暂无用户偏好，请在任务中逐步补充。\n",
    "POLICY.md": "# Workspace Policy\n\n- 默认禁止删除文件。\n- 高风险操作必须记录审计。\n",
    "TOOLS.md": "# Workspace Tools\n\n- file executor\n- python executor\n- browser executor\n",
}

MEMORY_SUBDIRS = ["short_term", "long_term", "skills", "knowledge", "vector", "failures"]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _payload_stats(value: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "type": type(value).__name__,
        "json_chars": _json_size(value),
    }
    if isinstance(value, dict):
        stats["top_level_keys"] = list(value.keys())
        stats["fields"] = {
            str(key): {
                "type": type(item).__name__,
                "json_chars": _json_size(item),
                "items": len(item) if isinstance(item, (dict, list, tuple, set)) else None,
            }
            for key, item in value.items()
        }
    elif isinstance(value, (list, tuple, set)):
        stats["items"] = len(value)
    return stats


def _task_request_payload(request: TaskRequest) -> dict[str, Any]:
    return {
        "task_id": request.task_id,
        "goal": request.goal,
        "expected_output": request.expected_output,
        "executor": request.executor,
        "skill_name": request.skill_name,
        "payload": request.payload,
        "mode": str(request.mode),
        "source": str(request.source),
        "context_refs": request.context_refs,
        "constraints": asdict(request.constraints),
        "preferred_skills": request.preferred_skills,
        "metadata": request.metadata,
        "execution_mode": request.execution_mode,
    }


@dataclass(slots=True)
class WorkspacePaths:
    root: Path
    memory: Path
    workflows: Path
    tasks: Path
    skills: Path


class WorkspaceManager:
    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve()

    def ensure(self) -> WorkspacePaths:
        self.root.mkdir(parents=True, exist_ok=True)
        for name, content in DEFAULT_BOOTSTRAP_TEMPLATES.items():
            path = self.root / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")

        memory_root = self.root / "MEMORY"
        memory_root.mkdir(exist_ok=True)
        for name in MEMORY_SUBDIRS:
            (memory_root / name).mkdir(parents=True, exist_ok=True)

        workflows = self.root / "WORKFLOWS"
        workflows.mkdir(exist_ok=True)
        tasks = self.root / "tasks"
        tasks.mkdir(exist_ok=True)
        skills = self.root / "skills"
        skills.mkdir(exist_ok=True)

        return WorkspacePaths(
            root=self.root,
            memory=memory_root,
            workflows=workflows,
            tasks=tasks,
            skills=skills,
        )

    def load_bootstrap_documents(self) -> dict[str, str]:
        self.ensure()
        documents: dict[str, str] = {}
        for filename in DEFAULT_BOOTSTRAP_TEMPLATES:
            documents[filename] = (self.root / filename).read_text(encoding="utf-8")
        return documents

    def create_task_dir(self, task_id: str) -> Path:
        task_dir = self.root / "tasks" / task_id
        for subdir in ["artifacts", "nodes", "llm_calls", "critic", "plan"]:
            (task_dir / subdir).mkdir(parents=True, exist_ok=True)
        return task_dir

    def create_thread_dir(self, thread_id: str) -> Path:
        """Return (creating if needed) workspace/threads/<thread_id>/.

        ISSUE-001: router + responder LLM calls live outside any single
        task — they are thread-scoped (e.g. small-talk, clarify, status
        queries that never create a task). To keep their observability
        metrics alongside task-scoped calls, we mirror the task dir
        layout under ``workspace/threads/<thread_id>/llm_calls/``.
        """
        thread_dir = self.root / "threads" / thread_id
        (thread_dir / "llm_calls").mkdir(parents=True, exist_ok=True)
        return thread_dir

    def build_bootstrap_context(
        self,
        *,
        memory_summaries: list[dict[str, Any]],
        workflow_summaries: list[dict[str, Any]],
    ) -> BootstrapContext:
        return BootstrapContext(
            documents=self.load_bootstrap_documents(),
            memory_summaries=memory_summaries,
            workflow_summaries=workflow_summaries,
        )


class TaskArtifactManager:
    def __init__(self, workspace_manager: WorkspaceManager) -> None:
        self.workspace_manager = workspace_manager

    def task_dir(self, task_id: str) -> Path:
        return self.workspace_manager.root / "tasks" / task_id

    def thread_dir(self, thread_id: str) -> Path:
        """Return ``workspace/threads/<thread_id>/`` path (does not create)."""
        return self.workspace_manager.root / "threads" / thread_id

    def ensure_thread_dir(self, thread_id: str) -> Path:
        """Create (if missing) and return the thread's artifact directory."""
        return self.workspace_manager.create_thread_dir(thread_id)

    def write_thread_llm_call(
        self,
        thread_id: str,
        response: LLMResponse,
        *,
        request_payload: dict[str, Any],
    ) -> None:
        """Persist a thread-scoped LLM call under workspace/threads/<tid>/llm_calls/.

        ISSUE-001: used for router + responder calls that are not
        attached to any task. The file format matches :meth:`write_llm_call`
        so the same aggregator (``gateway.llm_stats``) can read both.
        """
        thread_dir = self.ensure_thread_dir(thread_id)
        _write_json(
            thread_dir / "llm_calls" / f"{response.prompt_name}_{response.request_id}.json",
            {
                "provider": response.provider,
                "model": response.model,
                "prompt_name": response.prompt_name,
                "request_id": response.request_id,
                "thread_id": thread_id,
                "request_payload": request_payload,
                "request_payload_stats": _payload_stats(request_payload),
                "raw_output": response.raw_output,
                "structured_output": response.structured_output,
                "usage": response.usage,
                "latency_ms": response.latency_ms,
                "tier": response.tier,
                "attempts": response.attempts,
                "started_at": response.started_at,
                "created_at": response.created_at,
            },
        )

    def initialize_task(
        self,
        task: Task,
        request: TaskRequest,
        bootstrap: BootstrapContext,
    ) -> Path:
        task_dir = self.workspace_manager.create_task_dir(task.task_id)
        input_summary = "\n".join(
            [
                f"# Task {task.task_id}",
                "",
                f"- goal: {request.goal}",
                f"- expected_output: {request.expected_output or 'none'}",
                f"- executor: {task.executor}",
                f"- skill: {task.skill_name or 'none'}",
                f"- source: {task.source}",
                f"- mode: {task.mode}",
                f"- preferred_skills: {', '.join(request.preferred_skills) if request.preferred_skills else 'none'}",
            ]
        )
        (task_dir / "input_summary.md").write_text(input_summary, encoding="utf-8")
        _write_json(
            task_dir / "context_snapshot.json",
            {
                "bootstrap_documents": bootstrap.documents,
                "memory_summaries": bootstrap.memory_summaries,
                "workflow_summaries": bootstrap.workflow_summaries,
                "context_refs": request.context_refs,
            },
        )
        _write_json(
            task_dir / "request.json",
            _task_request_payload(request),
        )
        return task_dir

    def write_request_snapshot(self, task_dir: Path, request: TaskRequest) -> None:
        _write_json(task_dir / "request.json", _task_request_payload(request))

    def load_task_request(self, task: Task) -> TaskRequest:
        request_path = Path(task.workspace_path) / "tasks" / task.task_id / "request.json"
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        constraints_payload = dict(payload.get("constraints", {}))
        return TaskRequest(
            goal=str(payload.get("goal", task.goal)),
            expected_output=payload.get("expected_output", task.expected_output),
            payload=dict(payload.get("payload", task.payload)),
            executor=str(payload.get("executor", task.executor)),
            skill_name=payload.get("skill_name", task.skill_name),
            task_id=str(payload.get("task_id", task.task_id)),
            mode=AutonomyMode(payload.get("mode", task.mode)),
            source=TaskSource(payload.get("source", task.source)),
            context_refs=list(payload.get("context_refs", [])),
            constraints=TaskConstraints(
                allow_network=bool(constraints_payload.get("allow_network", False)),
                allow_delete=bool(constraints_payload.get("allow_delete", False)),
                allow_memory_read=bool(constraints_payload.get("allow_memory_read", True)),
                timeout_seconds=int(constraints_payload.get("timeout_seconds", 30)),
                max_permission=PermissionLevel(
                    str(constraints_payload.get("max_permission", "L2"))
                ),
            ),
            preferred_skills=list(payload.get("preferred_skills", [])),
            metadata=dict(payload.get("metadata", task.metadata)),
            execution_mode=str(payload.get("execution_mode", "plan_and_execute")),
        )

    def write_plan(self, task_dir: Path, plan: PlanResult) -> None:
        _write_json(task_dir / "plan" / "planner_output.json", plan.to_dict())
        (task_dir / "plan" / "planner_summary.md").write_text(
            "\n".join(
                [
                    "# Planner Summary",
                    "",
                    f"- summary: {plan.summary}",
                    f"- risk_level: {plan.risk_level}",
                    f"- required_capabilities: {', '.join(plan.required_capabilities) if plan.required_capabilities else 'none'}",
                    "",
                    "## Nodes",
                    *[
                        f"- {node.node_id} [{node.type}]: {node.goal} -> deps={','.join(node.dependencies) or 'none'}"
                        for node in plan.graph.nodes
                    ],
                ]
            ),
            encoding="utf-8",
        )
        _write_json(task_dir / "plan" / "task_graph.json", plan.graph.to_dict())

    def write_policy_decision(self, task_dir: Path, decision: dict[str, Any]) -> None:
        _write_json(task_dir / "plan" / "policy_decision.json", decision)

    def write_llm_call(
        self, task_dir: Path, response: LLMResponse, *, request_payload: dict[str, Any]
    ) -> None:
        _write_json(
            task_dir / "llm_calls" / f"{response.prompt_name}_{response.request_id}.json",
            {
                "provider": response.provider,
                "model": response.model,
                "prompt_name": response.prompt_name,
                "request_id": response.request_id,
                "request_payload": request_payload,
                "request_payload_stats": _payload_stats(request_payload),
                "raw_output": response.raw_output,
                "structured_output": response.structured_output,
                "usage": response.usage,
                # ISSUE-001: persist observability fields so audits/CLI
                # stats can reconstruct latency + fallback-tier history
                # without re-running the call.
                "latency_ms": response.latency_ms,
                "tier": response.tier,
                "attempts": response.attempts,
                "started_at": response.started_at,
                "created_at": response.created_at,
            },
        )

    def write_node_snapshot(self, task_dir: Path, node: TaskNode) -> None:
        node_dir = task_dir / "nodes" / node.node_id
        node_dir.mkdir(parents=True, exist_ok=True)
        _write_json(node_dir / "node.json", node.to_dict())
        if node.output:
            _write_json(node_dir / "output.json", node.output)

    def write_critic_verdict(self, task_dir: Path, verdict: CriticVerdict) -> None:
        _write_json(
            task_dir
            / "critic"
            / f"{verdict.target_type}_{verdict.target_id}_{verdict.created_at.replace(':', '-')}.json",
            verdict.to_dict(),
        )

    def write_completion_evaluation(
        self, task_dir: Path, evaluation: CompletionEvaluation
    ) -> None:
        payload = evaluation.to_dict()
        _write_json(task_dir / "completion" / "evaluation.json", payload)
        summary = "\n".join(
            [
                f"# Completion Evaluation: {evaluation.task_id}",
                "",
                f"- task_satisfied: {evaluation.task_satisfied}",
                f"- completion_score: {evaluation.completion_score}",
                f"- primary_failure_layer: {evaluation.primary_failure_layer}",
                f"- model_capability_suspected: {evaluation.model_capability_suspected}",
                f"- recommended_next_action: {evaluation.recommended_next_action}",
                f"- confidence: {evaluation.confidence}",
                "",
                evaluation.summary,
            ]
        )
        (task_dir / "completion" / "summary.md").write_text(summary, encoding="utf-8")

    def finalize_task(
        self,
        task_dir: Path,
        task: Task,
        result: ExecutionResult,
        *,
        task_graph: TaskGraph | None = None,
        critic_verdicts: list[CriticVerdict] | None = None,
        completion_evaluation: CompletionEvaluation | None = None,
    ) -> None:
        _write_json(
            task_dir / "execution_result.json",
            {
                "success": result.success,
                "status": result.status,
                "message": result.message,
                "output": result.output,
                "logs": result.logs,
                "executor": result.executor,
                "error_code": result.error_code,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            },
        )
        _write_json(
            task_dir / "artifacts.json",
            {"artifacts": [asdict(item) for item in result.artifacts]},
        )
        if task_graph is not None:
            _write_json(task_dir / "plan" / "final_task_graph.json", task_graph.to_dict())
        if critic_verdicts is not None:
            _write_json(
                task_dir / "critic" / "verdicts.json",
                {"verdicts": [verdict.to_dict() for verdict in critic_verdicts]},
            )
        if completion_evaluation is not None:
            self.write_completion_evaluation(task_dir, completion_evaluation)
        execution_summary = "\n".join(
            [
                f"# Execution Summary: {task.task_id}",
                "",
                f"- status: {task.status}",
                f"- executor: {task.executor}",
                f"- success: {result.success}",
                f"- message: {result.message}",
                f"- plan_summary: {task.plan_summary or 'none'}",
                f"- completion_score: {completion_evaluation.completion_score if completion_evaluation else 'none'}",
                f"- completion_failure_layer: {completion_evaluation.primary_failure_layer if completion_evaluation else 'none'}",
            ]
        )
        (task_dir / "execution_summary.md").write_text(execution_summary, encoding="utf-8")
        if "body" in result.output:
            preview = str(result.output["body"])[:4000]
            (task_dir / "artifacts" / "body_preview.txt").write_text(preview, encoding="utf-8")

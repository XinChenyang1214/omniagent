from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from omniagent.completion_evaluator import CompletionEvaluator
from omniagent.critic import Critic
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.events import EventBus
from omniagent.execution_engine import ExecutionEngine
from omniagent.execution_policy import ExecutionPolicy
from omniagent.graph_memory import GraphMemoryStore
from omniagent.hybrid_memory import HybridMemoryRetriever
from omniagent.logging_utils import get_logger
from omniagent.memory import FileMemoryManager
from omniagent.models import (
    CompletionEvaluation,
    CriticAction,
    CriticVerdict,
    ExecutionResult,
    PlanResult,
    PermissionDecision,
    PermissionLevel,
    Task,
    TaskEvent,
    TaskExecutionReport,
    TaskGraph,
    TaskRequest,
    TaskRunContext,
    TaskStatus,
    utc_now,
)
from omniagent.permissions import PermissionManager
from omniagent.planner import Planner
from omniagent.skill_registry import filter_skill_registry_entries_for_planner
from omniagent.skill_lifecycle import SkillLifecycleManager
from omniagent.storage import SQLiteStore
from omniagent.workflow_compiler import WorkflowCompiler
from omniagent.workspace import TaskArtifactManager, WorkspaceManager
from omniagent.config import SandboxConfig


def should_evaluate_task_critic(
    result: ExecutionResult,
    critic_verdicts: list[CriticVerdict],
) -> bool:
    if result.status == "waiting_input":
        return False
    if result.success:
        return True
    return not any(
        verdict.target_type == "node" and verdict.action == CriticAction.ACCEPT
        for verdict in critic_verdicts
    )


def should_evaluate_completion(result: ExecutionResult) -> bool:
    return result.status not in ("waiting_input", "suspended")


def should_fail_from_completion_evaluation(
    result: ExecutionResult,
    evaluation: CompletionEvaluation | None,
) -> bool:
    if evaluation is None:
        return False
    if not result.success or result.status in ("waiting_input", "suspended"):
        return False
    if evaluation.task_satisfied:
        return False
    if evaluation.primary_failure_layer in {"completion_evaluation", "test_expectation"}:
        return False
    return evaluation.confidence >= 0.7


def _completion_override_message(evaluation: CompletionEvaluation) -> str:
    message = f"[Completion evaluation] {evaluation.summary}"
    if evaluation.missing_requirements:
        message += " 未满足: " + "；".join(evaluation.missing_requirements[:5])
    return message


class EmbeddedRuntime:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        workspace_manager: WorkspaceManager,
        artifact_manager: TaskArtifactManager,
        memory_manager: FileMemoryManager,
        graph_memory: GraphMemoryStore,
        hybrid_memory: HybridMemoryRetriever,
        workflow_compiler: WorkflowCompiler,
        skill_lifecycle: SkillLifecycleManager,
        planner: Planner,
        critic: Critic,
        completion_evaluator: CompletionEvaluator,
        execution_engine: ExecutionEngine,
        permission_manager: PermissionManager,
        sandbox_config: SandboxConfig,
    ) -> None:
        self.store = store
        self.workspace_manager = workspace_manager
        self.artifact_manager = artifact_manager
        self.memory_manager = memory_manager
        self.graph_memory = graph_memory
        self.hybrid_memory = hybrid_memory
        self.workflow_compiler = workflow_compiler
        self.skill_lifecycle = skill_lifecycle
        self.planner = planner
        self.critic = critic
        self.completion_evaluator = completion_evaluator
        self.execution_engine = execution_engine
        self.permission_manager = permission_manager
        self.sandbox_config = sandbox_config
        self.execution_policy = ExecutionPolicy()
        self.logger = get_logger("omniagent.runtime")
        # 按 thread_id 缓存 bootstrap 文档的哈希，避免同一会话内重复发送 AGENT.md/POLICY.md 等全文
        self._bootstrap_sent_hashes: dict[str, str] = {}

    def run(
        self,
        request: TaskRequest,
        event_bus: EventBus,
        *,
        loaded_skills: dict[str, Any],
        skill_registry_entries: list[dict[str, Any]],
    ) -> TaskExecutionReport:
        context_bundle = self._build_context_bundle(request)
        workspace_paths = context_bundle["workspace_paths"]
        bootstrap = context_bundle["bootstrap"]
        workflow_summaries = context_bundle["workflow_summaries"]
        vector_matches = context_bundle["vector_matches"]
        self.graph_memory.sync_bootstrap_documents(bootstrap.documents)

        task = Task(
            task_id=request.task_id,
            goal=request.goal,
            expected_output=request.expected_output,
            mode=str(request.mode),
            source=str(request.source),
            status=TaskStatus.CREATED,
            executor=request.executor,
            skill_name=request.skill_name,
            payload=request.payload,
            metadata=dict(request.metadata),
            workspace_path=str(workspace_paths.root),
        )
        self.store.create_task(task)
        self.graph_memory.record_task(task)
        task_dir = self.artifact_manager.initialize_task(task, request, bootstrap)
        self._emit(event_bus, task.task_id, "task.created", task.status, {"goal": task.goal})

        task.status = TaskStatus.PLANNING
        task.updated_at = utc_now()
        self.store.update_task(task.task_id, status=task.status, updated_at=task.updated_at)

        thread_id = request.metadata.get("thread_id")
        conversation_history = self._load_conversation_history(thread_id)
        # META-008 pull model: surface metadata of prior task artifacts in
        # this thread so the Planner can choose to reference them via
        # `{"context_ref": {...}}` instead of blindly re-executing steps.
        recent_task_artifacts = self._load_recent_task_artifacts(thread_id, request.task_id)
        docs_for_prompt = self._prepare_bootstrap_for_prompt(thread_id, bootstrap.documents)

        planner_skill_registry_entries = filter_skill_registry_entries_for_planner(
            skill_registry_entries,
            request,
            goal=request.goal,
        )
        policy_decision = self.execution_policy.decide(
            request,
            skill_registry_entries=skill_registry_entries,
            context_counts=self._policy_context_counts(
                context_bundle=context_bundle,
                workflow_summaries=workflow_summaries,
                vector_matches=vector_matches,
                planner_skill_registry_entries=planner_skill_registry_entries,
            ),
        )
        self.artifact_manager.write_policy_decision(task_dir, policy_decision.to_dict())
        if policy_decision.policy_decision == "direct_execute":
            planner_result = policy_decision.to_plan_result(request)
            self.artifact_manager.write_plan(task_dir, planner_result)
            task.graph_id = planner_result.graph.graph_id
            task.plan_summary = planner_result.summary
            task.updated_at = utc_now()
            self.store.update_task(
                task.task_id,
                status=task.status,
                updated_at=task.updated_at,
                plan_summary=task.plan_summary,
                graph_id=task.graph_id,
            )
            self.store.save_task_graph(task.task_id, planner_result.graph)
            self.graph_memory.record_task(task)
            self.graph_memory.record_workflow(task, planner_result.graph)
            self._emit(
                event_bus,
                task.task_id,
                "task.planned",
                task.status,
                {
                    "graph_id": planner_result.graph.graph_id,
                    "summary": planner_result.summary,
                    "policy_decision": policy_decision.policy_decision,
                },
            )
            return self._execute_task_graph(
                task=task,
                request=request,
                task_graph=planner_result.graph,
                task_dir=task_dir,
                planner_result=planner_result,
                context_bundle=context_bundle,
                event_bus=event_bus,
                loaded_skills=loaded_skills,
                skill_registry_entries=skill_registry_entries,
            )
        try:
            planner_result, planner_response = self.planner.plan(
                task_request=request,
                bootstrap=bootstrap,
                memory_summaries=context_bundle["memory_summaries"],
                workflow_matches=workflow_summaries,
                vector_matches=vector_matches,
                failure_patterns=context_bundle["failure_patterns"],
                graph_workflow_matches=context_bundle["graph_workflow_matches"],
                graph_skill_matches=context_bundle["graph_skill_matches"],
                graph_failure_patterns=context_bundle["graph_failure_patterns"],
                recall_explanations=context_bundle["recall_explanations"],
                skill_registry_entries=planner_skill_registry_entries,
                conversation_history=conversation_history,
                bootstrap_documents=docs_for_prompt,
                recent_task_artifacts=recent_task_artifacts,
            )
        except OmniAgentError as exc:
            return self._finalize_planning_failure(
                task=task,
                request=request,
                task_dir=task_dir,
                error=exc,
                event_bus=event_bus,
            )
        self.artifact_manager.write_llm_call(
            task_dir,
            planner_response,
            request_payload=self.planner.build_request_payload(
                task_request=request,
                bootstrap_documents=docs_for_prompt,
                memory_summaries=context_bundle["memory_summaries"],
                workflow_matches=workflow_summaries,
                vector_matches=vector_matches,
                failure_patterns=context_bundle["failure_patterns"],
                graph_workflow_matches=context_bundle["graph_workflow_matches"],
                graph_skill_matches=context_bundle["graph_skill_matches"],
                graph_failure_patterns=context_bundle["graph_failure_patterns"],
                recall_explanations=context_bundle["recall_explanations"],
                skill_registry_entries=planner_skill_registry_entries,
                conversation_history=conversation_history,
                recent_task_artifacts=recent_task_artifacts,
            ),
        )
        self.artifact_manager.write_plan(task_dir, planner_result)

        # META-003: if the Planner refused to plan (decline_not_a_task /
        # clarify / missing_capability), short-circuit without running the
        # execution engine. The decline/clarify/missing message becomes the
        # task summary so the Responder can relay it back to the user via
        # the normal task-report flow — no new task status, no new event
        # type, no PendingCheckpoint plumbing required.
        if planner_result.decision != "plan":
            return self._finalize_non_plan_decision(
                task=task,
                request=request,
                task_dir=task_dir,
                planner_result=planner_result,
                event_bus=event_bus,
            )

        task.graph_id = planner_result.graph.graph_id
        task.plan_summary = planner_result.summary
        task.updated_at = utc_now()
        self.store.update_task(
            task.task_id,
            status=task.status,
            updated_at=task.updated_at,
            plan_summary=task.plan_summary,
            graph_id=task.graph_id,
        )
        self.store.save_task_graph(task.task_id, planner_result.graph)
        self.graph_memory.record_task(task)
        self.graph_memory.record_workflow(task, planner_result.graph)
        self._emit(
            event_bus,
            task.task_id,
            "task.planned",
            task.status,
            {"graph_id": planner_result.graph.graph_id, "summary": planner_result.summary},
        )

        return self._execute_task_graph(
            task=task,
            request=request,
            task_graph=planner_result.graph,
            task_dir=task_dir,
            planner_result=planner_result,
            context_bundle=context_bundle,
            event_bus=event_bus,
            loaded_skills=loaded_skills,
            skill_registry_entries=skill_registry_entries,
        )

    def _finalize_planning_failure(
        self,
        *,
        task: Task,
        request: TaskRequest,
        task_dir: Any,
        error: OmniAgentError,
        event_bus: EventBus,
    ) -> TaskExecutionReport:
        """Record planner-stage failures as normal failed tasks.

        LLM/network failures raised while building the plan happen before a
        planner response exists, so the regular plan/execution finalization
        path cannot run. Persisting a failed report keeps CLI/REPL sessions
        auditable instead of leaving tasks stuck in ``planning``.
        """
        now = utc_now()
        result = ExecutionResult(
            success=False,
            status=str(TaskStatus.FAILED),
            message=error.message,
            output={
                "phase": "planning",
                "details": error.details,
            },
            executor="planner",
            started_at=now,
            finished_at=now,
            error_code=error.code,
        )
        task.status = TaskStatus.FAILED
        task.summary = error.message
        task.error_code = error.code
        task.error_message = error.message
        task.updated_at = now
        self.store.log_execution(task.task_id, "planner", {"goal": request.goal}, result)
        self.store.update_task(
            task.task_id,
            status=task.status,
            updated_at=task.updated_at,
            summary=task.summary,
            plan_summary=task.plan_summary,
            graph_id=task.graph_id,
            error_code=task.error_code,
            error_message=task.error_message,
        )
        self.graph_memory.record_task(task)
        self._emit(
            event_bus,
            task.task_id,
            "task.failed",
            task.status,
            {"phase": "planning", "message": result.message, "output": result.output},
        )
        self.artifact_manager.finalize_task(
            task_dir,
            task,
            result,
            task_graph=None,
            critic_verdicts=[],
        )
        events = self.store.list_events(task.task_id)
        self.logger.warning(
            "task_failed_during_planning",
            extra={
                "task_id": task.task_id,
                "error_code": error.code,
                "error_message": error.message,
            },
        )
        return TaskExecutionReport(
            task=task,
            result=result,
            events=events,
            task_graph=None,
            critic_verdicts=[],
        )

    def _finalize_non_plan_decision(
        self,
        *,
        task: Task,
        request: TaskRequest,
        task_dir: Any,
        planner_result: PlanResult,
        event_bus: EventBus,
    ) -> TaskExecutionReport:
        """META-003 short-circuit path.

        Called when ``planner_result.decision`` is one of
        ``decline_not_a_task`` / ``clarify`` / ``missing_capability``. Builds
        a minimal ExecutionResult that carries the decision into the task
        summary so downstream Responder logic treats it as a regular
        completed/failed task. No execution engine, no sandbox, no critic —
        the Planner has already decided.
        """
        decision = planner_result.decision
        empty_graph = planner_result.graph
        if decision == "decline_not_a_task":
            final_status = TaskStatus.COMPLETED
            event_type = "task.declined"
            message = (
                planner_result.decline_reason or planner_result.summary or "这不是一个可规划的任务"
            )
            success = True
            output: dict[str, Any] = {
                "decision": decision,
                "decline_reason": planner_result.decline_reason,
                "summary": planner_result.summary,
            }
            error_code: str | None = None
        elif decision == "clarify":
            # WAITING_INPUT would be more honest but pulls in the full
            # PendingCheckpoint state machine. For the first pass we
            # complete the task with the clarify question as the summary so
            # the Responder reads it and asks the user naturally on the
            # next turn.
            final_status = TaskStatus.COMPLETED
            event_type = "task.clarify_requested"
            message = (
                planner_result.clarify_question or planner_result.summary or "需要澄清信息后再执行"
            )
            success = True
            output = {
                "decision": decision,
                "clarify_question": planner_result.clarify_question,
                "summary": planner_result.summary,
            }
            error_code = None
        elif decision == "missing_capability":
            final_status = TaskStatus.FAILED
            event_type = "task.missing_capability"
            missing = planner_result.missing_capability or {}
            message = (
                missing.get("description")
                or planner_result.summary
                or "缺少可执行 executor 或 skill"
            )
            success = False
            output = {
                "decision": decision,
                "missing_capability": missing,
                "summary": planner_result.summary,
            }
            error_code = ErrorCode.SKILL_ERROR
        else:
            # Schema enum should keep us out of here, but stay safe.
            final_status = TaskStatus.COMPLETED
            event_type = "task.completed"
            message = planner_result.summary or "未知规划决策"
            success = True
            output = {"decision": decision, "summary": planner_result.summary}
            error_code = None

        now = utc_now()
        result = ExecutionResult(
            success=success,
            status=str(final_status),
            message=message,
            output=output,
            executor="planner_decision",
            started_at=now,
            finished_at=now,
            error_code=error_code,
        )

        task.status = final_status
        task.summary = message
        task.plan_summary = planner_result.summary
        task.error_code = error_code
        task.error_message = None if success else message
        task.graph_id = empty_graph.graph_id
        task.updated_at = utc_now()
        self.store.update_task(
            task.task_id,
            status=task.status,
            updated_at=task.updated_at,
            summary=task.summary,
            plan_summary=task.plan_summary,
            graph_id=task.graph_id,
            error_code=task.error_code,
            error_message=task.error_message,
        )
        self.store.log_execution(task.task_id, "planner_decision", {"goal": task.goal}, result)
        self.graph_memory.record_task(task)
        self._emit(
            event_bus,
            task.task_id,
            event_type,
            task.status,
            {"decision": decision, "message": message},
        )
        self.artifact_manager.finalize_task(
            task_dir,
            task,
            result,
            task_graph=empty_graph,
            critic_verdicts=[],
        )
        # Persist a lightweight task-memory entry so later recalls know
        # this request was handled by a planner decision (not a successful
        # graph execution) — prevents the "decline" from being pulled back
        # as a positive workflow match on future planning passes.
        self.memory_manager.write_task_memory(task, result, capability_name="planner_decision")
        events = self.store.list_events(task.task_id)
        self.logger.info(
            "task_finished_via_planner_decision",
            extra={
                "task_id": task.task_id,
                "decision": decision,
                "status": task.status,
            },
        )
        return TaskExecutionReport(
            task=task,
            result=result,
            events=events,
            task_graph=empty_graph,
            critic_verdicts=[],
        )

    def resume(
        self,
        task_id: str,
        event_bus: EventBus,
        *,
        loaded_skills: dict[str, Any],
        skill_registry_entries: list[dict[str, Any]],
        request_overrides: dict[str, Any] | None = None,
    ) -> TaskExecutionReport:
        task = self.store.get_task(task_id)
        if task is None:
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"任务不存在: {task_id}")

        request = self.artifact_manager.load_task_request(task)
        self._apply_request_overrides(request, request_overrides or {})
        task.goal = request.goal
        task.payload = dict(request.payload)
        task.executor = request.executor
        task.skill_name = request.skill_name
        task.expected_output = request.expected_output
        task.metadata = dict(request.metadata)

        task_graph = self.store.get_task_graph(task_id)
        if task_graph is None:
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"任务图不存在: {task_id}")
        self.graph_memory.record_task(task)
        self.graph_memory.record_workflow(task, task_graph)

        task_dir = self.artifact_manager.task_dir(task.task_id)
        self.artifact_manager.write_request_snapshot(task_dir, request)
        context_bundle = self._build_context_bundle(request)
        self._emit(event_bus, task.task_id, "task.resumed", task.status, {"task_id": task.task_id})
        return self._execute_task_graph(
            task=task,
            request=request,
            task_graph=task_graph,
            task_dir=task_dir,
            planner_result=None,
            context_bundle=context_bundle,
            event_bus=event_bus,
            loaded_skills=loaded_skills,
            skill_registry_entries=skill_registry_entries,
        )

    def _execute_task_graph(
        self,
        *,
        task: Task,
        request: TaskRequest,
        task_graph: TaskGraph,
        task_dir: Any,
        planner_result: PlanResult | None,
        context_bundle: dict[str, Any],
        event_bus: EventBus,
        loaded_skills: dict[str, Any],
        skill_registry_entries: list[dict[str, Any]],
    ) -> TaskExecutionReport:
        task.status = TaskStatus.RUNNING
        task.updated_at = utc_now()
        self.store.update_task(
            task.task_id,
            status=task.status,
            updated_at=task.updated_at,
            plan_summary=task.plan_summary,
            graph_id=task.graph_id or task_graph.graph_id,
            error_code=None,
            error_message=None,
        )
        self._emit(
            event_bus, task.task_id, "task.started", task.status, {"task_dir": str(task_dir)}
        )

        from omniagent.sandbox import build_sandbox

        sandbox = build_sandbox(
            context_bundle["workspace_paths"].root,
            timeout_seconds=request.constraints.timeout_seconds,
            allow_network=request.constraints.allow_network,
            level="workspace",
            authorized_roots=self._authorized_paths_for_request(request),
            path_intents=dict(self.sandbox_config.path_intents),
        )
        context = TaskRunContext(
            task=task,
            task_request=request,
            workspace_root=context_bundle["workspace_paths"].root,
            task_dir=task_dir,
            bootstrap=context_bundle["bootstrap"],
            loaded_skills=loaded_skills,
            sandbox=sandbox,
            task_graph=task_graph,
            workflow_matches=context_bundle["workflow_summaries"],
            vector_matches=context_bundle["vector_matches"],
            failure_patterns=context_bundle["failure_patterns"],
            graph_workflow_matches=context_bundle["graph_workflow_matches"],
            graph_skill_matches=context_bundle["graph_skill_matches"],
            graph_failure_patterns=context_bundle["graph_failure_patterns"],
            hybrid_recall=context_bundle["hybrid_recall"],
        )

        try:
            task_graph, result, critic_verdicts = self.execution_engine.execute(
                task=task,
                task_request=request,
                task_graph=task_graph,
                context=context,
                event_bus=event_bus,
            )
        except OmniAgentError as exc:
            result = ExecutionResult(
                success=False,
                status="failed",
                message=exc.message,
                output={"details": exc.details},
                executor="task_graph",
                error_code=exc.code,
            )
            critic_verdicts = []

        if should_evaluate_task_critic(result, critic_verdicts):
            try:
                task_verdict, task_verdict_response = self.critic.evaluate_task(
                    task_id=task.task_id,
                    execution_result=result,
                    workflow_matches=context_bundle["workflow_summaries"],
                    graph_failure_patterns=context_bundle["graph_failure_patterns"],
                    recall_explanations=context_bundle["recall_explanations"],
                    user_intent=request.goal,
                    task_goal=task.goal,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "task_critic_failed",
                    extra={"task_id": task.task_id, "error": str(exc)},
                )
                task_verdict = CriticVerdict(
                    target_type="task",
                    target_id=task.task_id,
                    action=CriticAction.ACCEPT if result.success else CriticAction.FAIL,
                    summary=f"task critic evaluation failed; preserving execution result: {exc}",
                    rationale="The runtime keeps the already observed tool execution result when "
                    "the optional task-level critic LLM call fails.",
                    confidence=0.0,
                    llm_trace_id="critic_fallback",
                )
                task_verdict_response = None
            critic_verdicts.append(task_verdict)
            self.store.record_critic_verdict(task.task_id, task_verdict)
            self.artifact_manager.write_critic_verdict(task_dir, task_verdict)
            if task_verdict_response is not None:
                self.artifact_manager.write_llm_call(
                    task_dir,
                    task_verdict_response,
                    request_payload={
                        "task_id": task.task_id,
                        "result": {"success": result.success, "message": result.message},
                    },
                )

        # ISSUE-014: enforce critic fail verdict.
        # When the task-level critic says FAIL but the execution engine returned
        # success (e.g. node produced hallucinated output that "succeeded"
        # technically), override result.success so the final status is FAILED.
        if (
            result.status not in ("waiting_input", "suspended")
            and result.success
            and critic_verdicts
        ):
            task_level = [v for v in critic_verdicts if v.target_type == "task"]
            if task_level and task_level[-1].action == CriticAction.FAIL:
                tv = task_level[-1]
                self.logger.warning(
                    "ISSUE-014: critic overrides execution success → FAIL "
                    "(confidence=%.2f, summary=%s)",
                    tv.confidence,
                    tv.summary,
                )
                result = ExecutionResult(
                    success=False,
                    status="failed",
                    message=f"[Critic override] {tv.summary}",
                    output=result.output,
                    artifacts=result.artifacts,
                    executor=result.executor,
                    error_code="CRITIC_FAIL_OVERRIDE",
                )

        if result.status == "waiting_input":
            final_status = TaskStatus.WAITING_INPUT
            event_type = "task.awaiting_input"
        elif result.status == "suspended":
            final_status = TaskStatus.SUSPENDED
            event_type = "task.suspended"
        else:
            final_status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED
            event_type = "task.completed" if result.success else "task.failed"

        task.status = final_status
        task.graph_id = task.graph_id or task_graph.graph_id
        task.summary = result.message
        task.error_code = result.error_code
        task.error_message = (
            None
            if result.success or final_status in {TaskStatus.WAITING_INPUT, TaskStatus.SUSPENDED}
            else result.message
        )
        task.updated_at = utc_now()
        completion_evaluation = None
        if should_evaluate_completion(result):
            completion_evaluation = self._evaluate_completion(
                task=task,
                request=request,
                task_graph=task_graph,
                task_dir=task_dir,
                result=result,
                critic_verdicts=critic_verdicts,
                context_bundle=context_bundle,
            )
        if should_fail_from_completion_evaluation(result, completion_evaluation):
            self.logger.warning(
                "completion_evaluation_overrides_execution_success",
                extra={
                    "task_id": task.task_id,
                    "completion_score": completion_evaluation.completion_score,
                    "failure_layer": completion_evaluation.primary_failure_layer,
                    "summary": completion_evaluation.summary,
                },
            )
            result = ExecutionResult(
                success=False,
                status="failed",
                message=_completion_override_message(completion_evaluation),
                output={
                    **dict(result.output),
                    "completion_evaluation": completion_evaluation.to_dict(),
                    "original_execution_success": True,
                },
                artifacts=result.artifacts,
                logs=list(result.logs),
                executor=result.executor,
                started_at=result.started_at,
                finished_at=utc_now(),
                error_code="completion_unsatisfied",
            )
            final_status = TaskStatus.FAILED
            event_type = "task.failed"
            task.status = final_status
            task.summary = result.message
            task.error_code = result.error_code
            task.error_message = result.message
            task.updated_at = utc_now()
        self.store.log_execution(task.task_id, "task_graph", {"goal": task.goal}, result)
        self.store.update_task(
            task.task_id,
            status=task.status,
            updated_at=task.updated_at,
            summary=task.summary,
            plan_summary=task.plan_summary,
            graph_id=task.graph_id,
            error_code=task.error_code,
            error_message=task.error_message,
        )
        self.graph_memory.record_task(task)
        for node in task_graph.nodes:
            self.graph_memory.record_task_node(task.task_id, node)
        for item in self.store.list_permissions(task.task_id):
            self.graph_memory.record_permission_decision(
                PermissionDecision(
                    task_id=item["task_id"],
                    permission=item["permission"],
                    level=PermissionLevel(item["level"]),
                    status=item["status"],
                    rationale=item["rationale"],
                    scope=item.get("scope", "task"),
                    node_id=item.get("node_id"),
                    skill_name=item.get("skill_name"),
                    resource_kind=item.get("resource_kind"),
                    resource_ref=item.get("resource_ref"),
                    sandbox_level=item.get("sandbox_level", "workspace"),
                    source=item.get("source", "runtime"),
                    policy=item.get("policy", "task_constraints"),
                    requires_approval=bool(item.get("requires_approval", False)),
                    details=dict(item.get("details", {})),
                    created_at=item["created_at"],
                )
            )
        for verdict in critic_verdicts:
            self.graph_memory.record_verdict(task, verdict)
        self._emit(
            event_bus,
            task.task_id,
            event_type,
            task.status,
            {"message": result.message, "output": result.output},
        )
        self.artifact_manager.finalize_task(
            task_dir,
            task,
            result,
            task_graph=task_graph,
            critic_verdicts=critic_verdicts,
            completion_evaluation=completion_evaluation,
        )

        if final_status not in {TaskStatus.WAITING_INPUT, TaskStatus.SUSPENDED}:
            plan_snapshot = planner_result or PlanResult(
                summary=task.plan_summary or task_graph.summary or "",
                graph=task_graph,
            )
            self.memory_manager.write_task_memory(task, result, capability_name="task_graph")
            self.memory_manager.write_workflow_summary(task, result, plan=plan_snapshot)
            self.memory_manager.write_vector_entry(
                entry_id=task.task_id,
                text=f"{task.goal}\n{plan_snapshot.summary}\n{result.message}",
                source_type="task",
                metadata={
                    "task_id": task.task_id,
                    "graph_id": task.graph_id,
                    "status": str(task.status),
                },
            )
            for verdict in critic_verdicts:
                node = next(
                    (item for item in task_graph.nodes if item.node_id == verdict.target_id), None
                )
                if node is None or node.error_message is None:
                    continue
                self.memory_manager.write_failure_pattern(
                    task=task,
                    node=node,
                    result=ExecutionResult(
                        success=False,
                        status="failed",
                        message=node.error_message,
                        executor=node.executor_hint or node.skill_name or "node",
                        error_code=ErrorCode.EXECUTION_ERROR,
                    ),
                    verdict=verdict,
                )
                self.graph_memory.record_failure_pattern(
                    task=task,
                    node=node,
                    verdict=verdict,
                    error_message=node.error_message,
                )
            candidate = self.workflow_compiler.compile_for_task(
                task=task,
                plan=plan_snapshot,
                result=result,
            )
            if candidate is not None:
                self.skill_lifecycle.register_candidate(candidate)
                self._emit(
                    event_bus,
                    task.task_id,
                    "workflow.candidate_generated",
                    task.status,
                    {
                        "skill_name": candidate["skill_name"],
                        "version": candidate["version"],
                        "success_rate": candidate["success_rate"],
                        "source_task_ids": candidate["source_task_ids"],
                    },
                )

        events = self.store.list_events(task.task_id)
        self.logger.info(
            "task_finished",
            extra={
                "task_id": task.task_id,
                "status": task.status,
                "graph_id": task.graph_id,
                "node_count": len(task_graph.nodes),
            },
        )
        return TaskExecutionReport(
            task=task,
            result=result,
            events=events,
            task_graph=task_graph,
            critic_verdicts=critic_verdicts,
            completion_evaluation=completion_evaluation,
        )

    def _policy_context_counts(
        self,
        *,
        context_bundle: dict[str, Any],
        workflow_summaries: list[dict[str, Any]],
        vector_matches: list[dict[str, Any]],
        planner_skill_registry_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        def stats(value: Any) -> dict[str, Any]:
            return {
                "items": len(value) if isinstance(value, (dict, list, tuple, set)) else None,
                "json_chars": self._json_chars(value),
            }

        return {
            "memory_summaries": stats(context_bundle["memory_summaries"]),
            "workflow_matches": stats(workflow_summaries),
            "vector_matches": stats(vector_matches),
            "planner_skill_registry_entries": stats(planner_skill_registry_entries),
        }

    def _json_chars(self, value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return len(str(value))

    def _evaluate_completion(
        self,
        *,
        task: Task,
        request: TaskRequest,
        task_graph: TaskGraph,
        task_dir: Any,
        result: ExecutionResult,
        critic_verdicts: list[CriticVerdict],
        context_bundle: dict[str, Any],
    ) -> CompletionEvaluation:
        try:
            evaluation, response = self.completion_evaluator.evaluate_task(
                task=task,
                task_graph=task_graph,
                execution_result=result,
                critic_verdicts=critic_verdicts,
                workflow_matches=context_bundle["workflow_summaries"],
                graph_failure_patterns=context_bundle["graph_failure_patterns"],
                recall_explanations=context_bundle["recall_explanations"],
                user_intent=request.goal,
            )
            self.artifact_manager.write_llm_call(
                task_dir,
                response,
                request_payload={
                    "task_id": task.task_id,
                    "result": {"success": result.success, "message": result.message},
                },
            )
            return evaluation
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "completion_evaluation_failed",
                extra={"task_id": task.task_id, "error": str(exc)},
            )
            return CompletionEvaluation(
                task_id=task.task_id,
                task_satisfied=result.success,
                completion_score=1.0 if result.success else 0.0,
                summary=f"completion evaluation failed: {exc}",
                evidence=[f"execution_result.success={result.success}", result.message],
                primary_failure_layer="completion_evaluation",
                recommended_next_action="manual_review",
                confidence=0.2,
            )

    def _load_conversation_history(
        self, thread_id: str | None, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """加载 thread 最近 N 轮对话用于 Planner 的指代消解。

        仅取最近 limit 轮，按时间升序。每条只保留 role / message / task_id / route，
        避免把 payload 元信息再推给 LLM。
        """
        if not thread_id:
            return []
        try:
            turns = self.store.list_conversation_turns(thread_id)
        except Exception:  # noqa: BLE001
            return []
        if not turns:
            return []
        recent = turns[-limit:]
        return [
            {
                "role": str(turn.role),
                "message": turn.message,
                "task_id": turn.task_id,
                "route": turn.route,
            }
            for turn in recent
        ]

    def _load_recent_task_artifacts(
        self,
        thread_id: str | None,
        current_task_id: str,
        *,
        limit: int = 5,
        per_task_nodes_limit: int = 8,
        per_node_output_keys_limit: int = 10,
    ) -> list[dict[str, Any]]:
        """META-008: pull-model metadata of prior task artifacts in this thread.

        Only emits metadata (task_id / goal / node output field names /
        artifact paths) — never the actual data. The Planner uses this to
        decide whether a new task should reference a prior artifact via
        ``{"context_ref": {...}}`` in a node's ``input_payload``. The
        ExecutionEngine resolves those refs at dispatch time by reading
        ``workspace/tasks/<task_id>/nodes/<node_id>/output.json``.

        The design choice is deliberate: the LLM sees only enough to decide
        whether to pull, not enough to hallucinate data it did not verify.
        """
        if not thread_id:
            return []
        try:
            turns = self.store.list_conversation_turns(thread_id)
        except Exception:  # noqa: BLE001
            return []
        # Collect distinct task_ids in reverse chronological order.
        seen: set[str] = set()
        candidate_task_ids: list[str] = []
        for turn in reversed(turns):
            tid = turn.task_id
            if not tid or tid == current_task_id or tid in seen:
                continue
            seen.add(tid)
            candidate_task_ids.append(tid)
            # Overscan by 2x — we will filter out non-completed tasks below.
            if len(candidate_task_ids) >= limit * 2:
                break
        if not candidate_task_ids:
            return []

        artifacts: list[dict[str, Any]] = []
        for tid in candidate_task_ids:
            try:
                task = self.store.get_task(tid)
            except Exception:  # noqa: BLE001
                continue
            if task is None or str(task.status) != "completed":
                continue
            try:
                task_graph = self.store.get_task_graph(tid)
            except Exception:  # noqa: BLE001
                task_graph = None

            nodes_meta: list[dict[str, Any]] = []
            if task_graph is not None:
                for node in task_graph.nodes[:per_task_nodes_limit]:
                    if str(node.status) != "completed":
                        continue
                    entry: dict[str, Any] = {
                        "node_id": node.node_id,
                        "type": str(node.type),
                        "goal": node.goal,
                    }
                    if node.output:
                        entry["output_keys"] = list(node.output.keys())[:per_node_output_keys_limit]
                    if node.artifacts:
                        entry["artifacts"] = [
                            {
                                "path": a.path,
                                "kind": a.kind,
                                "description": a.description,
                            }
                            for a in node.artifacts[:5]
                        ]
                    nodes_meta.append(entry)

            artifacts.append(
                {
                    "task_id": task.task_id,
                    "goal": task.goal,
                    "status": str(task.status),
                    "summary": task.summary,
                    "plan_summary": task.plan_summary,
                    "created_at": task.created_at,
                    "nodes": nodes_meta,
                }
            )
            if len(artifacts) >= limit:
                break
        return artifacts

    def _prepare_bootstrap_for_prompt(
        self, thread_id: str | None, documents: dict[str, Any]
    ) -> dict[str, Any]:
        """避免同一 thread 内重复发送 bootstrap 全文。

        首次发送完整内容并记录哈希；后续若哈希未变，只发送一个占位 dict，
        由 prompt 指导 LLM 沿用之前看过的版本。
        """
        try:
            serialized = json.dumps(documents, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return documents
        current_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

        cache_key = thread_id or "__no_thread__"
        previous_hash = self._bootstrap_sent_hashes.get(cache_key)
        if previous_hash == current_hash:
            return {
                "__unchanged": True,
                "hash": current_hash,
                "note": "本 thread 之前已发送过完整 bootstrap 文档，请沿用之前的规则。",
            }
        self._bootstrap_sent_hashes[cache_key] = current_hash
        return documents

    def _build_context_bundle(self, request: TaskRequest) -> dict[str, Any]:
        workspace_paths = self.workspace_manager.ensure()
        memory_decision = self.permission_manager.evaluate_memory_access(request, phase="planning")
        self.store.record_permission(memory_decision)
        if memory_decision.status == "granted":
            memory_summaries = self.memory_manager.recall(request.goal)
            hybrid_recall = self.hybrid_memory.recall_for_planning(request.goal)
        else:
            memory_summaries = []
            hybrid_recall = self._empty_hybrid_recall()
        workflow_summaries = hybrid_recall["workflow_matches"]
        memory_summaries = self._dedupe_memory_against_workflows(
            memory_summaries,
            workflow_summaries,
        )
        vector_matches = self._dedupe_vector_against_context(
            hybrid_recall["vector_matches"],
            memory_summaries=memory_summaries,
            workflow_summaries=workflow_summaries,
        )
        if vector_matches != hybrid_recall["vector_matches"]:
            hybrid_recall = dict(hybrid_recall)
            hybrid_recall["vector_matches"] = vector_matches
            hybrid_recall["recall_explanations"] = self._trim_vector_explanations(
                hybrid_recall["recall_explanations"],
                vector_count=len(vector_matches),
            )
        failure_patterns = hybrid_recall["failure_patterns"]
        bootstrap = self.workspace_manager.build_bootstrap_context(
            memory_summaries=memory_summaries,
            workflow_summaries=workflow_summaries,
        )
        return {
            "workspace_paths": workspace_paths,
            "bootstrap": bootstrap,
            "memory_summaries": memory_summaries,
            "workflow_summaries": workflow_summaries,
            "vector_matches": vector_matches,
            "failure_patterns": failure_patterns,
            "graph_workflow_matches": hybrid_recall["graph_workflow_matches"],
            "graph_skill_matches": hybrid_recall["graph_skill_matches"],
            "graph_failure_patterns": hybrid_recall["graph_failure_patterns"],
            "recall_explanations": hybrid_recall["recall_explanations"],
            "hybrid_recall": hybrid_recall,
        }

    def _context_task_id(self, item: dict[str, Any]) -> str | None:
        task_id = item.get("task_id")
        if task_id is None and isinstance(item.get("metadata"), dict):
            task_id = item["metadata"].get("task_id")
        if task_id is None:
            task_id = item.get("entry_id")
        return str(task_id) if task_id else None

    def _dedupe_memory_against_workflows(
        self,
        memory_summaries: list[dict[str, Any]],
        workflow_summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        workflow_task_ids = {
            task_id
            for task_id in (self._context_task_id(item) for item in workflow_summaries)
            if task_id is not None
        }
        if not workflow_task_ids:
            return memory_summaries
        return [
            item
            for item in memory_summaries
            if self._context_task_id(item) not in workflow_task_ids
        ]

    def _dedupe_vector_against_context(
        self,
        vector_matches: list[dict[str, Any]],
        *,
        memory_summaries: list[dict[str, Any]],
        workflow_summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        context_task_ids = {
            task_id
            for task_id in (
                self._context_task_id(item) for item in [*memory_summaries, *workflow_summaries]
            )
            if task_id is not None
        }
        if not context_task_ids:
            return vector_matches
        return [
            item
            for item in vector_matches
            if self._context_task_id(item) not in context_task_ids
        ]

    def _trim_vector_explanations(
        self,
        explanations: list[dict[str, Any]],
        *,
        vector_count: int,
    ) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        seen_vector = 0
        for item in explanations:
            if item.get("channel") != "vector":
                kept.append(item)
                continue
            if seen_vector < vector_count:
                kept.append(item)
                seen_vector += 1
        return kept

    def _empty_hybrid_recall(self) -> dict[str, Any]:
        return {
            "workflow_matches": [],
            "vector_matches": [],
            "failure_patterns": [],
            "graph_workflow_matches": [],
            "graph_skill_matches": [],
            "graph_failure_patterns": [],
            "recall_explanations": [],
        }

    def _apply_request_overrides(self, request: TaskRequest, overrides: dict[str, Any]) -> None:
        if not overrides:
            return
        if "payload" in overrides:
            request.payload = {**request.payload, **dict(overrides["payload"])}
        if "metadata" in overrides:
            metadata_override = dict(overrides["metadata"])
            granted_paths = metadata_override.pop("grant_path_access", [])
            request.metadata = {**request.metadata, **metadata_override}
            if granted_paths:
                existing_paths = self._normalize_authorized_path_values(
                    request.metadata.get("authorized_paths", [])
                )
                merged_paths = self._merge_authorized_path_values(existing_paths, granted_paths)
                request.metadata["authorized_paths"] = merged_paths
        constraints = dict(overrides.get("constraints", {}))
        if constraints:
            request.constraints.allow_network = bool(
                constraints.get("allow_network", request.constraints.allow_network)
            )
            request.constraints.allow_delete = bool(
                constraints.get("allow_delete", request.constraints.allow_delete)
            )
            request.constraints.allow_memory_read = bool(
                constraints.get("allow_memory_read", request.constraints.allow_memory_read)
            )
            request.constraints.timeout_seconds = int(
                constraints.get("timeout_seconds", request.constraints.timeout_seconds)
            )
            if "max_permission" in constraints:
                request.constraints.max_permission = constraints["max_permission"]

    def _authorized_paths_for_request(self, request: TaskRequest) -> list[Path]:
        merged: list[Path] = []
        seen: set[str] = set()
        for item in list(self.sandbox_config.authorized_paths):
            resolved = Path(item).expanduser().resolve()
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            merged.append(resolved)
        for value in self._normalize_authorized_path_values(
            request.metadata.get("authorized_paths", [])
        ):
            resolved = Path(value).expanduser().resolve()
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            merged.append(resolved)
        return merged

    def _normalize_authorized_path_values(self, value: Any) -> list[str]:
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = value
        else:
            items = []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            resolved = str(Path(text).expanduser().resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            normalized.append(resolved)
        return normalized

    def _merge_authorized_path_values(self, existing: list[str], incoming: Any) -> list[str]:
        combined = list(existing)
        combined.extend(self._normalize_authorized_path_values(incoming))
        deduped: list[str] = []
        seen: set[str] = set()
        for item in combined:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _emit(
        self,
        event_bus: EventBus,
        task_id: str,
        event_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        event_bus.publish(
            TaskEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                event_type=event_type,
                status=str(status),
                payload=payload,
            )
        )

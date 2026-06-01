from __future__ import annotations

from copy import deepcopy
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from omniagent.critic import Critic
from omniagent.dispatcher import SkillDispatcher
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.events import EventBus
from omniagent.models import (
    CriticAction,
    CriticVerdict,
    ExecutionResult,
    PermissionDecision,
    PermissionLevel,
    SkillSelection,
    Task,
    TaskEvent,
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskNodeType,
    TaskRequest,
    TaskRunContext,
    utc_now,
)
from omniagent.permissions import PermissionManager
from omniagent.planner import Planner
from omniagent.skill_registry import EXECUTOR_TO_BUILTIN, filter_skill_registry_entries_for_planner
from omniagent.storage import SQLiteStore
from omniagent.workspace import TaskArtifactManager


DEPENDENCY_OUTPUT_TEXT_LIMIT = 2000
DEPENDENCY_OUTPUT_LIST_LIMIT = 20
DEPENDENCY_OUTPUT_DICT_LIMIT = 40
DEPENDENCY_OUTPUT_DEPTH_LIMIT = 4


def _compact_dependency_output(value: Any, *, depth: int = 0) -> Any:
    if depth > DEPENDENCY_OUTPUT_DEPTH_LIMIT:
        return "<truncated>"
    if isinstance(value, str):
        if len(value) <= DEPENDENCY_OUTPUT_TEXT_LIMIT:
            return value
        return value[:DEPENDENCY_OUTPUT_TEXT_LIMIT] + "...<truncated>"
    if isinstance(value, list):
        return [
            _compact_dependency_output(item, depth=depth + 1)
            for item in value[:DEPENDENCY_OUTPUT_LIST_LIMIT]
        ]
    if isinstance(value, dict):
        has_preview = "body_preview" in value
        compacted: dict[str, Any] = {}
        for key, item in list(value.items())[:DEPENDENCY_OUTPUT_DICT_LIMIT]:
            key_text = str(key)
            # HTTP results often carry the same large response twice as
            # `body` + `body_preview`. Downstream nodes only need one copy.
            if key_text == "body" and has_preview:
                compacted["body_omitted"] = "body_preview already present"
                continue
            compacted[key_text] = _compact_dependency_output(item, depth=depth + 1)
        return compacted
    return value


class ExecutionEngine:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        artifact_manager: TaskArtifactManager,
        dispatcher: SkillDispatcher,
        critic: Critic,
        planner: Planner,
        permission_manager: PermissionManager,
        graph_memory: Any | None = None,
    ) -> None:
        self.store = store
        self.artifact_manager = artifact_manager
        self.dispatcher = dispatcher
        self.critic = critic
        self.planner = planner
        self.permission_manager = permission_manager
        self.graph_memory = graph_memory
        self.builtin_to_executor = {value: key for key, value in EXECUTOR_TO_BUILTIN.items()}

    def execute(
        self,
        *,
        task: Task,
        task_request: TaskRequest,
        task_graph: TaskGraph,
        context: TaskRunContext,
        event_bus: EventBus,
    ) -> tuple[TaskGraph, ExecutionResult, list[CriticVerdict]]:
        critic_verdicts: list[CriticVerdict] = []
        task_graph.validate()
        self.store.save_task_graph(task.task_id, task_graph)

        fatal_result: ExecutionResult | None = None
        while True:
            ready_nodes = task_graph.ready_nodes()
            if not ready_nodes:
                break

            for node in ready_nodes:
                if node.status == TaskNodeStatus.PENDING:
                    node.status = TaskNodeStatus.READY
                    self.store.upsert_task_node(task.task_id, node)

            for node in ready_nodes:
                result = self._run_node(
                    task=task,
                    task_request=task_request,
                    node=node,
                    task_graph=task_graph,
                    context=context,
                    event_bus=event_bus,
                    critic_verdicts=critic_verdicts,
                )
                if result is not None:
                    fatal_result = result
                    break
            if fatal_result is not None:
                break

        if fatal_result is None:
            failed_nodes = [
                node for node in task_graph.nodes if node.status == TaskNodeStatus.FAILED
            ]
            finalize_nodes = [
                node for node in task_graph.nodes if node.type == TaskNodeType.FINALIZE
            ]
            if failed_nodes:
                fatal_result = ExecutionResult(
                    success=False,
                    status="failed",
                    message=failed_nodes[0].error_message or f"节点失败: {failed_nodes[0].node_id}",
                    output={
                        "failed_nodes": [node.node_id for node in failed_nodes],
                        "task_graph": task_graph.to_dict(),
                    },
                    executor="task_graph",
                    error_code=ErrorCode.EXECUTION_ERROR,
                )
            else:
                final_output = finalize_nodes[-1].output if finalize_nodes else {}
                fatal_result = ExecutionResult(
                    success=True,
                    status="completed",
                    message="TaskGraph 执行完成",
                    output={"task_graph": task_graph.to_dict(), "final_output": final_output},
                    executor="task_graph",
                )

        return task_graph, fatal_result, critic_verdicts

    def _run_node(
        self,
        *,
        task: Task,
        task_request: TaskRequest,
        node: TaskNode,
        task_graph: TaskGraph,
        context: TaskRunContext,
        event_bus: EventBus,
        critic_verdicts: list[CriticVerdict],
    ) -> ExecutionResult | None:
        node.attempts += 1
        node.status = TaskNodeStatus.RUNNING
        node.error_message = None
        node.metadata.pop("pending_checkpoint", None)
        self.store.upsert_task_node(task.task_id, node)
        self._emit_node_event(
            event_bus, task.task_id, "task.node.started", node, {"attempts": node.attempts}
        )

        try:
            selected_skill: str | None = None
            selection = self._select_skill_for_node(node, task_request)
            self._record_selection_signal_if_needed(task, node, selection)
            permission_result = self._check_node_permission(
                task_request, node, task, selection=selection
            )
            if permission_result is not None:
                result = permission_result
            elif node.type == TaskNodeType.FINALIZE:
                result = self._finalize_node(task_graph, node)
            elif node.type == TaskNodeType.APPROVAL:
                result = self._approval_node(task_request, node)
            elif node.type == TaskNodeType.DECISION:
                result = self._decision_node(task_request, node)
            elif node.type == TaskNodeType.WAIT:
                result = self._wait_node(task_request, node)
            elif node.type == TaskNodeType.REPLAN:
                result = self._explicit_replan_node(task_request, node, task_graph, context)
            else:
                # META-008: resolve any {"context_ref": {...}} entries in
                # the node's input_payload by loading the actual value from
                # the referenced task's workspace snapshot. Runs only for
                # dispatched ACTION nodes — control-flow nodes (approval /
                # decision / wait / replan / finalize) don't need a payload.
                self._resolve_node_context_refs(node)
                self._inject_dependency_outputs(node, task_graph)
                result, selected_skill = self.dispatcher.dispatch(
                    node, context, selection=selection
                )
                if selected_skill:
                    node.metadata["selected_skill"] = selected_skill
        except OmniAgentError as exc:
            if self.graph_memory is not None and exc.code == ErrorCode.VALIDATION_ERROR:
                details = dict(exc.details)
                if details.get("contract_action"):
                    self.graph_memory.record_skill_signal(
                        task=task,
                        node=node,
                        signal="contract_validation_failed",
                        skill_name=str(details.get("skill_name") or node.skill_name or ""),
                        message=exc.message,
                        details=details,
                    )
            result = ExecutionResult(
                success=False,
                status="failed",
                message=exc.message,
                output={"details": exc.details},
                logs=[str(exc.to_dict())],
                executor=node.executor_hint or node.skill_name or "node",
                error_code=exc.code,
            )
        except Exception as exc:
            result = ExecutionResult(
                success=False,
                status="failed",
                message=f"节点执行异常: {exc}",
                output={
                    "details": {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                },
                logs=[f"Unhandled exception in node {node.node_id}: {type(exc).__name__}: {exc}"],
                executor=node.executor_hint or node.skill_name or "node",
                error_code=ErrorCode.EXECUTION_ERROR,
            )

        result = self._maybe_convert_path_authorization_checkpoint(
            task_request=task_request,
            node=node,
            result=result,
        )
        node.output = dict(result.output)
        node.artifacts = list(result.artifacts)
        self.store.log_execution(task.task_id, result.executor, node.input_payload, result)

        if result.status in {"waiting_input", "suspended"}:
            return self._pause_node_for_input(
                task=task,
                node=node,
                task_graph=task_graph,
                context=context,
                event_bus=event_bus,
                result=result,
            )

        if result.success:
            if selected_skill:
                self.dispatcher.skill_registry.record_execution(selected_skill, success=True)
            node.status = TaskNodeStatus.COMPLETED
            node.error_message = None
            self.store.upsert_task_node(task.task_id, node)
            self.artifact_manager.write_node_snapshot(context.task_dir, node)
            self._emit_node_event(
                event_bus,
                task.task_id,
                "task.node.completed",
                node,
                {"message": result.message, "executor": result.executor},
            )
            task_graph.updated_at = utc_now()
            self.store.save_task_graph(task.task_id, task_graph)
            return None

        node.status = TaskNodeStatus.FAILED
        node.error_message = result.message
        if selected_skill:
            self.dispatcher.skill_registry.record_execution(selected_skill, success=False)
        self.store.upsert_task_node(task.task_id, node)
        self.artifact_manager.write_node_snapshot(context.task_dir, node)
        self._emit_node_event(
            event_bus,
            task.task_id,
            "task.node.failed",
            node,
            {"message": result.message, "error_code": result.error_code},
        )

        critic_failure_patterns = context.failure_patterns
        critic_graph_failure_patterns = context.graph_failure_patterns
        if node.metadata.get("replanned_from") or node.metadata.get("replan_attempted"):
            critic_failure_patterns = []
            critic_graph_failure_patterns = []

        verdict, llm_response = self.critic.evaluate_node(
            node=node,
            execution_result=result,
            failure_patterns=critic_failure_patterns,
            workflow_matches=context.workflow_matches,
            graph_failure_patterns=critic_graph_failure_patterns,
            recall_explanations=list(context.hybrid_recall.get("recall_explanations", [])),
            user_intent=context.task_request.goal,
            task_goal=context.task.goal,
        )
        critic_verdicts.append(verdict)
        self.store.record_critic_verdict(task.task_id, verdict)
        self.artifact_manager.write_critic_verdict(context.task_dir, verdict)
        self.artifact_manager.write_llm_call(
            context.task_dir,
            llm_response,
            request_payload={
                "node": node.to_dict(),
                "result": {
                    "success": result.success,
                    "message": result.message,
                    "error_code": result.error_code,
                },
            },
        )

        if verdict.action == CriticAction.ACCEPT:
            self.artifact_manager.write_node_snapshot(context.task_dir, node)
            return result

        if verdict.action == CriticAction.FAIL:
            self.artifact_manager.write_node_snapshot(context.task_dir, node)
            return result

        if verdict.action == CriticAction.RETRY and node.attempts <= node.retry_policy.max_retries:
            node.status = TaskNodeStatus.RETRYING
            self.store.upsert_task_node(task.task_id, node)
            self._emit_node_event(
                event_bus, task.task_id, "task.node.retrying", node, {"verdict": verdict.summary}
            )
            if node.retry_policy.backoff_seconds > 0:
                time.sleep(node.retry_policy.backoff_seconds)
            node.status = TaskNodeStatus.PENDING
            self.store.upsert_task_node(task.task_id, node)
            return None

        if verdict.action == CriticAction.DEGRADE and verdict.downgrade_target:
            node.skill_name = verdict.downgrade_target
            node.required_skills = [verdict.downgrade_target]
            node.executor_hint = self.builtin_to_executor.get(
                verdict.downgrade_target, node.executor_hint
            )
            node.status = TaskNodeStatus.PENDING
            node.metadata["degraded_from"] = node.metadata.get("selected_skill") or node.skill_name
            self.store.upsert_task_node(task.task_id, node)
            self._emit_node_event(
                event_bus,
                task.task_id,
                "task.node.degraded",
                node,
                {"downgrade_target": verdict.downgrade_target},
            )
            return None

        recovery_result, recovery_skill = self.dispatcher.recover_incompatible_skill(
            node,
            context,
            failed_skill=selected_skill,
            failure_message=result.message,
            critic_summary=verdict.summary,
        )
        if recovery_result is not None:
            node.output = dict(recovery_result.output)
            node.artifacts = list(recovery_result.artifacts)
            return self._pause_node_for_input(
                task=task,
                node=node,
                task_graph=task_graph,
                context=context,
                event_bus=event_bus,
                result=recovery_result,
            )
        if recovery_skill is not None:
            node.status = TaskNodeStatus.PENDING
            node.error_message = None
            node.output = {}
            node.artifacts = []
            node.metadata.pop("pending_checkpoint", None)
            node.metadata["selected_skill"] = recovery_skill
            self.store.upsert_task_node(task.task_id, node)
            self.artifact_manager.write_node_snapshot(context.task_dir, node)
            self._emit_node_event(
                event_bus,
                task.task_id,
                "task.node.skill_generated",
                node,
                {
                    "failed_skill": selected_skill,
                    "replacement_skill": recovery_skill,
                    "trigger": "incompatible_skill",
                },
            )
            task_graph.updated_at = utc_now()
            self.store.save_task_graph(task.task_id, task_graph)
            return None

        if (
            verdict.action == CriticAction.REPLAN
            and verdict.replan_goal
            and not node.metadata.get("replan_attempted")
        ):
            node.metadata["replan_attempted"] = True
            recovery_plan, response = self.planner.replan(
                original_request=task_request,
                replan_goal=verdict.replan_goal,
                bootstrap=context.bootstrap,
                workflow_matches=context.workflow_matches,
                vector_matches=context.vector_matches,
                skill_registry_entries=filter_skill_registry_entries_for_planner(
                    [entry.to_dict() for entry in self.dispatcher.skill_registry.list_entries()],
                    task_request,
                    goal=verdict.replan_goal,
                ),
            )
            self.artifact_manager.write_llm_call(
                context.task_dir,
                response,
                request_payload={"replan_goal": verdict.replan_goal, "failed_node": node.to_dict()},
            )
            applied = self._apply_replan(task_graph, node, recovery_plan.graph)
            if applied:
                self.store.save_task_graph(task.task_id, task_graph)
                self._emit_node_event(
                    event_bus,
                    task.task_id,
                    "task.node.replanned",
                    node,
                    {"summary": recovery_plan.summary},
                )
                return None

        self.artifact_manager.write_node_snapshot(context.task_dir, node)
        return result

    def _maybe_convert_path_authorization_checkpoint(
        self,
        *,
        task_request: TaskRequest,
        node: TaskNode,
        result: ExecutionResult,
    ) -> ExecutionResult:
        if result.status in {"waiting_input", "suspended"}:
            return result
        if result.error_code != ErrorCode.PERMISSION_DENIED:
            return result
        details = dict(result.output.get("details", {})) if isinstance(result.output, dict) else {}
        resolved_path = str(details.get("resolved_path", "")).strip()
        hint_env = str(details.get("hint_env", "")).strip()
        if not resolved_path or not hint_env:
            return result

        resolved = Path(resolved_path).expanduser()
        authorized_path = resolved if resolved.is_dir() else resolved.parent
        action = str(node.input_payload.get("action", "read"))
        permission = "filesystem.write" if action in {"write", "append"} else "filesystem.read"
        level = PermissionLevel.L2 if permission == "filesystem.write" else PermissionLevel.L1
        existing_authorized = self._normalize_authorized_paths(
            task_request.metadata.get("authorized_paths", [])
        )
        prompt = f"检测到任务需要访问外部路径 {authorized_path}，当前未授权。是否授权后继续执行？"
        return ExecutionResult(
            success=False,
            status="waiting_input",
            message=prompt,
            output={
                "checkpoint": {
                    "checkpoint_type": "permission",
                    "node_id": node.node_id,
                    "prompt": prompt,
                    "permission": permission,
                    "level": str(level),
                    "rationale": result.message,
                    "scope": "node",
                    "resource_kind": "path",
                    "resource_ref": str(authorized_path),
                    "authorization_type": "path_access",
                    "authorized_path": str(authorized_path),
                    "requested_path": details.get("requested_path"),
                    "resolved_path": resolved_path,
                    "existing_authorized_paths": existing_authorized,
                    "hint_env": hint_env,
                }
            },
            executor=result.executor,
            error_code=result.error_code,
        )

    def _normalize_authorized_paths(self, value: Any) -> list[str]:
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

    def _pause_node_for_input(
        self,
        *,
        task: Task,
        node: TaskNode,
        task_graph: TaskGraph,
        context: TaskRunContext,
        event_bus: EventBus,
        result: ExecutionResult,
    ) -> ExecutionResult:
        node.status = TaskNodeStatus.READY
        node.error_message = result.message
        node.metadata["pending_checkpoint"] = dict(result.output.get("checkpoint", {}))
        self.store.upsert_task_node(task.task_id, node)
        self.artifact_manager.write_node_snapshot(context.task_dir, node)
        event_type = (
            "task.node.awaiting_input"
            if result.status == "waiting_input"
            else "task.node.suspended"
        )
        self._emit_node_event(
            event_bus,
            task.task_id,
            event_type,
            node,
            {"message": result.message, "checkpoint": result.output.get("checkpoint", {})},
        )
        task_graph.updated_at = utc_now()
        self.store.save_task_graph(task.task_id, task_graph)
        return result

    def _check_node_permission(
        self,
        task_request: TaskRequest,
        node: TaskNode,
        task: Task,
        *,
        selection: SkillSelection,
    ) -> ExecutionResult | None:
        node_request = TaskRequest(
            goal=node.goal,
            expected_output=task_request.expected_output,
            payload=dict(node.input_payload),
            executor=node.executor_hint or "auto",
            skill_name=node.skill_name,
            task_id=task_request.task_id,
            mode=task_request.mode,
            source=task_request.source,
            context_refs=list(task_request.context_refs),
            constraints=task_request.constraints,
            preferred_skills=list(task_request.preferred_skills),
            metadata=task_request.metadata,
            execution_mode=task_request.execution_mode,
        )
        required_permissions: list[str] = []
        if selection.registry_match:
            required_permissions.extend(selection.registry_match.permission_requirements)
            required_permissions.extend(
                self._contract_side_effect_permissions(selection=selection, node=node)
            )
        decision = self.permission_manager.evaluate(
            node_request,
            node=node,
            selection=selection,
            required_permissions=required_permissions,
            phase="execution",
        )
        self.store.record_permission(decision)
        if (
            "skill.activate" in decision.details.get("activation_permissions", [])
            and decision.permission != "skill.activate"
        ):
            self.store.record_permission(
                PermissionDecision(
                    task_id=decision.task_id,
                    permission="skill.activate",
                    level=PermissionLevel.L3,
                    status=decision.status,
                    rationale="自动生成 Skill 激活沿用当前节点权限审批结果",
                    scope=decision.scope,
                    node_id=decision.node_id,
                    skill_name=decision.skill_name,
                    resource_kind="skill",
                    resource_ref=decision.skill_name,
                    sandbox_level=decision.sandbox_level,
                    source=decision.source,
                    policy=decision.policy,
                    requires_approval=decision.requires_approval,
                    details={
                        **decision.details,
                        "permissions": ["skill.activate"],
                    },
                )
            )
        node.metadata["permission_context"] = {
            "permission": decision.permission,
            "level": str(decision.level),
            "status": decision.status,
            "scope": decision.scope,
            "resource_kind": decision.resource_kind,
            "resource_ref": decision.resource_ref,
            "sandbox_level": decision.sandbox_level,
        }
        if decision.status == "granted":
            return None
        if self.graph_memory is not None:
            self.graph_memory.record_skill_signal(
                task=task,
                node=node,
                signal="permission_denied",
                skill_name=selection.selected_skill or node.skill_name or "",
                message=decision.rationale,
                details={
                    "permission": decision.permission,
                    "level": str(decision.level),
                    "sandbox_level": decision.sandbox_level,
                    "selection": dict(selection.explanation),
                },
            )
        return ExecutionResult(
            success=False,
            status="waiting_input",
            message=f"等待用户审批: {decision.rationale}",
            output={
                "checkpoint": {
                    "checkpoint_type": "permission",
                    "node_id": node.node_id,
                    "permission": decision.permission,
                    "level": str(decision.level),
                    "rationale": decision.rationale,
                    "scope": decision.scope,
                    "resource_kind": decision.resource_kind,
                    "resource_ref": decision.resource_ref,
                    "skill_name": decision.skill_name,
                    "sandbox_level": decision.sandbox_level,
                    "policy": decision.policy,
                    "details": dict(decision.details),
                }
            },
            executor=node.executor_hint or node.skill_name or "node",
        )

    def _select_skill_for_node(self, node: TaskNode, task_request: TaskRequest) -> SkillSelection:
        if node.type != TaskNodeType.ACTION:
            return SkillSelection(
                node_id=node.node_id,
                selected_skill=None,
                executor_hint=node.executor_hint,
                explanation={
                    "source": "control_node",
                    "reason": "control-flow nodes do not inherit preferred skill permissions",
                },
            )
        if (
            node.executor_hint
            and node.executor_hint not in EXECUTOR_TO_BUILTIN
            and node.skill_name is None
            and not node.required_skills
        ):
            return SkillSelection(
                node_id=node.node_id,
                selected_skill=None,
                executor_hint=node.executor_hint,
                explanation={
                    "source": "executor_hint",
                    "reason": "executor-only nodes do not inherit preferred skills",
                },
            )
        return self.dispatcher.skill_registry.select(node, task_request.preferred_skills)

    def _contract_side_effect_permissions(
        self, *, selection: SkillSelection, node: TaskNode
    ) -> list[str]:
        entry = selection.registry_match
        if entry is None:
            return []
        action = node.input_payload.get("action")
        if not isinstance(action, str) or not action.strip():
            return []
        contract = entry.contract_for_action(action.strip().lower())
        if contract is None:
            return []
        return [str(item) for item in contract.side_effects if str(item).startswith("filesystem.")]

    def _record_selection_signal_if_needed(
        self,
        task: Task,
        node: TaskNode,
        selection: SkillSelection,
    ) -> None:
        if self.graph_memory is None:
            return
        source = str(selection.explanation.get("source", ""))
        if source not in {"contract_gap", "contract_mismatch"}:
            return
        self.graph_memory.record_skill_signal(
            task=task,
            node=node,
            signal=source,
            skill_name=selection.selected_skill
            or (selection.registry_match.skill_name if selection.registry_match else "")
            or node.skill_name
            or "",
            message=selection.gap_reason or "skill selection contract signal",
            details=dict(selection.explanation),
        )

    # ------------------------------------------------------------------
    # META-008: cross-task artifact pull model
    # ------------------------------------------------------------------

    def _inject_dependency_outputs(self, node: TaskNode, task_graph: TaskGraph) -> None:
        if not node.dependencies:
            return
        if "dependency_outputs" in node.input_payload:
            return

        dependency_outputs = {
            dep: _compact_dependency_output(task_graph.get_node(dep).output)
            for dep in node.dependencies
        }
        if not dependency_outputs:
            return

        node.metadata.setdefault("input_payload_original", deepcopy(node.input_payload))
        node.input_payload = {
            **node.input_payload,
            "dependency_outputs": dependency_outputs,
        }
        node.metadata["dependency_outputs_injected"] = list(dependency_outputs)

    def _resolve_node_context_refs(self, node: TaskNode) -> None:
        """Replace any ``{"context_ref": {...}}`` entries in ``node.input_payload``.

        Raises :class:`OmniAgentError` (VALIDATION_ERROR) when a ref cannot
        be resolved — the surrounding ``try`` in :meth:`_run_node` converts
        that into a normal node-level failure so the Critic can decide
        whether to retry / replan / escalate.

        For audit, the unresolved payload is preserved under
        ``node.metadata["input_payload_original"]`` (only when at least
        one ref is actually resolved, to keep snapshots small).
        """
        if not node.input_payload:
            return
        if not self._payload_contains_context_ref(node.input_payload):
            return
        original = dict(node.input_payload)
        workspace_root = self.artifact_manager.workspace_manager.root
        resolved = self._resolve_context_refs(node.input_payload, workspace_root=workspace_root)
        if not isinstance(resolved, dict):
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "context_ref 解析后 input_payload 不再是 dict",
                details={"node_id": node.node_id, "resolved_type": type(resolved).__name__},
            )
        node.metadata["input_payload_original"] = original
        node.input_payload = resolved

    @staticmethod
    def _payload_contains_context_ref(value: Any) -> bool:
        if isinstance(value, dict):
            if "context_ref" in value and isinstance(value["context_ref"], dict):
                return True
            return any(ExecutionEngine._payload_contains_context_ref(v) for v in value.values())
        if isinstance(value, list):
            return any(ExecutionEngine._payload_contains_context_ref(v) for v in value)
        return False

    def _resolve_context_refs(self, value: Any, *, workspace_root: Path) -> Any:
        if isinstance(value, dict):
            # A sole {"context_ref": {...}} dict → replace with resolved value.
            if (
                "context_ref" in value
                and len(value) == 1
                and isinstance(value["context_ref"], dict)
            ):
                return self._load_context_ref(value["context_ref"], workspace_root=workspace_root)
            return {
                k: self._resolve_context_refs(v, workspace_root=workspace_root)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_context_refs(v, workspace_root=workspace_root) for v in value]
        return value

    def _load_context_ref(self, ref: dict[str, Any], *, workspace_root: Path) -> Any:
        import json as _json

        task_id = ref.get("task_id")
        if not task_id or not isinstance(task_id, str):
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "context_ref 缺少 task_id",
                details={"ref": ref},
            )
        task_dir = workspace_root / "tasks" / task_id
        if not task_dir.exists():
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"context_ref 指向的任务目录不存在: {task_id}",
                details={"ref": ref, "task_dir": str(task_dir)},
            )

        # Artifact path branch: read a file directly.
        artifact_path = ref.get("artifact_path")
        if artifact_path:
            candidate = Path(artifact_path)
            if not candidate.is_absolute():
                parts = candidate.parts
                # "workspace/tasks/..." is relative to workspace's parent.
                if parts and parts[0] == "workspace":
                    candidate = workspace_root.parent / candidate
                else:
                    candidate = workspace_root / candidate
            try:
                candidate_resolved = candidate.resolve()
            except Exception as exc:  # noqa: BLE001
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"context_ref artifact 路径解析失败: {candidate}: {exc}",
                    details={"ref": ref},
                ) from exc
            # Sandbox check: artifact must live under the referenced task dir.
            try:
                candidate_resolved.relative_to(task_dir.resolve())
            except ValueError as exc:
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"context_ref artifact 路径越界（必须在 task_dir 下）: {candidate_resolved}",
                    details={"ref": ref, "task_dir": str(task_dir)},
                ) from exc
            if not candidate_resolved.exists():
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"context_ref artifact 文件不存在: {candidate_resolved}",
                    details={"ref": ref},
                )
            try:
                return candidate_resolved.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"context_ref artifact 读取失败: {candidate_resolved}: {exc}",
                    details={"ref": ref},
                ) from exc

        # Node output branch: load output.json and optionally pick a field.
        node_id = ref.get("node_id")
        if not node_id or not isinstance(node_id, str):
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "context_ref 必须提供 node_id 或 artifact_path",
                details={"ref": ref},
            )
        output_path = task_dir / "nodes" / node_id / "output.json"
        if not output_path.exists():
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"context_ref 指向的节点输出不存在: {task_id}/{node_id}",
                details={"ref": ref, "output_path": str(output_path)},
            )
        try:
            output = _json.loads(output_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"context_ref 节点输出解析失败: {exc}",
                details={"ref": ref, "output_path": str(output_path)},
            ) from exc
        field = ref.get("field")
        if field is None:
            return output
        if field == "output" and isinstance(output, dict) and "output" not in output:
            return output.get("content", output)
        if not isinstance(output, dict) or field not in output:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"context_ref 字段不存在: {field}",
                details={
                    "ref": ref,
                    "available_keys": list(output.keys()) if isinstance(output, dict) else None,
                },
            )
        return output[field]

    def _finalize_node(self, task_graph: TaskGraph, node: TaskNode) -> ExecutionResult:
        dependency_outputs = {dep: task_graph.get_node(dep).output for dep in node.dependencies}
        return ExecutionResult(
            success=True,
            status="completed",
            message="节点结果汇总完成",
            output={
                "dependency_outputs": dependency_outputs,
                "completed_nodes": [
                    item.node_id
                    for item in task_graph.nodes
                    if item.status == TaskNodeStatus.COMPLETED
                ],
            },
            executor="finalize",
        )

    def _approval_node(self, task_request: TaskRequest, node: TaskNode) -> ExecutionResult:
        response = self._checkpoint_response(
            task_request, node, expected_type=TaskNodeType.APPROVAL.value
        )
        if response is not None:
            return ExecutionResult(
                success=True,
                status="completed",
                message=str(
                    node.input_payload.get("confirm_message", "approval 节点已收到用户确认")
                ),
                output={
                    "approval_payload": node.input_payload,
                    "approval_response": response,
                },
                executor="approval",
            )
        return ExecutionResult(
            success=False,
            status="waiting_input",
            message=str(node.input_payload.get("prompt", "等待用户审批后继续执行")),
            output={
                "checkpoint": {
                    "checkpoint_type": "approval",
                    "node_id": node.node_id,
                    "prompt": node.input_payload.get("prompt"),
                    "approval_payload": node.input_payload,
                }
            },
            executor="approval",
        )

    def _decision_node(self, task_request: TaskRequest, node: TaskNode) -> ExecutionResult:
        response = self._checkpoint_response(
            task_request, node, expected_type=TaskNodeType.DECISION.value
        )
        if response is not None:
            return ExecutionResult(
                success=True,
                status="completed",
                message=str(
                    node.input_payload.get("confirm_message", "decision 节点已记录用户选择")
                ),
                output={
                    "decision_payload": node.input_payload,
                    "decision_response": response,
                },
                executor="decision",
            )
        return ExecutionResult(
            success=False,
            status="waiting_input",
            message=str(node.input_payload.get("prompt", "等待用户提供决策输入后继续执行")),
            output={
                "checkpoint": {
                    "checkpoint_type": "decision",
                    "node_id": node.node_id,
                    "prompt": node.input_payload.get("prompt"),
                    "options": list(node.input_payload.get("options", [])),
                    "decision_payload": node.input_payload,
                }
            },
            executor="decision",
        )

    def _wait_node(self, task_request: TaskRequest, node: TaskNode) -> ExecutionResult:
        response = self._wait_checkpoint_response(task_request, node)
        if response is not None:
            return ExecutionResult(
                success=True,
                status="completed",
                message=str(node.input_payload.get("confirm_message", "wait 节点已恢复执行")),
                output={
                    "wait_payload": dict(node.input_payload),
                    "wait_response": response,
                    "resume_trigger": str(response.get("checkpoint_type", "timer")),
                    "resumed_at": response.get("resumed_at", utc_now()),
                    "event_payload": response.get("event_payload"),
                },
                executor="wait",
            )

        now = self._utc_now()
        wait_until = self._resolve_wait_until(node.input_payload, now=now)
        if wait_until is not None and wait_until <= now:
            return ExecutionResult(
                success=True,
                status="completed",
                message=str(node.input_payload.get("confirm_message", "等待时间已到，继续执行")),
                output={
                    "wait_payload": dict(node.input_payload),
                    "resume_trigger": "timer",
                    "resumed_at": self._datetime_to_str(now),
                },
                executor="wait",
            )

        event_key = str(node.input_payload.get("event_key", "")).strip() or None
        expires_at = self._resolve_expiry(node.input_payload, now=now)
        if wait_until is not None:
            prompt = str(
                node.input_payload.get(
                    "prompt", f"等待至 {self._datetime_to_str(wait_until)} 后恢复执行"
                )
            )
            return ExecutionResult(
                success=False,
                status="suspended",
                message=prompt,
                output={
                    "checkpoint": {
                        "checkpoint_type": "timer",
                        "node_id": node.node_id,
                        "prompt": prompt,
                        "wait_until": self._datetime_to_str(wait_until),
                        "resume_at": self._datetime_to_str(wait_until),
                        "expires_at": self._datetime_to_str(expires_at) if expires_at else None,
                        "wait_payload": dict(node.input_payload),
                    }
                },
                executor="wait",
            )
        if event_key:
            prompt = str(node.input_payload.get("prompt", f"等待外部事件 {event_key} 后恢复执行"))
            return ExecutionResult(
                success=False,
                status="suspended",
                message=prompt,
                output={
                    "checkpoint": {
                        "checkpoint_type": "external_event",
                        "node_id": node.node_id,
                        "prompt": prompt,
                        "event_key": event_key,
                        "expires_at": self._datetime_to_str(expires_at) if expires_at else None,
                        "wait_payload": dict(node.input_payload),
                    }
                },
                executor="wait",
            )
        return ExecutionResult(
            success=False,
            status="failed",
            message="wait 节点缺少 wait_until / delay_seconds / event_key 配置",
            executor="wait",
            error_code=ErrorCode.VALIDATION_ERROR,
        )

    def _checkpoint_response(
        self,
        task_request: TaskRequest,
        node: TaskNode,
        *,
        expected_type: str,
    ) -> dict[str, Any] | None:
        response = dict(task_request.metadata.get("checkpoint_response", {}))
        if not response:
            return None
        if str(response.get("checkpoint_type", "")) != expected_type:
            return None
        node_id = response.get("node_id")
        if node_id and str(node_id) != node.node_id:
            return None
        return response

    def _wait_checkpoint_response(
        self, task_request: TaskRequest, node: TaskNode
    ) -> dict[str, Any] | None:
        response = dict(task_request.metadata.get("checkpoint_response", {}))
        if not response:
            return None
        if str(response.get("checkpoint_type", "")) not in {"timer", "external_event"}:
            return None
        node_id = response.get("node_id")
        if node_id and str(node_id) != node.node_id:
            return None
        return response

    def _explicit_replan_node(
        self,
        task_request: TaskRequest,
        node: TaskNode,
        task_graph: TaskGraph,
        context: TaskRunContext,
    ) -> ExecutionResult:
        replan_goal = str(node.input_payload.get("goal", node.goal))
        recovery_plan, _ = self.planner.replan(
            original_request=task_request,
            replan_goal=replan_goal,
            bootstrap=context.bootstrap,
            workflow_matches=context.workflow_matches,
            vector_matches=context.vector_matches,
            skill_registry_entries=[
                entry.to_dict() for entry in self.dispatcher.skill_registry.list_entries()
            ],
        )
        applied = self._apply_replan(task_graph, node, recovery_plan.graph)
        if not applied:
            return ExecutionResult(
                success=False,
                status="failed",
                message="显式 replan 节点未生成可用恢复计划",
                executor="replan",
                error_code=ErrorCode.PLANNER_ERROR,
            )
        return ExecutionResult(
            success=True,
            status="completed",
            message="显式 replan 节点已扩展 TaskGraph",
            output={"recovery_summary": recovery_plan.summary},
            executor="replan",
        )

    def _apply_replan(
        self, task_graph: TaskGraph, failed_node: TaskNode, recovery_graph: TaskGraph
    ) -> bool:
        recovery_nodes = [
            node for node in recovery_graph.nodes if node.type != TaskNodeType.FINALIZE
        ]
        if not recovery_nodes:
            return False

        id_map: dict[str, str] = {}
        for index, node in enumerate(recovery_nodes, start=1):
            original_id = node.node_id
            node.node_id = f"{failed_node.node_id}_replan_{index}"
            id_map[original_id] = node.node_id
            node.metadata["replanned_from"] = failed_node.node_id
            node.retry_policy.replan_on_failure = False

        for node in recovery_nodes:
            node.dependencies = [id_map.get(dep, dep) for dep in node.dependencies]

        roots = [node for node in recovery_nodes if not node.dependencies]
        for root in roots:
            root.dependencies = list(failed_node.dependencies)

        child_dependencies = {dep for node in recovery_nodes for dep in node.dependencies}
        terminal_ids = [
            node.node_id for node in recovery_nodes if node.node_id not in child_dependencies
        ]
        if not terminal_ids:
            terminal_ids = [node.node_id for node in recovery_nodes]

        for downstream in task_graph.nodes:
            if failed_node.node_id not in downstream.dependencies:
                continue
            replaced = [dep for dep in downstream.dependencies if dep != failed_node.node_id]
            downstream.dependencies = list(dict.fromkeys(replaced + terminal_ids))

        failed_node.status = TaskNodeStatus.SKIPPED
        failed_node.metadata["replanned"] = True
        task_graph.nodes.extend(recovery_nodes)
        task_graph.updated_at = utc_now()
        return True

    def _emit_node_event(
        self,
        event_bus: EventBus,
        task_id: str,
        event_type: str,
        node: TaskNode,
        payload: dict[str, Any],
    ) -> None:
        event_bus.publish(
            TaskEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                event_type=event_type,
                status=str(node.status),
                payload={"node_id": node.node_id, "node_type": str(node.type), **payload},
            )
        )

    def _resolve_wait_until(self, payload: dict[str, Any], *, now: datetime) -> datetime | None:
        if payload.get("wait_until"):
            return self._parse_datetime(str(payload["wait_until"]))
        delay_seconds = payload.get("delay_seconds")
        if delay_seconds is None:
            return None
        return now + timedelta(seconds=float(delay_seconds))

    def _resolve_expiry(self, payload: dict[str, Any], *, now: datetime) -> datetime | None:
        if payload.get("timeout_at"):
            return self._parse_datetime(str(payload["timeout_at"]))
        timeout_seconds = payload.get("timeout_seconds")
        if timeout_seconds is None:
            return None
        return now + timedelta(seconds=float(timeout_seconds))

    def _parse_datetime(self, value: str) -> datetime:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _datetime_to_str(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def _utc_now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

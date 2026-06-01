from __future__ import annotations

from dataclasses import replace
from hashlib import sha1
from typing import Any

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.base import BaseExecutor
from omniagent.models import (
    ArtifactRecord,
    ExecutionResult,
    SkillContract,
    SkillRegistryEntry,
    SkillSelection,
    TaskRequest,
    TaskRunContext,
    utc_now,
)
from omniagent.skill_auto_generation import AutoSkillGenerator
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillDefinition, SkillLoader


def _value_matches_type(value: Any, declared_type: str) -> bool:
    """Lightweight shape check for META-001 contract validation.

    Returns True for `any` / unknown types so unfamiliar or legacy contracts
    don't block dispatch. Full JSON Schema validation can be layered on later.
    """
    t = (declared_type or "any").lower().strip()
    if t in {"any", ""}:
        return True
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    # Unknown type string: be permissive.
    return True


class SkillDispatcher:
    def __init__(
        self,
        *,
        skill_loader: SkillLoader,
        skill_registry: SkillRegistry,
        auto_skill_generator: AutoSkillGenerator,
        executors: dict[str, BaseExecutor],
    ) -> None:
        self.skill_loader = skill_loader
        self.skill_registry = skill_registry
        self.auto_skill_generator = auto_skill_generator
        self.executors = executors

    def dispatch(
        self, node, context: TaskRunContext, *, selection: SkillSelection | None = None
    ) -> tuple[ExecutionResult, str | None]:
        selection = selection or self.skill_registry.select(
            node, context.task_request.preferred_skills
        )
        generated_result, generated_selection = self._maybe_resolve_generated_skill(
            node, context, selection
        )
        if generated_selection is not None:
            selection = generated_selection
        if selection.explanation:
            node.metadata["skill_selection"] = selection.explanation
        if generated_result is not None:
            return generated_result, selection.selected_skill
        if selection.gap_reason and selection.explanation.get("source") in {
            "explicit_missing",
            "inactive_skill",
        }:
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR,
                selection.gap_reason,
                details={
                    "node_id": node.node_id,
                    "requested_skill": node.skill_name,
                    "required_skills": node.required_skills,
                    "executor_hint": node.executor_hint,
                    "selection": selection.explanation,
                },
            )
        node_dir = context.task_dir / "nodes" / node.node_id
        (node_dir / "artifacts").mkdir(parents=True, exist_ok=True)

        # META-001: proactive payload validation against the matched skill's
        # declared contract. Fails loudly with a structured error listing the
        # missing required fields, instead of letting the executor discover
        # the mismatch mid-run and return an opaque 500. Skipped for legacy
        # skills with no contracts and for executors we can't resolve a
        # contract for (llm_native, generated skills without contracts, etc).
        self._validate_payload_against_contract(node, selection)

        node_request = TaskRequest(
            goal=node.goal,
            expected_output=context.task_request.expected_output,
            payload=dict(node.input_payload),
            executor=node.executor_hint or "auto",
            skill_name=selection.selected_skill if selection.registry_match else node.skill_name,
            task_id=context.task_request.task_id,
            mode=context.task_request.mode,
            source=context.task_request.source,
            context_refs=list(context.task_request.context_refs),
            constraints=context.task_request.constraints,
            preferred_skills=list(context.task_request.preferred_skills),
            metadata={**context.task_request.metadata, "node_id": node.node_id},
            execution_mode=context.task_request.execution_mode,
        )
        sandbox_level = (
            selection.registry_match.sandbox_level
            if selection.registry_match is not None
            else "workspace"
        )
        node_sandbox = context.sandbox.with_profile(level=sandbox_level)
        node_context = replace(
            context,
            task_request=node_request,
            task_dir=node_dir,
            sandbox=node_sandbox,
        )
        try:
            if selection.selected_skill and selection.selected_skill in context.loaded_skills:
                result = self._run_skill(
                    context.loaded_skills[selection.selected_skill], node_context
                )
                return result, selection.selected_skill

            if node.executor_hint and node.executor_hint in self.executors:
                result = self.executors[node.executor_hint].execute(node_context)
                return result, selection.selected_skill

            raise OmniAgentError(
                ErrorCode.SKILL_ERROR,
                selection.gap_reason or f"节点缺少可执行 Skill: {node.node_id}",
                details={
                    "node_id": node.node_id,
                    "required_skills": node.required_skills,
                    "executor_hint": node.executor_hint,
                },
            )
        finally:
            if node_sandbox is not context.sandbox:
                node_sandbox.close()

    def _maybe_resolve_generated_skill(
        self,
        node,
        context: TaskRunContext,
        selection: SkillSelection,
    ) -> tuple[ExecutionResult | None, SkillSelection | None]:
        source = selection.explanation.get("source")
        if source not in {"explicit_missing", "inactive_skill"} or not node.skill_name:
            return None, None

        checkpoint_response = dict(context.task_request.metadata.get("checkpoint_response", {}))
        checkpoint_payload = dict(checkpoint_response.get("checkpoint_payload", {}))
        approval_payload = dict(checkpoint_payload.get("approval_payload", {}))
        if (
            checkpoint_response.get("task_action") == "approve"
            and approval_payload.get("kind") == "skill_generation"
            and approval_payload.get("skill_name") == node.skill_name
            and approval_payload.get("node_id") == node.node_id
        ):
            self.auto_skill_generator.approve_and_activate(
                skill_name=node.skill_name,
                version=approval_payload.get("version"),
                approver="task_user",
                rationale=f"任务节点 {node.node_id} 批准激活自动生成 Skill",
            )
            context.loaded_skills.update(self.skill_loader.load_all())
            return None, self._generated_selection(
                node_id=node.node_id,
                skill_name=node.skill_name,
                executor_hint=node.executor_hint,
                reason="用户审批通过后激活自动生成 Skill",
            )

        existing_draft = self.skill_registry.store.get_skill_draft(node.skill_name)
        if (
            source == "inactive_skill"
            and existing_draft is not None
            and existing_draft.source_kind == "generated"
            and str(existing_draft.lifecycle_status) in {"validated", "approved"}
        ):
            generation_report_path = existing_draft.metadata.get("generation_report_path")
            return self._generation_approval_result(
                node,
                {
                    "skill_name": node.skill_name,
                    "draft": existing_draft,
                    "generation_report_path": generation_report_path,
                },
            ), None

        if source != "explicit_missing":
            return None, None

        generation_type = self.auto_skill_generator.infer_generation_type(
            skill_name=node.skill_name,
            executor_hint=node.executor_hint,
            request=context.task_request,
            payload=dict(node.input_payload),
        )
        if generation_type is None:
            return None, None

        generation = self.auto_skill_generator.generate_skill(
            skill_name=node.skill_name,
            generation_type=generation_type,
            goal=node.goal,
            payload_example=dict(node.input_payload),
            trigger="missing_skill",
            task_id=context.task.task_id,
            node_id=node.node_id,
        )
        node.metadata["skill_generation"] = {
            "skill_name": generation["skill_name"],
            "status": generation["status"],
            "generation_report_path": generation.get("generation_report_path"),
        }
        context.loaded_skills.update(self.skill_loader.load_all())
        if generation["status"] == "active":
            return None, self._generated_selection(
                node_id=node.node_id,
                skill_name=node.skill_name,
                executor_hint=node.executor_hint,
                reason="能力缺口触发自动生成并激活 Skill",
            )
        if (
            generation["status"] in {"validated", "approved"}
            and generation.get("approval_record") is None
        ):
            return self._generation_approval_result(node, generation), None
        raise OmniAgentError(
            ErrorCode.SKILL_ERROR,
            f"自动生成 Skill {node.skill_name} 已创建，但当前状态为 {generation['status']}，尚不能执行",
            details={
                "node_id": node.node_id,
                "skill_name": node.skill_name,
                "generation": generation,
            },
        )

    def _generated_selection(
        self,
        *,
        node_id: str,
        skill_name: str,
        executor_hint: str | None,
        reason: str,
    ) -> SkillSelection:
        return SkillSelection(
            node_id=node_id,
            selected_skill=skill_name,
            executor_hint=executor_hint,
            registry_match=self.skill_registry.store.get_skill_registry_entry(skill_name),
            explanation={
                "source": "generated_skill",
                "reason": reason,
            },
        )

    def _generation_approval_result(self, node, generation: dict[str, Any]) -> ExecutionResult:
        draft = generation["draft"]
        approval_payload = {
            "kind": "skill_generation",
            "skill_name": generation["skill_name"],
            "version": draft.version,
            "node_id": node.node_id,
            "generation_report_path": generation.get("generation_report_path"),
        }
        return ExecutionResult(
            success=False,
            status="waiting_input",
            message=f"自动生成 Skill {generation['skill_name']} 已通过校验，等待用户审批后激活",
            output={
                "checkpoint": {
                    "checkpoint_type": "approval",
                    "node_id": node.node_id,
                    "prompt": f"是否批准激活自动生成 Skill {generation['skill_name']}@{draft.version}？",
                    "approval_payload": approval_payload,
                }
            },
            executor=node.executor_hint or generation["skill_name"],
        )

    def recover_incompatible_skill(
        self,
        node,
        context: TaskRunContext,
        *,
        failed_skill: str | None,
        failure_message: str,
        critic_summary: str,
    ) -> tuple[ExecutionResult | None, str | None]:
        if not failed_skill or node.metadata.get("auto_generation_incompatible_attempted"):
            return None, None
        if failed_skill.startswith("builtin.") and not node.skill_name:
            return None, None
        entry = self.skill_registry.store.get_skill_registry_entry(failed_skill)
        if entry is not None and entry.source == "skill_generator":
            return None, None

        generation_type = self.auto_skill_generator.infer_generation_type(
            skill_name=node.skill_name or failed_skill,
            executor_hint=node.executor_hint,
            request=context.task_request,
            payload=dict(node.input_payload),
        )
        if generation_type is None:
            return None, None

        replacement_skill = self._replacement_skill_name(
            requested_skill=node.skill_name or failed_skill,
            generation_type=generation_type,
            goal=node.goal,
        )
        generation = self.auto_skill_generator.generate_skill(
            skill_name=replacement_skill,
            generation_type=generation_type,
            goal=node.goal,
            payload_example=dict(node.input_payload),
            trigger="incompatible_skill",
            task_id=context.task.task_id,
            node_id=node.node_id,
        )
        node.metadata["auto_generation_incompatible_attempted"] = True
        node.metadata["incompatible_skill_recovery"] = {
            "failed_skill": failed_skill,
            "replacement_skill": replacement_skill,
            "failure_message": failure_message,
            "critic_summary": critic_summary,
            "generation_status": generation["status"],
            "generation_report_path": generation.get("generation_report_path"),
        }
        node.metadata["skill_generation"] = {
            "skill_name": generation["skill_name"],
            "status": generation["status"],
            "generation_report_path": generation.get("generation_report_path"),
            "trigger": "incompatible_skill",
            "replaced_skill": failed_skill,
        }
        node.skill_name = replacement_skill
        node.required_skills = [replacement_skill]
        context.loaded_skills.update(self.skill_loader.load_all())
        if generation["status"] in {"active", "existing_active"}:
            return None, replacement_skill
        if (
            generation["status"] in {"validated", "approved"}
            and generation.get("approval_record") is None
        ):
            return self._generation_approval_result(node, generation), replacement_skill
        raise OmniAgentError(
            ErrorCode.SKILL_ERROR,
            f"自动生成替代 Skill {replacement_skill} 已创建，但当前状态为 {generation['status']}，尚不能执行",
            details={
                "node_id": node.node_id,
                "failed_skill": failed_skill,
                "replacement_skill": replacement_skill,
                "generation": generation,
            },
        )

    def _resolve_contract(
        self,
        entry: SkillRegistryEntry | None,
        requested_action: str | None,
    ) -> SkillContract | None:
        """Pick the SkillContract relevant to this dispatch, or None if N/A."""
        if entry is None or not entry.contracts:
            return None
        if requested_action:
            return entry.contract_for_action(requested_action)
        # No action specified: unambiguous only if the skill has exactly one
        # contract (e.g. builtin.python.basic has just `run`). Skills with
        # multiple contracts but no action are a planner bug, but we don't
        # fail here — the executor's own validation will catch it.
        if len(entry.contracts) == 1:
            return entry.contracts[0]
        return None

    def _validate_payload_against_contract(self, node, selection: SkillSelection) -> None:
        payload = node.input_payload if isinstance(node.input_payload, dict) else {}
        requested_action: str | None = None
        action_val = payload.get("action") if isinstance(payload, dict) else None
        if isinstance(action_val, str) and action_val.strip():
            requested_action = action_val.strip()

        contract = self._resolve_contract(selection.registry_match, requested_action)
        if contract is None:
            return

        missing: list[str] = []
        type_errors: list[str] = []
        for field in contract.input_fields:
            # The `action` field itself is the discriminator, not a value the
            # planner needs to supply beyond what dispatch already asserted.
            if field.name == "action":
                continue
            if not field.required:
                continue
            value = payload.get(field.name)
            if value is None:
                missing.append(field.name)
                continue
            # Lightweight type shape check — just reject the obvious mismatches
            # (e.g. string contract but got a dict). Full JSON Schema comes later.
            if not _value_matches_type(value, field.type):
                type_errors.append(f"{field.name} 期望 {field.type}，收到 {type(value).__name__}")

        if missing or type_errors:
            parts: list[str] = []
            if missing:
                parts.append(f"缺失必填字段: {missing}")
            if type_errors:
                parts.append(f"类型不符: {type_errors}")
            details: dict[str, Any] = {
                "node_id": node.node_id,
                "skill_name": selection.selected_skill,
                "contract_action": contract.action,
                "missing": missing,
                "type_errors": type_errors,
                "required_fields": contract.required_input_fields(),
            }
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"节点 {node.node_id} 的 input_payload 不满足 contract({contract.action}): {'; '.join(parts)}",
                details=details,
            )

    def _run_skill(self, skill: SkillDefinition, context: TaskRunContext) -> ExecutionResult:
        runner = self.skill_loader.load_runner(skill)
        try:
            payload = runner(
                context.task_request.payload,
                {
                    "task": {
                        "task_id": context.task.task_id,
                        "goal": context.task.goal,
                        "status": str(context.task.status),
                    },
                    "workspace_root": str(context.sandbox.runtime_root),
                    "task_dir": str(context.sandbox.translate_path(context.task_dir)),
                    "sandbox": context.sandbox,
                    "bootstrap": context.bootstrap.documents,
                    "memory_summaries": context.bootstrap.memory_summaries,
                    "workflow_summaries": context.bootstrap.workflow_summaries,
                },
            )
        except OmniAgentError:
            raise
        except Exception as exc:
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR,
                f"Skill {skill.metadata.name} 运行失败: {exc}",
                details={
                    "skill_name": skill.metadata.name,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            ) from exc
        if isinstance(payload, ExecutionResult):
            payload.executor = f"skill:{skill.metadata.name}"
            return payload
        if not isinstance(payload, dict):
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR, "Skill 返回值必须是 dict 或 ExecutionResult"
            )
        artifacts = [
            ArtifactRecord(
                kind=item.get("kind", "data"),
                path=str(context.sandbox.resolve_host_path(str(item.get("path", ""))))
                if item.get("path")
                else "",
                description=str(item.get("description", "")),
            )
            for item in payload.get("artifacts", [])
        ]
        return ExecutionResult(
            success=bool(payload.get("success", True)),
            status=str(payload.get("status", "completed")),
            message=str(payload.get("message", f"Skill {skill.metadata.name} 执行完成")),
            output=dict(payload.get("output", {})),
            artifacts=artifacts,
            logs=[str(item) for item in payload.get("logs", [])],
            executor=f"skill:{skill.metadata.name}",
            error_code=payload.get("error_code"),
            started_at=utc_now(),
            finished_at=utc_now(),
        )

    def _replacement_skill_name(
        self, *, requested_skill: str, generation_type: str, goal: str
    ) -> str:
        normalized = "".join(ch.lower() if ch.isalnum() else "." for ch in requested_skill).strip(
            "."
        )
        normalized = ".".join(part for part in normalized.split(".") if part) or "skill"
        short_hash = sha1(
            f"{requested_skill}:{goal}:{generation_type}".encode("utf-8")
        ).hexdigest()[:10]
        return f"generated.{generation_type}.recovery.{normalized}.{short_hash}"

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from omniagent.models import (
    PermissionLevel,
    PlanResult,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskRequest,
)
from omniagent.skill_registry import EXECUTOR_TO_BUILTIN


_PERMISSION_RANK = {
    "memory.read": PermissionLevel.L1.rank,
    "filesystem.read": PermissionLevel.L1.rank,
    "task.resume": PermissionLevel.L1.rank,
    "filesystem.write": PermissionLevel.L2.rank,
    "network": PermissionLevel.L2.rank,
    "external.event.consume": PermissionLevel.L2.rank,
    "skill.activate": PermissionLevel.L3.rank,
    "code_execution": PermissionLevel.L3.rank,
    "system.exec": PermissionLevel.L4.rank,
    "dependency.install": PermissionLevel.L4.rank,
    "system.modify": PermissionLevel.L4.rank,
    "filesystem.delete": PermissionLevel.L5.rank,
}

_BLOCKED_PERMISSIONS = {
    "network",
    "skill.activate",
    "code_execution",
    "system.exec",
    "dependency.install",
    "system.modify",
    "filesystem.delete",
}

_BLOCKED_SIDE_EFFECTS = {
    "network_read",
    "network_write",
    "process_execution",
    "python_execution",
    "dependency.install",
    "system.modify",
    "filesystem.delete",
}

_DESTRUCTIVE_ACTIONS = {"delete", "remove", "unlink", "rmdir"}
_CONTEXT_REF_PATTERN = re.compile(
    r"(刚才|之前|前面|上次|同一个|那个文件|这个文件|same file|that file|previous|last|above)",
    re.IGNORECASE,
)
_PLANNER_CONTEXT_REQUIRED_PATTERN = re.compile(
    r"(planner context|planner payload|planning context|规划上下文|计划上下文)",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _payload_contains_key(value: Any, key_name: str) -> bool:
    if isinstance(value, dict):
        return any(
            str(key) == key_name or _payload_contains_key(item, key_name)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_payload_contains_key(item, key_name) for item in value)
    return False


def _payload_contains_url(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_payload_contains_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_payload_contains_url(item) for item in value)
    return isinstance(value, str) and _URL_PATTERN.search(value) is not None


def _payload_has_external_path(value: Any) -> bool:
    path_keys = {"path", "source", "target", "output_path", "archive_path", "extract_dir"}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in path_keys and isinstance(item, str):
                stripped = item.strip()
                if stripped.startswith("/") or stripped == ".." or stripped.startswith("../"):
                    return True
            if _payload_has_external_path(item):
                return True
    if isinstance(value, list):
        return any(_payload_has_external_path(item) for item in value)
    return False


def _permission_rank(permissions: list[str]) -> int:
    if not permissions:
        return PermissionLevel.L1.rank
    return max(_PERMISSION_RANK.get(item, PermissionLevel.L3.rank) for item in permissions)


def _contract_action(contract: dict[str, Any]) -> str:
    return str(contract.get("action") or "").strip().lower()


def _contract_for_action(entry: dict[str, Any], action: str) -> dict[str, Any] | None:
    for contract in entry.get("contracts", []) or []:
        if not isinstance(contract, dict):
            continue
        if _contract_action(contract) == action:
            return contract
    return None


def _required_input_fields(contract: dict[str, Any]) -> list[str]:
    fields = contract.get("input_fields", []) or []
    required: list[str] = []
    for field_payload in fields:
        if not isinstance(field_payload, dict):
            continue
        if bool(field_payload.get("required", False)):
            required.append(str(field_payload.get("name", "")))
    return [item for item in required if item]


def _entry_by_name(entries: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in entries if item.get("skill_name") == name), None)


@dataclass(slots=True)
class ExecutionPolicyDecision:
    policy_decision: str
    reason: str
    target_skill: str | None = None
    executor_hint: str | None = None
    skipped_stages: list[str] = field(default_factory=list)
    fallback: bool = False
    risk_flags: dict[str, bool] = field(default_factory=dict)
    context_budget: dict[str, Any] = field(default_factory=dict)
    expanded_manifest_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_decision": self.policy_decision,
            "reason": self.reason,
            "target_skill": self.target_skill,
            "executor_hint": self.executor_hint,
            "skipped_stages": list(self.skipped_stages),
            "fallback": self.fallback,
            "risk_flags": dict(self.risk_flags),
            "context_budget": dict(self.context_budget),
            "expanded_manifest_names": list(self.expanded_manifest_names),
        }

    def to_plan_result(self, request: TaskRequest) -> PlanResult:
        node = TaskNode(
            node_id="node_direct_execute",
            type=TaskNodeType.ACTION,
            goal=f"Execute explicit payload for: {request.goal}",
            executor_hint=self.executor_hint,
            skill_name=self.target_skill,
            input_payload=dict(request.payload),
            metadata={"execution_policy": self.to_dict()},
        )
        graph = TaskGraph(
            goal=request.goal,
            summary="ExecutionPolicy direct execute graph",
            nodes=[node],
        )
        return PlanResult(
            summary="使用 ExecutionPolicy 直接执行显式低风险 payload",
            reasoning=self.reason,
            risk_level="low",
            required_capabilities=[],
            graph=graph,
            llm_trace_id="execution_policy_direct_execute",
        )


class ExecutionPolicy:
    """Decide whether a task can bypass Planner without bypassing execution safety."""

    def decide(
        self,
        request: TaskRequest,
        *,
        skill_registry_entries: list[dict[str, Any]],
        context_counts: dict[str, int] | None = None,
    ) -> ExecutionPolicyDecision:
        payload = request.payload if isinstance(request.payload, dict) else {}
        action = str(payload.get("action") or "").strip().lower()
        target_skill, executor_hint = self._resolve_target(request)
        entry = _entry_by_name(skill_registry_entries, target_skill) if target_skill else None
        risk_flags = {
            "network": bool(
                request.constraints.allow_network or _payload_contains_url(payload)
            ),
            "delete": action in _DESTRUCTIVE_ACTIONS,
            "l4": request.constraints.max_permission.rank >= PermissionLevel.L4.rank,
            "context_ref": bool(request.context_refs)
            or _payload_contains_key(payload, "context_ref"),
            "ambiguous_reference": _CONTEXT_REF_PATTERN.search(request.goal) is not None,
            "planner_context_required": _PLANNER_CONTEXT_REQUIRED_PATTERN.search(
                f"{request.goal}\n{request.expected_output or ''}"
            )
            is not None,
            "external_path": _payload_has_external_path(payload),
        }
        context_budget: dict[str, Any] = {
            "task_request": _json_size(
                {
                    "goal": request.goal,
                    "expected_output": request.expected_output,
                    "executor": request.executor,
                    "skill_name": request.skill_name,
                    "payload": payload,
                    "context_refs": request.context_refs,
                    "preferred_skills": request.preferred_skills,
                }
            ),
            "payload": _json_size(payload),
            "skill_registry_entries": {
                "items": len(skill_registry_entries),
                "json_chars": _json_size(skill_registry_entries),
            },
            **(context_counts or {}),
        }

        def plan_required(reason: str) -> ExecutionPolicyDecision:
            return ExecutionPolicyDecision(
                policy_decision="plan_required",
                reason=reason,
                target_skill=target_skill,
                executor_hint=executor_hint,
                skipped_stages=[],
                fallback=False,
                risk_flags=risk_flags,
                context_budget=context_budget,
            )

        if not payload:
            return plan_required("没有结构化 payload，必须由 Planner 理解自然语言目标")
        if target_skill is None:
            return plan_required("未显式指定可静态映射的 executor 或 builtin skill")
        if entry is None:
            return plan_required(f"目标 skill 未在 registry 中注册: {target_skill}")
        if not str(target_skill).startswith("builtin."):
            return plan_required("第一阶段 fast path 只允许 builtin skill")
        if request.constraints.max_permission.rank > PermissionLevel.L2.rank:
            return plan_required("max_permission 高于 L2，保守回退 Planner")
        if any(risk_flags.values()):
            enabled = ", ".join(name for name, value in risk_flags.items() if value)
            return plan_required(f"存在 fast path 风险标记: {enabled}")

        permissions = [str(item) for item in entry.get("permission_requirements", []) or []]
        if _permission_rank(permissions) > min(request.constraints.max_permission.rank, PermissionLevel.L2.rank):
            return plan_required("目标 skill 权限等级超过 direct_execute 上限")
        blocked_permissions = sorted(set(permissions) & _BLOCKED_PERMISSIONS)
        if blocked_permissions:
            return plan_required(f"目标 skill 权限不允许 direct_execute: {blocked_permissions}")
        if not action:
            return plan_required("payload 缺少 action，无法做 contract 静态校验")
        if action in _DESTRUCTIVE_ACTIONS:
            return plan_required("payload action 是破坏性操作")
        contract = _contract_for_action(entry, action)
        if contract is None:
            return plan_required(f"目标 skill 未声明 action={action} 的 contract")
        missing = [name for name in _required_input_fields(contract) if name not in payload]
        if missing:
            return plan_required(f"payload 缺少 contract 必填字段: {missing}")
        side_effects = [str(item) for item in contract.get("side_effects", []) or []]
        blocked_effects = sorted(set(side_effects) & _BLOCKED_SIDE_EFFECTS)
        if blocked_effects:
            return plan_required(f"目标 action side_effect 不允许 direct_execute: {blocked_effects}")

        return ExecutionPolicyDecision(
            policy_decision="direct_execute",
            reason=(
                "显式 executor/skill + 结构化 payload 已通过权限、contract、风险标记和路径静态校验"
            ),
            target_skill=target_skill,
            executor_hint=executor_hint,
            skipped_stages=["planner"],
            fallback=False,
            risk_flags=risk_flags,
            context_budget=context_budget,
            expanded_manifest_names=[target_skill],
        )

    def _resolve_target(self, request: TaskRequest) -> tuple[str | None, str | None]:
        if request.skill_name:
            return request.skill_name, request.executor if request.executor != "auto" else None
        if request.executor != "auto":
            skill_name = EXECUTOR_TO_BUILTIN.get(request.executor)
            return skill_name, request.executor if skill_name else request.executor
        return None, None

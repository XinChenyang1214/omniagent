from __future__ import annotations

import ast
import re
from typing import Any

from omniagent.models import (
    PendingCheckpoint,
    PendingCheckpointType,
    PermissionDecision,
    PermissionLevel,
    TaskNode,
    TaskRequest,
)

PERMISSION_LEVEL_MAP = {
    "memory.read": PermissionLevel.L1,
    "filesystem.read": PermissionLevel.L1,
    "task.resume": PermissionLevel.L1,
    "filesystem.write": PermissionLevel.L2,
    "network": PermissionLevel.L2,
    "external.event.consume": PermissionLevel.L2,
    "skill.activate": PermissionLevel.L3,
    "code_execution": PermissionLevel.L3,
    "system.exec": PermissionLevel.L4,
    "dependency.install": PermissionLevel.L4,
    "system.modify": PermissionLevel.L4,
    "filesystem.delete": PermissionLevel.L5,
}

SANDBOX_LEVEL_RANK = {
    "workspace": 1,
    "process": 2,
    "container": 3,
}

MIN_SANDBOX_LEVEL = {
    "system.exec": "process",
    "dependency.install": "container",
}

_NETWORK_IMPORT_PREFIXES = (
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "requests",
    "socket",
    "smtplib",
    "telnetlib",
    "urllib",
    "websocket",
    "websockets",
)
_SYSTEM_EXEC_IMPORT_PREFIXES = ("asyncio.subprocess", "pty", "shlex", "subprocess")
_DEPENDENCY_INSTALL_IMPORT_PREFIXES = ("ensurepip", "pip", "poetry", "uv", "venv")


class PermissionManager:
    def evaluate(
        self,
        request: TaskRequest,
        *,
        node: TaskNode | None = None,
        selection: Any | None = None,
        required_permissions: list[str] | None = None,
        checkpoint: PendingCheckpoint | None = None,
        trigger: str | None = None,
        scope: str = "task",
        source: str | None = None,
        phase: str = "execution",
        resource_kind: str | None = None,
        resource_ref: str | None = None,
    ) -> PermissionDecision:
        inferred = self._infer_permissions(request, node=node)
        declared_permissions = list(required_permissions or [])
        if selection is not None and getattr(selection, "registry_match", None) is not None:
            declared_permissions.extend(selection.registry_match.permission_requirements)

        activation_permissions = self._infer_skill_activation_permissions(request, node=node)
        checkpoint_permissions = self._checkpoint_permissions(checkpoint, trigger=trigger)
        permissions = self._dedupe(
            declared_permissions
            + inferred["permissions"]
            + checkpoint_permissions
            + activation_permissions
        )
        if not permissions:
            permissions = ["filesystem.read"]

        primary_permission = max(
            permissions,
            key=lambda item: PERMISSION_LEVEL_MAP.get(item, PermissionLevel.L3).rank,
        )
        level = PERMISSION_LEVEL_MAP.get(primary_permission, PermissionLevel.L3)
        sandbox_level = self._effective_sandbox_level(
            getattr(getattr(selection, "registry_match", None), "sandbox_level", None),
            permissions,
        )
        decision_scope = scope
        if checkpoint is not None:
            decision_scope = "resume"
        elif node is not None:
            decision_scope = "node"
        elif scope == "memory":
            decision_scope = "memory"

        if resource_kind is None:
            resource_kind = inferred["resource_kind"] or (
                "checkpoint" if checkpoint is not None else None
            )
        if resource_ref is None:
            resource_ref = inferred["resource_ref"] or (
                checkpoint.checkpoint_id if checkpoint is not None else None
            )

        rationale_parts: list[str] = []
        if declared_permissions:
            rationale_parts.append(
                f"Skill 声明权限: {', '.join(self._dedupe(declared_permissions))}"
            )
        if inferred["permissions"]:
            rationale_parts.append(f"资源/执行推断权限: {', '.join(inferred['permissions'])}")
        if checkpoint_permissions:
            rationale_parts.append(f"恢复链路权限: {', '.join(checkpoint_permissions)}")
        if activation_permissions:
            rationale_parts.append(f"激活链路权限: {', '.join(activation_permissions)}")
        if not rationale_parts:
            rationale_parts.append("默认低风险读取")

        denied_reason: str | None = None
        if "memory.read" in permissions and not request.constraints.allow_memory_read:
            denied_reason = "当前任务未开启记忆读取权限"
        elif "network" in permissions and not request.constraints.allow_network:
            denied_reason = "当前任务未开启网络访问约束"
        elif "filesystem.delete" in permissions and not request.constraints.allow_delete:
            denied_reason = "当前任务未开启删除权限"
        elif level.rank > request.constraints.max_permission.rank:
            denied_reason = (
                f"请求权限 {level} 超过当前任务允许的最高权限 {request.constraints.max_permission}"
            )

        status = "denied" if denied_reason else "granted"
        rationale = denied_reason or "；".join(rationale_parts)
        resolved_source = source or str(request.source)
        skill_name = None
        if getattr(selection, "selected_skill", None):
            skill_name = selection.selected_skill
        elif node is not None and node.skill_name:
            skill_name = node.skill_name

        return PermissionDecision(
            task_id=request.task_id,
            permission=primary_permission,
            level=level,
            status=status,
            rationale=rationale,
            scope=decision_scope,
            node_id=node.node_id if node is not None else None,
            skill_name=skill_name,
            resource_kind=resource_kind,
            resource_ref=resource_ref,
            sandbox_level=sandbox_level,
            source=resolved_source,
            policy="approval_checkpoint" if status == "denied" else "policy_matrix_grant",
            requires_approval=status == "denied",
            details={
                "phase": phase,
                "mode": str(request.mode),
                "request_source": str(request.source),
                "decision_source": resolved_source,
                "risk_level": getattr(node, "risk_level", "low"),
                "permissions": permissions,
                "declared_permissions": self._dedupe(declared_permissions),
                "inferred_permissions": inferred["permissions"],
                "checkpoint_permissions": checkpoint_permissions,
                "activation_permissions": activation_permissions,
                "skill_scope": getattr(getattr(selection, "registry_match", None), "scope", None),
                "trigger": trigger,
            },
        )

    def evaluate_memory_access(
        self,
        request: TaskRequest,
        *,
        phase: str = "planning",
    ) -> PermissionDecision:
        return self.evaluate(
            request,
            required_permissions=["memory.read"],
            scope="memory",
            source="memory",
            phase=phase,
            resource_kind="memory",
            resource_ref=phase,
        )

    def evaluate_checkpoint_resume(
        self,
        request: TaskRequest,
        checkpoint: PendingCheckpoint,
        *,
        trigger: str,
    ) -> PermissionDecision:
        decision_source = "scheduler" if trigger == "timer" else "event"
        return self.evaluate(
            request,
            checkpoint=checkpoint,
            trigger=trigger,
            scope="resume",
            source=decision_source,
            phase="resume",
            resource_kind="checkpoint",
            resource_ref=checkpoint.checkpoint_id,
        )

    def _infer_permissions(
        self,
        request: TaskRequest,
        *,
        node: TaskNode | None,
    ) -> dict[str, Any]:
        payload = dict(node.input_payload if node is not None else request.payload)
        executor = str(
            node.executor_hint if node is not None and node.executor_hint else request.executor
        )
        permissions: list[str] = []
        resource_kind: str | None = None
        resource_ref: str | None = None

        if executor == "browser":
            url = str(payload.get("url", ""))
            if url.startswith("http://") or url.startswith("https://"):
                permissions.append("network")
                resource_kind = "url"
                resource_ref = url
            else:
                permissions.append("filesystem.read")
                resource_kind = "file"
                resource_ref = url or str(payload.get("path", ""))
        elif executor == "python":
            permissions.append("code_execution")
            resource_kind = "code"
            resource_ref = f"python:{(node.node_id if node is not None else request.task_id)}"
            code = str(payload.get("code", ""))
            static_permissions = self._infer_python_code_permissions(code)
            permissions.extend(static_permissions)
        elif executor == "file":
            action = str(payload.get("action", "read"))
            path = str(payload.get("path", ""))
            resource_kind = "file"
            resource_ref = path or "."
            if action in {"list", "read"}:
                permissions.append("filesystem.read")
            elif action in {"write", "append", "replace_line"}:
                permissions.append("filesystem.write")
            elif action in {"delete", "remove"}:
                permissions.append("filesystem.delete")

        return {
            "permissions": self._dedupe(permissions),
            "resource_kind": resource_kind,
            "resource_ref": resource_ref,
        }

    def _infer_python_code_permissions(self, code: str) -> list[str]:
        normalized = code.lower()
        permissions: list[str] = []
        references = self._python_static_references(code)
        imported = references["imports"]
        names = references["names"]

        if self._matches_any_reference(imported | names, _NETWORK_IMPORT_PREFIXES):
            permissions.append("network")
        if self._matches_any_reference(imported | names, _SYSTEM_EXEC_IMPORT_PREFIXES):
            permissions.append("system.exec")
        if self._matches_any_reference(imported, _DEPENDENCY_INSTALL_IMPORT_PREFIXES):
            permissions.append("dependency.install")
        if self._contains_dependency_install_command(normalized):
            permissions.append("dependency.install")
        return self._dedupe(permissions)

    def _python_static_references(self, code: str) -> dict[str, set[str]]:
        imports: set[str] = set()
        names: set[str] = set()
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return {
                "imports": set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*", code)),
                "names": set(),
            }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.lower())
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.lower())
                    for alias in node.names:
                        imports.add(f"{node.module}.{alias.name}".lower())
            elif isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                dotted = self._dotted_name(node)
                if dotted:
                    names.add(dotted.lower())
        return {"imports": imports, "names": names}

    def _dotted_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._dotted_name(node.value)
            if parent:
                return f"{parent}.{node.attr}"
        return None

    def _matches_any_reference(self, references: set[str], prefixes: tuple[str, ...]) -> bool:
        for reference in references:
            for prefix in prefixes:
                normalized_prefix = prefix.lower()
                if reference == normalized_prefix or reference.startswith(f"{normalized_prefix}."):
                    return True
        return False

    def _contains_dependency_install_command(self, normalized_code: str) -> bool:
        patterns = (
            r"\bpip\s+install\b",
            r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+pip\b",
            r"\buv\s+(?:pip\s+)?install\b",
            r"\bpoetry\s+(?:add|install)\b",
        )
        return any(re.search(pattern, normalized_code) for pattern in patterns)

    def _infer_skill_activation_permissions(
        self,
        request: TaskRequest,
        *,
        node: TaskNode | None,
    ) -> list[str]:
        if node is None:
            return []
        response = dict(request.metadata.get("checkpoint_response", {}))
        if response.get("task_action") != "approve":
            return []
        checkpoint_payload = dict(response.get("checkpoint_payload", {}))
        approval_payload = dict(checkpoint_payload.get("approval_payload", {}))
        if approval_payload.get("kind") != "skill_generation":
            return []
        if approval_payload.get("node_id") and str(approval_payload["node_id"]) != node.node_id:
            return []
        return ["skill.activate"]

    def _checkpoint_permissions(
        self,
        checkpoint: PendingCheckpoint | None,
        trigger: str | None,
    ) -> list[str]:
        if checkpoint is None:
            return []
        permissions = ["task.resume"]
        if (
            checkpoint.checkpoint_type == PendingCheckpointType.EXTERNAL_EVENT
            or trigger == "external_event"
        ):
            permissions.append("external.event.consume")
        return permissions

    def _effective_sandbox_level(
        self,
        declared_level: str | None,
        permissions: list[str],
    ) -> str:
        level = declared_level or "workspace"
        for permission in permissions:
            minimum = MIN_SANDBOX_LEVEL.get(permission)
            if minimum is None:
                continue
            if SANDBOX_LEVEL_RANK[level] < SANDBOX_LEVEL_RANK[minimum]:
                level = minimum
        return level

    def _dedupe(self, items: list[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            ordered.append(normalized)
            seen.add(normalized)
        return ordered

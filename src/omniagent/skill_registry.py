from __future__ import annotations

from omniagent.graph_memory import GraphMemoryStore
from typing import Any

from omniagent.models import (
    PermissionLevel,
    SkillLifecycleStatus,
    SkillRegistryEntry,
    SkillSelection,
    TaskNode,
    TaskRequest,
)
from omniagent.skills.loader import SkillDefinition
from omniagent.storage import SQLiteStore

EXECUTOR_TO_BUILTIN = {
    "file": "builtin.file.basic",
    "python": "builtin.python.basic",
    "browser": "builtin.browser.basic",
}

PLANNER_SKILL_ENTRY_LIMIT = 6
PLANNER_EXPANDED_MANIFEST_LIMIT = 2

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

_PLANNER_TEXT_TAG_HINTS = {
    "file": ("file", "files", "path", "directory", "folder", "文件", "目录", "路径"),
    "python": ("python", "计算", "脚本", "代码"),
    "browser": ("browser", "网页", "浏览", "抓取", "搜索", "url", "http"),
    "http": ("http", "api", "接口", "请求", "get", "post", "put", "delete"),
    "json": ("json",),
    "csv": ("csv", "表格", "行列"),
    "datetime": ("datetime", "date", "time", "日期", "时间"),
    "shell": ("shell", "命令", "grep", "find", "wc", "head", "tail", "sort", "cat", "ls"),
    "sqlite": ("sqlite", "database", "db", "数据库", "查询库", "sql"),
    "database": ("sqlite", "database", "db", "数据库", "查询库", "sql"),
    "markdown": ("markdown", "md", "heading", "section", "文档", "标题", "章节"),
    "git": ("git", "diff", "status", "commit", "log", "变更", "提交"),
    "html": ("html", "网页", "页面", "链接", "title", "table"),
    "archive": ("archive", "zip", "tar", "压缩", "解压", "打包"),
    "config": ("config", "toml", "yaml", "xml", "配置"),
}


def _requested_action_from_payload(payload: dict[str, Any]) -> str | None:
    action = payload.get("action")
    if isinstance(action, str) and action.strip():
        return action.strip().lower()
    return None


def _entry_contract_actions(entry: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    for item in entry.get("contracts", []) or []:
        if isinstance(item, dict):
            action = item.get("action")
        else:
            action = getattr(item, "action", None)
        if action:
            actions.add(str(action).strip().lower())
    return actions


def _entry_permission_rank(entry: dict[str, Any]) -> int:
    permissions = [str(item) for item in entry.get("permission_requirements", []) or []]
    if not permissions:
        return PermissionLevel.L1.rank
    return max(_PERMISSION_RANK.get(item, PermissionLevel.L3.rank) for item in permissions)


def _planner_text(request: TaskRequest, goal: str | None) -> str:
    payload = request.payload if isinstance(request.payload, dict) else {}
    payload_text = " ".join(str(value) for value in payload.values() if isinstance(value, str))
    return " ".join(
        item
        for item in [
            goal or request.goal,
            request.expected_output or "",
            request.executor,
            request.skill_name or "",
            " ".join(request.preferred_skills),
            " ".join(str(key) for key in payload.keys()),
            payload_text,
        ]
        if item
    ).lower()


def _planner_tag_hits(entry: dict[str, Any], text: str) -> set[str]:
    tags = {str(item).lower() for item in entry.get("tags", []) or []}
    name = str(entry.get("skill_name", "")).lower()
    description = str(entry.get("description", "")).lower()
    haystack = f"{text} {name} {description}"
    hits: set[str] = set()
    for tag in tags:
        if tag and tag in text:
            hits.add(tag)
    for tag, hints in _PLANNER_TEXT_TAG_HINTS.items():
        if tag not in tags and tag not in name:
            continue
        if any(hint in haystack for hint in hints):
            hits.add(tag)
    return hits


def filter_skill_registry_entries_for_planner(
    entries: list[dict[str, Any]],
    request: TaskRequest,
    *,
    goal: str | None = None,
    limit: int = PLANNER_SKILL_ENTRY_LIMIT,
) -> list[dict[str, Any]]:
    """Return the bounded skill manifest set sent to Planner.

    This keeps the prompt from receiving every active manifest while still
    preserving explicit skills, executor builtins, action matches, tag matches,
    permission fit, and past execution quality signals.
    """
    if limit <= 0:
        return []
    if len(entries) <= limit:
        return list(entries)

    payload = request.payload if isinstance(request.payload, dict) else {}
    requested_action = _requested_action_from_payload(payload)
    text = _planner_text(request, goal)
    explicit_names = {
        item
        for item in [request.skill_name, *request.preferred_skills]
        if isinstance(item, str) and item
    }
    builtin = EXECUTOR_TO_BUILTIN.get(request.executor if request.executor != "auto" else "")
    if builtin:
        explicit_names.add(builtin)

    scored: list[tuple[float, int, dict[str, Any]]] = []
    fallback: list[tuple[float, int, dict[str, Any]]] = []
    max_rank = request.constraints.max_permission.rank
    for index, entry in enumerate(entries):
        name = str(entry.get("skill_name", ""))
        actions = _entry_contract_actions(entry)
        permission_rank = _entry_permission_rank(entry)
        failure_count = int(entry.get("failure_count", 0) or 0)
        success_rate = float(entry.get("success_rate", 0.0) or 0.0)

        score = 0.0
        matched = False
        if name in explicit_names:
            score += 1000.0
            matched = True
        if requested_action:
            if requested_action in actions:
                score += 500.0
                matched = True
            elif actions:
                score -= 120.0
        tag_hits = _planner_tag_hits(entry, text)
        if tag_hits:
            score += 80.0 * len(tag_hits)
            matched = True
        if any(token in text for token in name.lower().split(".")):
            score += 20.0
            matched = True
        if permission_rank <= max_rank:
            score += max(0, 18 - permission_rank * 3)
        else:
            score -= (permission_rank - max_rank) * 8
        if "network" in entry.get("permission_requirements", []) and request.constraints.allow_network:
            score += 12.0
        score += success_rate * 20.0
        score -= min(failure_count, 10) * 5.0

        bucket = (score, -index, entry)
        fallback.append(bucket)
        if matched or score > 0:
            scored.append(bucket)

    selected = scored or fallback
    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in selected[:limit]]


def _short_description(value: Any, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_skill_capability_index(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return compact skill summaries for Planner's default context."""
    index: list[dict[str, Any]] = []
    for entry in entries:
        actions = sorted(_entry_contract_actions(entry))
        index.append(
            {
                "skill_name": entry.get("skill_name"),
                "actions": actions,
                "tags": list(entry.get("tags", []) or []),
                "permission_rank": _entry_permission_rank(entry),
                "sandbox_level": entry.get("sandbox_level"),
                "description": _short_description(entry.get("description", "")),
                "success_rate": float(entry.get("success_rate", 0.0) or 0.0),
                "failure_count": int(entry.get("failure_count", 0) or 0),
            }
        )
    return index


def build_planner_skill_context(
    entries: list[dict[str, Any]],
    request: TaskRequest,
    *,
    goal: str | None = None,
    expanded_limit: int = PLANNER_EXPANDED_MANIFEST_LIMIT,
) -> dict[str, Any]:
    """Split Planner skill context into compact index and expanded manifests.

    The legacy ``skill_registry_entries`` payload key should receive only the
    expanded manifests from this return value. Dispatcher still sees the full
    registry later; this only controls what the Planner prompt receives.
    """
    capability_index = build_skill_capability_index(entries)
    payload = request.payload if isinstance(request.payload, dict) else {}
    requested_action = _requested_action_from_payload(payload)
    explicit_names = {
        item
        for item in [request.skill_name, *request.preferred_skills]
        if isinstance(item, str) and item
    }
    builtin = EXECUTOR_TO_BUILTIN.get(request.executor if request.executor != "auto" else "")
    if builtin:
        explicit_names.add(builtin)

    expanded: list[dict[str, Any]] = []
    expanded_names: set[str] = set()
    reasons: dict[str, str] = {}

    def add(entry: dict[str, Any], reason: str) -> None:
        name = str(entry.get("skill_name", ""))
        if not name or name in expanded_names:
            return
        if len(expanded) >= expanded_limit and name not in explicit_names:
            return
        expanded_entry = dict(entry)
        expanded_entry["_expansion_reason"] = reason
        expanded.append(expanded_entry)
        expanded_names.add(name)
        reasons[name] = reason

    for entry in entries:
        name = str(entry.get("skill_name", ""))
        if name in explicit_names:
            add(entry, "explicit_skill_or_executor")
    if requested_action:
        for entry in entries:
            if requested_action in _entry_contract_actions(entry):
                add(entry, f"action_match:{requested_action}")
    if not expanded and entries:
        # Keep a small full-contract seed so legacy Planner prompt behavior
        # remains viable while capability-index prompting is phased in.
        add(entries[0], "top_ranked_fallback")

    return {
        "skill_capability_index": capability_index,
        "expanded_skill_manifests": expanded,
        "manifest_budget": {
            "capability_index_count": len(capability_index),
            "expanded_manifest_count": len(expanded),
            "expanded_manifest_names": [str(item.get("skill_name", "")) for item in expanded],
            "expansion_reasons": reasons,
            "unexpanded_count": max(0, len(entries) - len(expanded_names)),
            "expanded_limit": expanded_limit,
        },
    }


class SkillRegistry:
    def __init__(self, store: SQLiteStore, graph_memory: GraphMemoryStore | None = None) -> None:
        self.store = store
        self.graph_memory = graph_memory

    def sync(self, definitions: dict[str, SkillDefinition]) -> None:
        for skill in definitions.values():
            current = self.store.get_skill_registry_entry(skill.metadata.name)
            self.store.upsert_skill_registry_entry(
                SkillRegistryEntry(
                    skill_name=skill.metadata.name,
                    source=skill.location,
                    version=skill.metadata.version,
                    description=skill.metadata.description,
                    permission_requirements=skill.metadata.permission_requirements,
                    tags=skill.metadata.tags,
                    location=str(skill.root_path),
                    scope=self._scope_from_location(skill.location),
                    sandbox_level=skill.metadata.sandbox_level,
                    success_count=current.success_count if current else 0,
                    failure_count=current.failure_count if current else 0,
                    success_rate=current.success_rate if current else 0.0,
                    lifecycle_status=SkillLifecycleStatus.ACTIVE,
                    activation_source=current.activation_source
                    if current
                    else str(skill.root_path),
                    activation_target=str(skill.root_path),
                    last_validated_at=current.last_validated_at if current else None,
                    last_approved_at=current.last_approved_at if current else None,
                    # META-001: propagate structured contracts so dispatcher can
                    # hard-filter by requested action instead of text-similarity.
                    contracts=list(skill.metadata.contracts),
                )
            )

    def list_entries(self, *, active_only: bool = False) -> list[SkillRegistryEntry]:
        return self.store.list_skill_registry_entries(active_only=active_only)

    def select(self, node: TaskNode, preferred_skills: list[str] | None = None) -> SkillSelection:
        preferred_skills = preferred_skills or []
        all_entries = self.store.list_skill_registry_entries(active_only=True)
        builtin = EXECUTOR_TO_BUILTIN.get(node.executor_hint or "")

        # META-001: pull the requested `action` off node.input_payload so we can
        # hard-filter by contract before any scoring. Non-action executors
        # (python / browser / llm_native) don't set this field and skills with
        # empty contracts are legacy pass-through, both handled by
        # `SkillRegistryEntry.supports_action()`.
        requested_action: str | None = None
        if isinstance(node.input_payload, dict):
            action_val = node.input_payload.get("action")
            if isinstance(action_val, str) and action_val.strip():
                requested_action = action_val.strip()

        def _contract_ok(entry: SkillRegistryEntry) -> bool:
            return entry.supports_action(requested_action)

        entries = [entry for entry in all_entries if _contract_ok(entry)]
        if node.executor_hint and builtin:
            entries = [
                entry
                for entry in entries
                if entry.skill_name == builtin
                or node.executor_hint in entry.tags
                or entry.skill_name in preferred_skills
                or entry.skill_name in node.required_skills
            ]

        graph_candidates = {
            item["skill_name"]: item
            for item in (
                self.graph_memory.recommend_skills_for_goal(
                    node.goal, executor_hint=node.executor_hint, limit=10
                )
                if self.graph_memory is not None
                else []
            )
        }
        explicit_name = node.skill_name or next(
            (
                item
                for item in node.required_skills
                if "." in item and not item.startswith("builtin.")
            ),
            None,
        )
        if explicit_name:
            entry = self.store.get_skill_registry_entry(explicit_name)
            if entry is None:
                gap_reason = f"显式指定的 Skill 未注册: {explicit_name}"
                return SkillSelection(
                    node_id=node.node_id,
                    selected_skill=None,
                    executor_hint=node.executor_hint,
                    gap_reason=gap_reason,
                    explanation={
                        "source": "explicit_missing",
                        "reason": gap_reason,
                    },
                )
            if not _contract_ok(entry):
                gap_reason = (
                    f"Skill {explicit_name} 未声明 action={requested_action}，"
                    f"已声明 actions={[c.action for c in entry.contracts]}"
                )
                return SkillSelection(
                    node_id=node.node_id,
                    selected_skill=None,
                    executor_hint=node.executor_hint,
                    gap_reason=gap_reason,
                    registry_match=entry,
                    explanation={
                        "source": "contract_mismatch",
                        "reason": gap_reason,
                        "requested_action": requested_action,
                        "declared_actions": [c.action for c in entry.contracts],
                    },
                )
            if entry.lifecycle_status == SkillLifecycleStatus.ACTIVE:
                return SkillSelection(
                    node_id=node.node_id,
                    selected_skill=entry.skill_name,
                    executor_hint=node.executor_hint,
                    registry_match=entry,
                    explanation={"source": "explicit", "reason": "节点显式指定了 skill"},
                )
            gap_reason = f"Skill {explicit_name} 当前状态为 {entry.lifecycle_status}，尚不能执行"
            return SkillSelection(
                node_id=node.node_id,
                selected_skill=None,
                executor_hint=node.executor_hint,
                gap_reason=gap_reason,
                registry_match=entry,
                explanation={
                    "source": "inactive_skill",
                    "reason": f"skill {explicit_name} 当前状态为 {entry.lifecycle_status}",
                    "lifecycle_status": str(entry.lifecycle_status),
                },
            )

        scored: list[tuple[float, SkillRegistryEntry]] = []
        for entry in entries:
            score = 0.0
            matched = False
            if requested_action and entry.contract_for_action(requested_action) is not None:
                score += 60.0
                matched = True
            if entry.skill_name in node.required_skills:
                score += 100.0
                matched = True
            if entry.skill_name in preferred_skills:
                score += 25.0
                matched = True
            if builtin and entry.skill_name == builtin:
                score += 80.0
                matched = True
            if any(tag in entry.tags for tag in node.required_skills):
                score += 50.0
                matched = True
            if node.executor_hint and node.executor_hint in entry.tags:
                score += 40.0
                matched = True
            graph_candidate = graph_candidates.get(entry.skill_name)
            if graph_candidate is not None:
                score += 60.0 + float(graph_candidate.get("score", 0)) * 20.0
                matched = True
            if matched:
                permission_rank = max(
                    (
                        _PERMISSION_RANK.get(permission, PermissionLevel.L3.rank)
                        for permission in entry.permission_requirements
                    ),
                    default=PermissionLevel.L1.rank,
                )
                score += max(0, 10 - permission_rank)
                score += entry.success_rate * 20
                score -= min(entry.failure_count, 10) * 5
                scored.append((score, entry))

        if scored:
            scored.sort(key=lambda item: (item[0], item[1].success_rate), reverse=True)
            graph_candidate = graph_candidates.get(scored[0][1].skill_name)
            return SkillSelection(
                node_id=node.node_id,
                selected_skill=scored[0][1].skill_name,
                executor_hint=node.executor_hint,
                registry_match=scored[0][1],
                explanation={
                    "source": "graph+registry" if graph_candidate else "registry",
                    "graph_candidate": graph_candidate,
                },
            )

        # Nothing matched via scoring. Try the per-executor builtin as a last
        # resort, but only if it supports the requested action. If it doesn't,
        # we must report a contract gap rather than silently hand a `fetch` task
        # to builtin.file.basic.
        builtin_entry = self.store.get_skill_registry_entry(builtin) if builtin else None
        if builtin_entry is not None and _contract_ok(builtin_entry):
            gap_reason = (
                f"缺少匹配 Skill: required_skills={node.required_skills or ['none']}, "
                f"executor_hint={node.executor_hint or 'none'}"
            )
            return SkillSelection(
                node_id=node.node_id,
                selected_skill=builtin,
                executor_hint=node.executor_hint,
                gap_reason=gap_reason,
                registry_match=builtin_entry,
                explanation={"source": "builtin_fallback", "reason": gap_reason},
            )

        if requested_action is not None:
            declared = [c.action for c in builtin_entry.contracts] if builtin_entry else []
            gap_reason = (
                f"无 Skill 声明 action={requested_action}（executor_hint={node.executor_hint or 'none'}，"
                f"builtin 声明 actions={declared}）"
            )
            return SkillSelection(
                node_id=node.node_id,
                selected_skill=None,
                executor_hint=node.executor_hint,
                gap_reason=gap_reason,
                registry_match=builtin_entry,
                explanation={
                    "source": "contract_gap",
                    "reason": gap_reason,
                    "requested_action": requested_action,
                    "builtin_declared_actions": declared,
                },
            )

        gap_reason = (
            f"缺少匹配 Skill: required_skills={node.required_skills or ['none']}, "
            f"executor_hint={node.executor_hint or 'none'}"
        )
        return SkillSelection(
            node_id=node.node_id,
            selected_skill=builtin,
            executor_hint=node.executor_hint,
            gap_reason=gap_reason,
            registry_match=builtin_entry,
            explanation={"source": "builtin_fallback", "reason": gap_reason},
        )

    def record_execution(self, skill_name: str, *, success: bool) -> None:
        self.store.update_skill_registry_stats(skill_name, success=success)

    def _scope_from_location(self, location: str) -> str:
        if location == "builtin":
            return "builtin"
        if location.startswith("agent:"):
            return "agent"
        if location == "shared":
            return "shared"
        return "workspace"

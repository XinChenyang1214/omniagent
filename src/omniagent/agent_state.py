from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omniagent.models import utc_now


STATE_SCHEMA_VERSION = "agent-state/v1"

SECTION_FILES = {
    "profile": "agent_profile.json",
    "preferences": "user_preferences.json",
    "project": "project_state.json",
    "goal": "current_goal.json",
}
SECTION_ALIASES = {
    "agent_profile": "profile",
    "user_preferences": "preferences",
    "project_state": "project",
    "current_goal": "goal",
}
SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
)


class AgentStateValidationError(ValueError):
    """Raised when agent state would persist sensitive or invalid data."""


@dataclass(slots=True)
class AgentProfile:
    schema_version: str = STATE_SCHEMA_VERSION
    agent_name: str = "OmniAgent"
    role: str = "Workspace-local personal agent for developer and technical tasks."
    default_language: str = "zh-CN"
    communication_style: str = "Direct, evidence-based, concise, and implementation-oriented."
    capability_boundaries: list[str] = field(
        default_factory=lambda: [
            "Single-user, single-machine, CLI-first local runtime.",
            "Does not bypass permissions, sandbox boundaries, or explicit user instructions.",
            "Network, deletion, external services, real accounts, and paid APIs require consent.",
            "Learning outputs must stay auditable and must not promote without evidence.",
        ]
    )
    risk_policy_summary: str = (
        "High-risk actions such as deletion, external paths, network access, real accounts, "
        "dependency installation, system modification, and paid APIs require explicit permission "
        "and audit evidence."
    )
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class PreferenceItem:
    id: str
    scope: str
    statement: str
    source: str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UserPreference:
    schema_version: str = STATE_SCHEMA_VERSION
    preferences: list[dict[str, Any]] = field(default_factory=list)
    disabled_preferences: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ProjectState:
    schema_version: str = STATE_SCHEMA_VERSION
    project_root: str = ""
    current_workstream: str = "unknown"
    authoritative_docs: list[str] = field(default_factory=list)
    archived_docs: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class CurrentGoal:
    schema_version: str = STATE_SCHEMA_VERSION
    goal_id: str = field(default_factory=lambda: f"goal_{uuid.uuid4().hex[:10]}")
    title: str = "No active user goal recorded"
    definition_of_done: list[str] = field(default_factory=list)
    status: str = "paused"
    active_phase: str = "none"
    blockers: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AgentStateSnapshot:
    profile: dict[str, Any]
    preferences: dict[str, Any]
    project: dict[str, Any]
    goal: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "preferences": self.preferences,
            "project": self.project,
            "goal": self.goal,
        }


def normalize_section(section: str) -> str:
    normalized = section.strip().lower().replace("-", "_")
    normalized = SECTION_ALIASES.get(normalized, normalized)
    if normalized not in SECTION_FILES:
        allowed = ", ".join(sorted(SECTION_FILES))
        raise AgentStateValidationError(f"unknown agent state section: {section} ({allowed})")
    return normalized


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _sensitive_reason(key: str, value: Any) -> str | None:
    if SENSITIVE_KEY_PATTERN.search(key):
        return f"sensitive key: {key}"
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS):
            return f"sensitive value under key: {key}"
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            reason = _sensitive_reason(str(item_key), item_value)
            if reason:
                return reason
    if isinstance(value, list):
        for item in value:
            reason = _sensitive_reason(key, item)
            if reason:
                return reason
    return None


def assert_no_sensitive_values(payload: dict[str, Any]) -> None:
    reason = _sensitive_reason("agent_state", payload)
    if reason:
        raise AgentStateValidationError(
            f"agent state refuses to persist sensitive data ({reason})"
        )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS):
        return "[REDACTED]"
    return value


def _existing_relative_paths(project_root: Path, candidates: list[str]) -> list[str]:
    return [item for item in candidates if (project_root / item).exists()]


def _default_project_state(project_root: Path) -> ProjectState:
    authoritative_docs = _existing_relative_paths(
        project_root,
        [
            "README.md",
            "docs/doc-registry.md",
            "docs/specs/README.md",
            "docs/specs/current-system.md",
            "docs/specs/task-intake.md",
            "docs/product-architecture.md",
            "docs/specs/agent-state.md",
            "docs/specs/experience-record.md",
            "docs/specs/procedure-memory.md",
            "docs/specs/skill-promotion.md",
            "docs/specs/learning-feedback.md",
            "docs/specs/learning-correction.md",
            "ROADMAP.md",
            "issue.md",
        ],
    )
    archived_docs = _existing_relative_paths(
        project_root,
        ["docs/archive"],
    )
    workstream = (
        "none"
        if "docs/specs/current-system.md" in authoritative_docs
        else "unknown"
    )
    return ProjectState(
        project_root=str(project_root),
        current_workstream=workstream,
        authoritative_docs=authoritative_docs,
        archived_docs=archived_docs,
        open_questions=[],
    )


def _default_current_goal(project_root: Path) -> CurrentGoal:
    has_agent_state_spec = (project_root / "docs/specs/agent-state.md").exists()
    if not has_agent_state_spec:
        return CurrentGoal()
    return CurrentGoal(
        title="Validate general Agent usability with real task evidence",
        definition_of_done=[
            "Offline general-agent scenarios remain green.",
            "A healthy real LLM provider completes the five tool-choice scenarios.",
            "Failures are attributed to the correct runtime layer and repaired with regressions.",
        ],
        status="active",
        active_phase="General Agent product validation",
        next_actions=[
            "Run the offline scenario and test baselines.",
            "Confirm provider configuration before any real LLM run.",
            "Run the real tool-choice baseline after explicit authorization.",
        ],
        evidence_refs=[
            "ROADMAP.md",
            "issue.md",
            "docs/specs/current-system.md",
            "docs/runbooks/real-world-evaluation.md",
        ],
    )


def _merge_defaults(section: str, payload: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
    merged = dict(default)
    merged.update(payload)
    if not merged.get("schema_version"):
        merged["schema_version"] = STATE_SCHEMA_VERSION
    return merged


class AgentStateStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.root = self.workspace_root / "agent_state"
        self.project_root = self.workspace_root.parent.resolve()

    def path_for(self, section: str) -> Path:
        return self.root / SECTION_FILES[normalize_section(section)]

    def default_payloads(self) -> dict[str, dict[str, Any]]:
        return {
            "profile": asdict(AgentProfile()),
            "preferences": asdict(UserPreference()),
            "project": asdict(_default_project_state(self.project_root)),
            "goal": asdict(_default_current_goal(self.project_root)),
        }

    def ensure_defaults(self) -> AgentStateSnapshot:
        defaults = self.default_payloads()
        self.root.mkdir(parents=True, exist_ok=True)
        for section, payload in defaults.items():
            path = self.path_for(section)
            if not path.exists():
                assert_no_sensitive_values(payload)
                _atomic_write_json(path, payload)
        return self.load()

    def load(self) -> AgentStateSnapshot:
        defaults = self.default_payloads()
        self.root.mkdir(parents=True, exist_ok=True)
        loaded: dict[str, dict[str, Any]] = {}
        for section, default_payload in defaults.items():
            path = self.path_for(section)
            if not path.exists():
                assert_no_sensitive_values(default_payload)
                _atomic_write_json(path, default_payload)
                loaded[section] = dict(default_payload)
                continue
            payload = _merge_defaults(section, _read_json(path), default_payload)
            loaded[section] = _redact(payload)
        return AgentStateSnapshot(
            profile=loaded["profile"],
            preferences=loaded["preferences"],
            project=loaded["project"],
            goal=loaded["goal"],
        )

    def load_section(self, section: str) -> dict[str, Any]:
        snapshot = self.load().to_dict()
        return dict(snapshot[normalize_section(section)])

    def write_section(self, section: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_section(section)
        defaults = self.default_payloads()
        merged = _merge_defaults(normalized, payload, defaults[normalized])
        merged["updated_at"] = utc_now()
        assert_no_sensitive_values(merged)
        _atomic_write_json(self.path_for(normalized), merged)
        return _redact(merged)

    def update_current_goal(self, **updates: Any) -> dict[str, Any]:
        current = self.load_section("goal")
        current.update(updates)
        current["updated_at"] = utc_now()
        return self.write_section("goal", current)

    def disable_preference(self, preference_id: str) -> dict[str, Any]:
        preferences = self.load_section("preferences")
        disabled = list(preferences.get("disabled_preferences", []))
        if preference_id not in disabled:
            disabled.append(preference_id)
        preferences["disabled_preferences"] = disabled
        preferences["updated_at"] = utc_now()
        return self.write_section("preferences", preferences)

    def build_summary(self, *, task_goal: str | None = None) -> dict[str, Any]:
        snapshot = self.load()
        disabled = set(snapshot.preferences.get("disabled_preferences", []))
        active_preferences = [
            {
                "id": str(item.get("id", "")),
                "scope": str(item.get("scope", "")),
                "statement": str(item.get("statement", "")),
                "source": str(item.get("source", "")),
                "confidence": float(item.get("confidence", 0.0)),
                "evidence_refs": list(item.get("evidence_refs", [])),
            }
            for item in snapshot.preferences.get("preferences", [])
            if isinstance(item, dict) and str(item.get("id", "")) not in disabled
        ]
        summary = {
            "schema_version": STATE_SCHEMA_VERSION,
            "task_goal": task_goal,
            "agent": {
                "agent_name": snapshot.profile.get("agent_name"),
                "role": snapshot.profile.get("role"),
                "default_language": snapshot.profile.get("default_language"),
                "communication_style": snapshot.profile.get("communication_style"),
                "capability_boundaries": list(
                    snapshot.profile.get("capability_boundaries", [])
                ),
            },
            "project": {
                "project_root": snapshot.project.get("project_root"),
                "current_workstream": snapshot.project.get("current_workstream"),
                "authoritative_docs": list(snapshot.project.get("authoritative_docs", [])),
                "archived_docs": list(snapshot.project.get("archived_docs", [])),
            },
            "current_goal": {
                "goal_id": snapshot.goal.get("goal_id"),
                "title": snapshot.goal.get("title"),
                "status": snapshot.goal.get("status"),
                "active_phase": snapshot.goal.get("active_phase"),
                "definition_of_done": list(snapshot.goal.get("definition_of_done", [])),
                "next_actions": list(snapshot.goal.get("next_actions", [])),
                "evidence_refs": list(snapshot.goal.get("evidence_refs", [])),
                "blockers": list(snapshot.goal.get("blockers", [])),
            },
            "active_preferences": active_preferences,
            "disabled_preferences": sorted(disabled),
            "risk_policy_summary": snapshot.profile.get("risk_policy_summary"),
            "open_questions": list(snapshot.project.get("open_questions", [])),
        }
        return _redact(summary)


def format_agent_state_summary(summary: dict[str, Any]) -> str:
    project = summary.get("project", {})
    goal = summary.get("current_goal", {})
    agent = summary.get("agent", {})
    lines = [
        "# Agent State Summary",
        "",
        f"- current_workstream: {project.get('current_workstream') or 'unknown'}",
        f"- current_goal: {goal.get('title') or 'none'}",
        f"- goal_status: {goal.get('status') or 'unknown'}",
        f"- active_phase: {goal.get('active_phase') or 'none'}",
        f"- agent_role: {agent.get('role') or 'unknown'}",
        f"- default_language: {agent.get('default_language') or 'unknown'}",
        f"- risk_policy_summary: {summary.get('risk_policy_summary') or 'none'}",
    ]
    preferences = summary.get("active_preferences", [])
    if preferences:
        lines.extend(["", "## Active Preferences"])
        for item in preferences:
            lines.append(
                f"- {item.get('id')} [{item.get('scope')} confidence={item.get('confidence')}]: "
                f"{item.get('statement')}"
            )
    open_questions = summary.get("open_questions", [])
    if open_questions:
        lines.extend(["", "## Open Questions"])
        lines.extend(f"- {item}" for item in open_questions)
    return "\n".join(lines) + "\n"

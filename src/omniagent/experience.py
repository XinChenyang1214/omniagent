from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from omniagent.models import (
    CompletionEvaluation,
    CriticVerdict,
    ExecutionResult,
    Task,
    TaskGraph,
    TaskRequest,
    TaskStatus,
    utc_now,
)


EXPERIENCE_SCHEMA_VERSION = "experience-record/v1"
SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
)


def _json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


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


def _safe_text(value: Any, *, limit: int = 220) -> str:
    text = str(value)
    for pattern in SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _summarize_value(value: Any) -> str:
    if isinstance(value, dict):
        safe_keys = [
            str(key) if not SENSITIVE_KEY_PATTERN.search(str(key)) else "[REDACTED_KEY]"
            for key in value.keys()
        ]
        return f"dict(keys={safe_keys}, json_chars={_json_chars(value)})"
    if isinstance(value, list):
        return f"list(items={len(value)}, json_chars={_json_chars(value)})"
    if isinstance(value, str):
        return f"str(chars={len(value)}, preview={_safe_text(value)!r})"
    if value is None:
        return "none"
    return f"{type(value).__name__}({ _safe_text(value)!r })"


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _artifact_refs_for_node(node: Any) -> list[str]:
    refs: list[str] = []
    for artifact in getattr(node, "artifacts", []) or []:
        refs.append(str(getattr(artifact, "path", "")))
    return [item for item in refs if item]


def _terminal_status(
    task: Task,
    result: ExecutionResult,
    permissions: list[dict[str, Any]],
) -> str:
    if any(item.get("status") == "denied" for item in permissions):
        if str(task.status) in {str(TaskStatus.WAITING_INPUT), "waiting_input"}:
            return "permission_denied"
    if str(task.status) in {str(TaskStatus.WAITING_INPUT), "waiting_input"}:
        return "blocked"
    if str(task.status) in {str(TaskStatus.SUSPENDED), "suspended"}:
        return "blocked"
    if str(task.status) in {str(TaskStatus.CANCELLED), "cancelled"}:
        return "cancelled"
    if result.success and str(task.status) == str(TaskStatus.COMPLETED):
        return "succeeded"
    if str(task.status) == str(TaskStatus.FAILED) or not result.success:
        return "failed"
    return str(task.status)


def _validation(
    *,
    task_dir: Path,
    result: ExecutionResult,
    completion_evaluation: CompletionEvaluation | None,
    critic_verdicts: list[CriticVerdict],
) -> dict[str, Any]:
    evidence_refs: list[str] = []
    completion_path = task_dir / "completion" / "evaluation.json"
    if completion_evaluation is not None and completion_path.exists():
        evidence_refs.append(_relative(completion_path, task_dir.parent.parent))
    critic_dir = task_dir / "critic"
    if critic_dir.exists():
        evidence_refs.extend(
            _relative(path, task_dir.parent.parent)
            for path in sorted(critic_dir.glob("*.json"))[:20]
        )
    if completion_evaluation is not None:
        if completion_evaluation.task_satisfied and evidence_refs:
            verdict = "passed"
        elif completion_evaluation.completion_score > 0 and evidence_refs:
            verdict = "partial"
        else:
            verdict = "failed"
        return {
            "verdict": verdict,
            "method": "completion",
            "evidence_refs": evidence_refs,
            "notes": completion_evaluation.summary,
        }
    if critic_verdicts:
        failed = any(str(item.action) == "fail" for item in critic_verdicts)
        return {
            "verdict": "failed" if failed else "not_verified",
            "method": "critic",
            "evidence_refs": evidence_refs,
            "notes": critic_verdicts[-1].summary,
        }
    return {
        "verdict": "not_verified",
        "method": "runtime_result",
        "evidence_refs": evidence_refs,
        "notes": result.message,
    }


def _failure(
    result: ExecutionResult,
    completion_evaluation: CompletionEvaluation | None,
    permissions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    denied = [item for item in permissions if item.get("status") == "denied"]
    if denied:
        primary = denied[-1]
        return {
            "failure_layer": "permission",
            "error_code": "permission_denied",
            "cause": str(primary.get("rationale", "")),
            "permission": primary.get("permission"),
            "resource_ref": primary.get("resource_ref"),
        }
    if result.success:
        return None
    if completion_evaluation is not None:
        failure_layer = completion_evaluation.primary_failure_layer
    else:
        failure_layer = result.executor or "runtime"
    return {
        "failure_layer": failure_layer,
        "error_code": result.error_code,
        "cause": result.message,
    }


def _learning_candidates(
    *,
    status: str,
    result: ExecutionResult,
    validation: dict[str, Any],
    failure: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if failure is not None:
        candidates.append(
            {
                "type": "failure_pattern",
                "statement": _safe_text(failure.get("cause", result.message)),
                "confidence": 0.6,
                "requires_review": True,
                "evidence_refs": list(validation.get("evidence_refs", [])),
            }
        )
    if status == "succeeded" and validation.get("verdict") == "passed":
        candidates.append(
            {
                "type": "procedure_candidate",
                "statement": "Task completed with validation evidence; review before promotion.",
                "confidence": 0.4,
                "requires_review": True,
                "evidence_refs": list(validation.get("evidence_refs", [])),
            }
        )
    return candidates


class ExperienceRecordWriter:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def write(
        self,
        *,
        task: Task,
        request: TaskRequest,
        task_dir: Path,
        result: ExecutionResult,
        task_graph: TaskGraph | None,
        critic_verdicts: list[CriticVerdict],
        completion_evaluation: CompletionEvaluation | None = None,
        agent_state_summary: dict[str, Any] | None = None,
        permissions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        permissions = permissions or []
        agent_state_summary = dict(agent_state_summary or {})
        status = _terminal_status(task, result, permissions)
        validation = _validation(
            task_dir=task_dir,
            result=result,
            completion_evaluation=completion_evaluation,
            critic_verdicts=critic_verdicts,
        )
        failure = _failure(result, completion_evaluation, permissions)
        record = {
            "schema_version": EXPERIENCE_SCHEMA_VERSION,
            "experience_id": f"exp_{uuid.uuid4().hex[:12]}",
            "task_id": task.task_id,
            "thread_id": request.metadata.get("thread_id"),
            "goal_id": (
                agent_state_summary.get("current_goal", {}).get("goal_id")
                if isinstance(agent_state_summary.get("current_goal"), dict)
                else None
            ),
            "created_at": utc_now(),
            "status": status,
            "user_goal": request.goal,
            "definition_of_done": self._definition_of_done(request, agent_state_summary),
            "context_refs": self._context_refs(task_dir, agent_state_summary),
            "actions": self._actions(task_graph),
            "artifacts": self._artifacts(result, task_graph),
            "validation": validation,
            "failure": failure,
            "learning_candidates": _learning_candidates(
                status=status,
                result=result,
                validation=validation,
                failure=failure,
            ),
            "sensitive_data_policy": {
                "raw_payload_stored": False,
                "raw_outputs_stored": False,
                "large_artifacts_copied": False,
                "summary_strategy": "type_keys_counts_and_short_redacted_previews",
            },
        }
        record_path = task_dir / "experience" / "experience_record.json"
        _atomic_write_json(record_path, record)
        self._append_index(record)
        return record

    def _definition_of_done(
        self,
        request: TaskRequest,
        agent_state_summary: dict[str, Any],
    ) -> list[str]:
        if request.expected_output:
            return [request.expected_output]
        current_goal = agent_state_summary.get("current_goal", {})
        if isinstance(current_goal, dict):
            return [str(item) for item in current_goal.get("definition_of_done", [])]
        return []

    def _context_refs(self, task_dir: Path, agent_state_summary: dict[str, Any]) -> list[str]:
        refs = [
            _relative(task_dir / "request.json", self.workspace_root),
            _relative(task_dir / "context_snapshot.json", self.workspace_root),
            _relative(task_dir / "plan" / "policy_decision.json", self.workspace_root),
        ]
        project = agent_state_summary.get("project", {})
        if isinstance(project, dict):
            refs.extend(str(item) for item in project.get("authoritative_docs", []))
        refs.extend(
            [
                "agent_state/agent_profile.json",
                "agent_state/user_preferences.json",
                "agent_state/project_state.json",
                "agent_state/current_goal.json",
            ]
        )
        return sorted({item for item in refs if item})

    def _actions(self, task_graph: TaskGraph | None) -> list[dict[str, Any]]:
        if task_graph is None:
            return []
        actions: list[dict[str, Any]] = []
        for node in task_graph.nodes:
            tool_or_skill = node.skill_name or node.executor_hint or str(node.type)
            actions.append(
                {
                    "node_id": node.node_id,
                    "tool_or_skill": tool_or_skill,
                    "input_summary": _summarize_value(node.input_payload),
                    "output_summary": _summarize_value(node.output),
                    "status": str(node.status),
                    "artifact_refs": _artifact_refs_for_node(node),
                }
            )
        return actions

    def _artifacts(
        self,
        result: ExecutionResult,
        task_graph: TaskGraph | None,
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(kind: str, path: str, description: str) -> None:
            if not path or path in seen:
                return
            seen.add(path)
            artifacts.append(
                {
                    "kind": kind,
                    "path": path,
                    "description": description,
                }
            )

        for artifact in result.artifacts:
            add(artifact.kind, artifact.path, artifact.description)
        if task_graph is not None:
            for node in task_graph.nodes:
                for artifact in node.artifacts:
                    add(artifact.kind, artifact.path, artifact.description)
        return artifacts

    def _append_index(self, record: dict[str, Any]) -> None:
        index_path = self.workspace_root / "experience" / "index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": record["schema_version"],
            "experience_id": record["experience_id"],
            "task_id": record["task_id"],
            "goal_id": record.get("goal_id"),
            "status": record["status"],
            "user_goal": record["user_goal"],
            "validation_verdict": record["validation"]["verdict"],
            "created_at": record["created_at"],
        }
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

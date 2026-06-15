from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from omniagent.models import utc_now


PROCEDURE_SCHEMA_VERSION = "procedure-memory/v1"
HABIT_FAST_PATH_SCHEMA_VERSION = "habit-fast-path/v1"
ACTIVE_STATUSES = ("draft", "verified", "habit")
ALL_STATUSES = ("draft", "verified", "habit", "disabled", "deprecated")
HABIT_SUCCESS_THRESHOLD = 3
PROCEDURE_EDITABLE_FIELDS = {
    "name",
    "trigger",
    "risk_level",
    "required_permissions",
    "inputs",
    "steps",
    "outputs",
    "validator",
}
PROCEDURE_REVIEW_DECISIONS = ("approved", "needs_changes", "rejected")
PROCEDURE_REVIEW_STATUSES_REQUIRING_EVIDENCE = {
    "edited_requires_review",
    "decayed_requires_review",
}


class ProcedureMemoryError(ValueError):
    """Raised when procedure memory cannot complete a requested transition."""


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


def _slug(text: str, *, limit: int = 52) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized[:limit] or "Untitled procedure"


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", text.lower())
    tokens: set[str] = set()
    for word in words:
        if len(word) <= 1 and not re.match(r"[\u4e00-\u9fff]", word):
            continue
        tokens.add(word)
    return tokens


def _overlap_score(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap / max(len(left_tokens), len(right_tokens))


def _step_action_type(target: str) -> str:
    if target.startswith("builtin.") or target.startswith("workspace."):
        return "skill"
    if target in {"file", "python", "browser", "llm_native"}:
        return "executor"
    return "tool"


def _risk_level(experience: dict[str, Any]) -> str:
    text = json.dumps(experience, ensure_ascii=False).lower()
    if any(item in text for item in ("filesystem.delete", "delete", "remove", "system.exec")):
        return "high"
    if any(item in text for item in ("network", "code_execution", "python")):
        return "medium"
    return "low"


def _required_permissions(experience: dict[str, Any]) -> list[str]:
    permissions: set[str] = set()
    failure = experience.get("failure") or {}
    if isinstance(failure, dict) and failure.get("permission"):
        permissions.add(str(failure["permission"]))
    risk = _risk_level(experience)
    if risk == "high":
        permissions.add("user_confirmation")
    elif risk == "medium":
        permissions.add("review_before_auto_run")
    return sorted(permissions)


def _source_experience_path(workspace_root: Path, experience: dict[str, Any]) -> Path | None:
    task_id = experience.get("task_id")
    if not task_id:
        return None
    path = workspace_root / "tasks" / str(task_id) / "experience" / "experience_record.json"
    return path if path.exists() else None


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProcedureMemoryError(f"{field} must be a list")
    return [str(item) for item in value]


def _object_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProcedureMemoryError(f"{field} must be a list")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProcedureMemoryError(f"{field} items must be objects")
        rows.append(dict(item))
    return rows


class ProcedureMemoryStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.root = self.workspace_root / "procedures"

    def ensure(self) -> None:
        for status in ALL_STATUSES:
            self._dir_for_status(status).mkdir(parents=True, exist_ok=True)
        (self.root / "index.jsonl").parent.mkdir(parents=True, exist_ok=True)
        if not (self.root / "index.jsonl").exists():
            (self.root / "index.jsonl").write_text("", encoding="utf-8")

    def extract_draft_from_experience(self, experience: dict[str, Any]) -> dict[str, Any] | None:
        validation = experience.get("validation") or {}
        if validation.get("verdict") != "passed":
            return None
        evidence_refs = list(validation.get("evidence_refs", []))
        if not evidence_refs:
            return None
        existing = self._existing_from_experience(str(experience.get("experience_id", "")))
        if existing is not None:
            return existing
        procedure = self._draft_from_experience(experience)
        self.save(procedure)
        return procedure

    def _draft_from_experience(self, experience: dict[str, Any]) -> dict[str, Any]:
        experience_id = str(experience.get("experience_id") or "")
        user_goal = str(experience.get("user_goal") or "Untitled task")
        actions = [item for item in experience.get("actions", []) if isinstance(item, dict)]
        steps: list[dict[str, Any]] = []
        for index, action in enumerate(actions, start=1):
            target = str(action.get("tool_or_skill") or "unknown")
            steps.append(
                {
                    "step_id": f"step_{index}",
                    "action_type": _step_action_type(target),
                    "target": target,
                    "input_template": {"summary": action.get("input_summary", "")},
                    "expected_output": str(action.get("output_summary") or ""),
                    "fallback": "fallback_to_planner",
                }
            )
        if not steps:
            steps.append(
                {
                    "step_id": "step_1",
                    "action_type": "validator",
                    "target": "manual_review",
                    "input_template": {},
                    "expected_output": "Review the source experience before reuse.",
                    "fallback": "fallback_to_planner",
                }
            )
        validation = experience.get("validation") or {}
        now = utc_now()
        return {
            "schema_version": PROCEDURE_SCHEMA_VERSION,
            "procedure_id": f"proc_{uuid.uuid4().hex[:12]}",
            "name": _slug(user_goal),
            "status": "draft",
            "trigger": {
                "task_patterns": [user_goal],
                "required_context": [],
                "negative_patterns": [],
            },
            "risk_level": _risk_level(experience),
            "required_permissions": _required_permissions(experience),
            "inputs": [],
            "steps": steps,
            "outputs": [
                {
                    "kind": item.get("kind"),
                    "description": item.get("description"),
                    "artifact_ref": item.get("path"),
                }
                for item in experience.get("artifacts", [])
                if isinstance(item, dict)
            ],
            "validator": {
                "method": validation.get("method") or "manual_review",
                "required_evidence": list(validation.get("evidence_refs", [])),
                "failure_behavior": "request_review",
            },
            "source_experience_ids": [experience_id] if experience_id else [],
            "source_task_ids": [experience.get("task_id")] if experience.get("task_id") else [],
            "success_count": 0,
            "failure_count": 0,
            "confidence": 0.35,
            "audit_notes": [
                {
                    "event": "created_from_experience",
                    "experience_id": experience_id,
                    "created_at": now,
                }
            ],
            "created_at": now,
            "updated_at": now,
        }

    def list(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        self.ensure()
        statuses = ALL_STATUSES if include_disabled else ACTIVE_STATUSES
        procedures: list[dict[str, Any]] = []
        seen: set[str] = set()
        for status in statuses:
            for path in sorted(self._dir_for_status(status).glob("*.json")):
                payload = self._read(path)
                procedure_id = str(payload.get("procedure_id", path.stem))
                if procedure_id in seen:
                    continue
                seen.add(procedure_id)
                procedures.append(payload)
        return sorted(procedures, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def get(self, procedure_id: str) -> dict[str, Any] | None:
        self.ensure()
        for status in ALL_STATUSES:
            path = self._dir_for_status(status) / f"{procedure_id}.json"
            if path.exists():
                return self._read(path)
        return None

    def save(self, procedure: dict[str, Any]) -> dict[str, Any]:
        self.ensure()
        status = str(procedure.get("status") or "draft")
        if status not in ALL_STATUSES:
            raise ProcedureMemoryError(f"unknown procedure status: {status}")
        procedure_id = str(procedure.get("procedure_id") or f"proc_{uuid.uuid4().hex[:12]}")
        procedure["procedure_id"] = procedure_id
        procedure["schema_version"] = procedure.get("schema_version") or PROCEDURE_SCHEMA_VERSION
        procedure["updated_at"] = utc_now()
        for other_status in ALL_STATUSES:
            if other_status == status:
                continue
            old_path = self._dir_for_status(other_status) / f"{procedure_id}.json"
            if old_path.exists():
                old_path.unlink()
        _atomic_write_json(self._dir_for_status(status) / f"{procedure_id}.json", procedure)
        self.rebuild_index()
        return procedure

    def disable(self, procedure_id: str, *, note: str | None = None) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        previous_status = str(procedure.get("status") or "draft")
        procedure["status"] = "disabled"
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "disabled",
                "note": note or "",
                "previous_status": previous_status,
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def enable(self, procedure_id: str, *, note: str | None = None) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        current_status = str(procedure.get("status") or "draft")
        if current_status != "disabled":
            raise ProcedureMemoryError(
                f"procedure is not disabled and cannot be enabled: {procedure_id}"
            )
        previous_status = ""
        for audit_note in reversed(list(procedure.get("audit_notes") or [])):
            if not isinstance(audit_note, dict):
                continue
            if audit_note.get("event") == "disabled":
                previous_status = str(audit_note.get("previous_status") or "")
                break
        if previous_status not in ACTIVE_STATUSES:
            raise ProcedureMemoryError(
                f"disabled procedure has no restorable active status: {procedure_id}"
            )
        procedure["status"] = previous_status
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "enabled",
                "note": note or "",
                "previous_status": current_status,
                "restored_status": previous_status,
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def edit(
        self,
        procedure_id: str,
        *,
        patch: dict[str, Any],
        note: str | None = None,
        reviewer: str = "local_user",
    ) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        normalized_patch = self._normalize_edit_patch(patch)
        if not normalized_patch:
            raise ProcedureMemoryError("procedure edit patch is empty")

        previous_status = str(procedure.get("status", "draft"))
        previous_counts = {
            "success_count": int(procedure.get("success_count", 0)),
            "failure_count": int(procedure.get("failure_count", 0)),
        }
        for key, value in normalized_patch.items():
            if key == "trigger":
                trigger = dict(procedure.get("trigger") or {})
                trigger.update(value)
                procedure["trigger"] = trigger
            elif key == "validator":
                validator = dict(procedure.get("validator") or {})
                validator.update(value)
                procedure["validator"] = validator
            else:
                procedure[key] = value

        behavior_fields = set(normalized_patch) - {"name"}
        if behavior_fields and previous_status in ACTIVE_STATUSES:
            procedure["status"] = "draft"
            procedure["success_count"] = 0
            procedure["failure_count"] = 0
            procedure["confidence"] = min(float(procedure.get("confidence", 0.0)), 0.35)
            procedure["review_status"] = "edited_requires_review"
        else:
            procedure["review_status"] = "edited"

        now = utc_now()
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "edited",
                "reviewer": reviewer,
                "changed_fields": sorted(normalized_patch),
                "from_status": previous_status,
                "to_status": procedure.get("status"),
                "previous_counts": previous_counts,
                "note": note or "",
                "created_at": now,
            }
        )
        return self.save(procedure)

    def decay(
        self,
        procedure_id: str,
        *,
        note: str | None = None,
        reviewer: str = "local_user",
    ) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        previous_status = str(procedure.get("status") or "draft")
        if previous_status in {"disabled", "deprecated"}:
            raise ProcedureMemoryError(
                f"procedure is {previous_status} and cannot be decayed: {procedure_id}"
            )
        if previous_status == "habit":
            next_status = "verified"
        else:
            next_status = "draft"
        previous_counts = {
            "success_count": int(procedure.get("success_count", 0)),
            "failure_count": int(procedure.get("failure_count", 0)),
        }
        procedure["status"] = next_status
        procedure["success_count"] = 0
        procedure["failure_count"] = 0
        procedure["confidence"] = min(float(procedure.get("confidence", 0.0)), 0.35)
        procedure["review_status"] = "decayed_requires_review"
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "decayed",
                "reviewer": reviewer,
                "from_status": previous_status,
                "to_status": next_status,
                "previous_counts": previous_counts,
                "note": note or "",
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def review(
        self,
        procedure_id: str,
        *,
        decision: str,
        reviewer: str = "local_user",
        note: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        decision = decision.strip().lower()
        if decision not in PROCEDURE_REVIEW_DECISIONS:
            raise ProcedureMemoryError(
                f"review decision must be one of: {', '.join(PROCEDURE_REVIEW_DECISIONS)}"
            )
        evidence_refs = [str(item) for item in list(evidence_refs or [])]
        procedure = self._require(procedure_id)
        old_status = str(procedure.get("status", "draft"))
        old_review_status = str(procedure.get("review_status") or "")
        if decision == "approved" and old_status == "draft" and not evidence_refs:
            raise ProcedureMemoryError("approving a draft procedure requires evidence refs")
        if (
            decision == "approved"
            and old_status != "draft"
            and old_review_status in PROCEDURE_REVIEW_STATUSES_REQUIRING_EVIDENCE
            and not evidence_refs
        ):
            raise ProcedureMemoryError(
                "approving a procedure that requires review requires evidence refs"
            )

        now = utc_now()
        review_record = {
            "decision": decision,
            "reviewer": reviewer,
            "note": note or "",
            "evidence_refs": list(evidence_refs),
            "created_at": now,
        }
        procedure.setdefault("reviews", []).append(review_record)
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "reviewed",
                "decision": decision,
                "reviewer": reviewer,
                "from_status": old_status,
                "note": note or "",
                "evidence_refs": list(evidence_refs),
                "created_at": now,
            }
        )
        procedure["review_status"] = decision
        procedure["last_reviewed_at"] = now
        procedure["reviewed_by"] = reviewer

        if decision == "approved":
            if old_status == "draft":
                self.save(procedure)
                return self.promote_to_verified(
                    procedure_id,
                    evidence_refs=evidence_refs,
                    note=note,
                )
            return self.save(procedure)

        if decision == "needs_changes":
            if old_status in {"verified", "habit"}:
                procedure["status"] = "draft"
                procedure["success_count"] = 0
                procedure["confidence"] = min(float(procedure.get("confidence", 0.0)), 0.35)
            return self.save(procedure)

        procedure["status"] = "disabled"
        return self.save(procedure)

    def promote_to_verified(
        self,
        procedure_id: str,
        *,
        evidence_refs: list[str],
        note: str | None = None,
    ) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        if procedure.get("status") not in {"draft", "verified"}:
            raise ProcedureMemoryError("only draft or verified procedures can be verified")
        if not evidence_refs:
            raise ProcedureMemoryError("verified procedure requires validation evidence")
        if not procedure.get("source_experience_ids"):
            raise ProcedureMemoryError("verified procedure requires source experience ids")
        if not procedure.get("steps"):
            raise ProcedureMemoryError("verified procedure requires steps")
        validator = procedure.get("validator") or {}
        validator["required_evidence"] = sorted(
            {str(item) for item in list(validator.get("required_evidence", [])) + evidence_refs}
        )
        procedure["validator"] = validator
        procedure["status"] = "verified"
        procedure["success_count"] = max(1, int(procedure.get("success_count", 0)))
        procedure["confidence"] = max(float(procedure.get("confidence", 0.0)), 0.55)
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "promoted_to_verified",
                "evidence_refs": list(evidence_refs),
                "note": note or "",
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def promote_to_habit(
        self,
        procedure_id: str,
        *,
        note: str | None = None,
        require_confirmation: bool = False,
    ) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        if procedure.get("status") != "verified":
            raise ProcedureMemoryError("only verified procedures can be promoted to habit")
        success_count = int(procedure.get("success_count", 0))
        if success_count < HABIT_SUCCESS_THRESHOLD:
            raise ProcedureMemoryError(
                f"habit promotion requires at least {HABIT_SUCCESS_THRESHOLD} verified successes"
            )
        if int(procedure.get("failure_count", 0)) > 0:
            raise ProcedureMemoryError("habit promotion requires no recent validator failures")
        risk = str(procedure.get("risk_level", "low"))
        required_permissions = {
            str(item) for item in list(procedure.get("required_permissions", []))
        }
        if risk == "high" and not require_confirmation:
            raise ProcedureMemoryError("high risk habits require explicit confirmation gate")
        if risk == "high":
            required_permissions.add("user_confirmation")
        procedure["required_permissions"] = sorted(required_permissions)
        procedure["status"] = "habit"
        procedure["confidence"] = max(float(procedure.get("confidence", 0.0)), 0.75)
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "promoted_to_habit",
                "success_count": success_count,
                "require_confirmation": bool(require_confirmation),
                "note": note or "",
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def record_validator_success(
        self,
        procedure_id: str,
        *,
        evidence_refs: list[str],
        experience_id: str | None = None,
        task_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        old_status = str(procedure.get("status", "draft"))
        if old_status not in {"verified", "habit"}:
            raise ProcedureMemoryError(
                "only verified or habit procedures can record validator success"
            )
        evidence_refs = [str(item) for item in evidence_refs]
        if not evidence_refs:
            raise ProcedureMemoryError("validator success requires evidence refs")

        validator = dict(procedure.get("validator") or {})
        validator["required_evidence"] = sorted(
            {str(item) for item in list(validator.get("required_evidence", [])) + evidence_refs}
        )
        procedure["validator"] = validator
        procedure["success_count"] = int(procedure.get("success_count", 0)) + 1
        procedure["confidence"] = min(0.95, float(procedure.get("confidence", 0.0)) + 0.1)
        if experience_id:
            source_experience_ids = [
                str(item) for item in list(procedure.get("source_experience_ids", []))
            ]
            if experience_id not in source_experience_ids:
                source_experience_ids.append(experience_id)
            procedure["source_experience_ids"] = source_experience_ids
        if task_id:
            source_task_ids = [str(item) for item in list(procedure.get("source_task_ids", []))]
            if task_id not in source_task_ids:
                source_task_ids.append(task_id)
            procedure["source_task_ids"] = source_task_ids
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "validator_succeeded",
                "from_status": old_status,
                "to_status": old_status,
                "experience_id": experience_id or "",
                "task_id": task_id or "",
                "evidence_refs": list(evidence_refs),
                "success_count": int(procedure.get("success_count", 0)),
                "note": note or "",
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def record_validator_failure(
        self,
        procedure_id: str,
        *,
        evidence_ref: str,
        reason: str,
    ) -> dict[str, Any]:
        procedure = self._require(procedure_id)
        failure_count = int(procedure.get("failure_count", 0)) + 1
        procedure["failure_count"] = failure_count
        old_status = str(procedure.get("status", "draft"))
        if failure_count >= 2:
            procedure["status"] = "disabled"
        elif old_status == "habit":
            procedure["status"] = "verified"
        elif old_status == "verified":
            procedure["status"] = "draft"
        procedure["confidence"] = max(0.0, float(procedure.get("confidence", 0.0)) - 0.2)
        procedure.setdefault("audit_notes", []).append(
            {
                "event": "validator_failed",
                "from_status": old_status,
                "to_status": procedure.get("status"),
                "evidence_ref": evidence_ref,
                "reason": reason,
                "created_at": utc_now(),
            }
        )
        return self.save(procedure)

    def explain_match(
        self,
        task_goal: str,
        *,
        agent_state_summary: dict[str, Any] | None = None,
        threshold: float = 0.6,
    ) -> dict[str, Any]:
        self.ensure()
        agent_state_summary = dict(agent_state_summary or {})
        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for procedure in self.list(include_disabled=True):
            status = str(procedure.get("status", "draft"))
            if status in {"disabled", "deprecated"}:
                rejected.append(
                    {
                        "procedure_id": procedure.get("procedure_id"),
                        "status": status,
                        "reason": f"procedure is {status}",
                    }
                )
                continue
            trigger = procedure.get("trigger") or {}
            patterns = [str(item) for item in trigger.get("task_patterns", [])]
            score = max((_overlap_score(task_goal, item) for item in patterns), default=0.0)
            if score < threshold:
                rejected.append(
                    {
                        "procedure_id": procedure.get("procedure_id"),
                        "status": status,
                        "confidence": round(score, 4),
                        "reason": "trigger similarity below threshold",
                    }
                )
                continue
            candidates.append(
                {
                    "procedure": procedure,
                    "confidence": score,
                    "reason": f"matched trigger pattern with score {score:.2f}",
                }
            )
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        best = candidates[0] if candidates else None
        if best is None:
            return {
                "schema_version": PROCEDURE_SCHEMA_VERSION,
                "retrieval_phase": "before_planner",
                "task_goal": task_goal,
                "matched": False,
                "matched_procedure_id": None,
                "confidence": 0.0,
                "reason": "no procedure matched",
                "suggestion_mode": "fallback_to_planner",
                "required_confirmation": False,
                "agent_state_goal_id": (
                    agent_state_summary.get("current_goal", {}).get("goal_id")
                    if isinstance(agent_state_summary.get("current_goal"), dict)
                    else None
                ),
                "rejected_candidates": rejected[:20],
            }
        procedure = best["procedure"]
        status = str(procedure.get("status", "draft"))
        risk = str(procedure.get("risk_level", "low"))
        suggestion_mode = "suggest_only" if status == "draft" else "validator_required"
        if status == "habit" and risk == "low":
            suggestion_mode = "habit_fast_path_candidate"
        return {
            "schema_version": PROCEDURE_SCHEMA_VERSION,
            "retrieval_phase": "before_planner",
            "task_goal": task_goal,
            "matched": True,
            "matched_procedure_id": procedure.get("procedure_id"),
            "matched_status": status,
            "matched_name": procedure.get("name"),
            "risk_level": risk,
            "confidence": round(float(best["confidence"]), 4),
            "reason": best["reason"],
            "suggestion_mode": suggestion_mode,
            "required_confirmation": status != "habit" or risk != "low",
            "agent_state_goal_id": (
                agent_state_summary.get("current_goal", {}).get("goal_id")
                if isinstance(agent_state_summary.get("current_goal"), dict)
                else None
            ),
            "rejected_candidates": rejected[:20],
        }

    def build_habit_fast_path_decision(
        self,
        match: dict[str, Any],
    ) -> dict[str, Any]:
        base = {
            "schema_version": HABIT_FAST_PATH_SCHEMA_VERSION,
            "retrieval_phase": "before_planner",
            "decision": "planner_required",
            "matched": bool(match.get("matched")),
            "procedure_id": match.get("matched_procedure_id"),
            "habit_id": None,
            "confidence": match.get("confidence", 0.0),
            "risk_level": match.get("risk_level"),
            "required_confirmation": bool(match.get("required_confirmation", False)),
            "skipped_stages": [],
            "candidate_skipped_stages": [],
            "validator": {},
            "validator_evidence_refs": [],
            "required_permissions": [],
            "permission_gate": {
                "preserved": True,
                "note": "PermissionManager and ExecutionPolicy still apply before execution.",
            },
            "reason": "",
            "source_match": {
                "suggestion_mode": match.get("suggestion_mode"),
                "reason": match.get("reason"),
            },
        }
        if not match.get("matched"):
            base["reason"] = "no matched habit"
            return base
        procedure_id = str(match.get("matched_procedure_id") or "")
        procedure = self.get(procedure_id)
        if procedure is None:
            base["reason"] = "matched procedure no longer exists"
            return base
        validator = dict(procedure.get("validator") or {})
        required_evidence = [str(item) for item in list(validator.get("required_evidence", []))]
        base.update(
            {
                "habit_id": procedure_id if procedure.get("status") == "habit" else None,
                "risk_level": procedure.get("risk_level"),
                "validator": validator,
                "validator_evidence_refs": required_evidence,
                "required_permissions": [
                    str(item) for item in list(procedure.get("required_permissions", []))
                ],
                "source_experience_ids": list(procedure.get("source_experience_ids", [])),
                "success_count": int(procedure.get("success_count", 0)),
                "failure_count": int(procedure.get("failure_count", 0)),
            }
        )
        status = str(procedure.get("status", "draft"))
        risk = str(procedure.get("risk_level", "low"))
        if status != "habit":
            base["reason"] = f"matched procedure status is {status}, not habit"
            return base
        if risk != "low":
            base["required_confirmation"] = True
            base["reason"] = f"habit risk level {risk} requires confirmation"
            return base
        if int(procedure.get("success_count", 0)) < HABIT_SUCCESS_THRESHOLD:
            base["reason"] = "habit has insufficient verified successes"
            return base
        if int(procedure.get("failure_count", 0)) > 0:
            base["reason"] = "habit has recent validator failures"
            return base
        base.update(
            {
                "decision": "habit_fast_path_candidate",
                "required_confirmation": False,
                "skipped_stages": ["planner"],
                "candidate_skipped_stages": ["planner"],
                "reason": "low risk habit passed promotion gate and may skip full Planner",
            }
        )
        return base

    def rebuild_index(self) -> None:
        self.ensure()
        rows: list[dict[str, Any]] = []
        for procedure in self.list(include_disabled=True):
            rows.append(
                {
                    "schema_version": PROCEDURE_SCHEMA_VERSION,
                    "procedure_id": procedure.get("procedure_id"),
                    "name": procedure.get("name"),
                    "status": procedure.get("status"),
                    "risk_level": procedure.get("risk_level"),
                    "confidence": procedure.get("confidence"),
                    "trigger": procedure.get("trigger"),
                    "source_experience_ids": procedure.get("source_experience_ids", []),
                    "updated_at": procedure.get("updated_at"),
                }
            )
        index_path = self.root / "index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def _existing_from_experience(self, experience_id: str) -> dict[str, Any] | None:
        if not experience_id:
            return None
        for procedure in self.list(include_disabled=True):
            if experience_id in procedure.get("source_experience_ids", []):
                return procedure
        return None

    def _normalize_edit_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ProcedureMemoryError("procedure edit patch must be a JSON object")
        unknown = sorted(set(patch) - PROCEDURE_EDITABLE_FIELDS)
        if unknown:
            raise ProcedureMemoryError(
                "procedure edit patch contains unsupported fields: " + ", ".join(unknown)
            )

        normalized: dict[str, Any] = {}
        if "name" in patch:
            name = str(patch["name"]).strip()
            if not name:
                raise ProcedureMemoryError("name cannot be empty")
            normalized["name"] = name
        if "risk_level" in patch:
            risk = str(patch["risk_level"]).strip().lower()
            if risk not in {"low", "medium", "high"}:
                raise ProcedureMemoryError("risk_level must be low, medium, or high")
            normalized["risk_level"] = risk
        if "required_permissions" in patch:
            normalized["required_permissions"] = sorted(
                set(_string_list(patch["required_permissions"], field="required_permissions"))
            )
        if "inputs" in patch:
            normalized["inputs"] = _object_list(patch["inputs"], field="inputs")
        if "steps" in patch:
            steps = _object_list(patch["steps"], field="steps")
            for step in steps:
                if not step.get("step_id") or not step.get("action_type") or not step.get("target"):
                    raise ProcedureMemoryError(
                        "steps items require step_id, action_type, and target"
                    )
            normalized["steps"] = steps
        if "outputs" in patch:
            normalized["outputs"] = _object_list(patch["outputs"], field="outputs")
        if "trigger" in patch:
            trigger_patch = patch["trigger"]
            if not isinstance(trigger_patch, dict):
                raise ProcedureMemoryError("trigger must be an object")
            allowed = {"task_patterns", "required_context", "negative_patterns"}
            unknown_trigger = sorted(set(trigger_patch) - allowed)
            if unknown_trigger:
                raise ProcedureMemoryError(
                    "trigger contains unsupported fields: " + ", ".join(unknown_trigger)
                )
            normalized_trigger: dict[str, Any] = {}
            for field in allowed:
                if field in trigger_patch:
                    normalized_trigger[field] = _string_list(
                        trigger_patch[field],
                        field=f"trigger.{field}",
                    )
            normalized["trigger"] = normalized_trigger
        if "validator" in patch:
            validator_patch = patch["validator"]
            if not isinstance(validator_patch, dict):
                raise ProcedureMemoryError("validator must be an object")
            allowed = {"method", "required_evidence", "failure_behavior"}
            unknown_validator = sorted(set(validator_patch) - allowed)
            if unknown_validator:
                raise ProcedureMemoryError(
                    "validator contains unsupported fields: " + ", ".join(unknown_validator)
                )
            normalized_validator: dict[str, Any] = {}
            if "method" in validator_patch:
                method = str(validator_patch["method"]).strip()
                if not method:
                    raise ProcedureMemoryError("validator.method cannot be empty")
                normalized_validator["method"] = method
            if "required_evidence" in validator_patch:
                normalized_validator["required_evidence"] = _string_list(
                    validator_patch["required_evidence"],
                    field="validator.required_evidence",
                )
            if "failure_behavior" in validator_patch:
                failure_behavior = str(validator_patch["failure_behavior"]).strip()
                if not failure_behavior:
                    raise ProcedureMemoryError("validator.failure_behavior cannot be empty")
                normalized_validator["failure_behavior"] = failure_behavior
            normalized["validator"] = normalized_validator
        return normalized

    def _require(self, procedure_id: str) -> dict[str, Any]:
        procedure = self.get(procedure_id)
        if procedure is None:
            raise ProcedureMemoryError(f"procedure not found: {procedure_id}")
        return procedure

    def _dir_for_status(self, status: str) -> Path:
        folder = "drafts" if status == "draft" else status
        if status == "verified":
            folder = "verified"
        if status == "habit":
            folder = "habits"
        return self.root / folder

    def _read(self, path: Path) -> dict[str, Any]:
        return dict(json.loads(path.read_text(encoding="utf-8")))

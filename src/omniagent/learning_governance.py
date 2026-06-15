from __future__ import annotations

from typing import Any

from omniagent.models import utc_now


LEARNING_REVIEW_SCHEMA_VERSION = "learning-review/v1"
LEARNING_HISTORY_SCHEMA_VERSION = "learning-history/v1"
LEARNING_MATCH_EXPLANATION_SCHEMA_VERSION = "learning-match-explanation/v1"


def build_learning_review(
    *,
    experiences: list[dict[str, Any]] | None = None,
    procedures: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    object_type: str = "all",
    status: str | None = None,
) -> dict[str, Any]:
    object_type = object_type.strip().lower()
    if object_type not in {"all", "experience", "procedure", "habit", "skill"}:
        raise ValueError(
            "learning review object_type must be all, experience, procedure, habit, or skill"
        )

    items: list[dict[str, Any]] = []
    if object_type in {"all", "experience"}:
        items.extend(_experience_item(item) for item in experiences or [])
    if object_type == "all":
        items.extend(_procedure_item(item) for item in procedures if not _is_habit_object(item))
    elif object_type == "procedure":
        items.extend(_procedure_item(item) for item in procedures if not _is_habit_object(item))
    if object_type in {"all", "habit"}:
        items.extend(_habit_item(item) for item in procedures if _is_habit_object(item))
    if object_type in {"all", "skill"}:
        items.extend(_skill_item(item) for item in skills)

    if status is not None:
        expected = status.strip().lower()
        items = [item for item in items if str(item.get("status", "")).lower() == expected]

    items.sort(
        key=lambda item: (
            0 if item.get("requires_review") else 1,
            str(item.get("object_type")),
            str(item.get("id")),
        )
    )
    return {
        "schema_version": LEARNING_REVIEW_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "filters": {"object_type": object_type, "status": status},
        "summary": _summary(items),
        "items": items,
    }


def build_learning_history(
    *,
    object_type: str,
    object_id: str,
    raw_object: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    normalized_type = object_type.strip().lower()
    if normalized_type not in {"experience", "procedure", "habit", "skill"}:
        raise ValueError(
            "learning history object_type must be experience, procedure, habit, or skill"
        )
    events: list[dict[str, Any]] = []
    if normalized_type == "experience":
        events.extend(_experience_history_events(raw_object))
    elif normalized_type in {"procedure", "habit"}:
        events.extend(_procedure_history_events(raw_object))
    else:
        events.extend(_skill_history_events(raw_object))
    events.sort(key=lambda event: str(event.get("created_at") or ""))
    return {
        "schema_version": LEARNING_HISTORY_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "object_type": normalized_type,
        "id": object_id,
        "item": item,
        "summary": {
            "events": len(events),
            "by_source": _count_event_values(events, "source"),
            "by_event": _count_event_values(events, "event"),
        },
        "events": events,
    }


def build_learning_match_explanation(
    *,
    match: dict[str, Any],
    procedures: list[dict[str, Any]],
) -> dict[str, Any]:
    procedures_by_id = {
        str(procedure.get("procedure_id") or ""): procedure
        for procedure in procedures
        if procedure.get("procedure_id")
    }
    matched_id = str(match.get("matched_procedure_id") or "")
    matched_procedure = procedures_by_id.get(matched_id)
    matched_item = _procedure_or_habit_item(matched_procedure) if matched_procedure else None

    rejected_items: list[dict[str, Any]] = []
    for rejected in list(match.get("rejected_candidates") or []):
        if not isinstance(rejected, dict):
            continue
        procedure = procedures_by_id.get(str(rejected.get("procedure_id") or ""))
        item = _procedure_or_habit_item(procedure) if procedure else None
        rejected_items.append(
            {
                "procedure_id": rejected.get("procedure_id"),
                "status": rejected.get("status"),
                "confidence": rejected.get("confidence"),
                "reason": rejected.get("reason"),
                "item": item,
            }
        )

    return {
        "schema_version": LEARNING_MATCH_EXPLANATION_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "task_goal": match.get("task_goal"),
        "matched": bool(match.get("matched")),
        "matched_item": matched_item,
        "match": match,
        "rejected_candidate_items": rejected_items,
        "summary": {
            "matched": bool(match.get("matched")),
            "matched_object_type": matched_item.get("object_type") if matched_item else None,
            "matched_id": matched_item.get("id") if matched_item else None,
            "rejected_candidates": len(rejected_items),
            "requires_confirmation": bool(match.get("required_confirmation", False)),
            "suggestion_mode": match.get("suggestion_mode"),
        },
    }


def _experience_history_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
    events = [
        {
            "source": "experience",
            "event": "recorded",
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "actor": "runtime",
            "summary": record.get("user_goal"),
            "details": {
                "experience_id": record.get("experience_id"),
                "task_id": record.get("task_id"),
                "thread_id": record.get("thread_id"),
            },
        }
    ]
    if validation:
        events.append(
            {
                "source": "experience",
                "event": "validated",
                "status": validation.get("verdict"),
                "created_at": record.get("created_at"),
                "actor": validation.get("method") or "validator",
                "summary": validation.get("notes"),
                "evidence_refs": [str(item) for item in validation.get("evidence_refs", [])],
                "details": {"method": validation.get("method")},
            }
        )
    return events


def _procedure_history_events(procedure: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if procedure.get("created_at"):
        events.append(
            {
                "source": "procedure",
                "event": "created",
                "status": procedure.get("status"),
                "created_at": procedure.get("created_at"),
                "actor": "learning_engine",
                "summary": procedure.get("name"),
                "details": {"procedure_id": procedure.get("procedure_id")},
            }
        )
    for note in list(procedure.get("audit_notes") or []):
        if not isinstance(note, dict):
            continue
        event = {
            "source": "procedure_audit",
            "event": note.get("event") or "audit",
            "status": note.get("to_status") or note.get("status"),
            "created_at": note.get("created_at") or note.get("at"),
            "actor": note.get("reviewer") or note.get("actor") or note.get("by"),
            "summary": note.get("note") or note.get("reason"),
            "details": dict(note),
        }
        events.append(event)
    for review in list(procedure.get("reviews") or []):
        if not isinstance(review, dict):
            continue
        events.append(
            {
                "source": "procedure_review",
                "event": "reviewed",
                "status": review.get("decision"),
                "created_at": review.get("created_at") or review.get("reviewed_at"),
                "actor": review.get("reviewer"),
                "summary": review.get("note"),
                "evidence_refs": [str(item) for item in review.get("evidence_refs", [])],
                "details": dict(review),
            }
        )
    return events


def _skill_history_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for draft_raw in list(payload.get("drafts") or []):
        draft = _as_dict(draft_raw)
        if not draft:
            continue
        events.append(
            {
                "source": "skill_draft",
                "event": "drafted",
                "status": draft.get("lifecycle_status"),
                "version": draft.get("version"),
                "created_at": draft.get("created_at"),
                "actor": draft.get("source_kind"),
                "summary": draft.get("source_ref"),
                "details": draft,
            }
        )
    for report_raw in list(payload.get("validation_reports") or []):
        report = _as_dict(report_raw)
        if not report:
            continue
        events.append(
            {
                "source": "skill_validation",
                "event": "validated",
                "status": "passed" if report.get("passed") else "failed",
                "version": report.get("version"),
                "created_at": report.get("created_at"),
                "actor": "validator",
                "summary": report.get("summary"),
                "details": report,
            }
        )
    for approval_raw in list(payload.get("approval_records") or []):
        approval = _as_dict(approval_raw)
        if not approval:
            continue
        events.append(
            {
                "source": "skill_approval",
                "event": "approved" if approval.get("status") == "approved" else "reviewed",
                "status": approval.get("status"),
                "version": approval.get("version"),
                "created_at": approval.get("created_at"),
                "actor": approval.get("approver"),
                "summary": approval.get("rationale"),
                "details": approval,
            }
        )
    for registration_raw in list(payload.get("registration_records") or []):
        registration = _as_dict(registration_raw)
        if not registration:
            continue
        events.append(
            {
                "source": "skill_registration",
                "event": registration.get("action") or "registered",
                "status": registration.get("status"),
                "version": registration.get("version"),
                "created_at": registration.get("created_at"),
                "actor": "skill_lifecycle",
                "summary": (registration.get("metadata") or {}).get("note"),
                "details": registration,
            }
        )
    for lineage in list(payload.get("lineage_records") or []):
        if not isinstance(lineage, dict):
            continue
        events.append(
            {
                "source": "skill_lineage",
                "event": "lineage_recorded",
                "status": "recorded",
                "version": lineage.get("version"),
                "created_at": lineage.get("promoted_at"),
                "actor": "learning_engine",
                "summary": lineage.get("generation_type"),
                "evidence_refs": [
                    str(item) for item in lineage.get("validator_evidence_refs", [])
                ],
                "details": dict(lineage),
            }
        )
    return events


def _count_event_values(events: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        value = str(event.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    finding_count = 0
    requires_review = 0
    for item in items:
        object_type = str(item.get("object_type") or "unknown")
        status = str(item.get("status") or "unknown")
        by_type[object_type] = by_type.get(object_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        findings = list(item.get("findings") or [])
        finding_count += len(findings)
        if item.get("requires_review"):
            requires_review += 1
    return {
        "total": len(items),
        "experiences": by_type.get("experience", 0),
        "procedures": by_type.get("procedure", 0),
        "habits": by_type.get("habit", 0),
        "skills": by_type.get("skill", 0),
        "requires_review": requires_review,
        "findings": finding_count,
        "by_status": by_status,
    }


def _experience_item(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status") or "unknown")
    validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
    failure = record.get("failure") if isinstance(record.get("failure"), dict) else {}
    sensitive = (
        record.get("sensitive_data_policy")
        if isinstance(record.get("sensitive_data_policy"), dict)
        else {}
    )
    actions = [item for item in list(record.get("actions") or []) if isinstance(item, dict)]
    artifacts = [item for item in list(record.get("artifacts") or []) if isinstance(item, dict)]
    learning_candidates = [
        item for item in list(record.get("learning_candidates") or []) if isinstance(item, dict)
    ]
    validation_verdict = str(validation.get("verdict") or "missing")
    validation_evidence_refs = [str(item) for item in list(validation.get("evidence_refs", []))]
    action_artifact_refs: list[str] = []
    for action in actions:
        action_artifact_refs.extend(str(item) for item in list(action.get("artifact_refs", [])))
    artifact_refs = _dedupe(
        [str(item.get("path")) for item in artifacts if item.get("path")] + action_artifact_refs
    )
    context_refs = [str(item) for item in list(record.get("context_refs", []))]

    findings: list[dict[str, Any]] = []
    next_actions: list[str] = []
    if record.get("schema_version") != "experience-record/v1":
        findings.append(
            _finding("medium", "schema_version_mismatch", "Experience record schema is unexpected.")
        )
        next_actions.append("Inspect the raw experience record before using it for learning.")
    if not record.get("experience_id"):
        findings.append(_finding("high", "missing_experience_id", "Experience has no id."))
        next_actions.append("Do not use this record for learning until it is repaired.")
    if not record.get("task_id"):
        findings.append(_finding("high", "missing_task_id", "Experience has no task id."))
        next_actions.append("Reconnect this experience to its source task before review.")
    if validation_verdict == "missing":
        findings.append(
            _finding("high", "missing_validation", "Experience has no validation verdict.")
        )
        next_actions.append("Run or reconstruct completion validation before learning from it.")
    elif validation_verdict != "passed":
        findings.append(
            _finding(
                "medium",
                "validation_not_passed",
                "Experience did not pass validation and should not be promoted.",
            )
        )
        next_actions.append("Inspect failure attribution and keep this as negative evidence.")
    if status != "succeeded":
        findings.append(
            _finding(
                "medium",
                "non_success_experience",
                "Experience is not a successful reusable learning source.",
            )
        )
        next_actions.append("Review failure evidence before deriving a procedure.")
    if not actions:
        findings.append(_finding("medium", "missing_actions", "Experience has no action trace."))
        next_actions.append("Inspect task artifacts before deriving any procedure.")
    if not validation_evidence_refs:
        findings.append(
            _finding(
                "medium",
                "missing_validation_evidence",
                "Experience validation has no evidence refs.",
            )
        )
        next_actions.append("Attach validation evidence before promotion decisions.")
    if sensitive.get("raw_payload_stored") or sensitive.get("raw_outputs_stored"):
        findings.append(
            _finding(
                "high",
                "raw_sensitive_data_stored",
                "Experience policy indicates raw payloads or outputs were stored.",
            )
        )
        next_actions.append("Review sensitive data policy before exposing this record.")

    if not findings and learning_candidates:
        next_actions.append(
            "Review candidate procedure or failure pattern derived from this experience."
        )

    return {
        "object_type": "experience",
        "id": record.get("experience_id"),
        "name": record.get("user_goal"),
        "status": status,
        "risk_level": "high"
        if sensitive.get("raw_payload_stored") or sensitive.get("raw_outputs_stored")
        else "low",
        "review_status": "needs_review"
        if findings
        else "candidate_ready"
        if learning_candidates
        else "not_required",
        "requires_review": bool(findings),
        "lineage": {
            "task_id": record.get("task_id"),
            "thread_id": record.get("thread_id"),
            "goal_id": record.get("goal_id"),
            "context_refs": context_refs,
            "artifact_refs": artifact_refs,
            "validation_evidence_refs": validation_evidence_refs,
        },
        "health": {
            "validation_verdict": validation_verdict,
            "validation_method": validation.get("method"),
            "action_count": len(actions),
            "artifact_count": len(artifact_refs),
            "learning_candidate_count": len(learning_candidates),
            "has_failure": bool(failure),
            "failure_layer": failure.get("primary_failure_layer"),
            "raw_payload_stored": bool(sensitive.get("raw_payload_stored")),
            "raw_outputs_stored": bool(sensitive.get("raw_outputs_stored")),
        },
        "findings": findings,
        "next_actions": _dedupe(next_actions),
        "updated_at": record.get("created_at"),
    }


def _procedure_item(procedure: dict[str, Any]) -> dict[str, Any]:
    status = str(procedure.get("status") or "draft")
    validator = procedure.get("validator") if isinstance(procedure.get("validator"), dict) else {}
    evidence_refs = [str(item) for item in list(validator.get("required_evidence", []))]
    source_experience_ids = [str(item) for item in list(procedure.get("source_experience_ids", []))]
    source_task_ids = [str(item) for item in list(procedure.get("source_task_ids", []))]
    review_status = str(
        procedure.get("review_status") or ("unreviewed" if status == "draft" else "not_required")
    )
    success_count = int(procedure.get("success_count", 0) or 0)
    failure_count = int(procedure.get("failure_count", 0) or 0)
    findings: list[dict[str, Any]] = []
    next_actions: list[str] = []

    if not source_experience_ids:
        findings.append(
            _finding(
                "high", "missing_source_experience", "Procedure has no source experience lineage."
            )
        )
        next_actions.append(
            "Inspect or recreate this procedure from a validated experience record."
        )
    if not evidence_refs:
        findings.append(
            _finding(
                "high", "missing_validator_evidence", "Procedure has no validator evidence refs."
            )
        )
        next_actions.append("Review the procedure with evidence refs before reuse or promotion.")
    if not list(procedure.get("steps") or []):
        findings.append(_finding("high", "missing_steps", "Procedure has no executable steps."))
        next_actions.append("Edit the procedure steps, then review it again.")
    if review_status == "edited_requires_review":
        findings.append(
            _finding("high", "edited_requires_review", "Behavior-affecting edits require review.")
        )
        next_actions.append("Run procedure review with evidence refs.")
    if review_status == "decayed_requires_review":
        findings.append(
            _finding(
                "medium",
                "decayed_requires_review",
                "Decayed learning object requires review before automatic reuse.",
            )
        )
        next_actions.append("Review the decayed learning object with fresh evidence.")
    if status == "draft" and review_status in {
        "unreviewed",
        "edited",
        "edited_requires_review",
        "decayed_requires_review",
        "needs_changes",
    }:
        findings.append(
            _finding(
                "medium",
                "draft_requires_review",
                "Draft procedure is not eligible for automatic reuse.",
            )
        )
        next_actions.append("Approve, revise, or reject the draft procedure.")
    if failure_count > 0:
        findings.append(
            _finding(
                "medium", "recent_validator_failures", "Procedure has recent validator failures."
            )
        )
        next_actions.append("Inspect failure evidence before reuse or promotion.")

    if status == "disabled":
        review_status = "disabled"
        findings = []
        next_actions = ["Disabled procedures are excluded from retrieval and automatic reuse."]

    return {
        "object_type": "procedure",
        "id": procedure.get("procedure_id"),
        "name": procedure.get("name"),
        "status": status,
        "risk_level": procedure.get("risk_level", "low"),
        "review_status": review_status,
        "requires_review": bool(findings),
        "lineage": {
            "source_experience_ids": source_experience_ids,
            "source_task_ids": source_task_ids,
            "validator_evidence_refs": evidence_refs,
        },
        "health": {
            "confidence": procedure.get("confidence", 0.0),
            "success_count": success_count,
            "failure_count": failure_count,
            "has_source_experience": bool(source_experience_ids),
            "has_validator_evidence": bool(evidence_refs),
            "has_steps": bool(list(procedure.get("steps") or [])),
            "reuse_eligible": status != "disabled",
        },
        "findings": findings,
        "next_actions": _dedupe(next_actions),
        "updated_at": procedure.get("updated_at"),
    }


def _habit_item(procedure: dict[str, Any]) -> dict[str, Any]:
    item = _procedure_item(procedure)
    item["object_type"] = "habit"
    success_count = int(procedure.get("success_count", 0) or 0)
    failure_count = int(procedure.get("failure_count", 0) or 0)
    risk_level = str(procedure.get("risk_level") or "low")
    validator = procedure.get("validator") if isinstance(procedure.get("validator"), dict) else {}
    evidence_refs = [str(item) for item in list(validator.get("required_evidence", []))]

    findings = list(item.get("findings") or [])
    next_actions = list(item.get("next_actions") or [])
    if success_count < 3:
        findings.append(
            _finding(
                "medium",
                "insufficient_habit_successes",
                "Habit has fewer than 3 verified successes.",
            )
        )
        next_actions.append("Keep collecting validation evidence before skill promotion.")
    if failure_count > 0:
        findings.append(
            _finding(
                "medium",
                "habit_has_recent_failures",
                "Habit has recent failures and should be reviewed before reuse.",
            )
        )
        next_actions.append("Review failed runs and downgrade or repair this habit.")
    if not evidence_refs:
        findings.append(
            _finding(
                "high",
                "missing_validator_evidence",
                "Habit has no validator evidence refs.",
            )
        )
        next_actions.append("Attach validator evidence before reuse or promotion.")
    if risk_level in {"medium", "high"}:
        findings.append(
            _finding(
                "medium",
                "habit_requires_confirmation",
                "Medium or high risk habit requires confirmation before automatic reuse.",
            )
        )
        next_actions.append("Keep confirmation gates enabled for this habit.")

    item["risk_level"] = risk_level
    status = str(procedure.get("status") or "draft").lower()
    if status == "disabled":
        findings = []
        next_actions = ["Disabled habits are excluded from retrieval, fast path, and promotion."]
        review_status = "disabled"
        promotion_candidate = False
    else:
        review_status = "needs_review" if findings else "promotion_ready"
        promotion_candidate = bool(
            success_count >= 3
            and failure_count == 0
            and bool(evidence_refs)
            and risk_level == "low"
        )

    item["review_status"] = review_status
    item["requires_review"] = bool(findings)
    item["findings"] = findings
    item["next_actions"] = _dedupe(next_actions)
    item["health"] = {
        **dict(item.get("health") or {}),
        "promotion_candidate": promotion_candidate,
        "requires_confirmation": risk_level in {"medium", "high"},
    }
    return item


def _procedure_or_habit_item(procedure: dict[str, Any] | None) -> dict[str, Any] | None:
    if procedure is None:
        return None
    if _is_habit_object(procedure):
        return _habit_item(procedure)
    return _procedure_item(procedure)


def _skill_item(payload: dict[str, Any]) -> dict[str, Any]:
    entry = _as_dict(payload.get("registry_entry"))
    drafts = [_as_dict(item) for item in list(payload.get("drafts") or [])]
    validations = [_as_dict(item) for item in list(payload.get("validation_reports") or [])]
    approvals = [_as_dict(item) for item in list(payload.get("approval_records") or [])]
    registrations = [_as_dict(item) for item in list(payload.get("registration_records") or [])]
    lineage_records = [
        dict(item) for item in list(payload.get("lineage_records") or []) if isinstance(item, dict)
    ]
    latest_draft = drafts[0] if drafts else {}
    latest_validation = validations[0] if validations else {}
    latest_approval = approvals[0] if approvals else {}
    latest_registration = registrations[0] if registrations else {}
    latest_lineage = lineage_records[0] if lineage_records else {}

    skill_name = str(
        (entry or {}).get("skill_name")
        or latest_draft.get("skill_name")
        or payload.get("skill_name")
        or ""
    )
    status = str(
        (entry or {}).get("lifecycle_status") or latest_draft.get("lifecycle_status") or "draft"
    )
    version = str((entry or {}).get("version") or latest_draft.get("version") or "")
    source = str((entry or {}).get("source") or latest_draft.get("source_kind") or "unknown")
    contracts = list((entry or {}).get("contracts") or [])
    source_kind = str(latest_draft.get("source_kind") or source)
    source_risk = str(latest_lineage.get("risk_level") or "unknown")
    validator_evidence_refs = [
        str(item) for item in list(latest_lineage.get("validator_evidence_refs", []))
    ]
    source_experience_ids = [
        str(item) for item in list(latest_lineage.get("source_experience_ids", []))
    ]
    approval_status = str(latest_approval.get("status") or "missing")
    validation_passed = latest_validation.get("passed")

    findings: list[dict[str, Any]] = []
    next_actions: list[str] = []
    habit_promoted = (
        source in {"habit_promotion", "habit_promoted"} or source_kind == "habit_promoted"
    )
    if habit_promoted and not lineage_records:
        findings.append(
            _finding("high", "missing_lineage", "Habit-promoted skill has no lineage record.")
        )
        next_actions.append("Inspect draft metadata and lineage.json before use.")
    if habit_promoted and not validator_evidence_refs:
        findings.append(
            _finding(
                "high",
                "missing_validator_evidence",
                "Habit-promoted skill has no validator evidence refs.",
            )
        )
        next_actions.append("Re-promote from a habit with validator evidence.")
    if status in {"draft", "validated"}:
        findings.append(_finding("medium", "skill_not_active", "Skill is not active."))
        next_actions.append("Validate, approve, or activate the skill as appropriate.")
    if validation_passed is False:
        findings.append(_finding("high", "validation_failed", "Latest skill validation failed."))
        next_actions.append("Fix the generated package and validate a new version.")
    if status != "active" and not validations:
        findings.append(_finding("medium", "missing_validation", "Skill has no validation report."))
        next_actions.append("Run skill validate before approval or activation.")
    if status == "validated" and approval_status not in {"approved", "not_required"}:
        findings.append(
            _finding("medium", "awaiting_approval", "Validated skill is waiting for approval.")
        )
        next_actions.append("Review approval policy and run skill approve if acceptable.")
    if not contracts:
        findings.append(_finding("medium", "missing_contracts", "Skill has no action contracts."))
        next_actions.append("Add action contracts before relying on automated selection.")

    return {
        "object_type": "skill",
        "id": skill_name,
        "name": skill_name,
        "status": status,
        "version": version,
        "source": source,
        "source_kind": source_kind,
        "risk_level": source_risk,
        "review_status": approval_status,
        "requires_review": bool(findings),
        "lineage": {
            "procedure_id": latest_lineage.get("procedure_id"),
            "habit_id": latest_lineage.get("habit_id"),
            "generation_type": latest_lineage.get("generation_type"),
            "source_experience_ids": source_experience_ids,
            "validator_evidence_refs": validator_evidence_refs,
        },
        "health": {
            "validation_passed": validation_passed,
            "approval_status": approval_status,
            "registration_status": latest_registration.get("status"),
            "contract_count": len(contracts),
            "lineage_present": bool(lineage_records),
        },
        "findings": findings,
        "next_actions": _dedupe(next_actions),
        "updated_at": (entry or latest_draft).get("last_seen_at") or latest_draft.get("updated_at"),
    }


def _finding(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _is_habit_object(procedure: dict[str, Any]) -> bool:
    status = str(procedure.get("status") or "").lower()
    if status == "habit":
        return True
    if status != "disabled":
        return False
    for note in reversed(list(procedure.get("audit_notes") or [])):
        if not isinstance(note, dict):
            continue
        if note.get("event") == "disabled" and note.get("previous_status") == "habit":
            return True
        if (
            note.get("event") == "reviewed"
            and note.get("decision") == "rejected"
            and note.get("from_status") == "habit"
        ):
            return True
    return False


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return dict(value)
    return {}


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))

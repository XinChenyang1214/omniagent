from __future__ import annotations

import json
from typing import Any

from omniagent.models import utc_now


LEARNING_REVIEW_SCHEMA_VERSION = "learning-review/v1"
LEARNING_HISTORY_SCHEMA_VERSION = "learning-history/v1"
LEARNING_MATCH_EXPLANATION_SCHEMA_VERSION = "learning-match-explanation/v1"
LEARNING_FEEDBACK_SCHEMA_VERSION = "learning-feedback/v1"


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


def build_task_learning_feedback(
    *,
    task_id: str,
    task_goal: str,
    experience: dict[str, Any] | None = None,
    procedure_match: dict[str, Any] | None = None,
    habit_fast_path: dict[str, Any] | None = None,
    procedures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build user-facing feedback from task learning artifacts.

    This is a derived view: it does not change learning state. The intent is to
    make the existing experience/procedure/habit loop visible at task end.
    """
    experience = dict(experience or {})
    procedure_match = dict(procedure_match or {})
    habit_fast_path = dict(habit_fast_path or {})
    procedures = list(procedures or [])
    procedures_by_id = {
        str(procedure.get("procedure_id") or ""): procedure
        for procedure in procedures
        if procedure.get("procedure_id")
    }

    validation = experience.get("validation") if isinstance(experience.get("validation"), dict) else {}
    validation_verdict = str(validation.get("verdict") or "missing")
    evidence_refs = [str(item) for item in list(validation.get("evidence_refs") or [])]
    learning_candidates = [
        item for item in list(experience.get("learning_candidates") or []) if isinstance(item, dict)
    ]
    learnable = (
        bool(experience)
        and experience.get("status") == "succeeded"
        and validation_verdict == "passed"
        and bool(evidence_refs)
    )

    matched_id = str(procedure_match.get("matched_procedure_id") or "")
    matched_procedure = procedures_by_id.get(matched_id)
    generated_procedures = [
        procedure
        for procedure in procedures
        if task_id in {str(item) for item in list(procedure.get("source_task_ids") or [])}
        and str(procedure.get("procedure_id") or "") != matched_id
    ]
    generated_procedure = generated_procedures[0] if generated_procedures else None

    matched_item = _procedure_or_habit_item(matched_procedure)
    generated_item = _procedure_or_habit_item(generated_procedure)

    reuse = _build_reuse_feedback(
        learnable=learnable,
        validation_verdict=validation_verdict,
        procedure_match=procedure_match,
        habit_fast_path=habit_fast_path,
        matched_procedure=matched_procedure,
        generated_procedure=generated_procedure,
    )
    review = _build_review_feedback(
        task_goal=task_goal,
        experience_id=str(experience.get("experience_id") or ""),
        generated_procedure=generated_procedure,
        matched_procedure=matched_procedure,
        learnable=learnable,
    )
    summary_lines = _learning_feedback_summary_lines(
        experience=experience,
        learnable=learnable,
        validation_verdict=validation_verdict,
        procedure_match=procedure_match,
        habit_fast_path=habit_fast_path,
        matched_procedure=matched_procedure,
        generated_procedure=generated_procedure,
        reuse=reuse,
        review=review,
    )

    return {
        "schema_version": LEARNING_FEEDBACK_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "task_id": task_id,
        "task_goal": task_goal,
        "experience": {
            "record_written": bool(experience),
            "experience_id": experience.get("experience_id"),
            "status": experience.get("status"),
            "validation_verdict": validation_verdict,
            "validation_method": validation.get("method"),
            "evidence_refs": evidence_refs,
            "learnable": learnable,
            "learning_candidate_count": len(learning_candidates),
            "reason": _experience_feedback_reason(
                has_experience=bool(experience),
                learnable=learnable,
                validation_verdict=validation_verdict,
                evidence_refs=evidence_refs,
            ),
        },
        "procedure": {
            "matched": bool(procedure_match.get("matched")),
            "matched_id": matched_id or None,
            "matched_status": matched_procedure.get("status") if matched_procedure else None,
            "matched_object_type": matched_item.get("object_type") if matched_item else None,
            "suggestion_mode": procedure_match.get("suggestion_mode"),
            "match_reason": procedure_match.get("reason"),
            "required_confirmation": bool(procedure_match.get("required_confirmation", False)),
            "generated_draft_id": generated_procedure.get("procedure_id")
            if generated_procedure
            else None,
            "generated_status": generated_procedure.get("status") if generated_procedure else None,
            "generated_review_status": generated_item.get("review_status")
            if generated_item
            else None,
            "success_count": matched_procedure.get("success_count")
            if matched_procedure
            else generated_procedure.get("success_count")
            if generated_procedure
            else None,
        },
        "habit_fast_path": {
            "decision": habit_fast_path.get("decision"),
            "habit_id": habit_fast_path.get("habit_id"),
            "skipped_stages": list(habit_fast_path.get("skipped_stages") or []),
            "policy_gate": habit_fast_path.get("policy_gate") or {},
            "reason": habit_fast_path.get("reason"),
        },
        "reuse": reuse,
        "review": review,
        "summary_lines": summary_lines,
    }


def format_learning_feedback(feedback: dict[str, Any] | None) -> str:
    if not feedback:
        return ""
    lines = [str(item) for item in list(feedback.get("summary_lines") or []) if str(item)]
    review = feedback.get("review") if isinstance(feedback.get("review"), dict) else {}
    commands = [str(item) for item in list(review.get("commands") or []) if str(item)]
    if not lines and not commands:
        return ""
    output = ["学习反馈:"]
    output.extend(f"- {line}" for line in lines)
    if commands:
        output.append("- 你可以执行:")
        output.extend(f"  - {command}" for command in commands[:5])
    return "\n".join(output)


def _build_reuse_feedback(
    *,
    learnable: bool,
    validation_verdict: str,
    procedure_match: dict[str, Any],
    habit_fast_path: dict[str, Any],
    matched_procedure: dict[str, Any] | None,
    generated_procedure: dict[str, Any] | None,
) -> dict[str, Any]:
    policy_gate = (
        habit_fast_path.get("policy_gate") if isinstance(habit_fast_path.get("policy_gate"), dict) else {}
    )
    if habit_fast_path.get("decision") == "habit_fast_path_candidate":
        accepted = policy_gate.get("result") == "accepted"
        return {
            "status": "habit_fast_path" if accepted else "habit_candidate_blocked",
            "reusable_now": bool(accepted),
            "why": "命中低风险 habit，且 policy gate 已接受 fast path。"
            if accepted
            else "命中 habit，但 policy gate 未接受 fast path。",
            "next_time": "下一次类似任务可继续复用该 habit，并在权限、contract 和风险 gate 后跳过 Planner。"
            if accepted
            else "下一次类似任务仍会保留 Planner 或确认路径，直到 gate 满足。",
        }

    if matched_procedure is not None:
        status = str(matched_procedure.get("status") or "draft")
        success_count = int(matched_procedure.get("success_count", 0) or 0)
        if status == "verified":
            remaining = max(0, 3 - success_count)
            return {
                "status": "verified_procedure_reuse",
                "reusable_now": True,
                "why": "命中已验证 procedure，本次验证成功会回写到原 procedure。"
                if learnable
                else "命中已验证 procedure，但本次没有 passed validation，不能作为正向升级证据。",
                "next_time": "下一次类似任务可继续优先使用该 procedure；再积累 "
                f"{remaining} 次成功后可升级为 habit。"
                if remaining
                else "该 procedure 已满足 habit 成功次数门槛，可进入 habit promotion。",
            }
        if status == "habit":
            return {
                "status": "habit_requires_planner_or_confirmation",
                "reusable_now": False,
                "why": "命中 habit，但当前风险、确认或 policy gate 未允许 fast path。",
                "next_time": "下一次类似任务会继续解释命中原因，并在满足 gate 后进入 fast path。",
            }
        return {
            "status": "draft_suggestion",
            "reusable_now": False,
            "why": "只命中 draft procedure；draft 只能建议，不能自动复用。",
            "next_time": "用户 review 并提供 evidence 后，类似任务才能优先复用该 procedure。",
        }

    if generated_procedure is not None:
        return {
            "status": "new_draft_needs_review",
            "reusable_now": False,
            "why": "本次任务生成了新的 procedure draft，但 draft 需要用户 review 后才能复用。",
            "next_time": "审核通过后，下一次类似任务可命中 verified procedure；累计 3 次验证成功后可升级 habit。",
        }

    if not learnable:
        return {
            "status": "not_learnable",
            "reusable_now": False,
            "why": "本次任务没有可升级的 passed validation evidence。"
            if validation_verdict != "passed"
            else "本次任务缺少可用于升级的 evidence refs。",
            "next_time": "下一次需要通过 completion / validator 并保留 evidence 后，才会形成可复用学习结果。",
        }

    if not procedure_match.get("matched"):
        return {
            "status": "learnable_no_procedure",
            "reusable_now": False,
            "why": "本次任务有可学习经验，但尚未形成可复用 procedure。",
            "next_time": "学习引擎需要从该 experience 提炼 draft，并经 review 后才能复用。",
        }
    return {
        "status": "unknown",
        "reusable_now": False,
        "why": "学习状态无法从现有 artifacts 完整判断。",
        "next_time": "请查看 learning show/history/explain-match 进一步确认。",
    }


def _build_review_feedback(
    *,
    task_goal: str,
    experience_id: str,
    generated_procedure: dict[str, Any] | None,
    matched_procedure: dict[str, Any] | None,
    learnable: bool,
) -> dict[str, Any]:
    commands: list[str] = []
    if experience_id:
        commands.append(f"omniagent learning show --type experience {experience_id}")

    target = generated_procedure or matched_procedure
    procedure_id = str((target or {}).get("procedure_id") or "")
    target_status = str((target or {}).get("status") or "")
    required = False
    reason = "无需立即处理；可以按需查看本次学习记录。"
    if generated_procedure is not None:
        required = True
        reason = "新 procedure draft 需要用户审核；审核前不会自动复用。"
        evidence_ref = _first_validator_evidence(generated_procedure)
        if evidence_ref:
            commands.append(
                "omniagent procedure review "
                f"{procedure_id} --decision approved --evidence-ref {evidence_ref}"
            )
    elif target_status == "draft":
        required = True
        reason = "命中的是 draft procedure，需要修正或审核后才能复用。"
    elif not learnable:
        reason = "本次没有 passed validation evidence，不会升级学习结果。"
    elif target_status in {"verified", "habit"}:
        reason = "已命中可治理的学习对象；如果结果不合适，可以修正或禁用。"

    if procedure_id:
        commands.extend(
            [
                f"omniagent learning show --type {'habit' if target_status == 'habit' else 'procedure'} {procedure_id}",
                f"omniagent learning history --type {'habit' if target_status == 'habit' else 'procedure'} {procedure_id}",
                f"omniagent learning explain-match {json.dumps(task_goal, ensure_ascii=False)}",
                f"omniagent learning edit --type {'habit' if target_status == 'habit' else 'procedure'} {procedure_id} --patch '{{...}}'",
                f"omniagent learning disable --type {'habit' if target_status == 'habit' else 'procedure'} {procedure_id}",
            ]
        )
    else:
        commands.append(f"omniagent learning explain-match {json.dumps(task_goal, ensure_ascii=False)}")

    return {
        "required": required,
        "reason": reason,
        "target_type": "habit"
        if target_status == "habit"
        else "procedure"
        if procedure_id
        else "experience",
        "target_id": procedure_id or experience_id or None,
        "commands": _dedupe(commands),
    }


def _learning_feedback_summary_lines(
    *,
    experience: dict[str, Any],
    learnable: bool,
    validation_verdict: str,
    procedure_match: dict[str, Any],
    habit_fast_path: dict[str, Any],
    matched_procedure: dict[str, Any] | None,
    generated_procedure: dict[str, Any] | None,
    reuse: dict[str, Any],
    review: dict[str, Any],
) -> list[str]:
    lines = [
        "经验记录："
        + (
            f"已写入 {experience.get('experience_id')}，validation={validation_verdict}，"
            + ("可作为学习输入。" if learnable else "不会用于自动升级。")
            if experience
            else "未找到 experience record。"
        )
    ]
    if generated_procedure is not None:
        lines.append(
            "Procedure：本次未命中可复用 procedure，已生成 draft "
            f"{generated_procedure.get('procedure_id')}，需要 review。"
        )
    elif matched_procedure is not None:
        status = str(matched_procedure.get("status") or "draft")
        object_type = "habit" if status == "habit" else "procedure"
        lines.append(
            f"Procedure：命中 {object_type} {matched_procedure.get('procedure_id')}，"
            f"mode={procedure_match.get('suggestion_mode')}，原因：{procedure_match.get('reason') or '未记录'}。"
        )
    else:
        lines.append(
            "Procedure：未命中已有 procedure / habit。"
            if not procedure_match.get("matched")
            else "Procedure：命中记录不完整，请查看 procedure_match artifact。"
        )

    if habit_fast_path.get("decision") == "habit_fast_path_candidate":
        gate = habit_fast_path.get("policy_gate") if isinstance(habit_fast_path.get("policy_gate"), dict) else {}
        lines.append(
            "复用判断：habit fast path "
            f"{gate.get('result') or 'unknown'}，skipped_stages="
            f"{','.join(str(item) for item in habit_fast_path.get('skipped_stages', [])) or 'none'}。"
        )
    else:
        lines.append(f"复用判断：{reuse.get('why')} {reuse.get('next_time')}")
    lines.append(f"需要你做：{review.get('reason')}")
    return lines


def _experience_feedback_reason(
    *,
    has_experience: bool,
    learnable: bool,
    validation_verdict: str,
    evidence_refs: list[str],
) -> str:
    if not has_experience:
        return "task did not produce an experience record"
    if learnable:
        return "succeeded with passed validation evidence"
    if validation_verdict != "passed":
        return f"validation verdict is {validation_verdict}"
    if not evidence_refs:
        return "validation evidence refs are missing"
    return "experience is not eligible for learning promotion"


def _first_validator_evidence(procedure: dict[str, Any]) -> str:
    validator = procedure.get("validator") if isinstance(procedure.get("validator"), dict) else {}
    evidence_refs = [str(item) for item in list(validator.get("required_evidence") or [])]
    return evidence_refs[0] if evidence_refs else ""


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

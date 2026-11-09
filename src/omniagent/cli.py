from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, TextIO

from omniagent.config import AppConfig
from omniagent.errors import OmniAgentError
from omniagent.events import TaskEvent
from omniagent.gateway import RuntimeGateway
from omniagent.learning_governance import format_learning_feedback
from omniagent.logging_utils import configure_logging
from omniagent.models import (
    AutonomyMode,
    ConversationThreadStatus,
    PermissionLevel,
    TaskConstraints,
    TaskRequest,
    TaskSource,
    TaskStatus,
)
from omniagent.skill_auto_generation import SUPPORTED_GENERATION_TYPES


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw)


def _to_builtin(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_builtin(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_to_builtin(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_builtin(item) for key, item in value.items()}
    return value


def _annotate_llm_check_failure(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("healthy"):
        return payload
    error = payload.get("error")
    if not isinstance(error, dict):
        return payload
    message = str(error.get("message") or "").lower()
    provider = str(payload.get("provider") or "").strip() or "current provider"
    model = str(payload.get("model") or "").strip()
    auth_markers = (
        "401",
        "unauthorized",
        "invalid api key",
        "incorrect api key",
        "authentication",
        "auth",
    )
    if any(marker in message for marker in auth_markers):
        payload["diagnosis"] = "llm_authentication_failed"
        if provider.lower() == "openrouter":
            key_name = "OPENROUTER_API_KEY (or OMNIAGENT_LLM_API_KEY)"
        elif provider.lower() == "opencode":
            key_name = "OPENCODE_API_KEY (or OMNIAGENT_LLM_API_KEY)"
        else:
            key_name = "OMNIAGENT_LLM_API_KEY"
        next_actions = [
            f"Check {key_name} for {provider}; do not write the key into files.",
            "Confirm OMNIAGENT_LLM_API_BASE or api_base points to the selected provider.",
        ]
        if model:
            next_actions.append(f"Confirm model {model} is available for this API key.")
        payload["next_actions"] = next_actions
    return payload


def _format_event_line(event: TaskEvent) -> str:
    payload = event.payload or {}
    if event.event_type == "task.intake_assessed":
        tools = payload.get("tools") or []
        tools_text = ", ".join(str(item) for item in tools) if tools else "none"
        return (
            "任务预判: "
            f"type={payload.get('task_type')} | "
            f"risk={payload.get('risk_level')} | "
            f"tools={tools_text} | "
            f"validation={payload.get('validation_strategy')} | "
            f"confirmation_required={payload.get('confirmation_required')}"
        )
    segments = [f"event: {event.event_type}", f"status={event.status}"]
    node_id = payload.get("node_id")
    if node_id:
        segments.append(f"node={node_id}")
    message = payload.get("message") or payload.get("summary") or payload.get("verdict")
    if message:
        segments.append(f"message={message}")
    return " | ".join(segments)


def _build_event_printer(stream: TextIO):
    def _printer(event: TaskEvent) -> None:
        print(_format_event_line(event), file=stream, flush=True)

    return _printer


def _print_conversation_response(response: Any, stream: TextIO) -> int:
    print(f"thread_id: {response.thread.thread_id}", file=stream)
    if response.task_report is not None:
        print(f"task_id: {response.task_report.task.task_id}", file=stream)
    if response.checkpoint is not None:
        print(f"checkpoint_id: {response.checkpoint.checkpoint_id}", file=stream)
    print("", file=stream)
    print(response.assistant_message, file=stream)
    if response.task_report is not None:
        return (
            0
            if (
                response.task_report.result.success
                or response.task_report.task.status
                in {TaskStatus.WAITING_INPUT, TaskStatus.SUSPENDED}
            )
            else 1
        )
    return 0


def _print_thread_payload(payload: dict[str, Any], stream: TextIO) -> None:
    thread = payload["thread"]
    print(f"thread_id: {thread.thread_id}", file=stream)
    print(f"status: {thread.status}", file=stream)
    print(f"active_task_id: {thread.active_task_id or 'none'}", file=stream)
    print(f"pending_checkpoint_id: {thread.pending_checkpoint_id or 'none'}", file=stream)
    print("turns:", file=stream)
    for turn in payload["turns"]:
        print(f"- {turn.role}: {turn.message}", file=stream)
    print("events:", file=stream)
    for event in payload["events"]:
        print(f"- {event['event_type']}: {event['payload']}", file=stream)


def _print_agent_state_payload(payload: dict[str, Any], stream: TextIO) -> None:
    if {"profile", "preferences", "project", "goal"}.issubset(payload):
        for section in ("profile", "preferences", "project", "goal"):
            print(f"{section}:", file=stream)
            print(json.dumps(payload[section], ensure_ascii=False, indent=2), file=stream)
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
            print(f"{key}: {rendered}", file=stream)
        else:
            print(f"{key}: {value}", file=stream)


def _print_procedure_payload(payload: dict[str, Any], stream: TextIO) -> None:
    for key in (
        "procedure_id",
        "name",
        "status",
        "risk_level",
        "confidence",
        "success_count",
        "failure_count",
        "review_status",
        "last_reviewed_at",
        "reviewed_by",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=stream)
    if payload.get("trigger"):
        print(f"trigger: {json.dumps(payload['trigger'], ensure_ascii=False)}", file=stream)
    if payload.get("source_experience_ids"):
        print(
            "source_experience_ids: " + ", ".join(payload["source_experience_ids"]),
            file=stream,
        )
    if payload.get("steps"):
        print("steps:", file=stream)
        for step in payload["steps"]:
            print(
                f"- {step.get('step_id')} {step.get('action_type')} {step.get('target')}",
                file=stream,
            )
    if payload.get("reviews"):
        print("reviews:", file=stream)
        for review in payload["reviews"][-3:]:
            print(
                f"- {review.get('decision')} by {review.get('reviewer')} "
                f"at {review.get('created_at')}",
                file=stream,
            )


def _print_procedure_match(payload: dict[str, Any], stream: TextIO) -> None:
    print(f"matched: {payload.get('matched')}", file=stream)
    print(f"matched_procedure_id: {payload.get('matched_procedure_id') or 'none'}", file=stream)
    print(f"confidence: {payload.get('confidence')}", file=stream)
    print(f"reason: {payload.get('reason')}", file=stream)
    print(f"suggestion_mode: {payload.get('suggestion_mode')}", file=stream)
    print(f"required_confirmation: {payload.get('required_confirmation')}", file=stream)
    rejected = payload.get("rejected_candidates") or []
    if rejected:
        print("rejected_candidates:", file=stream)
        for item in rejected:
            print(
                f"- {item.get('procedure_id')}: {item.get('reason')} "
                f"confidence={item.get('confidence', '-')}",
                file=stream,
            )


def _print_learning_review(payload: dict[str, Any], stream: TextIO) -> None:
    summary = payload.get("summary") or {}
    print(f"schema_version: {payload.get('schema_version')}", file=stream)
    print(f"total: {summary.get('total', 0)}", file=stream)
    print(
        f"experiences: {summary.get('experiences', 0)} "
        f"procedures: {summary.get('procedures', 0)} "
        f"habits: {summary.get('habits', 0)} "
        f"skills: {summary.get('skills', 0)}",
        file=stream,
    )
    print(
        f"requires_review: {summary.get('requires_review', 0)} findings: {summary.get('findings', 0)}",
        file=stream,
    )
    for item in payload.get("items", []):
        findings = item.get("findings") or []
        print(
            f"- {item.get('object_type')}:{item.get('id')} "
            f"status={item.get('status')} review={item.get('review_status')} "
            f"risk={item.get('risk_level')} findings={len(findings)}",
            file=stream,
        )
        if findings:
            first = findings[0]
            print(
                f"  first_finding={first.get('severity')}:{first.get('code')}",
                file=stream,
            )


def _print_learning_match(payload: dict[str, Any], stream: TextIO) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    matched_item = (
        payload.get("matched_item") if isinstance(payload.get("matched_item"), dict) else {}
    )
    print(f"schema_version: {payload.get('schema_version')}", file=stream)
    print(f"matched: {summary.get('matched')}", file=stream)
    print(f"matched_object_type: {summary.get('matched_object_type') or 'none'}", file=stream)
    print(f"matched_id: {summary.get('matched_id') or 'none'}", file=stream)
    print(f"confidence: {match.get('confidence')}", file=stream)
    print(f"suggestion_mode: {summary.get('suggestion_mode')}", file=stream)
    print(f"requires_confirmation: {summary.get('requires_confirmation')}", file=stream)
    if matched_item:
        print(
            f"matched_review: status={matched_item.get('status')} "
            f"review={matched_item.get('review_status')} "
            f"risk={matched_item.get('risk_level')} "
            f"findings={len(matched_item.get('findings') or [])}",
            file=stream,
        )
    rejected = payload.get("rejected_candidate_items") or []
    if rejected:
        print("rejected_candidates:", file=stream)
        for item in rejected:
            print(
                f"- {item.get('procedure_id')}: {item.get('reason')} "
                f"status={item.get('status', '-')} "
                f"confidence={item.get('confidence', '-')}",
                file=stream,
            )


def _print_learning_object(payload: dict[str, Any], stream: TextIO) -> None:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    findings = [entry for entry in list(item.get("findings") or []) if isinstance(entry, dict)]
    next_actions = [str(entry) for entry in list(item.get("next_actions") or [])]
    print(f"schema_version: {payload.get('schema_version')}", file=stream)
    if payload.get("operation"):
        print(f"operation: {payload.get('operation')}", file=stream)
    print(f"object_type: {payload.get('object_type')}", file=stream)
    print(f"id: {item.get('id') or payload.get('id')}", file=stream)
    if payload.get("previous_status"):
        print(f"previous_status: {payload.get('previous_status')}", file=stream)
    print(f"name: {item.get('name')}", file=stream)
    print(f"status: {item.get('status')}", file=stream)
    print(f"review_status: {item.get('review_status')}", file=stream)
    print(f"risk_level: {item.get('risk_level')}", file=stream)
    print(f"requires_review: {item.get('requires_review')}", file=stream)
    if findings:
        print("findings:", file=stream)
        for finding in findings:
            print(
                f"- {finding.get('severity')} {finding.get('code')}: {finding.get('message')}",
                file=stream,
            )
    if next_actions:
        print("next_actions:", file=stream)
        for action in next_actions:
            print(f"- {action}", file=stream)


def _print_learning_history(payload: dict[str, Any], stream: TextIO) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    print(f"schema_version: {payload.get('schema_version')}", file=stream)
    print(f"object_type: {payload.get('object_type')}", file=stream)
    print(f"id: {payload.get('id')}", file=stream)
    print(f"events: {summary.get('events', 0)}", file=stream)
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        print(
            f"- {event.get('created_at') or '-'} "
            f"{event.get('source')}:{event.get('event')} "
            f"status={event.get('status') or '-'} "
            f"version={event.get('version') or '-'}",
            file=stream,
        )


def _print_repl_help(stream: TextIO) -> None:
    print(
        "repl_commands: /help /status /thread /show /switch <thread_id> /resume <thread_id>"
        " /state [profile|preferences|project|goal] /allow [delete|network]"
        " /deny [delete|network] /exit",
        file=stream,
    )


# ISSUE-006: REPL runtime constraint toggles. Both ``delete`` (filesystem.delete)
# and ``network`` have CLI flags at launch time; these commands let the user
# flip them for subsequent turns in the same REPL session without restarting.
_REPL_CONSTRAINT_KEYS = {"delete": "allow_delete", "network": "allow_network"}


def _print_repl_constraints(args: argparse.Namespace, stream: TextIO) -> None:
    print(
        "constraints: "
        f"allow_delete={bool(args.allow_delete)} allow_network={bool(args.allow_network)}",
        file=stream,
    )


def _apply_repl_constraint(
    message: str, args: argparse.Namespace, stream: TextIO, *, enable: bool
) -> bool:
    parts = message.split()
    if len(parts) < 2:
        _print_repl_constraints(args, stream)
        return True
    target = parts[1].strip().lower()
    attr = _REPL_CONSTRAINT_KEYS.get(target)
    if attr is None:
        print(
            f"unknown constraint: {target} (expected: delete | network)",
            file=stream,
        )
        return False
    setattr(args, attr, bool(enable))
    verb = "allow" if enable else "deny"
    print(f"{verb}_{target}: {enable}", file=stream)
    return True


def _run_chat_repl(gateway: RuntimeGateway, args: argparse.Namespace, stream: TextIO) -> int:
    request_options = _build_chat_request_options(
        args,
        default_timeout_seconds=gateway.config.gateway.task_timeout_seconds,
    )
    if args.thread_id:
        payload = gateway.show_thread(args.thread_id)
        if not payload:
            print(f"thread not found: {args.thread_id}", file=sys.stderr)
            return 1
        current_thread_id = args.thread_id
        print(f"thread_id: {current_thread_id}", file=stream)
        print(f"active_task_id: {payload['thread'].active_task_id or 'none'}", file=stream)
        print(
            f"pending_checkpoint_id: {payload['thread'].pending_checkpoint_id or 'none'}",
            file=stream,
        )
    else:
        thread = gateway.create_thread(title="CLI REPL")
        current_thread_id = thread.thread_id
        print(f"thread_id: {current_thread_id}", file=stream)
    _print_repl_help(stream)
    _print_repl_constraints(args, stream)

    while True:
        try:
            raw = input("chat> ")
        except EOFError:
            print(f"session_closed: {current_thread_id}", file=stream)
            return 0
        message = raw.strip()
        if not message:
            continue
        if message in {"/exit", "/quit"}:
            print(f"session_closed: {current_thread_id}", file=stream)
            return 0
        if message in {"/help", "help"}:
            _print_repl_help(stream)
            _print_repl_constraints(args, stream)
            continue
        if message == "/thread":
            print(f"thread_id: {current_thread_id}", file=stream)
            continue
        if message == "/show":
            payload = gateway.show_thread(current_thread_id)
            if not payload:
                print(f"thread not found: {current_thread_id}", file=sys.stderr)
                return 1
            _print_thread_payload(payload, stream)
            continue
        if message == "/state" or message.startswith("/state "):
            parts = message.split(maxsplit=1)
            section = parts[1].strip() if len(parts) > 1 else None
            try:
                payload = gateway.show_agent_state(section=section)
            except ValueError as exc:
                print(f"state error: {exc}", file=stream)
                continue
            _print_agent_state_payload(payload, stream)
            continue
        if message == "/allow" or message.startswith("/allow "):
            if _apply_repl_constraint(message, args, stream, enable=True):
                request_options = _build_chat_request_options(
                    args,
                    default_timeout_seconds=gateway.config.gateway.task_timeout_seconds,
                )
            continue
        if message == "/deny" or message.startswith("/deny "):
            if _apply_repl_constraint(message, args, stream, enable=False):
                request_options = _build_chat_request_options(
                    args,
                    default_timeout_seconds=gateway.config.gateway.task_timeout_seconds,
                )
            continue
        if message.startswith("/switch ") or message.startswith("/resume "):
            _, target = message.split(" ", 1)
            target = target.strip()
            payload = gateway.show_thread(target)
            if not payload:
                print(f"thread not found: {target}", file=stream)
                continue
            current_thread_id = target
            print(f"thread_id: {current_thread_id}", file=stream)
            print(f"active_task_id: {payload['thread'].active_task_id or 'none'}", file=stream)
            print(
                f"pending_checkpoint_id: {payload['thread'].pending_checkpoint_id or 'none'}",
                file=stream,
            )
            continue

        outbound = "当前状态如何" if message == "/status" else message
        try:
            response = gateway.send_message(
                outbound,
                thread_id=current_thread_id,
                request_options=request_options,
                event_handlers=[_build_event_printer(stream)],
            )
        except OmniAgentError as exc:
            # 单次对话失败不应当中止整个 REPL
            print(f"\n[error] {exc}", file=stream)
            print("（会话仍在继续，可以重新输入或换一种说法）\n", file=stream)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"\n[unexpected error] {type(exc).__name__}: {exc}", file=stream)
            print("（会话仍在继续，可以重新输入或换一种说法）\n", file=stream)
            continue
        current_thread_id = response.thread.thread_id
        _print_conversation_response(response, stream)


def _build_task_constraints(
    args: argparse.Namespace,
    *,
    default_timeout_seconds: int = 30,
) -> TaskConstraints:
    timeout_seconds = args.timeout if args.timeout is not None else default_timeout_seconds
    return TaskConstraints(
        allow_network=bool(args.allow_network),
        allow_delete=bool(args.allow_delete),
        timeout_seconds=timeout_seconds,
        max_permission=PermissionLevel(args.max_permission),
    )


def _build_task_request(
    args: argparse.Namespace,
    *,
    default_timeout_seconds: int = 30,
) -> TaskRequest:
    return TaskRequest(
        goal=args.goal,
        expected_output=args.expected_output,
        payload=_parse_payload(args.payload),
        executor=args.executor,
        skill_name=args.skill_name,
        mode=AutonomyMode(args.mode),
        source=TaskSource(args.source),
        context_refs=list(args.context_ref),
        preferred_skills=list(args.preferred_skill),
        constraints=_build_task_constraints(
            args,
            default_timeout_seconds=default_timeout_seconds,
        ),
    )


def _build_chat_request_options(
    args: argparse.Namespace,
    *,
    default_timeout_seconds: int = 30,
) -> dict[str, Any]:
    return {
        "expected_output": args.expected_output,
        "payload": _parse_payload(args.payload),
        "executor": args.executor,
        "skill_name": args.skill_name,
        "mode": AutonomyMode(args.mode),
        "preferred_skills": list(args.preferred_skill),
        "constraints": _build_task_constraints(
            args,
            default_timeout_seconds=default_timeout_seconds,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omniagent")
    parser.add_argument("--workspace", default="workspace", help="workspace 根目录")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="提交并执行任务")
    run_parser.add_argument("goal", help="任务目标")
    run_parser.add_argument("--expected-output")
    run_parser.add_argument(
        "--executor", default="auto", choices=["auto", "file", "python", "browser"]
    )
    run_parser.add_argument("--skill", dest="skill_name")
    run_parser.add_argument("--preferred-skill", action="append", default=[])
    run_parser.add_argument("--payload", help="JSON 格式执行输入")
    run_parser.add_argument(
        "--mode", default="semi_auto", choices=["assist", "semi_auto", "full_auto"]
    )
    run_parser.add_argument("--source", default="cli", choices=["cli", "api", "scheduler", "event"])
    run_parser.add_argument(
        "--max-permission", default="L2", choices=["L1", "L2", "L3", "L4", "L5"]
    )
    run_parser.add_argument("--allow-network", action="store_true")
    run_parser.add_argument("--allow-delete", action="store_true")
    run_parser.add_argument("--timeout", type=int)
    run_parser.add_argument("--context-ref", action="append", default=[])
    run_parser.add_argument("--json", action="store_true", help="JSON 输出")

    chat_parser = subparsers.add_parser("chat", help="多轮对话入口")
    chat_subparsers = chat_parser.add_subparsers(dest="chat_command", required=True)

    chat_send = chat_subparsers.add_parser("send", help="发送一条消息")
    chat_send.add_argument("message")
    chat_send.add_argument("--thread-id")
    chat_send.add_argument("--expected-output")
    chat_send.add_argument(
        "--executor", default="auto", choices=["auto", "file", "python", "browser"]
    )
    chat_send.add_argument("--skill", dest="skill_name")
    chat_send.add_argument("--preferred-skill", action="append", default=[])
    chat_send.add_argument("--payload", help="JSON 格式执行输入")
    chat_send.add_argument(
        "--mode", default="semi_auto", choices=["assist", "semi_auto", "full_auto"]
    )
    chat_send.add_argument("--max-permission", default="L2", choices=["L1", "L2", "L3", "L4", "L5"])
    chat_send.add_argument("--allow-network", action="store_true")
    chat_send.add_argument("--allow-delete", action="store_true")
    chat_send.add_argument("--timeout", type=int)
    chat_send.add_argument("--json", action="store_true")

    chat_list = chat_subparsers.add_parser("list", help="列出对话线程")
    chat_list.add_argument("--limit", type=int, default=20)
    chat_list.add_argument("--status", choices=[str(item) for item in ConversationThreadStatus])
    chat_list.add_argument("--json", action="store_true")

    chat_show = chat_subparsers.add_parser("show", help="查看对话线程")
    chat_show.add_argument("thread_id")
    chat_show.add_argument("--json", action="store_true")

    chat_repl = chat_subparsers.add_parser("repl", help="进入常驻会话模式")
    chat_repl.add_argument("--thread-id")
    chat_repl.add_argument("--expected-output")
    chat_repl.add_argument(
        "--executor", default="auto", choices=["auto", "file", "python", "browser"]
    )
    chat_repl.add_argument("--skill", dest="skill_name")
    chat_repl.add_argument("--preferred-skill", action="append", default=[])
    chat_repl.add_argument("--payload", help="JSON 格式执行输入")
    chat_repl.add_argument(
        "--mode", default="semi_auto", choices=["assist", "semi_auto", "full_auto"]
    )
    chat_repl.add_argument("--max-permission", default="L2", choices=["L1", "L2", "L3", "L4", "L5"])
    chat_repl.add_argument("--allow-network", action="store_true")
    chat_repl.add_argument("--allow-delete", action="store_true")
    chat_repl.add_argument("--timeout", type=int)

    state_parser = subparsers.add_parser("state", help="查看 Agent state")
    state_subparsers = state_parser.add_subparsers(dest="state_command", required=True)

    state_show = state_subparsers.add_parser("show", help="显示当前 Agent state")
    state_show.add_argument("--section", choices=["profile", "preferences", "project", "goal"])
    state_show.add_argument("--json", action="store_true")

    procedure_parser = subparsers.add_parser("procedure", help="管理 Procedure memory")
    procedure_subparsers = procedure_parser.add_subparsers(dest="procedure_command", required=True)

    procedure_list = procedure_subparsers.add_parser("list", help="列出 procedures")
    procedure_list.add_argument("--include-disabled", action="store_true")
    procedure_list.add_argument("--json", action="store_true")

    procedure_show = procedure_subparsers.add_parser("show", help="查看 procedure")
    procedure_show.add_argument("procedure_id")
    procedure_show.add_argument("--json", action="store_true")

    procedure_disable = procedure_subparsers.add_parser("disable", help="禁用 procedure")
    procedure_disable.add_argument("procedure_id")
    procedure_disable.add_argument("--note")
    procedure_disable.add_argument("--json", action="store_true")

    procedure_edit = procedure_subparsers.add_parser("edit", help="受控编辑 procedure")
    procedure_edit.add_argument("procedure_id")
    procedure_edit.add_argument(
        "--patch",
        required=True,
        help="JSON object; allowed fields: name, trigger, risk_level, required_permissions, inputs, steps, outputs, validator",
    )
    procedure_edit.add_argument("--note")
    procedure_edit.add_argument("--reviewer", default="local_user")
    procedure_edit.add_argument("--json", action="store_true")

    procedure_review = procedure_subparsers.add_parser("review", help="审核 procedure 学习内容")
    procedure_review.add_argument("procedure_id")
    procedure_review.add_argument(
        "--decision",
        required=True,
        choices=["approved", "needs_changes", "rejected"],
    )
    procedure_review.add_argument("--reviewer", default="local_user")
    procedure_review.add_argument("--note")
    procedure_review.add_argument("--evidence-ref", action="append", default=[])
    procedure_review.add_argument("--json", action="store_true")

    procedure_promote_habit = procedure_subparsers.add_parser(
        "promote-habit",
        help="将满足验证门槛的 verified procedure 升级为 habit",
    )
    procedure_promote_habit.add_argument("procedure_id")
    procedure_promote_habit.add_argument("--note")
    procedure_promote_habit.add_argument(
        "--require-confirmation",
        action="store_true",
        help="允许高风险 habit，但后续匹配仍必须要求人工确认",
    )
    procedure_promote_habit.add_argument("--json", action="store_true")

    procedure_explain = procedure_subparsers.add_parser(
        "explain-match",
        help="解释给定任务目标或 task_id 的 procedure retrieval 结果",
    )
    procedure_explain.add_argument("task_or_goal")
    procedure_explain.add_argument("--json", action="store_true")

    learning_parser = subparsers.add_parser("learning", help="审计学习对象")
    learning_subparsers = learning_parser.add_subparsers(dest="learning_command", required=True)

    learning_review = learning_subparsers.add_parser(
        "review",
        help="统一审计 experience / procedure / habit / skill 学习对象",
    )
    learning_review.add_argument(
        "--type",
        dest="object_type",
        default="all",
        choices=["all", "experience", "procedure", "habit", "skill"],
    )
    learning_review.add_argument("--status")
    learning_review.add_argument("--include-disabled", action="store_true")
    learning_review.add_argument("--json", action="store_true")

    learning_show = learning_subparsers.add_parser(
        "show",
        help="查看单个 experience / procedure / habit / skill 学习对象",
    )
    learning_show.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["experience", "procedure", "habit", "skill"],
    )
    learning_show.add_argument("object_id")
    learning_show.add_argument("--include-disabled", action="store_true")
    learning_show.add_argument("--json", action="store_true")

    learning_history = learning_subparsers.add_parser(
        "history",
        help="查看 learning object 的审计历史",
    )
    learning_history.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["experience", "procedure", "habit", "skill"],
    )
    learning_history.add_argument("object_id")
    learning_history.add_argument("--include-disabled", action="store_true")
    learning_history.add_argument("--json", action="store_true")

    learning_explain = learning_subparsers.add_parser(
        "explain-match",
        help="通过统一学习治理入口解释任务目标的 procedure / habit retrieval 结果",
    )
    learning_explain.add_argument("task_or_goal")
    learning_explain.add_argument("--json", action="store_true")

    learning_edit = learning_subparsers.add_parser(
        "edit",
        help="通过统一学习治理入口受控编辑 procedure / habit",
    )
    learning_edit.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit"],
    )
    learning_edit.add_argument("object_id")
    learning_edit.add_argument(
        "--patch",
        required=True,
        help="JSON object; allowed fields: name, trigger, risk_level, required_permissions, inputs, steps, outputs, validator",
    )
    learning_edit.add_argument("--note")
    learning_edit.add_argument("--reviewer", default="local_user")
    learning_edit.add_argument("--json", action="store_true")

    learning_decay = learning_subparsers.add_parser(
        "decay",
        help="通过统一学习治理入口将过期 procedure / habit 降级为需复审状态",
    )
    learning_decay.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit"],
    )
    learning_decay.add_argument("object_id")
    learning_decay.add_argument("--note")
    learning_decay.add_argument("--reviewer", default="local_user")
    learning_decay.add_argument("--json", action="store_true")

    learning_promote = learning_subparsers.add_parser(
        "promote",
        help="通过统一学习治理入口升级 procedure -> habit 或 habit -> skill",
    )
    learning_promote.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit"],
    )
    learning_promote.add_argument("object_id")
    learning_promote.add_argument("--note")
    learning_promote.add_argument(
        "--require-confirmation",
        action="store_true",
        help="仅用于 --type procedure：允许高风险 habit，但后续匹配仍必须要求人工确认",
    )
    learning_promote.add_argument("--skill-name", help="仅用于 --type habit：目标 skill 名称")
    learning_promote.add_argument("--version", help="仅用于 --type habit：目标 skill 版本")
    learning_promote.add_argument("--approver", default="local_user")
    learning_promote.add_argument("--rationale")
    learning_promote.add_argument("--json", action="store_true")

    learning_validate = learning_subparsers.add_parser(
        "validate",
        help="通过统一学习治理入口校验学习对象",
    )
    learning_validate.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit", "skill"],
    )
    learning_validate.add_argument("object_id")
    learning_validate.add_argument("--version")
    learning_validate.add_argument("--reviewer", default="local_user")
    learning_validate.add_argument("--note")
    learning_validate.add_argument("--evidence-ref", action="append", default=[])
    learning_validate.add_argument("--json", action="store_true")

    learning_disable = learning_subparsers.add_parser(
        "disable",
        help="通过统一学习治理入口禁用 procedure / habit / skill",
    )
    learning_disable.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit", "skill"],
    )
    learning_disable.add_argument("object_id")
    learning_disable.add_argument("--note")
    learning_disable.add_argument("--json", action="store_true")

    learning_enable = learning_subparsers.add_parser(
        "enable",
        help="通过统一学习治理入口恢复已禁用的 procedure / habit / skill",
    )
    learning_enable.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit", "skill"],
    )
    learning_enable.add_argument("object_id")
    learning_enable.add_argument("--note")
    learning_enable.add_argument("--json", action="store_true")

    learning_rollback = learning_subparsers.add_parser(
        "rollback",
        help="通过统一学习治理入口回滚 skill 到归档版本",
    )
    learning_rollback.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["skill"],
    )
    learning_rollback.add_argument("object_id")
    learning_rollback.add_argument("--to-version")
    learning_rollback.add_argument("--json", action="store_true")

    learning_approve = learning_subparsers.add_parser(
        "approve",
        help="通过统一学习治理入口审批 procedure / habit / skill",
    )
    learning_approve.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit", "skill"],
    )
    learning_approve.add_argument("object_id")
    learning_approve.add_argument("--reviewer", default="local_user")
    learning_approve.add_argument("--note")
    learning_approve.add_argument("--evidence-ref", action="append", default=[])
    learning_approve.add_argument("--version")
    learning_approve.add_argument("--json", action="store_true")

    learning_reject = learning_subparsers.add_parser(
        "reject",
        help="通过统一学习治理入口拒绝 procedure / habit / skill",
    )
    learning_reject.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit", "skill"],
    )
    learning_reject.add_argument("object_id")
    learning_reject.add_argument("--reviewer", default="local_user")
    learning_reject.add_argument("--note")
    learning_reject.add_argument("--evidence-ref", action="append", default=[])
    learning_reject.add_argument("--version")
    learning_reject.add_argument("--json", action="store_true")

    learning_request_changes = learning_subparsers.add_parser(
        "request-changes",
        help="通过统一学习治理入口要求 procedure / habit 修改后复审",
    )
    learning_request_changes.add_argument(
        "--type",
        dest="object_type",
        required=True,
        choices=["procedure", "habit"],
    )
    learning_request_changes.add_argument("object_id")
    learning_request_changes.add_argument("--reviewer", default="local_user")
    learning_request_changes.add_argument("--note")
    learning_request_changes.add_argument("--evidence-ref", action="append", default=[])
    learning_request_changes.add_argument("--json", action="store_true")

    llm_parser = subparsers.add_parser("llm", help="检查 LLM provider")
    llm_subparsers = llm_parser.add_subparsers(dest="llm_command", required=True)

    llm_check = llm_subparsers.add_parser("check", help="执行 provider 健康检查与烟雾测试")
    llm_check.add_argument("--json", action="store_true")

    llm_stats = llm_subparsers.add_parser("stats", help="汇总 LLM 调用观测指标")
    llm_stats.add_argument("--task-id", help="仅统计指定 task_id 的调用")
    llm_stats.add_argument(
        "--thread-id",
        help="仅统计指定 thread_id 的调用（router / responder 等线程态 LLM 调用）",
    )
    llm_stats.add_argument("--prompt", help="仅统计指定 prompt_name 的调用")
    llm_stats.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多扫描多少个 task / thread 目录 (0=全部)",
    )
    llm_stats.add_argument("--json", action="store_true")

    skill_parser = subparsers.add_parser("skill", help="管理 Skill 生命周期")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)

    skill_list = skill_subparsers.add_parser("list", help="列出 Skill")
    skill_list.add_argument("--status")
    skill_list.add_argument("--json", action="store_true")

    skill_show = skill_subparsers.add_parser("show", help="查看 Skill 详情")
    skill_show.add_argument("skill_name")
    skill_show.add_argument("--json", action="store_true")

    skill_import = skill_subparsers.add_parser("import", help="导入外部 Skill 为 draft")
    skill_import.add_argument("path")
    skill_import.add_argument("--json", action="store_true")

    skill_generate = skill_subparsers.add_parser("generate", help="受控生成一个模板化 Skill")
    skill_generate.add_argument("skill_name")
    skill_generate.add_argument(
        "--type", dest="generation_type", choices=list(SUPPORTED_GENERATION_TYPES), required=True
    )
    skill_generate.add_argument("--goal", required=True)
    skill_generate.add_argument("--payload", help="JSON 格式输入样例")
    skill_generate.add_argument("--trigger", default="user_request")
    skill_generate.add_argument("--task-id")
    skill_generate.add_argument("--node-id")
    skill_generate.add_argument("--json", action="store_true")

    skill_promote_habit = skill_subparsers.add_parser(
        "promote-habit",
        help="将稳定 habit 提升为可验证 Skill",
    )
    skill_promote_habit.add_argument("procedure_id")
    skill_promote_habit.add_argument("--skill-name")
    skill_promote_habit.add_argument("--version")
    skill_promote_habit.add_argument("--approver", default="local_user")
    skill_promote_habit.add_argument("--rationale")
    skill_promote_habit.add_argument("--json", action="store_true")

    skill_validate = skill_subparsers.add_parser("validate", help="校验 draft Skill")
    skill_validate.add_argument("skill_name")
    skill_validate.add_argument("--version")
    skill_validate.add_argument("--json", action="store_true")

    skill_approve = skill_subparsers.add_parser("approve", help="审批 Skill")
    skill_approve.add_argument("skill_name")
    skill_approve.add_argument("--version")
    skill_approve.add_argument("--approver", default="local_user")
    skill_approve.add_argument("--reject", action="store_true")
    skill_approve.add_argument("--rationale")
    skill_approve.add_argument("--json", action="store_true")

    skill_activate = skill_subparsers.add_parser("activate", help="激活已审批的 Skill")
    skill_activate.add_argument("skill_name")
    skill_activate.add_argument("--version")
    skill_activate.add_argument("--json", action="store_true")

    skill_rollback = skill_subparsers.add_parser("rollback", help="回滚到已归档版本")
    skill_rollback.add_argument("skill_name")
    skill_rollback.add_argument("--to-version")
    skill_rollback.add_argument("--json", action="store_true")

    skill_deprecate = skill_subparsers.add_parser("deprecate", help="弃用当前 Skill")
    skill_deprecate.add_argument("skill_name")
    skill_deprecate.add_argument("--json", action="store_true")

    workflow_parser = subparsers.add_parser("workflow", help="管理 workflow 候选与提升")
    workflow_subparsers = workflow_parser.add_subparsers(dest="workflow_command", required=True)

    workflow_promote = workflow_subparsers.add_parser(
        "promote", help="确认将 workflow 提升为可复用 Skill"
    )
    workflow_promote.add_argument("task_id")
    workflow_promote.add_argument("--skill-name")
    workflow_promote.add_argument("--version")
    workflow_promote.add_argument("--approver", default="local_user")
    workflow_promote.add_argument("--rationale")
    workflow_promote.add_argument("--json", action="store_true")

    scheduler_parser = subparsers.add_parser("scheduler", help="处理长期任务挂起/恢复")
    scheduler_subparsers = scheduler_parser.add_subparsers(dest="scheduler_command", required=True)

    scheduler_run = scheduler_subparsers.add_parser("run", help="执行一次定时唤醒扫描")
    scheduler_run.add_argument("--at", help="使用给定 ISO 时间作为当前时间")
    scheduler_run.add_argument("--json", action="store_true")

    scheduler_signal = scheduler_subparsers.add_parser(
        "signal", help="发送一个外部事件并尝试恢复任务"
    )
    scheduler_signal.add_argument("event_key")
    scheduler_signal.add_argument("--payload", help="JSON 格式事件载荷")
    scheduler_signal.add_argument("--at", help="使用给定 ISO 时间作为当前时间")
    scheduler_signal.add_argument("--json", action="store_true")

    task_parser = subparsers.add_parser("task", help="查看任务")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)

    task_list = task_subparsers.add_parser("list", help="列出任务")
    task_list.add_argument("--limit", type=int, default=20)
    task_list.add_argument("--status")
    task_list.add_argument("--json", action="store_true")

    task_show = task_subparsers.add_parser("show", help="查看任务详情")
    task_show.add_argument("task_id")
    task_show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    project_root = Path.cwd().resolve()
    config = AppConfig.from_env(project_root)
    configure_logging(config.logging)
    gateway = RuntimeGateway(config, workspace)

    if args.command == "run":
        request = _build_task_request(
            args,
            default_timeout_seconds=config.gateway.task_timeout_seconds,
        )
        event_handlers = [] if args.json else [_build_event_printer(sys.stdout)]
        report = gateway.submit(request, event_handlers=event_handlers)
        if args.json:
            print(json.dumps(_to_builtin(report), ensure_ascii=False, indent=2))
        else:
            print(f"task_id: {report.task.task_id}")
            print(f"status: {report.task.status}")
            print(f"graph_id: {report.task.graph_id}")
            print(f"executor: {report.task.executor}")
            print(f"message: {report.result.message}")
            if report.intake_assessment:
                intake = report.intake_assessment
                print("任务预判:")
                print(f"- type: {intake.get('task_type')}")
                print(f"- risk: {intake.get('risk_level')}")
                print(
                    "- tools: "
                    + (
                        ", ".join(str(item) for item in intake.get("tools", []))
                        if intake.get("tools")
                        else "none"
                    )
                )
                print(f"- validation: {intake.get('validation_strategy')}")
                print(f"- confirmation_required: {intake.get('confirmation_required')}")
            if report.task_graph is not None:
                print("nodes:")
                for node in report.task_graph.nodes:
                    print(f"- {node.node_id} [{node.type}] {node.status}: {node.goal}")
            print("events:")
            for event in report.events:
                print(f"- {event.event_type} [{event.status}]")
            learning_feedback = format_learning_feedback(report.learning_feedback)
            if learning_feedback:
                print(learning_feedback)
        return (
            0
            if report.result.success
            or report.task.status in {TaskStatus.WAITING_INPUT, TaskStatus.SUSPENDED}
            else 1
        )

    if args.command == "chat" and args.chat_command == "send":
        event_handlers = [] if args.json else [_build_event_printer(sys.stdout)]
        response = gateway.send_message(
            args.message,
            thread_id=args.thread_id,
            request_options=_build_chat_request_options(
                args,
                default_timeout_seconds=config.gateway.task_timeout_seconds,
            ),
            event_handlers=event_handlers,
        )
        if args.json:
            print(json.dumps(_to_builtin(response), ensure_ascii=False, indent=2))
            return 0
        return _print_conversation_response(response, sys.stdout)

    if args.command == "chat" and args.chat_command == "list":
        payload = gateway.list_threads(limit=args.limit, status=args.status)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            for thread in payload:
                print(
                    f"{thread.thread_id}\t{thread.status}\t{thread.updated_at}\t"
                    f"active={thread.active_task_id or '-'}\tcheckpoint={thread.pending_checkpoint_id or '-'}\t{thread.title}"
                )
        return 0

    if args.command == "chat" and args.chat_command == "show":
        payload = gateway.show_thread(args.thread_id)
        if not payload:
            print(f"thread not found: {args.thread_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_thread_payload(payload, sys.stdout)
        return 0

    if args.command == "chat" and args.chat_command == "repl":
        return _run_chat_repl(gateway, args, sys.stdout)

    if args.command == "state" and args.state_command == "show":
        payload = gateway.show_agent_state(section=args.section)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_agent_state_payload(payload, sys.stdout)
        return 0

    if args.command == "procedure" and args.procedure_command == "list":
        payload = gateway.list_procedures(include_disabled=args.include_disabled)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            for item in payload:
                print(
                    f"{item.get('procedure_id')}\t{item.get('status')}\t"
                    f"risk={item.get('risk_level')}\tconf={item.get('confidence')}\t"
                    f"{item.get('name')}"
                )
        return 0

    if args.command == "procedure" and args.procedure_command == "show":
        payload = gateway.show_procedure(args.procedure_id)
        if not payload:
            print(f"procedure not found: {args.procedure_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_procedure_payload(payload, sys.stdout)
        return 0

    if args.command == "procedure" and args.procedure_command == "disable":
        try:
            payload = gateway.disable_procedure(args.procedure_id, note=args.note)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_procedure_payload(payload, sys.stdout)
        return 0

    if args.command == "procedure" and args.procedure_command == "edit":
        try:
            payload = gateway.edit_procedure(
                args.procedure_id,
                patch=_parse_payload(args.patch),
                note=args.note,
                reviewer=args.reviewer,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_procedure_payload(payload, sys.stdout)
        return 0

    if args.command == "procedure" and args.procedure_command == "review":
        try:
            payload = gateway.review_procedure(
                args.procedure_id,
                decision=args.decision,
                reviewer=args.reviewer,
                note=args.note,
                evidence_refs=args.evidence_ref,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_procedure_payload(payload, sys.stdout)
        return 0

    if args.command == "procedure" and args.procedure_command == "promote-habit":
        try:
            payload = gateway.promote_procedure_to_habit(
                args.procedure_id,
                note=args.note,
                require_confirmation=args.require_confirmation,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_procedure_payload(payload, sys.stdout)
        return 0

    if args.command == "procedure" and args.procedure_command == "explain-match":
        task_or_goal = args.task_or_goal
        task_payload = gateway.show_task(task_or_goal)
        goal = task_payload["task"].goal if task_payload else task_or_goal
        payload = gateway.explain_procedure_match(goal)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_procedure_match(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "review":
        try:
            payload = gateway.review_learning(
                object_type=args.object_type,
                status=args.status,
                include_disabled=args.include_disabled,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_review(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "show":
        try:
            payload = gateway.show_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                include_disabled=args.include_disabled,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "history":
        try:
            payload = gateway.history_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                include_disabled=args.include_disabled,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_history(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "explain-match":
        task_or_goal = args.task_or_goal
        task_payload = gateway.show_task(task_or_goal)
        goal = task_payload["task"].goal if task_payload else task_or_goal
        payload = gateway.explain_learning_match(goal)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_match(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "edit":
        try:
            payload = gateway.edit_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                patch=_parse_payload(args.patch),
                note=args.note,
                reviewer=args.reviewer,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "decay":
        try:
            payload = gateway.decay_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                note=args.note,
                reviewer=args.reviewer,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "promote":
        try:
            payload = gateway.promote_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                note=args.note,
                require_confirmation=args.require_confirmation,
                skill_name=args.skill_name,
                version=args.version,
                approver=args.approver,
                rationale=args.rationale,
            )
        except (ValueError, OmniAgentError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        validation_report = payload.get("validation_report")
        if validation_report is not None and not validation_report.passed:
            return 1
        return 0

    if args.command == "learning" and args.learning_command == "validate":
        try:
            payload = gateway.validate_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                version=args.version,
                reviewer=args.reviewer,
                note=args.note,
                evidence_refs=args.evidence_ref,
            )
        except (ValueError, OmniAgentError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        validation_report = payload.get("validation_report")
        if validation_report is not None and not validation_report.passed:
            return 1
        return 0

    if args.command == "learning" and args.learning_command == "disable":
        try:
            payload = gateway.disable_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                note=args.note,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "enable":
        try:
            payload = gateway.enable_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                note=args.note,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "rollback":
        try:
            payload = gateway.rollback_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                to_version=args.to_version,
            )
        except (ValueError, OmniAgentError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "approve":
        try:
            payload = gateway.approve_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                reviewer=args.reviewer,
                note=args.note,
                evidence_refs=args.evidence_ref,
                version=args.version,
            )
        except (ValueError, OmniAgentError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "reject":
        try:
            payload = gateway.reject_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                reviewer=args.reviewer,
                note=args.note,
                evidence_refs=args.evidence_ref,
                version=args.version,
            )
        except (ValueError, OmniAgentError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "learning" and args.learning_command == "request-changes":
        try:
            payload = gateway.request_changes_learning_object(
                object_type=args.object_type,
                object_id=args.object_id,
                reviewer=args.reviewer,
                note=args.note,
                evidence_refs=args.evidence_ref,
            )
        except (ValueError, OmniAgentError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not payload:
            print(
                f"learning object not found: {args.object_type}:{args.object_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            _print_learning_object(payload, sys.stdout)
        return 0

    if args.command == "llm" and args.llm_command == "check":
        try:
            payload = gateway.check_llm()
        except OmniAgentError as exc:
            error_payload = exc.to_dict()
            payload = {
                "healthy": False,
                "degraded_to_fallback": False,
                "error": error_payload,
            }
            details = error_payload.get("details") or {}
            for key in ("provider", "model", "endpoint"):
                if details.get(key):
                    payload[key] = details[key]
        payload = _annotate_llm_check_failure(payload)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            print(f"healthy: {payload.get('healthy')}")
            print(f"degraded_to_fallback: {payload.get('degraded_to_fallback', False)}")
            primary = payload.get("primary") or payload
            print(f"provider: {primary.get('provider')}")
            if primary.get("model"):
                print(f"model: {primary.get('model')}")
            if primary.get("endpoint"):
                print(f"endpoint: {primary.get('endpoint')}")
            fallback = payload.get("fallback")
            if fallback:
                print(f"fallback_provider: {fallback.get('provider')}")
            error = payload.get("error")
            if error:
                print(f"error_code: {error.get('code')}")
                print(f"error_message: {error.get('message')}")
            if payload.get("diagnosis"):
                print(f"diagnosis: {payload.get('diagnosis')}")
            for action in payload.get("next_actions") or []:
                print(f"next_action: {action}")
        return 0 if payload.get("healthy") else 1

    if args.command == "llm" and args.llm_command == "stats":
        stats = gateway.llm_stats(
            task_id=args.task_id,
            thread_id=args.thread_id,
            prompt_name=args.prompt,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(_to_builtin(stats), ensure_ascii=False, indent=2))
        else:
            print(f"tasks_scanned: {stats['tasks_scanned']}")
            print(f"threads_scanned: {stats.get('threads_scanned', 0)}")
            print(f"total_calls: {stats['total_calls']}")
            by_scope = stats.get("by_scope") or {}
            if by_scope:
                print(
                    f"by_scope: task={by_scope.get('task', 0)} thread={by_scope.get('thread', 0)}"
                )
            latency = stats["latency_ms"]
            print(
                f"latency_ms: avg={latency['avg']} p50={latency['p50']} p95={latency['p95']} max={latency['max']}"
            )
            tokens = stats["tokens"]
            print(
                f"tokens: prompt={tokens['prompt']} completion={tokens['completion']} total={tokens['total']}"
            )
            attempts = stats["attempts"]
            print(f"attempts: avg={attempts['avg']} max={attempts['max']}")
            if stats["by_tier"]:
                tiers = ", ".join(f"{k}={v}" for k, v in sorted(stats["by_tier"].items()))
                print(f"by_tier: {tiers}")
            if stats["by_prompt"]:
                print("by_prompt:")
                for pname, bucket in sorted(stats["by_prompt"].items()):
                    print(
                        f"  {pname}: calls={bucket['calls']} "
                        f"avg_latency_ms={bucket.get('avg_latency_ms', 0.0)} "
                        f"tokens={bucket['total_tokens']}"
                    )
        return 0

    if args.command == "skill" and args.skill_command == "list":
        payload = gateway.list_skills(status=args.status)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            for entry in payload:
                print(
                    f"{entry.skill_name}\t{entry.lifecycle_status}\tv{entry.version}\t{entry.source}"
                )
        return 0

    if args.command == "skill" and args.skill_command == "show":
        payload = gateway.show_skill(args.skill_name)
        if not payload:
            print(f"skill not found: {args.skill_name}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            entry = payload.get("registry_entry")
            if entry is not None:
                print(f"skill_name: {entry.skill_name}")
                print(f"status: {entry.lifecycle_status}")
                print(f"version: {entry.version}")
                print(f"source: {entry.source}")
            print("drafts:")
            for draft in payload.get("drafts", []):
                print(f"- {draft.version} [{draft.lifecycle_status}] {draft.source_kind}")
            print("validations:")
            for report in payload.get("validation_reports", []):
                print(f"- {report.version}: passed={report.passed} {report.summary}")
            print("approvals:")
            for record in payload.get("approval_records", []):
                print(f"- {record.version}: {record.status} by {record.approver}")
            print("registrations:")
            for record in payload.get("registration_records", []):
                print(f"- {record.action} {record.version} [{record.status}]")
            lineage_records = payload.get("lineage_records") or []
            if lineage_records:
                print("lineage:")
                for item in lineage_records:
                    print(
                        f"- habit={item.get('habit_id')} "
                        f"experiences={','.join(item.get('source_experience_ids', []))}"
                    )
        return 0

    if args.command == "skill" and args.skill_command == "import":
        payload = gateway.import_skill(args.path)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            entry = payload.get("registry_entry")
            if entry is not None:
                print(f"skill_name: {entry.skill_name}")
                print(f"status: {entry.lifecycle_status}")
                print(f"version: {entry.version}")
        return 0

    if args.command == "skill" and args.skill_command == "generate":
        payload = gateway.generate_skill(
            skill_name=args.skill_name,
            generation_type=args.generation_type,
            goal=args.goal,
            payload_example=_parse_payload(args.payload),
            trigger=args.trigger,
            task_id=args.task_id,
            node_id=args.node_id,
        )
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            print(f"skill_name: {payload['skill_name']}")
            print(f"status: {payload['status']}")
            draft = payload.get("draft")
            if draft is not None:
                print(f"version: {draft.version}")
            report_path = payload.get("generation_report_path")
            if report_path:
                print(f"generation_report: {report_path}")
        return 0

    if args.command == "skill" and args.skill_command == "promote-habit":
        payload = gateway.promote_habit_to_skill(
            args.procedure_id,
            skill_name=args.skill_name,
            version=args.version,
            approver=args.approver,
            rationale=args.rationale,
        )
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            print(f"procedure_id: {payload['procedure_id']}")
            print(f"skill_name: {payload['skill_name']}")
            print(f"version: {payload['version']}")
            print(f"lineage_path: {payload['lineage_path']}")
            print(f"validation_passed: {payload['validation_report'].passed}")
            approval = payload.get("approval_record")
            if approval is not None:
                print(f"approval_status: {approval.status}")
            registration = payload.get("registration_record")
            if registration is not None:
                print(f"registration_status: {registration.status}")
        return 0 if payload["validation_report"].passed else 1

    if args.command == "skill" and args.skill_command == "validate":
        payload = gateway.validate_skill(args.skill_name, version=args.version)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            report = payload["validation_report"]
            print(f"skill_name: {report.skill_name}")
            print(f"version: {report.version}")
            print(f"passed: {report.passed}")
            print(f"summary: {report.summary}")
            print(f"approval_required: {payload['approval_policy']['approval_required']}")
        return 0 if payload["validation_report"].passed else 1

    if args.command == "skill" and args.skill_command == "approve":
        payload = gateway.approve_skill(
            args.skill_name,
            version=args.version,
            approver=args.approver,
            approved=not args.reject,
            rationale=args.rationale,
        )
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            record = payload["approval_record"]
            print(f"skill_name: {record.skill_name}")
            print(f"version: {record.version}")
            print(f"status: {record.status}")
            print(f"approver: {record.approver}")
        return 0 if payload["approval_record"].status in {"approved", "not_required"} else 1

    if args.command == "skill" and args.skill_command == "activate":
        payload = gateway.activate_skill(args.skill_name, version=args.version)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            record = payload["registration_record"]
            print(f"skill_name: {record.skill_name}")
            print(f"version: {record.version}")
            print(f"action: {record.action}")
            print(f"status: {record.status}")
        return 0

    if args.command == "skill" and args.skill_command == "rollback":
        payload = gateway.rollback_skill(args.skill_name, to_version=args.to_version)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            record = payload["registration_record"]
            print(f"skill_name: {record.skill_name}")
            print(f"version: {record.version}")
            print(f"previous_version: {record.previous_version}")
            print(f"status: {record.status}")
        return 0

    if args.command == "skill" and args.skill_command == "deprecate":
        payload = gateway.deprecate_skill(args.skill_name)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            record = payload["registration_record"]
            print(f"skill_name: {record.skill_name}")
            print(f"version: {record.version}")
            print(f"status: {record.status}")
        return 0

    if args.command == "workflow" and args.workflow_command == "promote":
        payload = gateway.promote_workflow(
            args.task_id,
            skill_name=args.skill_name,
            version=args.version,
            approver=args.approver,
            rationale=args.rationale,
        )
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            candidate = payload["candidate"]
            print(f"task_id: {args.task_id}")
            print(f"skill_name: {candidate['skill_name']}")
            print(f"version: {candidate['version']}")
            print(f"status: {candidate['status']}")
            print(f"promotion_source: {candidate.get('promotion_source')}")
            print(f"promotion_confirmed_by: {candidate.get('promotion_confirmed_by')}")
            print(f"solidification_result: {candidate.get('solidification_result')}")
            if payload.get("generated_skill") is not None:
                generated_skill = payload["generated_skill"]
                print(f"generated_skill_name: {generated_skill['skill_name']}")
                print(f"generated_skill_status: {generated_skill['status']}")
                if generated_skill.get("draft") is not None:
                    print(f"generated_skill_version: {generated_skill['draft'].version}")
        return 0

    if args.command == "scheduler" and args.scheduler_command == "run":
        event_handlers = [] if args.json else [_build_event_printer(sys.stdout)]
        payload = gateway.run_scheduler(now=args.at, event_handlers=event_handlers)
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            if not payload:
                print("scheduler: no checkpoints processed")
            for item in payload:
                print(f"task_id: {item['task_id']}")
                print(f"checkpoint_id: {item['checkpoint_id']}")
                print(f"trigger: {item['trigger']}")
                print(f"status: {item['status']}")
                print(f"message: {item['message']}")
        return 0

    if args.command == "scheduler" and args.scheduler_command == "signal":
        event_handlers = [] if args.json else [_build_event_printer(sys.stdout)]
        payload = gateway.signal_external_event(
            args.event_key,
            payload=_parse_payload(args.payload),
            now=args.at,
            event_handlers=event_handlers,
        )
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            if not payload:
                print(f"scheduler: no checkpoints matched event {args.event_key}")
            for item in payload:
                print(f"task_id: {item['task_id']}")
                print(f"checkpoint_id: {item['checkpoint_id']}")
                print(f"trigger: {item['trigger']}")
                print(f"status: {item['status']}")
                print(f"message: {item['message']}")
        return 0

    if args.command == "task" and args.task_command == "list":
        tasks = gateway.list_tasks(limit=args.limit, status=args.status)
        if args.json:
            print(json.dumps(_to_builtin(tasks), ensure_ascii=False, indent=2))
        else:
            for task in tasks:
                print(f"{task.task_id}\t{task.status}\t{task.executor}\t{task.goal}")
        return 0

    if args.command == "task" and args.task_command == "show":
        payload = gateway.show_task(args.task_id)
        if not payload:
            print(f"task not found: {args.task_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2))
        else:
            task = payload["task"]
            print(f"task_id: {task.task_id}")
            print(f"status: {task.status}")
            print(f"summary: {task.summary}")
            graph = payload.get("task_graph")
            if graph is not None:
                print(f"graph_id: {graph.graph_id}")
                print("nodes:")
                for node in payload.get("task_nodes", []):
                    print(f"- {node.node_id} [{node.type}] {node.status}: {node.goal}")
            print("critic:")
            for verdict in payload.get("critic_verdicts", []):
                print(f"- {verdict.target_id}: {verdict.action} {verdict.summary}")
            evaluation = payload.get("completion_evaluation")
            if evaluation is not None:
                print("completion:")
                print(f"- satisfied: {evaluation.task_satisfied}")
                print(f"- score: {evaluation.completion_score}")
                print(f"- failure_layer: {evaluation.primary_failure_layer}")
            print("workflow_candidates:")
            for item in payload.get("workflow_candidates", []):
                source = item.get("promotion_source", "unknown")
                print(
                    f"- {item['skill_name']} [{item['status']}] v{item['version']} source={source}"
                )
            print("events:")
            for event in payload["events"]:
                print(f"- {event.event_type}: {event.payload}")
            print("permissions:")
            for item in payload["permissions"]:
                resource = item.get("resource_ref") or item.get("resource_kind") or "-"
                print(
                    f"- {item['permission']} {item['level']} {item['status']} "
                    f"scope={item.get('scope', 'task')} sandbox={item.get('sandbox_level', 'workspace')} resource={resource}"
                )
            print("resume_attempts:")
            for item in payload["resume_attempts"]:
                print(
                    f"- {item.attempt_id} {item.trigger} {item.status} checkpoint={item.checkpoint_id} "
                    f"message={item.message}"
                )
        return 0

    parser.error("未知命令")
    return 2

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS = REPO_ROOT / "evals" / "real_world" / "scenarios.json"
DEFAULT_RUN_ROOT = REPO_ROOT / "eval_runs"
EVIDENCE_INDEX_SCHEMA_VERSION = "eval-evidence-index/v1"
EVIDENCE_QUERY_SCHEMA_VERSION = "eval-evidence-query/v1"
EVIDENCE_DASHBOARD_SCHEMA_VERSION = "eval-evidence-dashboard/v1"
WORKSPACE_SNAPSHOT_SKIP_DIRS = {"tasks", "threads", "MEMORY", "WORKFLOWS", "__pycache__"}
WORKSPACE_SNAPSHOT_SKIP_FILES = {"AGENT.md", "POLICY.md", "TOOLS.md", "USER.md"}
TEXT_PREVIEW_SUFFIXES = {
    "",
    ".csv",
    ".json",
    ".log",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
}


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _loads_json_object(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _loads_json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        payload = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate == resolved_root or resolved_root in candidate.parents:
        return candidate
    raise ValueError(f"path escapes eval workspace: {relative}")


def _permission_rank(level: str) -> int:
    value = str(level or "L2").strip().upper()
    if len(value) == 2 and value.startswith("L") and value[1].isdigit():
        return int(value[1])
    return 2


def _parse_json_stdout(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    raw = stdout.strip()
    if not raw:
        return None, "stdout is empty"
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload, None
        return None, "stdout JSON is not an object"
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(raw[start : end + 1])
                if isinstance(payload, dict):
                    return payload, None
            except json.JSONDecodeError as exc:
                return None, f"stdout JSON parse failed: {exc}"
        return None, "stdout does not contain parseable JSON"


def _normalize_scenarios(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, dict):
        scenarios = payload.get("scenarios", [])
    else:
        scenarios = payload
    if not isinstance(scenarios, list):
        raise ValueError(f"scenarios must be a list: {path}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in scenarios:
        if not isinstance(item, dict):
            raise ValueError("each scenario must be an object")
        scenario_id = str(item.get("id", "")).strip()
        if not scenario_id:
            raise ValueError("scenario is missing id")
        if scenario_id in seen:
            raise ValueError(f"duplicate scenario id: {scenario_id}")
        seen.add(scenario_id)
        kind = str(item.get("kind", "task"))
        if kind not in {"task", "chat_sequence", "task_sequence"}:
            raise ValueError(f"{scenario_id}: unsupported kind {kind}")
        if kind == "task" and not item.get("goal"):
            raise ValueError(f"{scenario_id}: task scenario requires goal")
        if kind == "chat_sequence" and not item.get("turns"):
            raise ValueError(f"{scenario_id}: chat_sequence requires turns")
        if kind == "task_sequence" and not item.get("steps"):
            raise ValueError(f"{scenario_id}: task_sequence requires steps")
        normalized.append(item)
    return normalized


def _scenario_skip_reason(scenario: dict[str, Any], args: argparse.Namespace) -> str | None:
    if not _scenario_matches_run_selection(scenario, args):
        return "not selected"
    requires = {str(item).lower() for item in scenario.get("requires", [])}
    if "network" in requires and not (args.include_network or args.include_all):
        return "requires --include-network"
    if "l4" in requires and not (args.include_l4 or args.include_all):
        return "requires --include-l4"
    if "complex" in requires and not (args.include_complex or args.include_all):
        return "requires --include-complex"
    if _permission_rank(str(scenario.get("max_permission", "L2"))) >= 4 and not (
        args.include_l4 or args.include_all
    ):
        return "requires --include-l4"
    return None


def _scenario_matches_run_selection(scenario: dict[str, Any], args: argparse.Namespace) -> bool:
    exact_ids = set(_split_filter_values(getattr(args, "scenario", None)))
    id_globs = _split_filter_values(getattr(args, "scenario_glob", None))
    categories = _split_filter_values(getattr(args, "scenario_category", None))
    if not (exact_ids or id_globs or categories):
        return True

    scenario_id = str(scenario["id"])
    if scenario_id in exact_ids:
        return True

    category = str(scenario.get("category") or "uncategorized")
    id_matches = True if not id_globs else _matches_selector(scenario_id, id_globs)
    category_matches = True if not categories else _matches_selector(category, categories)
    return (bool(id_globs) or bool(categories)) and id_matches and category_matches


def _matches_selector(value: str, selectors: list[str]) -> bool:
    return any(value == selector or fnmatch.fnmatchcase(value, selector) for selector in selectors)


def _selection_result(
    scenario: dict[str, Any],
    *,
    status: str,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": scenario["id"],
        "category": scenario.get("category", "uncategorized"),
        "kind": scenario.get("kind", "task"),
        "requires": scenario.get("requires", []),
        "status": status,
        "skip_reason": skip_reason,
        "diagnosis": skip_reason or status,
        "failure_layer": "none",
        "issues": [],
        "task_ids": [],
        "thread_ids": [],
    }


def _select_scenarios(
    scenarios: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    include_not_selected: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        reason = _scenario_skip_reason(scenario, args)
        if reason:
            if reason != "not selected" or include_not_selected:
                results.append(_selection_result(scenario, status="skipped", skip_reason=reason))
            continue
        selected.append(scenario)
    return selected, results


def _prepare_workspace(run_dir: Path, scenario: dict[str, Any]) -> Path:
    workspace = run_dir / "workspaces" / str(scenario["id"])
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    for relative, content in dict(scenario.get("setup_files", {})).items():
        target = _safe_child(workspace, str(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    _prepare_sqlite_databases(workspace, scenario)
    _prepare_git_repo(workspace, scenario)
    _prepare_archives(workspace, scenario)
    _prepare_workflow_memory(workspace, scenario)
    _prepare_vector_memory(workspace, scenario)
    return workspace


def _prepare_archives(workspace: Path, scenario: dict[str, Any]) -> None:
    entries = scenario.get("setup_archives", [])
    if not entries:
        return
    if not isinstance(entries, list):
        raise ValueError(f"{scenario['id']}: setup_archives must be a list")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{scenario['id']}: setup_archives[{index}] must be an object")
        archive_path = _safe_child(workspace, str(entry.get("path") or ""))
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        fmt = str(entry.get("format") or archive_path.suffix.lstrip(".") or "zip").lower()
        if fmt != "zip":
            raise ValueError(f"{scenario['id']}: setup_archives[{index}] only supports zip")
        archive_entries = entry.get("entries", [])
        if not isinstance(archive_entries, list) or not archive_entries:
            raise ValueError(f"{scenario['id']}: setup_archives[{index}].entries must be non-empty")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry_index, archive_entry in enumerate(archive_entries, start=1):
                if not isinstance(archive_entry, dict):
                    raise ValueError(
                        f"{scenario['id']}: setup_archives[{index}].entries[{entry_index}] "
                        "must be an object"
                    )
                name = str(archive_entry.get("name") or "").strip()
                if not name:
                    raise ValueError(
                        f"{scenario['id']}: setup_archives[{index}].entries[{entry_index}] "
                        "requires name"
                    )
                content = str(archive_entry.get("content") or "")
                archive.writestr(name, content)


def _prepare_sqlite_databases(workspace: Path, scenario: dict[str, Any]) -> None:
    entries = scenario.get("setup_sqlite", [])
    if not entries:
        return
    if not isinstance(entries, list):
        raise ValueError(f"{scenario['id']}: setup_sqlite must be a list")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{scenario['id']}: setup_sqlite[{index}] must be an object")
        db_path = _safe_child(workspace, str(entry.get("path") or ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            for statement in entry.get("schema", []) or []:
                conn.execute(str(statement))
            rows = entry.get("rows", {}) or {}
            if not isinstance(rows, dict):
                raise ValueError(f"{scenario['id']}: setup_sqlite[{index}].rows must be an object")
            for table, table_payload in rows.items():
                if not isinstance(table_payload, dict):
                    raise ValueError(
                        f"{scenario['id']}: setup_sqlite[{index}].rows.{table} must be an object"
                    )
                columns = [str(item) for item in table_payload.get("columns", [])]
                values = table_payload.get("values", [])
                if not columns or not isinstance(values, list):
                    raise ValueError(
                        f"{scenario['id']}: setup_sqlite[{index}].rows.{table} requires columns and values"
                    )
                placeholders = ", ".join("?" for _ in columns)
                column_sql = ", ".join(columns)
                conn.executemany(
                    f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                    values,
                )
            conn.commit()
        finally:
            conn.close()


def _prepare_git_repo(workspace: Path, scenario: dict[str, Any]) -> None:
    if not scenario.get("setup_git_repo"):
        return
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "eval@example.test"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "OmniAgent Eval"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    for relative, content in dict(scenario.get("setup_git_changes", {})).items():
        target = _safe_child(workspace, str(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")


def _prepare_workflow_memory(workspace: Path, scenario: dict[str, Any]) -> None:
    entries = scenario.get("setup_workflow_memory", [])
    if not entries:
        return
    if not isinstance(entries, list):
        raise ValueError(f"{scenario['id']}: setup_workflow_memory must be a list")
    workflow_root = workspace / "WORKFLOWS"
    workflow_root.mkdir(parents=True, exist_ok=True)
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{scenario['id']}: setup_workflow_memory[{index}] must be an object")
        task_id = str(entry.get("task_id") or f"{scenario['id']}_workflow_{index}")
        payload = {
            "task_id": task_id,
            "goal": str(entry.get("goal") or ""),
            "executor": str(entry.get("executor") or "file"),
            "skill_name": entry.get("skill_name"),
            "status": str(entry.get("status") or "completed"),
            "result_message": str(entry.get("result_message") or ""),
            "plan_summary": str(entry.get("plan_summary") or ""),
            "nodes": list(entry.get("nodes") or []),
            "created_at": str(entry.get("created_at") or datetime.now(timezone.utc).isoformat()),
        }
        _write_json(workflow_root / f"{task_id}.json", payload)


def _prepare_vector_memory(workspace: Path, scenario: dict[str, Any]) -> None:
    entries = scenario.get("setup_vector_memory", [])
    if not entries:
        return
    if not isinstance(entries, list):
        raise ValueError(f"{scenario['id']}: setup_vector_memory must be a list")

    src_path = str(REPO_ROOT / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    from omniagent.config import AppConfig
    from omniagent.embeddings import build_embedding_provider
    from omniagent.memory import FileMemoryManager
    from omniagent.vector_recall import VectorRecallEngine

    config = AppConfig.from_env(REPO_ROOT)
    memory = FileMemoryManager(
        workspace,
        vector_engine=VectorRecallEngine(build_embedding_provider(config.embedding)),
    )
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{scenario['id']}: setup_vector_memory[{index}] must be an object")
        entry_id = str(entry.get("entry_id") or f"{scenario['id']}_seed_{index}")
        text = str(entry.get("text") or "")
        if not text:
            raise ValueError(f"{scenario['id']}: setup_vector_memory[{index}] requires text")
        memory.write_vector_entry(
            entry_id=entry_id,
            text=text,
            source_type=str(entry.get("source_type") or "eval_seed"),
            metadata=dict(entry.get("metadata") or {}),
        )


def _base_env(run_dir: Path, workspace: Path, scenario: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = str(REPO_ROOT / "src")
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    env["OMNIAGENT_SQLITE_PATH"] = str(run_dir / "data" / f"{scenario['id']}.sqlite3")
    env["OMNIAGENT_LOG_PATH"] = str(run_dir / "logs" / f"{scenario['id']}.log")
    env.setdefault("OMNIAGENT_LLM_VERIFY_ON_STARTUP", "false")
    authorized = [str(workspace.resolve())]
    existing_auth = env.get("OMNIAGENT_SANDBOX_AUTHORIZED_PATHS")
    if existing_auth:
        authorized.append(existing_auth)
    env["OMNIAGENT_SANDBOX_AUTHORIZED_PATHS"] = ",".join(authorized)
    return env


def _append_common_args(cmd: list[str], scenario: dict[str, Any]) -> None:
    if scenario.get("expected_output"):
        cmd.extend(["--expected-output", str(scenario["expected_output"])])
    if scenario.get("executor"):
        cmd.extend(["--executor", str(scenario["executor"])])
    if scenario.get("skill"):
        cmd.extend(["--skill", str(scenario["skill"])])
    for skill in scenario.get("preferred_skills", []):
        cmd.extend(["--preferred-skill", str(skill)])
    if scenario.get("payload") is not None:
        cmd.extend(["--payload", json.dumps(scenario["payload"], ensure_ascii=False)])
    cmd.extend(["--max-permission", str(scenario.get("max_permission", "L2"))])
    if scenario.get("allow_network"):
        cmd.append("--allow-network")
    if scenario.get("allow_delete"):
        cmd.append("--allow-delete")
    if scenario.get("timeout"):
        cmd.extend(["--timeout", str(int(scenario["timeout"]))])
    if scenario.get("mode"):
        cmd.extend(["--mode", str(scenario["mode"])])
    cmd.append("--json")


def _render_sequence_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for name, replacement in variables.items():
            rendered = rendered.replace("${" + name + "}", str(replacement))
        return rendered
    if isinstance(value, list):
        return [_render_sequence_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_sequence_value(item, variables) for key, item in value.items()}
    return value


def _json_path_value(payload: Any, path: str) -> Any:
    current = payload
    for part in str(path).split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _capture_sequence_values(
    *, execution: dict[str, Any], step: dict[str, Any], variables: dict[str, Any]
) -> None:
    captures = step.get("capture")
    if not isinstance(captures, dict):
        return
    payload = execution.get("json")
    for name, path in captures.items():
        value = _json_path_value(payload, str(path))
        if value is not None:
            variables[str(name)] = value


def _run_command(
    *,
    cmd: list[str],
    env: dict[str, str],
    timeout_seconds: int,
    cwd: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        duration = round(time.monotonic() - started, 3)
        payload, parse_error = _parse_json_stdout(completed.stdout)
        return {
            "command": cmd,
            "exit_code": completed.returncode,
            "duration_seconds": duration,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "json": payload,
            "json_error": parse_error,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - started, 3)
        return {
            "command": cmd,
            "exit_code": 124,
            "duration_seconds": duration,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "json": None,
            "json_error": "process timed out",
            "timed_out": True,
        }


def _extract_task_ids(payload: Any) -> list[str]:
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            task_id = value.get("task_id")
            if isinstance(task_id, str) and task_id.startswith("task_") and task_id not in found:
                found.append(task_id)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return found


def _extract_thread_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    thread = payload.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("thread_id"), str):
        return thread["thread_id"]
    thread_id = payload.get("thread_id")
    return thread_id if isinstance(thread_id, str) else None


def _short_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_turn_summaries(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for index, execution in enumerate(executions, start=1):
        payload = execution.get("json")
        route = payload.get("route") if isinstance(payload, dict) else None
        if not isinstance(route, dict):
            route = {}
        task_report = payload.get("task_report") if isinstance(payload, dict) else None
        if not isinstance(task_report, dict):
            task_report = payload if isinstance(payload, dict) else {}
        task = task_report.get("task") if isinstance(task_report.get("task"), dict) else {}
        result = task_report.get("result") if isinstance(task_report.get("result"), dict) else {}
        thread_id = _extract_thread_id(payload)
        turns.append(
            {
                "turn": index,
                "exit_code": execution.get("exit_code"),
                "duration_seconds": execution.get("duration_seconds"),
                "json_error": execution.get("json_error"),
                "thread_id": thread_id,
                "route_input_type": route.get("input_type"),
                "route_source": route.get("metadata", {}).get("route_source")
                if isinstance(route.get("metadata"), dict)
                else None,
                "route_action": route.get("task_action"),
                "route_confidence": route.get("confidence"),
                "route_reasoning": route.get("reasoning"),
                "task_id": task.get("task_id"),
                "task_status": task.get("status"),
                "task_plan_summary": task.get("plan_summary"),
                "result_success": result.get("success"),
                "result_status": result.get("status"),
                "assistant_message": _short_text(payload.get("assistant_message"))
                if isinstance(payload, dict)
                else None,
            }
        )
    return turns


def _task_success(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("result"), dict):
        return bool(payload["result"].get("success"))
    report = payload.get("task_report")
    if isinstance(report, dict) and isinstance(report.get("result"), dict):
        return bool(report["result"].get("success"))
    return False


def _execution_success(execution: dict[str, Any]) -> bool:
    payload = execution.get("json")
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("result"), dict) or isinstance(payload.get("task_report"), dict):
        return _task_success(payload)
    return int(execution.get("exit_code") or 0) == 0


def _collect_evidence(
    workspace: Path,
    task_ids: list[str],
    thread_ids: list[str],
    executions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "task_ids": task_ids,
        "thread_ids": thread_ids,
        "turns": _extract_turn_summaries(executions or []),
        "used_skills": [],
        "permissions": [],
        "node_statuses": [],
        "critic_actions": [],
        "completion_evaluations": [],
        "request_snapshots": [],
        "memory_summaries": [],
        "workflow_matches": [],
        "vector_matches": [],
        "vector_memory_entries": [],
        "policy_decisions": [],
        "request_payload_stats": [],
        "llm_calls": [],
        "task_intake_assessments": [],
        "experience_records": [],
        "learning_feedbacks": [],
        "learning_corrections": [],
    }
    used_skills: set[str] = set()

    for task_id in task_ids:
        task_dir = workspace / "tasks" / task_id
        request_path = task_dir / "request.json"
        if request_path.exists():
            request_payload = _load_json(request_path)
            if isinstance(request_payload, dict):
                evidence["request_snapshots"].append(
                    {
                        "task_id": task_id,
                        "goal": request_payload.get("goal"),
                        "constraints": request_payload.get("constraints", {}),
                        "metadata": request_payload.get("metadata", {}),
                        "executor": request_payload.get("executor"),
                        "skill_name": request_payload.get("skill_name"),
                    }
                )

        policy_path = task_dir / "plan" / "policy_decision.json"
        if policy_path.exists():
            policy_payload = _load_json(policy_path)
            if isinstance(policy_payload, dict):
                evidence["policy_decisions"].append(
                    {
                        "task_id": task_id,
                        "policy_decision": policy_payload.get("policy_decision"),
                        "reason": policy_payload.get("reason"),
                        "skipped_stages": policy_payload.get("skipped_stages", []),
                        "fallback": policy_payload.get("fallback"),
                        "risk_flags": policy_payload.get("risk_flags", {}),
                        "context_budget": policy_payload.get("context_budget", {}),
                    }
                )

        intake_path = task_dir / "plan" / "intake_assessment.json"
        if intake_path.exists():
            intake_payload = _load_json(intake_path)
            if isinstance(intake_payload, dict):
                evidence["task_intake_assessments"].append(
                    _summarize_task_intake_assessment(task_id, intake_payload)
                )

        graph_path = task_dir / "plan" / "final_task_graph.json"
        if not graph_path.exists():
            graph_path = task_dir / "plan" / "task_graph.json"
        if graph_path.exists():
            graph = _load_json(graph_path)
            for node in graph.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                skill_name = node.get("skill_name")
                if isinstance(skill_name, str) and skill_name and skill_name != "null":
                    used_skills.add(skill_name)
                metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
                selected = metadata.get("selected_skill")
                if isinstance(selected, str) and selected:
                    used_skills.add(selected)
                permission = metadata.get("permission_context")
                if isinstance(permission, dict):
                    evidence["permissions"].append(
                        {
                            "task_id": task_id,
                            "node_id": node.get("node_id"),
                            "permission": permission.get("permission"),
                            "status": permission.get("status"),
                            "level": permission.get("level"),
                            "resource_ref": permission.get("resource_ref"),
                            "resource_kind": permission.get("resource_kind"),
                        }
                    )
                input_payload = node.get("input_payload")
                if not isinstance(input_payload, dict):
                    input_payload = {}
                evidence["node_statuses"].append(
                    {
                        "task_id": task_id,
                        "node_id": node.get("node_id"),
                        "type": node.get("type"),
                        "status": node.get("status"),
                        "executor_hint": node.get("executor_hint"),
                        "skill_name": skill_name,
                        "selected_skill": selected,
                        "action": input_payload.get("action"),
                        "attempts": node.get("attempts"),
                        "error_message": node.get("error_message"),
                    }
                )

        verdicts_path = task_dir / "critic" / "verdicts.json"
        if verdicts_path.exists():
            verdicts = _load_json(verdicts_path)
            for verdict in verdicts.get("verdicts", []):
                evidence["critic_actions"].append(
                    {
                        "task_id": task_id,
                        "target_id": verdict.get("target_id"),
                        "action": verdict.get("action"),
                        "summary": verdict.get("summary"),
                    }
                )

        completion_path = task_dir / "completion" / "evaluation.json"
        if completion_path.exists():
            completion = _load_json(completion_path)
            if isinstance(completion, dict):
                evidence["completion_evaluations"].append(
                    _summarize_completion_evaluation(task_id, completion)
                )

        experience_path = task_dir / "experience" / "experience_record.json"
        if experience_path.exists():
            experience = _load_json(experience_path)
            if isinstance(experience, dict):
                evidence["experience_records"].append(
                    _summarize_experience_record(task_id, experience)
                )

        feedback_path = task_dir / "learning" / "feedback.json"
        if feedback_path.exists():
            feedback = _load_json(feedback_path)
            if isinstance(feedback, dict):
                evidence["learning_feedbacks"].append(
                    _summarize_learning_feedback(task_id, feedback)
                )

        correction_path = task_dir / "learning" / "correction.json"
        if correction_path.exists():
            correction = _load_json(correction_path)
            if isinstance(correction, dict):
                evidence["learning_corrections"].append(
                    _summarize_learning_correction(task_id, correction)
                )

        for call_path in sorted((task_dir / "llm_calls").glob("*.json")):
            call = _load_json(call_path)
            usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
            evidence["llm_calls"].append(
                {
                    "scope": "task",
                    "task_id": task_id,
                    "prompt_name": call.get("prompt_name"),
                    "model": call.get("model"),
                    "total_tokens": usage.get("total_tokens", 0),
                    "latency_ms": call.get("latency_ms"),
                    "tier": call.get("tier"),
                }
            )
            request_payload = call.get("request_payload")
            if isinstance(request_payload, dict):
                for summary in request_payload.get("memory_summaries", []):
                    if isinstance(summary, dict):
                        evidence["memory_summaries"].append(
                            _summarize_memory_summary(task_id, summary)
                        )
                for match in request_payload.get("workflow_matches", []):
                    if isinstance(match, dict):
                        evidence["workflow_matches"].append(
                            _summarize_workflow_match(task_id, match)
                        )
                for match in request_payload.get("vector_matches", []):
                    if isinstance(match, dict):
                        evidence["vector_matches"].append(_summarize_vector_match(task_id, match))
            stats = call.get("request_payload_stats")
            if isinstance(stats, dict):
                evidence["request_payload_stats"].append(
                    {
                        "scope": "task",
                        "task_id": task_id,
                        "prompt_name": call.get("prompt_name"),
                        "json_chars": stats.get("json_chars"),
                        "fields": stats.get("fields", {}),
                    }
                )

    for thread_id in thread_ids:
        thread_dir = workspace / "threads" / thread_id
        for call_path in sorted((thread_dir / "llm_calls").glob("*.json")):
            call = _load_json(call_path)
            usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
            evidence["llm_calls"].append(
                {
                    "scope": "thread",
                    "thread_id": thread_id,
                    "prompt_name": call.get("prompt_name"),
                    "model": call.get("model"),
                    "total_tokens": usage.get("total_tokens", 0),
                    "latency_ms": call.get("latency_ms"),
                    "tier": call.get("tier"),
                }
            )
            stats = call.get("request_payload_stats")
            if isinstance(stats, dict):
                evidence["request_payload_stats"].append(
                    {
                        "scope": "thread",
                        "thread_id": thread_id,
                        "prompt_name": call.get("prompt_name"),
                        "json_chars": stats.get("json_chars"),
                        "fields": stats.get("fields", {}),
                    }
                )

    evidence["used_skills"] = sorted(used_skills)
    evidence["vector_memory_entries"] = _collect_vector_memory_entries(workspace)
    evidence["embedding_summary"] = _build_embedding_summary(evidence)
    return evidence


def _summarize_completion_evaluation(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_satisfied": payload.get("task_satisfied"),
        "completion_score": payload.get("completion_score"),
        "primary_failure_layer": payload.get("primary_failure_layer"),
        "model_capability_suspected": payload.get("model_capability_suspected"),
        "recommended_next_action": payload.get("recommended_next_action"),
        "confidence": payload.get("confidence"),
        "summary": payload.get("summary"),
        "missing_requirements": payload.get("missing_requirements", []),
        "unexpected_side_effects": payload.get("unexpected_side_effects", []),
        "evidence": payload.get("evidence", []),
    }


def _summarize_learning_feedback(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    experience = payload.get("experience") if isinstance(payload.get("experience"), dict) else {}
    procedure = payload.get("procedure") if isinstance(payload.get("procedure"), dict) else {}
    habit = (
        payload.get("habit_fast_path") if isinstance(payload.get("habit_fast_path"), dict) else {}
    )
    reuse = payload.get("reuse") if isinstance(payload.get("reuse"), dict) else {}
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    return {
        "task_id": task_id,
        "schema_version": payload.get("schema_version"),
        "experience_id": experience.get("experience_id"),
        "experience_status": experience.get("status"),
        "validation_verdict": experience.get("validation_verdict"),
        "learnable": experience.get("learnable"),
        "matched_procedure_id": procedure.get("matched_id"),
        "generated_draft_id": procedure.get("generated_draft_id"),
        "matched_object_type": procedure.get("matched_object_type"),
        "habit_fast_path_decision": habit.get("decision"),
        "reuse_status": reuse.get("status"),
        "reusable_now": reuse.get("reusable_now"),
        "review_required": review.get("required"),
        "review_target_type": review.get("target_type"),
        "review_target_id": review.get("target_id"),
        "summary_line_count": len(payload.get("summary_lines") or []),
    }


def _summarize_learning_correction(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "schema_version": payload.get("schema_version"),
        "experience_id": payload.get("experience_id"),
        "procedure_id": payload.get("procedure_id"),
        "object_type": payload.get("object_type"),
        "trigger": payload.get("trigger"),
        "previous_status": payload.get("previous_status"),
        "new_status": payload.get("new_status"),
        "previous_review_status": payload.get("previous_review_status"),
        "new_review_status": payload.get("new_review_status"),
        "failure_count": payload.get("failure_count"),
        "frozen": payload.get("frozen"),
        "evidence_refs": _string_values(payload.get("evidence_refs")),
        "reason": payload.get("reason"),
        "next_action_count": len(payload.get("next_actions") or []),
    }


def _summarize_task_intake_assessment(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "schema_version": payload.get("schema_version"),
        "task_type": payload.get("task_type"),
        "task_type_signals": _string_values(payload.get("task_type_signals")),
        "risk_level": payload.get("risk_level"),
        "risk_reasons": _string_values(payload.get("risk_reasons")),
        "tools": _string_values(payload.get("tools")),
        "validation_strategy": payload.get("validation_strategy"),
        "validation_targets": _string_values(payload.get("validation_targets")),
        "confirmation_required": payload.get("confirmation_required"),
        "confirmation_reasons": _string_values(payload.get("confirmation_reasons")),
    }


def _summarize_experience_record(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    return {
        "task_id": task_id,
        "schema_version": payload.get("schema_version"),
        "experience_id": payload.get("experience_id"),
        "status": payload.get("status"),
        "validation_verdict": validation.get("verdict"),
        "failure_layer": failure.get("failure_layer"),
        "error_code": failure.get("error_code"),
        "permission": failure.get("permission"),
        "learnable": payload.get("learnable"),
    }


def _summarize_vector_match(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    details = payload.get("match_details") if isinstance(payload.get("match_details"), dict) else {}
    return {
        "task_id": task_id,
        "entry_id": payload.get("entry_id"),
        "source_type": payload.get("source_type"),
        "score": payload.get("score"),
        "raw_embedding_present": "embedding" in payload,
        "embedding_provider": payload.get("embedding_provider")
        or details.get("stored_embedding_provider"),
        "embedding_model": payload.get("embedding_model") or details.get("stored_embedding_model"),
        "embedding_dimensions": payload.get("embedding_dimensions")
        or details.get("stored_embedding_dimensions"),
        "embedding_version": payload.get("embedding_version")
        or details.get("stored_embedding_version"),
        "embedding_model_fingerprint": payload.get("embedding_model_fingerprint")
        or details.get("stored_embedding_model_fingerprint"),
        "embedding_fallback_used": payload.get("embedding_fallback_used"),
        "embedding_error": payload.get("embedding_error") or details.get("embedding_error"),
        "vector_compatible": details.get("vector_compatible"),
        "vector_score": details.get("vector_score"),
        "matched_concepts": details.get("matched_concepts", []),
    }


def _summarize_workflow_match(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "matched_task_id": payload.get("task_id"),
        "goal": payload.get("goal"),
        "summary": payload.get("summary") or payload.get("plan_summary"),
        "score": payload.get("score"),
        "sources": payload.get("sources", []),
    }


def _summarize_memory_summary(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "matched_task_id": payload.get("task_id"),
        "status": payload.get("status"),
        "capability": payload.get("capability"),
        "memory_tiers": payload.get("memory_tiers", []),
    }


def _collect_vector_memory_entries(workspace: Path, limit: int = 20) -> list[dict[str, Any]]:
    vector_root = workspace / "MEMORY" / "vector"
    entries: list[dict[str, Any]] = []
    if not vector_root.exists():
        return entries
    for path in sorted(vector_root.glob("*.json"))[:limit]:
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        embedding = payload.get("embedding")
        dimensions = payload.get("embedding_dimensions")
        if dimensions is None and isinstance(embedding, list):
            dimensions = len(embedding)
        entries.append(
            {
                "entry_id": payload.get("entry_id") or path.stem,
                "source_type": payload.get("source_type"),
                "metadata": payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {},
                "embedding_provider": payload.get("embedding_provider") or "unknown",
                "embedding_model": payload.get("embedding_model") or "unknown",
                "embedding_dimensions": dimensions,
                "embedding_version": payload.get("embedding_version") or "unknown",
                "embedding_model_fingerprint": payload.get("embedding_model_fingerprint"),
                "embedding_fallback_used": payload.get("embedding_fallback_used"),
                "embedding_error": payload.get("embedding_error"),
                "created_at": payload.get("created_at"),
            }
        )
    return entries


def _build_embedding_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    items = list(evidence.get("vector_matches", [])) + list(
        evidence.get("vector_memory_entries", [])
    )
    providers = sorted(
        {str(item.get("embedding_provider")) for item in items if item.get("embedding_provider")}
    )
    models = sorted(
        {str(item.get("embedding_model")) for item in items if item.get("embedding_model")}
    )
    fallback_count = sum(1 for item in items if item.get("embedding_fallback_used"))
    error_count = sum(1 for item in items if item.get("embedding_error"))
    incompatible_count = sum(
        1 for item in evidence.get("vector_matches", []) if item.get("vector_compatible") is False
    )
    return {
        "providers": providers,
        "models": models,
        "vector_match_count": len(evidence.get("vector_matches", [])),
        "vector_memory_entry_count": len(evidence.get("vector_memory_entries", [])),
        "fallback_count": fallback_count,
        "error_count": error_count,
        "incompatible_match_count": incompatible_count,
    }


def _failure_layer(diagnosis: str, passed: bool = False) -> str:
    if passed:
        return "none"
    return {
        "passed": "none",
        "runtime_timeout": "infrastructure",
        "cli_or_infrastructure": "infrastructure",
        "router_or_interaction_dispatch": "routing",
        "planner_or_skill_selection": "planning",
        "permission_guardrail": "permission",
        "permission_detection": "permission",
        "permission_policy_or_scenario_config": "permission",
        "executor_or_skill_contract": "skill_or_tool",
        "task_outcome_mismatch": "task_flow_or_output",
        "agent_flow": "task_flow_or_output",
        "memory_retrieval": "memory_retrieval",
        "completion_evaluation": "completion_evaluation",
        "model_capability": "model_capability",
        "test_expectation": "assertion",
        "assertion_failure": "assertion",
    }.get(diagnosis, "unknown")


def _fix_entrypoint(diagnosis: str, failure_layer: str) -> str:
    if failure_layer == "routing":
        return "先检查 interaction router 的规则层和 LLM fallback，确认用户意图是否被派发到正确处理路径。"
    if failure_layer == "planning":
        return "先检查 planner prompt、skill manifest/contract 暴露和 required/preferred skill 选择信号。"
    if failure_layer == "permission":
        return "先检查权限推断、contract side_effects、场景 max_permission/allow_* 配置和审批 checkpoint。"
    if failure_layer == "skill_or_tool":
        return "先检查失败节点的 skill contract、payload 校验、executor 分派和工具实现。"
    if failure_layer == "task_flow_or_output":
        return "先核对计划是否覆盖目标产物，再检查节点输出、文件写入和断言是否表达了真实目标。"
    if failure_layer == "memory_retrieval":
        return "先检查 vector_matches、embedding provider/model/dimensions/fingerprint、fallback 和召回是否命中正确历史。"
    if failure_layer == "completion_evaluation":
        return "先检查 completion/evaluation.json 的证据是否充分，再核对 evaluator prompt、输入上下文和评分阈值。"
    if failure_layer == "model_capability":
        return "先确认 Planner/工具/召回/完成度评估上下文都充分，再记录模型能力边界，不要急着改 Agent 流程。"
    if failure_layer == "assertion":
        return "先判断断言是否过窄或场景描述不完整，再决定是否修代码、prompt 或调整评测定义。"
    if failure_layer == "infrastructure":
        return "先检查 provider 配置、网络、CLI JSON 输出、超时和本地环境变量。"
    if diagnosis == "passed":
        return "无需修复；保留本报告作为回归证据。"
    return "从 summary.md、result.json、turn 输出和 workspace 产物开始定位。"


def _build_failure_attribution(
    *,
    diagnosis: str,
    passed: bool,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    completion_layers = [
        item.get("primary_failure_layer")
        for item in evidence.get("completion_evaluations", [])
        if item.get("primary_failure_layer") not in {None, "", "none"}
    ]
    model_suspected = any(
        bool(item.get("model_capability_suspected"))
        for item in evidence.get("completion_evaluations", [])
    )
    embedding_summary = evidence.get("embedding_summary", {})
    return {
        "primary_layer": _failure_layer(diagnosis, passed=passed),
        "diagnosis": diagnosis,
        "completion_failure_layers": completion_layers,
        "model_capability_suspected": model_suspected,
        "embedding_providers": embedding_summary.get("providers", []),
        "embedding_models": embedding_summary.get("models", []),
        "embedding_fallback_count": embedding_summary.get("fallback_count", 0),
        "embedding_error_count": embedding_summary.get("error_count", 0),
        "embedding_incompatible_match_count": embedding_summary.get("incompatible_match_count", 0),
    }


def _collect_workspace_snapshot(workspace: Path, limit: int = 80) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if not workspace.exists():
        return files
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if any(part in WORKSPACE_SNAPSHOT_SKIP_DIRS for part in relative.parts):
            continue
        if path.name in WORKSPACE_SNAPSHOT_SKIP_FILES:
            continue
        item: dict[str, Any] = {
            "path": str(relative),
            "size": path.stat().st_size,
        }
        if path.suffix.lower() in TEXT_PREVIEW_SUFFIXES and item["size"] <= 8192:
            try:
                item["preview"] = _short_text(
                    path.read_text(encoding="utf-8", errors="replace"), 600
                )
            except OSError as exc:
                item["preview_error"] = str(exc)
        files.append(item)
        if len(files) >= limit:
            break
    return files


def _workspace_file_paths(workspace: Path) -> list[str]:
    return [str(item["path"]) for item in _collect_workspace_snapshot(workspace, limit=10_000)]


def _assistant_messages(executions: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for execution in executions:
        payload = execution.get("json")
        if not isinstance(payload, dict):
            continue
        message = payload.get("assistant_message")
        if isinstance(message, str) and message:
            messages.append(message)
    return messages


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        escaped = [str(value if value is not None else "-").replace("|", "\\|") for value in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return lines


def _write_case_diagnostics(
    *,
    case_dir: Path,
    scenario: dict[str, Any],
    result: dict[str, Any],
    executions: list[dict[str, Any]],
    evidence: dict[str, Any],
    workspace: Path,
) -> None:
    workspace_files = _collect_workspace_snapshot(workspace)
    result["workspace_files"] = workspace_files
    failure_layer = str(result.get("failure_layer", "unknown"))
    diagnosis = str(result.get("diagnosis", "unknown"))
    status = str(result.get("status", "unknown"))
    lines: list[str] = [
        f"# Eval Diagnostics: {scenario['id']}",
        "",
        "## Outcome",
        "",
        f"- status: `{status}`",
        f"- diagnosis: `{diagnosis}`",
        f"- failure_layer: `{failure_layer}`",
        f"- category: `{scenario.get('category', 'uncategorized')}`",
        f"- kind: `{scenario.get('kind', 'task')}`",
        f"- duration_seconds: `{result.get('duration_seconds', 0)}`",
        f"- workspace: `{workspace}`",
        "",
        "## Repair Entry Point",
        "",
        _fix_entrypoint(diagnosis, failure_layer),
        "",
        "## Goal",
        "",
        str(scenario.get("goal") or scenario.get("description") or "(chat sequence)"),
        "",
    ]
    if scenario.get("turns"):
        lines.extend(["## Scenario Turns", ""])
        for index, turn in enumerate(scenario.get("turns", []), start=1):
            lines.append(f"{index}. {_short_text(turn.get('message'), 500)}")
        lines.append("")

    if result.get("issues"):
        lines.extend(["## Issues", ""])
        lines.extend(f"- {issue}" for issue in result.get("issues", []))
        lines.append("")

    turn_rows = [
        [
            turn.get("turn"),
            turn.get("exit_code"),
            turn.get("duration_seconds"),
            turn.get("route_input_type"),
            turn.get("route_source"),
            turn.get("route_action"),
            turn.get("task_id"),
            turn.get("task_status") or turn.get("result_status"),
        ]
        for turn in evidence.get("turns", [])
    ]
    if turn_rows:
        lines.extend(["## Turn Trace", ""])
        lines.extend(
            _markdown_table(
                [
                    "Turn",
                    "Exit",
                    "Seconds",
                    "Route",
                    "Source",
                    "Action",
                    "Task",
                    "Status",
                ],
                turn_rows,
            )
        )
        lines.append("")
        for turn in evidence.get("turns", []):
            if turn.get("route_reasoning") or turn.get("assistant_message"):
                lines.extend(
                    [
                        f"- turn {turn.get('turn')} route_reasoning: "
                        f"{_short_text(turn.get('route_reasoning'), 260)}",
                        f"- turn {turn.get('turn')} assistant_message: "
                        f"{_short_text(turn.get('assistant_message'), 260)}",
                    ]
                )
        lines.append("")

    command_rows = [
        [
            index,
            execution.get("exit_code"),
            execution.get("duration_seconds"),
            "yes" if execution.get("json") is not None else "no",
            execution.get("json_error") or "-",
            f"turn_{index:02d}_stdout.txt",
            f"turn_{index:02d}_stderr.txt",
        ]
        for index, execution in enumerate(executions, start=1)
    ]
    lines.extend(["## Command Trace", ""])
    lines.extend(
        _markdown_table(
            ["Turn", "Exit", "Seconds", "JSON", "JSON Error", "Stdout", "Stderr"],
            command_rows,
        )
    )
    lines.append("")

    if evidence.get("used_skills"):
        lines.extend(
            ["## Skill Usage", "", ", ".join(f"`{item}`" for item in evidence["used_skills"]), ""]
        )

    if evidence.get("policy_decisions"):
        lines.extend(["## Execution Policy", ""])
        rows: list[list[Any]] = []
        for item in evidence.get("policy_decisions", []):
            risk_flags = item.get("risk_flags") if isinstance(item.get("risk_flags"), dict) else {}
            enabled_risks = (
                ", ".join(name for name, enabled in risk_flags.items() if enabled) or "-"
            )
            rows.append(
                [
                    item.get("task_id"),
                    item.get("policy_decision"),
                    ", ".join(item.get("skipped_stages", []) or []) or "-",
                    item.get("fallback"),
                    enabled_risks,
                    _short_text(item.get("reason"), 120),
                ]
            )
        lines.extend(
            _markdown_table(
                ["Task", "Decision", "Skipped", "Fallback", "Risk Flags", "Reason"],
                rows,
            )
        )
        lines.append("")

    if evidence.get("task_intake_assessments"):
        lines.extend(["## Task Intake Assessment", ""])
        lines.extend(
            _markdown_table(
                [
                    "Task",
                    "Type",
                    "Risk",
                    "Tools",
                    "Validation",
                    "Confirm",
                    "Reasons",
                ],
                [
                    [
                        item.get("task_id"),
                        item.get("task_type"),
                        item.get("risk_level"),
                        ", ".join(item.get("tools") or []) or "-",
                        item.get("validation_strategy"),
                        item.get("confirmation_required"),
                        ", ".join(item.get("confirmation_reasons") or []) or "-",
                    ]
                    for item in evidence.get("task_intake_assessments", [])
                ],
            )
        )
        lines.append("")

    if evidence.get("experience_records"):
        lines.extend(["## Experience Records", ""])
        lines.extend(
            _markdown_table(
                [
                    "Task",
                    "Experience",
                    "Status",
                    "Validation",
                    "Failure Layer",
                    "Error",
                    "Permission",
                ],
                [
                    [
                        item.get("task_id"),
                        item.get("experience_id"),
                        item.get("status"),
                        item.get("validation_verdict"),
                        item.get("failure_layer"),
                        item.get("error_code"),
                        item.get("permission") or "-",
                    ]
                    for item in evidence.get("experience_records", [])
                ],
            )
        )
        lines.append("")

    if evidence.get("request_payload_stats"):
        lines.extend(["## Request Payload Stats", ""])
        rows: list[list[Any]] = []
        for item in evidence.get("request_payload_stats", []):
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            rows.append(
                [
                    item.get("prompt_name"),
                    item.get("task_id"),
                    item.get("json_chars"),
                    fields.get("memory_summaries", {}).get("json_chars")
                    if isinstance(fields.get("memory_summaries"), dict)
                    else None,
                    fields.get("workflow_matches", {}).get("json_chars")
                    if isinstance(fields.get("workflow_matches"), dict)
                    else None,
                    fields.get("vector_matches", {}).get("json_chars")
                    if isinstance(fields.get("vector_matches"), dict)
                    else None,
                ]
            )
        lines.extend(
            _markdown_table(
                [
                    "Prompt",
                    "Task",
                    "Total Chars",
                    "Memory Chars",
                    "Workflow Chars",
                    "Vector Chars",
                ],
                rows,
            )
        )
        lines.append("")

    if evidence.get("workflow_matches"):
        lines.extend(["## Workflow Matches", ""])
        lines.extend(
            _markdown_table(
                ["Task", "Matched Task", "Score", "Sources", "Summary"],
                [
                    [
                        item.get("task_id"),
                        item.get("matched_task_id"),
                        item.get("score"),
                        item.get("sources"),
                        _short_text(item.get("summary"), 140),
                    ]
                    for item in evidence.get("workflow_matches", [])
                ],
            )
        )
        lines.append("")

    if evidence.get("permissions"):
        lines.extend(["## Permissions", ""])
        lines.extend(
            _markdown_table(
                ["Task", "Node", "Permission", "Status", "Level", "Resource"],
                [
                    [
                        item.get("task_id"),
                        item.get("node_id"),
                        item.get("permission"),
                        item.get("status"),
                        item.get("level"),
                        item.get("resource_ref"),
                    ]
                    for item in evidence.get("permissions", [])
                ],
            )
        )
        lines.append("")

    if evidence.get("node_statuses"):
        lines.extend(["## Node Statuses", ""])
        lines.extend(
            _markdown_table(
                ["Task", "Node", "Type", "Status", "Skill", "Action", "Attempts", "Error"],
                [
                    [
                        item.get("task_id"),
                        item.get("node_id"),
                        item.get("type"),
                        item.get("status"),
                        item.get("skill_name") or item.get("selected_skill"),
                        item.get("action"),
                        item.get("attempts"),
                        _short_text(item.get("error_message"), 120),
                    ]
                    for item in evidence.get("node_statuses", [])
                ],
            )
        )
        lines.append("")

    if evidence.get("critic_actions"):
        lines.extend(["## Critic Actions", ""])
        lines.extend(
            _markdown_table(
                ["Task", "Target", "Action", "Summary"],
                [
                    [
                        item.get("task_id"),
                        item.get("target_id"),
                        item.get("action"),
                        _short_text(item.get("summary"), 140),
                    ]
                    for item in evidence.get("critic_actions", [])
                ],
            )
        )
        lines.append("")

    failure_attribution = result.get("failure_attribution")
    if isinstance(failure_attribution, dict):
        lines.extend(
            [
                "## Failure Attribution",
                "",
                f"- primary_layer: `{failure_attribution.get('primary_layer')}`",
                f"- completion_failure_layers: `{failure_attribution.get('completion_failure_layers') or []}`",
                f"- model_capability_suspected: `{failure_attribution.get('model_capability_suspected')}`",
                f"- embedding_providers: `{failure_attribution.get('embedding_providers') or []}`",
                f"- embedding_models: `{failure_attribution.get('embedding_models') or []}`",
                f"- embedding_fallback_count: `{failure_attribution.get('embedding_fallback_count', 0)}`",
                f"- embedding_error_count: `{failure_attribution.get('embedding_error_count', 0)}`",
                f"- embedding_incompatible_match_count: `{failure_attribution.get('embedding_incompatible_match_count', 0)}`",
                "",
            ]
        )

    if evidence.get("completion_evaluations"):
        lines.extend(["## Completion Evaluation", ""])
        lines.extend(
            _markdown_table(
                [
                    "Task",
                    "Satisfied",
                    "Score",
                    "Failure Layer",
                    "Model Suspected",
                    "Confidence",
                    "Summary",
                ],
                [
                    [
                        item.get("task_id"),
                        item.get("task_satisfied"),
                        item.get("completion_score"),
                        item.get("primary_failure_layer"),
                        item.get("model_capability_suspected"),
                        item.get("confidence"),
                        _short_text(item.get("summary"), 160),
                    ]
                    for item in evidence.get("completion_evaluations", [])
                ],
            )
        )
        for item in evidence.get("completion_evaluations", []):
            missing = item.get("missing_requirements") or []
            eval_evidence = item.get("evidence") or []
            if missing:
                lines.append(
                    f"- {item.get('task_id')} missing_requirements: "
                    f"{_short_text('; '.join(str(value) for value in missing), 240)}"
                )
            if eval_evidence:
                lines.append(
                    f"- {item.get('task_id')} evidence: "
                    f"{_short_text('; '.join(str(value) for value in eval_evidence), 240)}"
                )
        lines.append("")

    if evidence.get("learning_feedbacks"):
        lines.extend(["## Learning Feedback", ""])
        lines.extend(
            _markdown_table(
                [
                    "Task",
                    "Schema",
                    "Experience",
                    "Validation",
                    "Reuse",
                    "Reusable",
                    "Generated",
                    "Matched",
                    "Fast Path",
                    "Review Target",
                ],
                [
                    [
                        item.get("task_id"),
                        item.get("schema_version"),
                        item.get("experience_id"),
                        item.get("validation_verdict"),
                        item.get("reuse_status"),
                        item.get("reusable_now"),
                        item.get("generated_draft_id"),
                        item.get("matched_procedure_id"),
                        item.get("habit_fast_path_decision"),
                        item.get("review_target_type"),
                    ]
                    for item in evidence.get("learning_feedbacks", [])
                ],
            )
        )
        lines.append("")

    if evidence.get("learning_corrections"):
        lines.extend(["## Learning Correction", ""])
        lines.extend(
            _markdown_table(
                [
                    "Task",
                    "Schema",
                    "Procedure",
                    "Type",
                    "Trigger",
                    "Previous",
                    "New",
                    "Review",
                    "Failures",
                    "Frozen",
                    "Reason",
                ],
                [
                    [
                        item.get("task_id"),
                        item.get("schema_version"),
                        item.get("procedure_id"),
                        item.get("object_type"),
                        item.get("trigger"),
                        item.get("previous_status"),
                        item.get("new_status"),
                        item.get("new_review_status"),
                        item.get("failure_count"),
                        item.get("frozen"),
                        _short_text(item.get("reason"), 120),
                    ]
                    for item in evidence.get("learning_corrections", [])
                ],
            )
        )
        lines.append("")

    embedding_summary = (
        evidence.get("embedding_summary")
        if isinstance(evidence.get("embedding_summary"), dict)
        else {}
    )
    if embedding_summary or evidence.get("vector_matches") or evidence.get("vector_memory_entries"):
        lines.extend(
            [
                "## Embedding And Vector Recall",
                "",
                f"- providers: `{embedding_summary.get('providers', [])}`",
                f"- models: `{embedding_summary.get('models', [])}`",
                f"- vector_match_count: `{embedding_summary.get('vector_match_count', 0)}`",
                f"- vector_memory_entry_count: `{embedding_summary.get('vector_memory_entry_count', 0)}`",
                f"- fallback_count: `{embedding_summary.get('fallback_count', 0)}`",
                f"- error_count: `{embedding_summary.get('error_count', 0)}`",
                f"- incompatible_match_count: `{embedding_summary.get('incompatible_match_count', 0)}`",
                "",
            ]
        )
        if evidence.get("vector_matches"):
            lines.extend(["### Vector Matches", ""])
            lines.extend(
                _markdown_table(
                    [
                        "Task",
                        "Entry",
                        "Score",
                        "Provider",
                        "Model",
                        "Dims",
                        "Compatible",
                        "Vector Score",
                        "Fallback",
                        "Error",
                    ],
                    [
                        [
                            item.get("task_id"),
                            item.get("entry_id"),
                            item.get("score"),
                            item.get("embedding_provider"),
                            item.get("embedding_model"),
                            item.get("embedding_dimensions"),
                            item.get("vector_compatible"),
                            item.get("vector_score"),
                            item.get("embedding_fallback_used"),
                            _short_text(item.get("embedding_error"), 100),
                        ]
                        for item in evidence.get("vector_matches", [])
                    ],
                )
            )
            lines.append("")
        if evidence.get("vector_memory_entries"):
            lines.extend(["### Vector Memory Entries", ""])
            lines.extend(
                _markdown_table(
                    [
                        "Entry",
                        "Source",
                        "Provider",
                        "Model",
                        "Dims",
                        "Version",
                        "Fingerprint",
                        "Fallback",
                        "Error",
                    ],
                    [
                        [
                            item.get("entry_id"),
                            item.get("source_type"),
                            item.get("embedding_provider"),
                            item.get("embedding_model"),
                            item.get("embedding_dimensions"),
                            item.get("embedding_version"),
                            item.get("embedding_model_fingerprint"),
                            item.get("embedding_fallback_used"),
                            _short_text(item.get("embedding_error"), 100),
                        ]
                        for item in evidence.get("vector_memory_entries", [])
                    ],
                )
            )
            lines.append("")

    if evidence.get("llm_calls"):
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in evidence["llm_calls"])
        lines.extend(
            [
                "## LLM Calls",
                "",
                f"- count: `{len(evidence['llm_calls'])}`",
                f"- total_tokens: `{total_tokens}`",
                "",
            ]
        )
        lines.extend(
            _markdown_table(
                ["Scope", "Prompt", "Model", "Tokens", "Latency Ms", "Task/Thread"],
                [
                    [
                        item.get("scope"),
                        item.get("prompt_name"),
                        item.get("model"),
                        item.get("total_tokens"),
                        item.get("latency_ms"),
                        item.get("task_id") or item.get("thread_id"),
                    ]
                    for item in evidence.get("llm_calls", [])
                ],
            )
        )
        lines.append("")

    lines.extend(["## Workspace Snapshot", ""])
    if workspace_files:
        for item in workspace_files:
            lines.append(f"- `{item['path']}` ({item['size']} bytes)")
            if item.get("preview"):
                lines.append(f"  - preview: `{item['preview']}`")
    else:
        lines.append("- no user-facing workspace files captured")
    lines.extend(
        [
            "",
            "## Raw Artifacts",
            "",
            "- `result.json`: normalized evidence and assertion result",
            "- `turn_XX_command.json`: exact CLI command",
            "- `turn_XX_stdout.txt` / `turn_XX_stderr.txt`: raw process output",
            "- `turn_XX_parsed.json`: parsed CLI JSON payload when available",
            "- workspace `tasks/<task_id>/`: planner, graph, executor, critic and LLM call artifacts",
            "- workspace `threads/<thread_id>/`: conversation-scoped responder LLM calls",
            "",
        ]
    )
    (case_dir / "diagnostics.md").write_text("\n".join(lines), encoding="utf-8")


def _assert_file_contains(workspace: Path, item: dict[str, Any], issues: list[str]) -> None:
    relative = str(item.get("path", ""))
    expected = str(item.get("text", ""))
    target = _safe_child(workspace, relative)
    if not target.exists():
        issues.append(f"missing file: {relative}")
        return
    content = target.read_text(encoding="utf-8", errors="replace")
    if expected not in content:
        issues.append(f"file {relative} does not contain expected text: {expected}")


def _assert_file_not_contains(workspace: Path, item: dict[str, Any], issues: list[str]) -> None:
    relative = str(item.get("path", ""))
    forbidden = str(item.get("text", ""))
    target = _safe_child(workspace, relative)
    if not target.exists():
        return
    content = target.read_text(encoding="utf-8", errors="replace")
    if forbidden in content:
        issues.append(f"file {relative} contains forbidden text: {forbidden}")


def _workspace_procedure_records(workspace: Path) -> list[dict[str, Any]]:
    procedures_root = workspace / "procedures"
    if not procedures_root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(procedures_root.glob("*/*.json")):
        payload = _load_json(path)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _procedure_record_values(workspace: Path, key: str) -> list[str]:
    values: list[str] = []
    for record in _workspace_procedure_records(workspace):
        value = record.get(key)
        if isinstance(value, list):
            candidates = [str(item) for item in value if item is not None]
        elif value is None:
            candidates = []
        else:
            candidates = [str(value)]
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)
    return values


def _procedure_audit_event_values(workspace: Path) -> list[str]:
    values: list[str] = []
    for record in _workspace_procedure_records(workspace):
        notes = record.get("audit_notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            event = str(note.get("event") or "")
            if event and event not in values:
                values.append(event)
    return values


def _assert_required_any(
    *, label: str, expected: set[str], observed: list[str], issues: list[str]
) -> None:
    if expected and not (expected & set(observed)):
        issues.append(
            f"missing required {label}; expected any of {sorted(expected)}, got {sorted(observed)}"
        )


def _assert_required_all(
    *, label: str, expected: set[str], observed: list[str], issues: list[str]
) -> None:
    missing = expected - set(observed)
    if missing:
        issues.append(
            f"missing required {label}; expected all of {sorted(expected)}, "
            f"missing {sorted(missing)}, got {sorted(observed)}"
        )


def _evaluate_assertions(
    *,
    scenario: dict[str, Any],
    workspace: Path,
    executions: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> tuple[bool, list[str], str]:
    assertions = scenario.get("assertions", {})
    issues: list[str] = []
    successes = [_execution_success(item) for item in executions]
    any_success = any(successes)
    all_commands_parsed = all(item.get("json") is not None for item in executions)

    if not all_commands_parsed:
        for item in executions:
            if item.get("json") is None:
                issues.append(str(item.get("json_error") or "command did not return JSON"))

    if assertions.get("must_succeed") and not all(successes):
        issues.append("scenario expected every task turn to succeed")
    if assertions.get("must_not_succeed") and any_success:
        issues.append("scenario expected no task success")
    min_successful_turns = assertions.get("min_successful_turns")
    if min_successful_turns is not None and sum(1 for item in successes if item) < int(
        min_successful_turns
    ):
        issues.append(
            f"too few successful task turns: {sum(1 for item in successes if item)} "
            f"< {min_successful_turns}"
        )
    min_failed_turns = assertions.get("min_failed_turns")
    if min_failed_turns is not None and sum(1 for item in successes if not item) < int(
        min_failed_turns
    ):
        issues.append(
            f"too few failed task turns: {sum(1 for item in successes if not item)} "
            f"< {min_failed_turns}"
        )

    assistant_messages = _assistant_messages(executions)
    for expected in assertions.get("assistant_message_contains", []):
        text = str(expected)
        if not any(text in message for message in assistant_messages):
            issues.append(f"assistant message does not contain expected text: {text}")
    for forbidden in assertions.get("assistant_message_not_contains", []):
        text = str(forbidden)
        if any(text in message for message in assistant_messages):
            issues.append(f"assistant message contains forbidden text: {text}")

    min_thread_ids = assertions.get("min_thread_ids")
    if min_thread_ids is not None and len(evidence.get("thread_ids", [])) < int(min_thread_ids):
        issues.append(
            f"too few thread ids: {len(evidence.get('thread_ids', []))} < {min_thread_ids}"
        )

    route_types = {str(item.get("route_input_type")) for item in evidence.get("turns", [])}
    required_route_types = {
        str(item) for item in assertions.get("required_route_input_type_any", [])
    }
    if required_route_types and not (required_route_types & route_types):
        issues.append(
            "missing required route input type; expected any of "
            f"{sorted(required_route_types)}, got {sorted(route_types)}"
        )
    forbidden_route_types = {
        str(item) for item in assertions.get("forbidden_route_input_type_any", [])
    }
    leaked_route_types = forbidden_route_types & route_types
    if leaked_route_types:
        issues.append(f"forbidden route input types were used: {sorted(leaked_route_types)}")

    route_actions = {str(item.get("route_action")) for item in evidence.get("turns", [])}
    required_route_actions = {str(item) for item in assertions.get("required_route_action_any", [])}
    if required_route_actions and not (required_route_actions & route_actions):
        issues.append(
            "missing required route action; expected any of "
            f"{sorted(required_route_actions)}, got {sorted(route_actions)}"
        )

    for relative in assertions.get("file_exists", []):
        if not _safe_child(workspace, str(relative)).exists():
            issues.append(f"missing file: {relative}")
    for item in assertions.get("file_contains", []):
        if isinstance(item, dict):
            _assert_file_contains(workspace, item, issues)
    for item in assertions.get("file_not_contains", []):
        if isinstance(item, dict):
            _assert_file_not_contains(workspace, item, issues)
    for relative in assertions.get("file_not_exists", []):
        if _safe_child(workspace, str(relative)).exists():
            issues.append(f"unexpected file exists: {relative}")

    required_skills = {str(item) for item in assertions.get("required_skill_any", [])}
    if required_skills and not (required_skills & set(evidence.get("used_skills", []))):
        issues.append(
            "missing required skill usage; expected any of "
            f"{sorted(required_skills)}, got {evidence.get('used_skills', [])}"
        )

    forbidden_skills = {str(item) for item in assertions.get("forbidden_skill_any", [])}
    if forbidden_skills:
        leaked = forbidden_skills & set(evidence.get("used_skills", []))
        if leaked:
            issues.append(f"forbidden skills were used: {sorted(leaked)}")

    required_statuses = {str(item) for item in assertions.get("required_node_status_any", [])}
    if required_statuses:
        seen_statuses = {str(item.get("status")) for item in evidence.get("node_statuses", [])}
        if not (required_statuses & seen_statuses):
            issues.append(
                "missing required node status; expected any of "
                f"{sorted(required_statuses)}, got {sorted(seen_statuses)}"
            )

    denied_required = {str(item) for item in assertions.get("required_permission_denied_any", [])}
    if denied_required:
        denied_seen = {
            str(item.get("permission"))
            for item in evidence.get("permissions", [])
            if item.get("status") == "denied"
        }
        if not (denied_required & denied_seen):
            issues.append(
                "missing required permission denial; expected any of "
                f"{sorted(denied_required)}, got {sorted(denied_seen)}"
            )

    forbidden_grants = {
        str(item) for item in assertions.get("forbidden_permission_granted_any", [])
    }
    if forbidden_grants:
        granted_seen = {
            str(item.get("permission"))
            for item in evidence.get("permissions", [])
            if item.get("status") == "granted"
        }
        leaked = forbidden_grants & granted_seen
        if leaked:
            issues.append(f"forbidden permissions were granted: {sorted(leaked)}")

    request_snapshots = evidence.get("request_snapshots", [])
    for expected in assertions.get("required_request_constraint_any", []):
        if not isinstance(expected, dict):
            continue
        key = str(expected.get("key", ""))
        value = expected.get("value")
        if not any(
            isinstance(item.get("constraints"), dict) and item["constraints"].get(key) == value
            for item in request_snapshots
        ):
            issues.append(
                f"missing request constraint {key}={value!r}; "
                f"seen={[item.get('constraints') for item in request_snapshots]}"
            )

    for expected in assertions.get("required_request_metadata_any", []):
        if not isinstance(expected, dict):
            continue
        key = str(expected.get("key", ""))
        value = expected.get("value")
        if not any(
            isinstance(item.get("metadata"), dict) and item["metadata"].get(key) == value
            for item in request_snapshots
        ):
            issues.append(
                f"missing request metadata {key}={value!r}; "
                f"seen={[item.get('metadata') for item in request_snapshots]}"
            )

    max_llm_calls = assertions.get("max_llm_calls")
    if max_llm_calls is not None and len(evidence.get("llm_calls", [])) > int(max_llm_calls):
        issues.append(f"too many LLM calls: {len(evidence.get('llm_calls', []))} > {max_llm_calls}")
    required_llm_prompts = {str(item) for item in assertions.get("required_llm_prompt_any", [])}
    if required_llm_prompts:
        seen_prompts = {
            str(item.get("prompt_name"))
            for item in evidence.get("llm_calls", [])
            if item.get("prompt_name") is not None
        }
        if not (required_llm_prompts & seen_prompts):
            issues.append(
                "missing required LLM prompt; expected any of "
                f"{sorted(required_llm_prompts)}, got {sorted(seen_prompts)}"
            )

    vector_matches = evidence.get("vector_matches", [])
    min_vector_match_count = assertions.get("min_vector_match_count")
    if min_vector_match_count is not None and len(vector_matches) < int(min_vector_match_count):
        issues.append(f"too few vector matches: {len(vector_matches)} < {min_vector_match_count}")
    required_vector_entries = {
        str(item) for item in assertions.get("required_vector_match_entry_any", [])
    }
    if required_vector_entries:
        seen_entries = {str(item.get("entry_id")) for item in vector_matches}
        if not (required_vector_entries & seen_entries):
            issues.append(
                "missing required vector match entry; expected any of "
                f"{sorted(required_vector_entries)}, got {sorted(seen_entries)}"
            )
    if assertions.get("require_vector_compatible") and not any(
        item.get("vector_compatible") is True for item in vector_matches
    ):
        issues.append("no compatible vector match was observed")
    max_vector_match_count = assertions.get("max_vector_match_count")
    if max_vector_match_count is not None and len(vector_matches) > int(max_vector_match_count):
        issues.append(f"too many vector matches: {len(vector_matches)} > {max_vector_match_count}")
    forbidden_vector_entries = {
        str(item) for item in assertions.get("forbidden_vector_match_entry_any", [])
    }
    if forbidden_vector_entries:
        seen_entries = {str(item.get("entry_id")) for item in vector_matches}
        leaked = forbidden_vector_entries & seen_entries
        if leaked:
            issues.append(f"forbidden vector match entries were used: {sorted(leaked)}")
    if assertions.get("forbid_raw_embedding_in_vector_matches") and any(
        bool(item.get("raw_embedding_present")) for item in vector_matches
    ):
        issues.append("raw embedding appeared in vector matches")

    workflow_matches = evidence.get("workflow_matches", [])
    required_workflow_tasks = {
        str(item) for item in assertions.get("required_workflow_match_task_any", [])
    }
    if required_workflow_tasks:
        seen_workflows = {str(item.get("matched_task_id")) for item in workflow_matches}
        if not (required_workflow_tasks & seen_workflows):
            issues.append(
                "missing required workflow match; expected any of "
                f"{sorted(required_workflow_tasks)}, got {sorted(seen_workflows)}"
            )
    forbidden_workflow_tasks = {
        str(item) for item in assertions.get("forbidden_workflow_match_task_any", [])
    }
    if forbidden_workflow_tasks:
        seen_workflows = {str(item.get("matched_task_id")) for item in workflow_matches}
        leaked = forbidden_workflow_tasks & seen_workflows
        if leaked:
            issues.append(f"forbidden workflow matches were used: {sorted(leaked)}")

    for expected in assertions.get("max_request_payload_field_json_chars", []):
        if not isinstance(expected, dict):
            continue
        field = str(expected.get("field", ""))
        max_chars = int(expected.get("max", 0))
        if not field or max_chars <= 0:
            continue
        observed: list[int] = []
        for stats in evidence.get("request_payload_stats", []):
            fields = stats.get("fields") if isinstance(stats.get("fields"), dict) else {}
            field_stats = fields.get(field)
            if isinstance(field_stats, dict) and field_stats.get("json_chars") is not None:
                observed.append(int(field_stats.get("json_chars") or 0))
        if observed and max(observed) > max_chars:
            issues.append(
                f"request payload field {field} too large: max {max(observed)} > {max_chars}"
            )

    required_policy_decisions = {
        str(item) for item in assertions.get("required_policy_decision_any", [])
    }
    if required_policy_decisions:
        seen_policy_decisions = {
            str(item.get("policy_decision")) for item in evidence.get("policy_decisions", [])
        }
        if not (required_policy_decisions & seen_policy_decisions):
            issues.append(
                "missing required policy decision; expected any of "
                f"{sorted(required_policy_decisions)}, got {sorted(seen_policy_decisions)}"
            )

    required_policy_risk_flags = {
        str(item) for item in assertions.get("required_policy_risk_flag_any", [])
    }
    if required_policy_risk_flags:
        seen_policy_risk_flags = {
            str(name)
            for item in evidence.get("policy_decisions", [])
            for name, enabled in (
                item.get("risk_flags") if isinstance(item.get("risk_flags"), dict) else {}
            ).items()
            if enabled
        }
        if not (required_policy_risk_flags & seen_policy_risk_flags):
            issues.append(
                "missing required policy risk flag; expected any of "
                f"{sorted(required_policy_risk_flags)}, got {sorted(seen_policy_risk_flags)}"
            )

    experience_records = [
        item for item in evidence.get("experience_records", []) if isinstance(item, dict)
    ]
    experience_statuses = [
        str(item.get("status"))
        for item in experience_records
        if item.get("status") is not None
    ]
    _assert_required_any(
        label="experience status",
        expected={str(item) for item in assertions.get("required_experience_status_any", [])},
        observed=experience_statuses,
        issues=issues,
    )
    _assert_required_all(
        label="experience statuses",
        expected={str(item) for item in assertions.get("required_experience_status_all", [])},
        observed=experience_statuses,
        issues=issues,
    )

    intake_assessments = [
        item for item in evidence.get("task_intake_assessments", []) if isinstance(item, dict)
    ]
    intake_task_types = [
        str(item.get("task_type"))
        for item in intake_assessments
        if item.get("task_type") is not None
    ]
    _assert_required_any(
        label="task intake type",
        expected={str(item) for item in assertions.get("required_intake_task_type_any", [])},
        observed=intake_task_types,
        issues=issues,
    )
    _assert_required_all(
        label="task intake types",
        expected={str(item) for item in assertions.get("required_intake_task_type_all", [])},
        observed=intake_task_types,
        issues=issues,
    )
    intake_risk_levels = [
        str(item.get("risk_level"))
        for item in intake_assessments
        if item.get("risk_level") is not None
    ]
    _assert_required_any(
        label="task intake risk level",
        expected={str(item) for item in assertions.get("required_intake_risk_level_any", [])},
        observed=intake_risk_levels,
        issues=issues,
    )
    _assert_required_all(
        label="task intake risk levels",
        expected={str(item) for item in assertions.get("required_intake_risk_level_all", [])},
        observed=intake_risk_levels,
        issues=issues,
    )
    intake_tools: list[str] = []
    for item in intake_assessments:
        intake_tools.extend(str(tool) for tool in item.get("tools") or [])
    _assert_required_any(
        label="task intake tool",
        expected={str(item) for item in assertions.get("required_intake_tool_any", [])},
        observed=intake_tools,
        issues=issues,
    )
    _assert_required_all(
        label="task intake tools",
        expected={str(item) for item in assertions.get("required_intake_tool_all", [])},
        observed=intake_tools,
        issues=issues,
    )
    intake_validation_strategies = [
        str(item.get("validation_strategy"))
        for item in intake_assessments
        if item.get("validation_strategy") is not None
    ]
    _assert_required_any(
        label="task intake validation strategy",
        expected={
            str(item) for item in assertions.get("required_intake_validation_strategy_any", [])
        },
        observed=intake_validation_strategies,
        issues=issues,
    )
    _assert_required_all(
        label="task intake validation strategies",
        expected={
            str(item) for item in assertions.get("required_intake_validation_strategy_all", [])
        },
        observed=intake_validation_strategies,
        issues=issues,
    )
    if "required_intake_confirmation_required" in assertions:
        expected_confirmation = bool(assertions["required_intake_confirmation_required"])
        if not any(
            item.get("confirmation_required") is expected_confirmation
            for item in intake_assessments
        ):
            issues.append(
                "missing required task intake confirmation_required="
                f"{expected_confirmation}; got "
                f"{[item.get('confirmation_required') for item in intake_assessments]}"
            )

    learning_feedbacks = [
        item for item in evidence.get("learning_feedbacks", []) if isinstance(item, dict)
    ]
    feedback_reuse_statuses = [
        str(item.get("reuse_status"))
        for item in learning_feedbacks
        if item.get("reuse_status") is not None
    ]
    _assert_required_any(
        label="learning feedback reuse status",
        expected={
            str(item) for item in assertions.get("required_learning_feedback_reuse_status_any", [])
        },
        observed=feedback_reuse_statuses,
        issues=issues,
    )
    _assert_required_all(
        label="learning feedback reuse statuses",
        expected={
            str(item) for item in assertions.get("required_learning_feedback_reuse_status_all", [])
        },
        observed=feedback_reuse_statuses,
        issues=issues,
    )
    feedback_review_targets = [
        str(item.get("review_target_type"))
        for item in learning_feedbacks
        if item.get("review_target_type") is not None
    ]
    _assert_required_any(
        label="learning feedback review target",
        expected={
            str(item) for item in assertions.get("required_learning_feedback_review_target_any", [])
        },
        observed=feedback_review_targets,
        issues=issues,
    )
    feedback_fast_path_decisions = [
        str(item.get("habit_fast_path_decision"))
        for item in learning_feedbacks
        if item.get("habit_fast_path_decision") is not None
    ]
    _assert_required_any(
        label="learning feedback habit fast path decision",
        expected={
            str(item)
            for item in assertions.get("required_learning_feedback_habit_decision_any", [])
        },
        observed=feedback_fast_path_decisions,
        issues=issues,
    )

    learning_corrections = [
        item for item in evidence.get("learning_corrections", []) if isinstance(item, dict)
    ]
    correction_triggers = [
        str(item.get("trigger")) for item in learning_corrections if item.get("trigger") is not None
    ]
    _assert_required_any(
        label="learning correction trigger",
        expected={
            str(item) for item in assertions.get("required_learning_correction_trigger_any", [])
        },
        observed=correction_triggers,
        issues=issues,
    )
    _assert_required_all(
        label="learning correction triggers",
        expected={
            str(item) for item in assertions.get("required_learning_correction_trigger_all", [])
        },
        observed=correction_triggers,
        issues=issues,
    )
    correction_statuses = [
        str(item.get("new_status"))
        for item in learning_corrections
        if item.get("new_status") is not None
    ]
    _assert_required_any(
        label="learning correction new status",
        expected={
            str(item) for item in assertions.get("required_learning_correction_new_status_any", [])
        },
        observed=correction_statuses,
        issues=issues,
    )
    _assert_required_all(
        label="learning correction new statuses",
        expected={
            str(item) for item in assertions.get("required_learning_correction_new_status_all", [])
        },
        observed=correction_statuses,
        issues=issues,
    )
    correction_review_statuses = [
        str(item.get("new_review_status"))
        for item in learning_corrections
        if item.get("new_review_status") is not None
    ]
    _assert_required_any(
        label="learning correction review status",
        expected={
            str(item)
            for item in assertions.get("required_learning_correction_review_status_any", [])
        },
        observed=correction_review_statuses,
        issues=issues,
    )
    _assert_required_all(
        label="learning correction review statuses",
        expected={
            str(item)
            for item in assertions.get("required_learning_correction_review_status_all", [])
        },
        observed=correction_review_statuses,
        issues=issues,
    )
    correction_object_types = [
        str(item.get("object_type"))
        for item in learning_corrections
        if item.get("object_type") is not None
    ]
    _assert_required_any(
        label="learning correction object type",
        expected={
            str(item) for item in assertions.get("required_learning_correction_object_type_any", [])
        },
        observed=correction_object_types,
        issues=issues,
    )
    if assertions.get("require_learning_correction_frozen") and not any(
        item.get("frozen") is True for item in learning_corrections
    ):
        issues.append("learning correction did not record a frozen object")

    _assert_required_any(
        label="procedure status",
        expected={str(item) for item in assertions.get("required_procedure_status_any", [])},
        observed=_procedure_record_values(workspace, "status"),
        issues=issues,
    )
    _assert_required_any(
        label="procedure review status",
        expected={str(item) for item in assertions.get("required_procedure_review_status_any", [])},
        observed=_procedure_record_values(workspace, "review_status"),
        issues=issues,
    )
    _assert_required_all(
        label="procedure audit events",
        expected={str(item) for item in assertions.get("required_procedure_audit_event_all", [])},
        observed=_procedure_audit_event_values(workspace),
        issues=issues,
    )

    required_completion_layers = {
        str(item) for item in assertions.get("required_completion_failure_layer_any", [])
    }
    if required_completion_layers:
        seen_layers = {
            str(item.get("primary_failure_layer"))
            for item in evidence.get("completion_evaluations", [])
        }
        if not (required_completion_layers & seen_layers):
            issues.append(
                "missing required completion failure layer; expected any of "
                f"{sorted(required_completion_layers)}, got {sorted(seen_layers)}"
            )

    if assertions.get("forbid_model_capability_suspected") and any(
        bool(item.get("model_capability_suspected"))
        for item in evidence.get("completion_evaluations", [])
    ):
        issues.append("completion evaluation suspected model capability unexpectedly")

    required_embedding_providers = {
        str(item) for item in assertions.get("required_embedding_provider_any", [])
    }
    if required_embedding_providers:
        seen_providers = set(evidence.get("embedding_summary", {}).get("providers", []))
        if not (required_embedding_providers & seen_providers):
            issues.append(
                "missing required embedding provider; expected any of "
                f"{sorted(required_embedding_providers)}, got {sorted(seen_providers)}"
            )

    if assertions.get("forbid_embedding_fallback") and int(
        evidence.get("embedding_summary", {}).get("fallback_count", 0)
    ):
        issues.append("embedding fallback occurred unexpectedly")

    if assertions.get("forbid_embedding_errors") and int(
        evidence.get("embedding_summary", {}).get("error_count", 0)
    ):
        issues.append("embedding errors occurred unexpectedly")

    file_count_max = assertions.get("file_count_max")
    forbidden_workspace_globs = [
        str(item) for item in assertions.get("forbidden_workspace_glob", [])
    ]
    if file_count_max is not None or forbidden_workspace_globs:
        workspace_files = _workspace_file_paths(workspace)
        if file_count_max is not None and len(workspace_files) > int(file_count_max):
            issues.append(
                f"too many workspace files: {len(workspace_files)} > {file_count_max}; "
                f"files={workspace_files}"
            )
        for pattern in forbidden_workspace_globs:
            matches = [path for path in workspace_files if fnmatch.fnmatch(path, pattern)]
            if matches:
                issues.append(f"forbidden workspace glob matched {pattern}: {matches}")

    diagnosis = _diagnose(issues, executions, evidence)
    diagnosis_issue_count = len(issues)
    allowed_diagnoses = {str(item) for item in assertions.get("diagnosis_any", [])}
    if allowed_diagnoses and diagnosis not in allowed_diagnoses:
        issues.append(
            f"unexpected diagnosis: expected any of {sorted(allowed_diagnoses)}, got {diagnosis}"
        )
    required_layers = {str(item) for item in assertions.get("required_failure_layer_any", [])}
    if required_layers:
        diagnosis_layer = _failure_layer(diagnosis, passed=False)
        if diagnosis_layer not in required_layers:
            issues.append(
                "unexpected failure layer: expected any of "
                f"{sorted(required_layers)}, got {diagnosis_layer}"
            )

    if len(issues) > diagnosis_issue_count and diagnosis == "passed":
        diagnosis = "assertion_failure"
    return not issues, issues, diagnosis


def _diagnose(issues: list[str], executions: list[dict[str, Any]], evidence: dict[str, Any]) -> str:
    if any(item.get("timed_out") for item in executions):
        return "runtime_timeout"
    if any(item.get("json") is None for item in executions):
        return "cli_or_infrastructure"
    if issues and any(
        turn.get("route_input_type")
        in {"chitchat", "knowledge_query", "status_query", "system_request"}
        for turn in evidence.get("turns", [])
    ):
        return "router_or_interaction_dispatch"
    if any("missing required skill usage" in issue for issue in issues):
        return "planner_or_skill_selection"
    if any("forbidden permissions" in issue for issue in issues):
        return "permission_guardrail"
    if any("missing required permission denial" in issue for issue in issues):
        return "permission_detection"
    denied = [item for item in evidence.get("permissions", []) if item.get("status") == "denied"]
    if denied:
        return "permission_policy_or_scenario_config"
    if any(
        item.get("status") == "permission_denied"
        for item in evidence.get("experience_records", [])
        if isinstance(item, dict)
    ):
        return "permission_policy_or_scenario_config"
    if any(
        item.get("experience_status") == "permission_denied"
        for item in evidence.get("learning_feedbacks", [])
        if isinstance(item, dict)
    ):
        return "permission_policy_or_scenario_config"
    failed_nodes = [
        item for item in evidence.get("node_statuses", []) if item.get("status") == "failed"
    ]
    if failed_nodes:
        return "executor_or_skill_contract"
    if issues:
        completion_diagnosis = _diagnose_from_completion(evidence)
        if completion_diagnosis:
            return completion_diagnosis
        embedding_summary = evidence.get("embedding_summary", {})
        if isinstance(embedding_summary, dict) and (
            int(embedding_summary.get("error_count", 0))
            or int(embedding_summary.get("incompatible_match_count", 0))
        ):
            return "memory_retrieval"
    if any(
        issue.startswith("missing file")
        or issue.startswith("unexpected file")
        or "does not contain" in issue
        or "contains forbidden text" in issue
        for issue in issues
    ):
        return "task_outcome_mismatch"
    if issues:
        return "assertion_failure"
    return "passed"


def _diagnose_from_completion(evidence: dict[str, Any]) -> str | None:
    for item in evidence.get("completion_evaluations", []):
        layer = str(item.get("primary_failure_layer") or "none")
        if bool(item.get("model_capability_suspected")) or layer == "model_capability":
            return "model_capability"
        if layer in {
            "memory_retrieval",
            "completion_evaluation",
            "agent_flow",
            "planner_or_skill_selection",
            "tool_or_skill",
            "test_expectation",
        }:
            return layer
    return None


def _run_task_scenario(
    *, scenario: dict[str, Any], workspace: Path, env: dict[str, str]
) -> list[dict[str, Any]]:
    cmd = [
        sys.executable,
        "-m",
        "omniagent",
        "--workspace",
        str(workspace),
        "run",
        str(scenario["goal"]),
    ]
    _append_common_args(cmd, scenario)
    timeout = int(scenario.get("process_timeout", int(scenario.get("timeout", 60)) + 180))
    return [_run_command(cmd=cmd, env=env, timeout_seconds=timeout, cwd=REPO_ROOT)]


def _run_chat_scenario(
    *, scenario: dict[str, Any], workspace: Path, env: dict[str, str]
) -> list[dict[str, Any]]:
    executions: list[dict[str, Any]] = []
    thread_id: str | None = None
    timeout = int(scenario.get("process_timeout", int(scenario.get("timeout", 60)) + 180))
    for turn in scenario.get("turns", []):
        cmd = [
            sys.executable,
            "-m",
            "omniagent",
            "--workspace",
            str(workspace),
            "chat",
            "send",
            str(turn["message"]),
        ]
        merged = dict(scenario)
        merged.update({key: value for key, value in turn.items() if key != "message"})
        if thread_id:
            cmd.extend(["--thread-id", thread_id])
        _append_common_args(cmd, merged)
        execution = _run_command(cmd=cmd, env=env, timeout_seconds=timeout, cwd=REPO_ROOT)
        executions.append(execution)
        parsed_thread_id = _extract_thread_id(execution.get("json"))
        if parsed_thread_id:
            thread_id = parsed_thread_id
    return executions


def _run_task_sequence_scenario(
    *, scenario: dict[str, Any], workspace: Path, env: dict[str, str]
) -> list[dict[str, Any]]:
    executions: list[dict[str, Any]] = []
    variables: dict[str, Any] = {}
    timeout = int(scenario.get("process_timeout", int(scenario.get("timeout", 60)) + 180))
    for index, raw_step in enumerate(scenario.get("steps", []), start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"{scenario['id']}: steps[{index}] must be an object")
        step = _render_sequence_value(raw_step, variables)
        step_type = str(step.get("type", "run")).strip().lower()
        if step_type == "run":
            if not step.get("goal"):
                raise ValueError(f"{scenario['id']}: steps[{index}] run requires goal")
            merged = dict(scenario)
            merged.update({key: value for key, value in step.items() if key != "type"})
            cmd = [
                sys.executable,
                "-m",
                "omniagent",
                "--workspace",
                str(workspace),
                "run",
                str(merged["goal"]),
            ]
            _append_common_args(cmd, merged)
        elif step_type == "cli":
            args = step.get("args")
            if not isinstance(args, list) or not args:
                raise ValueError(f"{scenario['id']}: steps[{index}] cli requires args")
            cmd = [
                sys.executable,
                "-m",
                "omniagent",
                "--workspace",
                str(workspace),
                *[str(item) for item in args],
            ]
            if step.get("json", True) and "--json" not in cmd:
                cmd.append("--json")
        else:
            raise ValueError(f"{scenario['id']}: unsupported sequence step type {step_type}")
        execution = _run_command(cmd=cmd, env=env, timeout_seconds=timeout, cwd=REPO_ROOT)
        executions.append(execution)
        _capture_sequence_values(execution=execution, step=step, variables=variables)
    return executions


def _write_case_artifacts(case_dir: Path, executions: list[dict[str, Any]]) -> None:
    for index, execution in enumerate(executions, start=1):
        prefix = f"turn_{index:02d}"
        _write_json(case_dir / f"{prefix}_command.json", execution["command"])
        (case_dir / f"{prefix}_stdout.txt").write_text(
            str(execution.get("stdout") or ""), encoding="utf-8"
        )
        (case_dir / f"{prefix}_stderr.txt").write_text(
            str(execution.get("stderr") or ""), encoding="utf-8"
        )
        if execution.get("json") is not None:
            _write_json(case_dir / f"{prefix}_parsed.json", execution["json"])


def _run_one(
    *, scenario: dict[str, Any], run_dir: Path, args: argparse.Namespace
) -> dict[str, Any]:
    if args.verbose:
        print(f"running: {scenario['id']}", flush=True)
    workspace = _prepare_workspace(run_dir, scenario)
    env = _base_env(run_dir, workspace, scenario)
    case_dir = run_dir / "cases" / str(scenario["id"])
    case_dir.mkdir(parents=True, exist_ok=True)

    if scenario.get("kind", "task") == "chat_sequence":
        executions = _run_chat_scenario(scenario=scenario, workspace=workspace, env=env)
    elif scenario.get("kind", "task") == "task_sequence":
        executions = _run_task_sequence_scenario(scenario=scenario, workspace=workspace, env=env)
    else:
        executions = _run_task_scenario(scenario=scenario, workspace=workspace, env=env)

    _write_case_artifacts(case_dir, executions)
    task_ids: list[str] = []
    thread_ids: list[str] = []
    for execution in executions:
        payload = execution.get("json")
        task_ids.extend([item for item in _extract_task_ids(payload) if item not in task_ids])
        thread_id = _extract_thread_id(payload)
        if thread_id and thread_id not in thread_ids:
            thread_ids.append(thread_id)
    evidence = _collect_evidence(workspace, task_ids, thread_ids, executions)
    passed, issues, diagnosis = _evaluate_assertions(
        scenario=scenario, workspace=workspace, executions=executions, evidence=evidence
    )
    result = {
        "id": scenario["id"],
        "category": scenario.get("category", "uncategorized"),
        "kind": scenario.get("kind", "task"),
        "requires": scenario.get("requires", []),
        "status": "passed" if passed else "failed",
        "diagnosis": diagnosis,
        "failure_layer": _failure_layer(diagnosis, passed=passed),
        "issues": issues,
        "workspace": str(workspace),
        "case_dir": str(case_dir),
        "task_ids": task_ids,
        "thread_ids": thread_ids,
        "exit_codes": [item.get("exit_code") for item in executions],
        "duration_seconds": round(
            sum(float(item.get("duration_seconds", 0)) for item in executions), 3
        ),
        "evidence": evidence,
    }
    result["failure_attribution"] = _build_failure_attribution(
        diagnosis=diagnosis,
        passed=passed,
        evidence=evidence,
    )
    _write_case_diagnostics(
        case_dir=case_dir,
        scenario=scenario,
        result=result,
        executions=executions,
        evidence=evidence,
        workspace=workspace,
    )
    _write_json(case_dir / "result.json", result)
    if args.verbose:
        print(f"{result['status']}: {result['id']} ({result['diagnosis']})", flush=True)
    return result


def _write_summary(run_dir: Path, results: list[dict[str, Any]]) -> None:
    total = len(results)
    passed = sum(1 for item in results if item["status"] == "passed")
    failed = sum(1 for item in results if item["status"] == "failed")
    skipped = sum(1 for item in results if item["status"] == "skipped")
    selected = sum(1 for item in results if item["status"] == "selected")
    summary = {
        "run_dir": str(run_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "selected": selected,
        "results": results,
    }
    _write_json(run_dir / "summary.json", summary)
    lines = [
        "# Real-World Eval Summary",
        "",
        f"- run_dir: `{run_dir}`",
        f"- total: {total}",
        f"- passed: {passed}",
        f"- failed: {failed}",
        f"- skipped: {skipped}",
        f"- selected: {selected}",
        "",
        "| Scenario | Status | Diagnosis | Failure Layer | Diagnostics | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    for item in results:
        evidence = ", ".join(item.get("task_ids", []) + item.get("thread_ids", [])) or "-"
        diagnostics = "-"
        if item.get("case_dir"):
            diagnostics = f"[diagnostics]({item['case_dir']}/diagnostics.md)"
        lines.append(
            f"| `{item['id']}` | {item['status']} | {item.get('diagnosis', '-')} | "
            f"{item.get('failure_layer', '-')} | {diagnostics} | {evidence} |"
        )
    failed_items = [item for item in results if item["status"] == "failed"]
    if failed_items:
        lines.extend(["", "## Failed Cases", ""])
        for item in failed_items:
            lines.extend(
                [
                    f"### {item['id']}",
                    "",
                    f"- diagnosis: `{item.get('diagnosis')}`",
                    f"- failure_layer: `{item.get('failure_layer')}`",
                    f"- case_dir: `{item.get('case_dir')}`",
                    f"- diagnostics: `{item.get('case_dir')}/diagnostics.md`",
                    "- issues:",
                    *[f"  - {issue}" for issue in item.get("issues", [])],
                    "- next inspection:",
                    "  - open `diagnostics.md` for the structured repair entry point",
                    "  - open `result.json` for normalized evidence",
                    "  - inspect `turn_XX_stdout.txt` and workspace task artifacts",
                    "",
                ]
            )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _artifact_record(path: Path, *, run_dir: Path, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path),
        "run_relative_path": _relative_path(path, run_dir),
        "exists": path.exists(),
    }


def _load_case_result(summary_result: dict[str, Any]) -> dict[str, Any]:
    case_dir = summary_result.get("case_dir")
    if isinstance(case_dir, str) and case_dir:
        result_path = Path(case_dir) / "result.json"
        if result_path.exists():
            payload = _load_json(result_path)
            if isinstance(payload, dict):
                return payload
    return summary_result


def _load_scenario_metadata_for_index() -> dict[str, dict[str, Any]]:
    try:
        scenarios = _normalize_scenarios(DEFAULT_SCENARIOS)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {
        str(scenario["id"]): {
            "category": scenario.get("category", "uncategorized"),
            "kind": scenario.get("kind", "task"),
            "requires": scenario.get("requires", []),
        }
        for scenario in scenarios
        if scenario.get("id")
    }


def _summarize_permissions(permissions: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in permissions:
        permission = str(item.get("permission") or "unknown")
        status = str(item.get("status") or "unknown")
        key = f"{permission}:{status}"
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items()))


def _sqlite_has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _skill_lifecycle_key(skill_name: Any, version: Any) -> str:
    return f"{skill_name or ''}\u0000{version or ''}"


def _read_skill_lifecycle_records(
    *,
    run_dir: Path,
    db_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not db_path.exists():
        return [], []

    records: dict[str, dict[str, Any]] = {}

    def ensure_record(skill_name: Any, version: Any) -> dict[str, Any]:
        key = _skill_lifecycle_key(skill_name, version)
        if key not in records:
            records[key] = {
                "skill_name": skill_name,
                "version": version,
                "source": None,
                "lifecycle_status": None,
                "draft_status": None,
                "permission_requirements": [],
                "tags": [],
                "sandbox_level": None,
                "registration_actions": [],
                "registration_statuses": [],
                "approval_statuses": [],
                "validation_passed": [],
                "last_validated_at": None,
                "last_approved_at": None,
                "last_seen_at": None,
                "path": str(db_path),
            }
        return records[key]

    artifacts = [_artifact_record(db_path, run_dir=run_dir, kind="skill_lifecycle_db")]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return [], artifacts

    try:
        if _sqlite_has_table(conn, "skill_registry"):
            rows = conn.execute(
                """
                SELECT skill_name, version, source, lifecycle_status,
                       permission_requirements_json, tags_json, sandbox_level,
                       last_validated_at, last_approved_at, last_seen_at
                FROM skill_registry
                ORDER BY skill_name, version
                """
            ).fetchall()
            for row in rows:
                record = ensure_record(row["skill_name"], row["version"])
                record.update(
                    {
                        "source": row["source"],
                        "lifecycle_status": row["lifecycle_status"],
                        "permission_requirements": _loads_json_list(
                            row["permission_requirements_json"]
                        ),
                        "tags": _loads_json_list(row["tags_json"]),
                        "sandbox_level": row["sandbox_level"],
                        "last_validated_at": row["last_validated_at"],
                        "last_approved_at": row["last_approved_at"],
                        "last_seen_at": row["last_seen_at"],
                    }
                )

        if _sqlite_has_table(conn, "skill_drafts"):
            rows = conn.execute(
                """
                SELECT skill_name, version, lifecycle_status, permission_requirements_json
                FROM skill_drafts
                ORDER BY skill_name, version
                """
            ).fetchall()
            for row in rows:
                record = ensure_record(row["skill_name"], row["version"])
                record["draft_status"] = row["lifecycle_status"]
                if not record["permission_requirements"]:
                    record["permission_requirements"] = _loads_json_list(
                        row["permission_requirements_json"]
                    )

        if _sqlite_has_table(conn, "skill_registration_records"):
            rows = conn.execute(
                """
                SELECT skill_name, version, action, status, metadata_json, created_at
                FROM skill_registration_records
                ORDER BY created_at DESC
                """
            ).fetchall()
            for row in rows:
                record = ensure_record(row["skill_name"], row["version"])
                action = str(row["action"])
                status = str(row["status"])
                if action not in record["registration_actions"]:
                    record["registration_actions"].append(action)
                if status not in record["registration_statuses"]:
                    record["registration_statuses"].append(status)
                metadata = _loads_json_object(row["metadata_json"])
                if metadata.get("note") and not record.get("latest_registration_note"):
                    record["latest_registration_note"] = metadata.get("note")

        if _sqlite_has_table(conn, "skill_approval_records"):
            rows = conn.execute(
                """
                SELECT skill_name, version, status, created_at
                FROM skill_approval_records
                ORDER BY created_at DESC
                """
            ).fetchall()
            for row in rows:
                record = ensure_record(row["skill_name"], row["version"])
                status = str(row["status"])
                if status not in record["approval_statuses"]:
                    record["approval_statuses"].append(status)

        if _sqlite_has_table(conn, "skill_validation_reports"):
            rows = conn.execute(
                """
                SELECT skill_name, version, passed, created_at
                FROM skill_validation_reports
                ORDER BY created_at DESC
                """
            ).fetchall()
            for row in rows:
                record = ensure_record(row["skill_name"], row["version"])
                passed = bool(row["passed"])
                if passed not in record["validation_passed"]:
                    record["validation_passed"].append(passed)
    except sqlite3.Error:
        return [], artifacts
    finally:
        conn.close()

    return (
        sorted(
            records.values(),
            key=lambda item: (str(item.get("skill_name") or ""), str(item.get("version") or "")),
        ),
        artifacts,
    )


def _read_learning_artifacts(
    *,
    run_dir: Path,
    workspace: Path,
    task_ids: list[str],
    skill_db_path: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    learning: dict[str, Any] = {
        "procedure_matches": [],
        "habit_fast_paths": [],
        "experience_records": [],
        "learning_feedbacks": [],
        "learning_corrections": [],
        "procedure_memory_records": [],
        "skill_lineages": [],
        "skill_lifecycle_records": [],
    }
    artifacts: list[dict[str, Any]] = []
    for task_id in task_ids:
        task_dir = workspace / "tasks" / task_id
        procedure_match_path = task_dir / "procedure" / "procedure_match.json"
        if procedure_match_path.exists():
            payload = _load_json(procedure_match_path)
            if isinstance(payload, dict):
                rejected_candidates = [
                    item
                    for item in list(payload.get("rejected_candidates") or [])
                    if isinstance(item, dict)
                ]
                learning["procedure_matches"].append(
                    {
                        "task_id": task_id,
                        "matched": bool(payload.get("matched")),
                        "procedure_id": payload.get("matched_procedure_id"),
                        "status": payload.get("matched_status"),
                        "confidence": payload.get("confidence"),
                        "reason": payload.get("reason"),
                        "suggestion_mode": payload.get("suggestion_mode"),
                        "required_confirmation": bool(
                            payload.get("required_confirmation")
                            or payload.get("requires_confirmation")
                        ),
                        "rejected_candidate_count": len(rejected_candidates),
                        "rejected_candidates": rejected_candidates,
                        "path": str(procedure_match_path),
                    }
                )
            artifacts.append(
                _artifact_record(
                    procedure_match_path,
                    run_dir=run_dir,
                    kind="procedure_match",
                )
            )

        habit_path = task_dir / "procedure" / "habit_fast_path.json"
        if habit_path.exists():
            payload = _load_json(habit_path)
            if isinstance(payload, dict):
                learning["habit_fast_paths"].append(
                    {
                        "task_id": task_id,
                        "decision": payload.get("decision"),
                        "habit_id": payload.get("habit_id"),
                        "source_experience_ids": payload.get("source_experience_ids", []),
                        "validator_evidence_refs": payload.get(
                            "validator_evidence_refs",
                            [],
                        ),
                        "path": str(habit_path),
                    }
                )
            artifacts.append(_artifact_record(habit_path, run_dir=run_dir, kind="habit_fast_path"))

        experience_path = task_dir / "experience" / "experience_record.json"
        if experience_path.exists():
            payload = _load_json(experience_path)
            if isinstance(payload, dict):
                validation = payload.get("validation")
                if not isinstance(validation, dict):
                    validation = {}
                learning["experience_records"].append(
                    {
                        "task_id": task_id,
                        "experience_id": payload.get("experience_id"),
                        "status": payload.get("status"),
                        "validation_verdict": validation.get("verdict"),
                        "path": str(experience_path),
                    }
                )
            artifacts.append(
                _artifact_record(experience_path, run_dir=run_dir, kind="experience_record")
            )

        feedback_path = task_dir / "learning" / "feedback.json"
        if feedback_path.exists():
            payload = _load_json(feedback_path)
            if isinstance(payload, dict):
                summary = _summarize_learning_feedback(task_id, payload)
                summary["path"] = str(feedback_path)
                learning["learning_feedbacks"].append(summary)
            artifacts.append(
                _artifact_record(feedback_path, run_dir=run_dir, kind="learning_feedback")
            )

        correction_path = task_dir / "learning" / "correction.json"
        if correction_path.exists():
            payload = _load_json(correction_path)
            if isinstance(payload, dict):
                summary = _summarize_learning_correction(task_id, payload)
                summary["path"] = str(correction_path)
                learning["learning_corrections"].append(summary)
            artifacts.append(
                _artifact_record(correction_path, run_dir=run_dir, kind="learning_correction")
            )

    procedures_root = workspace / "procedures"
    if procedures_root.exists():
        for procedure_path in sorted(procedures_root.glob("*/*.json")):
            payload = _load_json(procedure_path)
            if isinstance(payload, dict):
                audit_notes = [
                    item
                    for item in list(payload.get("audit_notes") or [])
                    if isinstance(item, dict)
                ]
                reviews = [
                    item for item in list(payload.get("reviews") or []) if isinstance(item, dict)
                ]
                audit_events = [str(item.get("event")) for item in audit_notes if item.get("event")]
                review_decisions = [
                    str(item.get("decision")) for item in reviews if item.get("decision")
                ]
                status = str(payload.get("status") or "draft")
                previous_status = ""
                restored_status = ""
                for note in reversed(audit_notes):
                    if not restored_status and note.get("event") == "enabled":
                        restored_status = str(note.get("restored_status") or "")
                    if note.get("event") == "disabled" and note.get("previous_status"):
                        previous_status = str(note.get("previous_status"))
                    if (
                        not previous_status
                        and note.get("event") == "reviewed"
                        and note.get("decision") == "rejected"
                        and note.get("from_status")
                    ):
                        previous_status = str(note.get("from_status"))
                    if previous_status and restored_status:
                        break
                object_type = (
                    "habit"
                    if status == "habit" or (status == "disabled" and previous_status == "habit")
                    else "procedure"
                )
                learning["procedure_memory_records"].append(
                    {
                        "procedure_id": payload.get("procedure_id"),
                        "object_type": object_type,
                        "status": status,
                        "previous_status": previous_status or None,
                        "restored_status": restored_status or None,
                        "review_status": payload.get("review_status"),
                        "review_decisions": review_decisions,
                        "latest_review_decision": (
                            review_decisions[-1] if review_decisions else None
                        ),
                        "risk_level": payload.get("risk_level"),
                        "success_count": payload.get("success_count", 0),
                        "failure_count": payload.get("failure_count", 0),
                        "review_count": len(reviews),
                        "audit_event_count": len(audit_notes),
                        "audit_events": audit_events,
                        "latest_audit_event": audit_events[-1] if audit_events else None,
                        "source_experience_ids": payload.get("source_experience_ids", []),
                        "path": str(procedure_path),
                    }
                )
            artifacts.append(
                _artifact_record(
                    procedure_path,
                    run_dir=run_dir,
                    kind="procedure_memory_record",
                )
            )

    managed_skills = workspace / "skills" / "managed"
    if managed_skills.exists():
        for lineage_path in sorted(managed_skills.glob("*/lineage.json")):
            payload = _load_json(lineage_path)
            if isinstance(payload, dict):
                learning["skill_lineages"].append(
                    {
                        "skill_name": payload.get("skill_name"),
                        "version": payload.get("skill_version") or payload.get("version"),
                        "habit_id": payload.get("habit_id"),
                        "procedure_id": payload.get("procedure_id"),
                        "generation_type": payload.get("generation_type"),
                        "source_targets": _string_values(payload.get("source_targets")),
                        "source_experience_ids": payload.get("source_experience_ids", []),
                        "path": str(lineage_path),
                    }
                )
            artifacts.append(_artifact_record(lineage_path, run_dir=run_dir, kind="skill_lineage"))
    if skill_db_path is not None:
        skill_records, skill_artifacts = _read_skill_lifecycle_records(
            run_dir=run_dir,
            db_path=skill_db_path,
        )
        learning["skill_lifecycle_records"].extend(skill_records)
        artifacts.extend(skill_artifacts)
    return learning, artifacts


def _summarize_result_for_index(
    *,
    run_root: Path,
    summary_path: Path,
    summary_result: dict[str, Any],
    scenario_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    run_dir = summary_path.parent
    result = _load_case_result(summary_result)
    scenario_meta = (scenario_metadata or {}).get(str(result.get("id") or ""), {})
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    task_ids = [str(item) for item in result.get("task_ids", [])]
    thread_ids = [str(item) for item in result.get("thread_ids", [])]
    case_dir = Path(str(result.get("case_dir"))) if result.get("case_dir") else None
    workspace = Path(str(result.get("workspace"))) if result.get("workspace") else None

    artifacts: list[dict[str, Any]] = []
    if case_dir is not None:
        artifacts.extend(
            [
                _artifact_record(case_dir / "result.json", run_dir=run_dir, kind="case_result"),
                _artifact_record(case_dir / "diagnostics.md", run_dir=run_dir, kind="diagnostics"),
            ]
        )

    learning_evidence = {
        "procedure_matches": [],
        "habit_fast_paths": [],
        "experience_records": [],
        "learning_feedbacks": [],
        "learning_corrections": [],
        "procedure_memory_records": [],
        "skill_lineages": [],
        "skill_lifecycle_records": [],
    }
    if workspace is not None and workspace.exists():
        learning_evidence, learning_artifacts = _read_learning_artifacts(
            run_dir=run_dir,
            workspace=workspace,
            task_ids=task_ids,
            skill_db_path=run_dir / "data" / f"{result.get('id')}.sqlite3",
        )
        artifacts.extend(learning_artifacts)

    llm_calls = evidence.get("llm_calls", []) if isinstance(evidence, dict) else []
    total_tokens = sum(int(item.get("total_tokens") or 0) for item in llm_calls)
    completion_evaluations = (
        evidence.get("completion_evaluations", []) if isinstance(evidence, dict) else []
    )
    completion_layers = sorted(
        {
            str(item.get("primary_failure_layer"))
            for item in completion_evaluations
            if item.get("primary_failure_layer")
        }
    )
    permissions = evidence.get("permissions", []) if isinstance(evidence, dict) else []
    requires = result.get("requires")
    if not isinstance(requires, list):
        requires = scenario_meta.get("requires", [])
    skip_reason = result.get("skip_reason")
    if not skip_reason and result.get("status") == "skipped":
        skip_reason = result.get("diagnosis")
    failure_layer = result.get("failure_layer") or "none"
    return {
        "run_id": _relative_path(run_dir, run_root),
        "run_dir": str(run_dir),
        "scenario_id": result.get("id"),
        "category": result.get("category") or scenario_meta.get("category", "uncategorized"),
        "kind": result.get("kind") or scenario_meta.get("kind", "task"),
        "requires": requires,
        "status": result.get("status"),
        "skip_reason": skip_reason,
        "diagnosis": result.get("diagnosis"),
        "failure_layer": failure_layer,
        "issues": result.get("issues", []),
        "duration_seconds": result.get("duration_seconds"),
        "task_ids": task_ids,
        "thread_ids": thread_ids,
        "used_skills": evidence.get("used_skills", []) if isinstance(evidence, dict) else [],
        "permission_summary": _summarize_permissions(permissions),
        "policy_decisions": evidence.get("policy_decisions", [])
        if isinstance(evidence, dict)
        else [],
        "node_statuses": evidence.get("node_statuses", []) if isinstance(evidence, dict) else [],
        "critic_actions": evidence.get("critic_actions", []) if isinstance(evidence, dict) else [],
        "request_payload_stats": evidence.get("request_payload_stats", [])
        if isinstance(evidence, dict)
        else [],
        "completion_failure_layers": completion_layers,
        "model_capability_suspected": any(
            bool(item.get("model_capability_suspected")) for item in completion_evaluations
        ),
        "embedding_summary": evidence.get("embedding_summary", {})
        if isinstance(evidence, dict)
        else {},
        "llm_call_count": len(llm_calls),
        "llm_total_tokens": total_tokens,
        "learning_evidence": learning_evidence,
        "artifacts": artifacts,
    }


def _write_evidence_index(run_root: Path, output_json: Path) -> dict[str, Any]:
    run_root = run_root.resolve()
    summary_paths = sorted(run_root.rglob("summary.json"))
    scenario_metadata = _load_scenario_metadata_for_index()
    cases: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for summary_path in summary_paths:
        summary = _load_json(summary_path)
        if not isinstance(summary, dict):
            continue
        results = [item for item in summary.get("results", []) if isinstance(item, dict)]
        runs.append(
            {
                "run_id": _relative_path(summary_path.parent, run_root),
                "run_dir": str(summary_path.parent),
                "created_at": summary.get("created_at"),
                "total": summary.get("total", len(results)),
                "passed": summary.get("passed", 0),
                "failed": summary.get("failed", 0),
                "skipped": summary.get("skipped", 0),
                "summary_path": str(summary_path),
            }
        )
        for result in results:
            cases.append(
                _summarize_result_for_index(
                    run_root=run_root,
                    summary_path=summary_path,
                    summary_result=result,
                    scenario_metadata=scenario_metadata,
                )
            )

    payload = {
        "schema_version": EVIDENCE_INDEX_SCHEMA_VERSION,
        "run_root": str(run_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_count": len(runs),
        "case_count": len(cases),
        "passed": sum(1 for item in cases if item.get("status") == "passed"),
        "failed": sum(1 for item in cases if item.get("status") == "failed"),
        "skipped": sum(1 for item in cases if item.get("status") == "skipped"),
        "summary": {
            "by_status": _count_case_values(cases, lambda case: [case.get("status", "unknown")]),
            "by_scenario": _count_case_values(
                cases,
                lambda case: [case.get("scenario_id", "unknown")],
            ),
            "by_category": _count_case_values(
                cases,
                lambda case: [case.get("category", "uncategorized")],
            ),
            "by_requires": _count_case_values(cases, _requires_values),
            "by_skip_reason": _count_case_values(
                cases,
                lambda case: [case.get("skip_reason")] if case.get("skip_reason") else [],
            ),
            "by_failure_layer": _count_case_values(
                cases,
                lambda case: [case.get("failure_layer", "unknown")],
            ),
        },
        "runs": runs,
        "cases": cases,
    }
    _write_json(output_json, payload)
    _write_evidence_index_markdown(output_json.with_suffix(".md"), payload)
    return payload


def _learning_evidence_count(case: dict[str, Any]) -> int:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return 0
    return sum(len(value) for value in learning.values() if isinstance(value, list))


def _split_filter_values(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            value = part.strip()
            if value and value not in normalized:
                normalized.append(value)
    return normalized


def _string_values(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _learning_values(case: dict[str, Any], section: str, key: str) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    records = learning.get(section)
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        value = record.get(key)
        if value is None:
            continue
        text = str(value)
        if text and text not in values:
            values.append(text)
    return values


def _procedure_ids(case: dict[str, Any]) -> list[str]:
    values = _learning_values(case, "procedure_matches", "procedure_id")
    for value in _learning_values(case, "learning_corrections", "procedure_id"):
        if value not in values:
            values.append(value)
    learning = case.get("learning_evidence")
    if isinstance(learning, dict):
        records = learning.get("procedure_memory_records")
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict) or record.get("object_type") == "habit":
                    continue
                value = record.get("procedure_id")
                if value is None:
                    continue
                text = str(value)
                if text and text not in values:
                    values.append(text)
    for value in _learning_values(case, "skill_lineages", "procedure_id"):
        if value not in values:
            values.append(value)
    return values


def _habit_ids(case: dict[str, Any]) -> list[str]:
    values = _learning_values(case, "habit_fast_paths", "habit_id")
    learning = case.get("learning_evidence")
    if isinstance(learning, dict):
        corrections = learning.get("learning_corrections")
        if isinstance(corrections, list):
            for record in corrections:
                if not isinstance(record, dict) or record.get("object_type") != "habit":
                    continue
                value = record.get("procedure_id")
                if value is None:
                    continue
                text = str(value)
                if text and text not in values:
                    values.append(text)
        records = learning.get("procedure_memory_records")
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict) or record.get("object_type") != "habit":
                    continue
                value = record.get("procedure_id")
                if value is None:
                    continue
                text = str(value)
                if text and text not in values:
                    values.append(text)
    for value in _learning_values(case, "skill_lineages", "habit_id"):
        if value not in values:
            values.append(value)
    return values


def _lineage_skill_names(case: dict[str, Any]) -> list[str]:
    values = _learning_values(case, "skill_lineages", "skill_name")
    for value in _learning_values(case, "skill_lifecycle_records", "skill_name"):
        if value not in values:
            values.append(value)
    return values


def _promotion_generation_types(case: dict[str, Any]) -> list[str]:
    return _learning_values(case, "skill_lineages", "generation_type")


def _learning_list_values(case: dict[str, Any], section: str, key: str) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    records = learning.get(section)
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for value in _string_values(record.get(key)):
            if value not in values:
                values.append(value)
    return values


def _promotion_source_targets(case: dict[str, Any]) -> list[str]:
    return _learning_list_values(case, "skill_lineages", "source_targets")


def _requires_values(case: dict[str, Any]) -> list[str]:
    return _string_values(case.get("requires"))


def _procedure_match_summaries(case: dict[str, Any]) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    records = learning.get("procedure_matches")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        procedure_id = str(record.get("procedure_id") or "none")
        matched_value = record.get("matched")
        if matched_value is True:
            matched = "matched"
        elif matched_value is False:
            matched = "not_matched"
        else:
            matched = "unknown"
        mode = str(record.get("suggestion_mode") or "-")
        requires_confirmation = bool(
            record.get("required_confirmation") or record.get("requires_confirmation")
        )
        rejected_count = record.get("rejected_candidate_count")
        if rejected_count is None:
            rejected = record.get("rejected_candidates")
            rejected_count = len(rejected) if isinstance(rejected, list) else 0
        summary = (
            f"{procedure_id} {matched} mode={mode} "
            f"confirm={str(requires_confirmation).lower()} rejected={rejected_count}"
        )
        if summary not in values:
            values.append(summary)
    return values


def _skill_validation_statuses(case: dict[str, Any]) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    records = learning.get("skill_lifecycle_records")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        validation_results = record.get("validation_passed")
        added_from_result = False
        if isinstance(validation_results, list):
            for result in validation_results:
                status = "passed" if bool(result) else "failed"
                added_from_result = True
                if status not in values:
                    values.append(status)
        if record.get("last_validated_at") and not added_from_result and "recorded" not in values:
            values.append("recorded")
    return values


def _learning_validation_statuses(case: dict[str, Any]) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    procedure_records = learning.get("procedure_memory_records")
    if isinstance(procedure_records, list):
        for record in procedure_records:
            if not isinstance(record, dict):
                continue
            is_approved = (
                str(record.get("review_status") or "") == "approved"
                or str(record.get("latest_review_decision") or "") == "approved"
                or "approved" in _string_values(record.get("review_decisions"))
            )
            if is_approved:
                object_type = str(record.get("object_type") or "procedure")
                if object_type in {"procedure", "habit"}:
                    add(object_type)
                add("approved")
                add("recorded")

    skill_statuses = _skill_validation_statuses(case)
    if skill_statuses:
        add("skill")
        add("recorded")
        for status in skill_statuses:
            add(status)
    return values


def _learning_review_decisions(case: dict[str, Any]) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for decision in _string_values(record.get("review_decisions")):
            if decision not in values:
                values.append(decision)
        latest = str(record.get("latest_review_decision") or "")
        if latest and latest not in values:
            values.append(latest)
    return values


def _learning_audit_events(case: dict[str, Any]) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for event in _string_values(record.get("audit_events")):
            if event not in values:
                values.append(event)
        latest = str(record.get("latest_audit_event") or "")
        if latest and latest not in values:
            values.append(latest)
    return values


def _permission_values(case: dict[str, Any]) -> list[str]:
    summary = case.get("permission_summary")
    if not isinstance(summary, dict):
        return []
    values: list[str] = []
    for key, value in summary.items():
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            values.append(str(key))
    return sorted(values)


def _learning_evidence_kinds(case: dict[str, Any]) -> list[str]:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return []
    kinds: list[str] = []
    for key, records in learning.items():
        if isinstance(records, list) and any(isinstance(item, dict) for item in records):
            kinds.append(str(key))
    return sorted(kinds)


def _completion_layers(case: dict[str, Any]) -> list[str]:
    return _string_values(case.get("completion_failure_layers"))


def _completion_layer_filter_values(case: dict[str, Any]) -> list[str]:
    values = _completion_layers(case)
    if any(value and value != "none" for value in values):
        values.append("recorded")
    return values


def _policy_decisions(case: dict[str, Any]) -> list[str]:
    records = case.get("policy_decisions")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if isinstance(record, dict):
            value = record.get("policy_decision")
        else:
            value = record
        if value is None:
            continue
        text = str(value)
        if text and text not in values:
            values.append(text)
    return values


def _policy_skipped_stages(case: dict[str, Any]) -> list[str]:
    records = case.get("policy_decisions")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for value in _string_values(record.get("skipped_stages")):
            if value not in values:
                values.append(value)
    return values


def _policy_risk_flags(case: dict[str, Any]) -> list[str]:
    records = case.get("policy_decisions")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        flags = record.get("risk_flags")
        if not isinstance(flags, dict):
            continue
        for name, enabled in flags.items():
            text = str(name)
            if enabled and text and text not in values:
                values.append(text)
    return values


def _policy_reasons(case: dict[str, Any]) -> list[str]:
    records = case.get("policy_decisions")
    if not isinstance(records, list):
        return []
    values: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        reason = record.get("reason")
        if reason is None:
            continue
        text = str(reason)
        if text and text not in values:
            values.append(text)
    return values


def _bucket_items(value: int) -> str:
    if value <= 0:
        return "0"
    if value <= 5:
        return "1-5"
    if value <= 20:
        return "6-20"
    return "21+"


def _bucket_json_chars(value: int) -> str:
    if value <= 0:
        return "0"
    if value < 1_000:
        return "1-999"
    if value < 10_000:
        return "1000-9999"
    if value < 50_000:
        return "10000-49999"
    return "50000+"


def _policy_context_budget_signals(case: dict[str, Any]) -> list[str]:
    records = case.get("policy_decisions")
    if not isinstance(records, list):
        return []
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    for record in records:
        if not isinstance(record, dict):
            continue
        budget = record.get("context_budget")
        if not isinstance(budget, dict) or not budget:
            continue
        add("recorded")
        for name, payload in budget.items():
            key = str(name)
            if not key:
                continue
            if isinstance(payload, dict):
                saw_metric = False
                if payload.get("items") is not None:
                    try:
                        items = int(payload.get("items") or 0)
                    except (TypeError, ValueError):
                        items = 0
                    add(f"{key}:items:{_bucket_items(items)}")
                    saw_metric = True
                if payload.get("json_chars") is not None:
                    try:
                        json_chars = int(payload.get("json_chars") or 0)
                    except (TypeError, ValueError):
                        json_chars = 0
                    add(f"{key}:json_chars:{_bucket_json_chars(json_chars)}")
                    saw_metric = True
                if not saw_metric:
                    add(f"{key}:recorded")
                continue
            try:
                json_chars = int(payload or 0)
            except (TypeError, ValueError):
                add(f"{key}:recorded")
                continue
            add(f"{key}:json_chars:{_bucket_json_chars(json_chars)}")
    return values


def _request_payload_stats_signals(case: dict[str, Any]) -> list[str]:
    records = case.get("request_payload_stats")
    if not isinstance(records, list):
        return []
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    for record in records:
        if not isinstance(record, dict):
            continue
        add("recorded")
        scope = str(record.get("scope") or "")
        if scope:
            add(f"scope:{scope}")
        prompt_name = str(record.get("prompt_name") or "")
        if prompt_name:
            add(f"prompt:{prompt_name}")
        if record.get("json_chars") is not None:
            try:
                total_chars = int(record.get("json_chars") or 0)
            except (TypeError, ValueError):
                total_chars = 0
            add(f"total:json_chars:{_bucket_json_chars(total_chars)}")
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        for name, payload in fields.items():
            key = str(name)
            if not key or not isinstance(payload, dict):
                continue
            if payload.get("items") is not None:
                try:
                    items = int(payload.get("items") or 0)
                except (TypeError, ValueError):
                    items = 0
                add(f"{key}:items:{_bucket_items(items)}")
            if payload.get("json_chars") is not None:
                try:
                    json_chars = int(payload.get("json_chars") or 0)
                except (TypeError, ValueError):
                    json_chars = 0
                add(f"{key}:json_chars:{_bucket_json_chars(json_chars)}")
    return values


def _bucket_attempts(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def _node_status_signals(case: dict[str, Any]) -> list[str]:
    records = case.get("node_statuses")
    if not isinstance(records, list):
        return []
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    for record in records:
        if not isinstance(record, dict):
            continue
        add("recorded")
        status = str(record.get("status") or "")
        if status:
            add(f"status:{status}")
        node_type = str(record.get("type") or "")
        if node_type:
            add(f"type:{node_type}")
        skill_name = str(record.get("skill_name") or record.get("selected_skill") or "")
        if skill_name:
            add(f"skill:{skill_name}")
        executor_hint = str(record.get("executor_hint") or "")
        if executor_hint:
            add(f"executor:{executor_hint}")
        action = str(record.get("action") or "")
        if action:
            add(f"action:{action}")
        if record.get("attempts") is not None:
            try:
                attempts = int(record.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            add(f"attempts:{_bucket_attempts(attempts)}")
        if record.get("error_message"):
            add("error:present")
    return values


def _critic_action_signals(case: dict[str, Any]) -> list[str]:
    records = case.get("critic_actions")
    if not isinstance(records, list):
        return []
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    for record in records:
        if not isinstance(record, dict):
            continue
        add("recorded")
        action = str(record.get("action") or "")
        if action:
            add(f"action:{action}")
        target_id = str(record.get("target_id") or "")
        if target_id:
            add(f"target:{target_id}")
        if record.get("summary"):
            add("summary:present")
    return values


def _embedding_summary(case: dict[str, Any]) -> dict[str, Any]:
    summary = case.get("embedding_summary")
    return summary if isinstance(summary, dict) else {}


def _case_int_value(case: dict[str, Any], key: str) -> int | None:
    if key not in case or case.get(key) is None:
        return None
    try:
        return int(case.get(key))
    except (TypeError, ValueError):
        return None


def _case_float_value(case: dict[str, Any], key: str) -> float | None:
    if key not in case or case.get(key) is None:
        return None
    try:
        return float(case.get(key))
    except (TypeError, ValueError):
        return None


def _token_usage_bucket(total_tokens: int) -> str:
    if total_tokens <= 0:
        return "tokens:0"
    if total_tokens < 1_000:
        return "tokens:1-999"
    if total_tokens < 10_000:
        return "tokens:1000-9999"
    return "tokens:10000+"


def _duration_bucket(duration_seconds: float) -> str:
    if duration_seconds < 1:
        return "duration:<1s"
    if duration_seconds < 10:
        return "duration:1-10s"
    if duration_seconds < 60:
        return "duration:10-60s"
    return "duration:60s+"


def _llm_usage_signals(case: dict[str, Any]) -> list[str]:
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    call_count = _case_int_value(case, "llm_call_count")
    total_tokens = _case_int_value(case, "llm_total_tokens")
    duration_seconds = _case_float_value(case, "duration_seconds")

    if call_count is None and total_tokens is None:
        add("llm_unknown")
    elif (call_count or 0) > 0:
        add("llm_called")
    else:
        add("no_llm")

    if total_tokens is not None:
        add(_token_usage_bucket(total_tokens))
    elif call_count == 0:
        add("tokens:0")

    if duration_seconds is not None:
        add(_duration_bucket(duration_seconds))

    return values


def _memory_retrieval_signals(case: dict[str, Any]) -> list[str]:
    values: list[str] = []

    def add(value: str) -> None:
        if value and value not in values:
            values.append(value)

    if str(case.get("failure_layer") or "") == "memory_retrieval":
        add("failure_layer")
        add("recorded")

    summary = _embedding_summary(case)
    vector_match_count = int(summary.get("vector_match_count") or 0)
    vector_memory_entry_count = int(summary.get("vector_memory_entry_count") or 0)
    fallback_count = int(summary.get("fallback_count") or 0)
    error_count = int(summary.get("error_count") or 0)
    incompatible_count = int(summary.get("incompatible_match_count") or 0)

    if vector_match_count > 0:
        add("vector_match")
        add("recorded")
    if vector_memory_entry_count > 0:
        add("vector_memory_entry")
        add("recorded")
    if fallback_count > 0:
        add("fallback")
        add("recorded")
    if error_count > 0:
        add("error")
        add("recorded")
    if incompatible_count > 0:
        add("incompatible")
        add("recorded")
    for provider in _string_values(summary.get("providers")):
        add(f"provider:{provider}")
    for model in _string_values(summary.get("models")):
        add(f"model:{model}")
    return values


def _matches_values(actual_values: list[str], expected_values: list[str]) -> bool:
    if not expected_values:
        return True
    for expected in expected_values:
        for actual in actual_values:
            if actual == expected or fnmatch.fnmatchcase(actual, expected):
                return True
    return False


def _case_matches_evidence_filters(
    case: dict[str, Any],
    *,
    statuses: list[str],
    failure_layers: list[str],
    skills: list[str],
    procedure_ids: list[str],
    habit_ids: list[str],
    lineage_skills: list[str],
    promotion_generation_types: list[str],
    promotion_source_targets: list[str],
    scenario_ids: list[str],
    categories: list[str],
    requires: list[str],
    skip_reasons: list[str],
    permissions: list[str],
    completion_layers: list[str],
    policy_decisions: list[str],
    skipped_stages: list[str],
    policy_risk_flags: list[str],
    policy_reasons: list[str],
    policy_context_budgets: list[str],
    request_payload_stats: list[str],
    node_statuses: list[str],
    critic_actions: list[str],
    memory_retrieval_signals: list[str],
    llm_usage_signals: list[str],
    learning_kinds: list[str],
    learning_validation_statuses: list[str],
    skill_validation_statuses: list[str],
    learning_review_decisions: list[str],
    learning_audit_events: list[str],
) -> bool:
    return (
        _matches_values([str(case.get("status") or "")], statuses)
        and _matches_values([str(case.get("failure_layer") or "")], failure_layers)
        and _matches_values(_string_values(case.get("used_skills")), skills)
        and _matches_values(_procedure_ids(case), procedure_ids)
        and _matches_values(_habit_ids(case), habit_ids)
        and _matches_values(_lineage_skill_names(case), lineage_skills)
        and _matches_values(_promotion_generation_types(case), promotion_generation_types)
        and _matches_values(_promotion_source_targets(case), promotion_source_targets)
        and _matches_values([str(case.get("scenario_id") or "")], scenario_ids)
        and _matches_values([str(case.get("category") or "")], categories)
        and _matches_values(_requires_values(case), requires)
        and _matches_values(
            [str(case.get("skip_reason") or "")] if case.get("skip_reason") else [],
            skip_reasons,
        )
        and _matches_values(_permission_values(case), permissions)
        and _matches_values(_completion_layer_filter_values(case), completion_layers)
        and _matches_values(_policy_decisions(case), policy_decisions)
        and _matches_values(_policy_skipped_stages(case), skipped_stages)
        and _matches_values(_policy_risk_flags(case), policy_risk_flags)
        and _matches_values(_policy_reasons(case), policy_reasons)
        and _matches_values(
            _policy_context_budget_signals(case),
            policy_context_budgets,
        )
        and _matches_values(
            _request_payload_stats_signals(case),
            request_payload_stats,
        )
        and _matches_values(_node_status_signals(case), node_statuses)
        and _matches_values(_critic_action_signals(case), critic_actions)
        and _matches_values(_memory_retrieval_signals(case), memory_retrieval_signals)
        and _matches_values(_llm_usage_signals(case), llm_usage_signals)
        and _matches_values(_learning_evidence_kinds(case), learning_kinds)
        and _matches_values(_learning_validation_statuses(case), learning_validation_statuses)
        and _matches_values(_skill_validation_statuses(case), skill_validation_statuses)
        and _matches_values(_learning_review_decisions(case), learning_review_decisions)
        and _matches_values(_learning_audit_events(case), learning_audit_events)
    )


def _count_case_values(cases: list[dict[str, Any]], value_getter: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        values = value_getter(case)
        for value in sorted(set(str(item) for item in values if str(item))):
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _artifact_path(case: dict[str, Any], kind: str) -> str | None:
    artifacts = case.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if (
            isinstance(artifact, dict)
            and artifact.get("kind") == kind
            and artifact.get("exists")
            and artifact.get("path")
        ):
            return str(artifact.get("path"))
    return None


def _compact_evidence_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": case.get("run_id"),
        "scenario_id": case.get("scenario_id"),
        "category": case.get("category"),
        "kind": case.get("kind"),
        "requires": _requires_values(case),
        "status": case.get("status"),
        "skip_reason": case.get("skip_reason"),
        "failure_layer": case.get("failure_layer"),
        "diagnosis": case.get("diagnosis"),
        "used_skills": _string_values(case.get("used_skills")),
        "procedure_ids": _procedure_ids(case),
        "habit_ids": _habit_ids(case),
        "lineage_skill_names": _lineage_skill_names(case),
        "promotion_generation_types": _promotion_generation_types(case),
        "promotion_source_targets": _promotion_source_targets(case),
        "procedure_match_summaries": _procedure_match_summaries(case),
        "learning_validation_statuses": _learning_validation_statuses(case),
        "skill_validation_statuses": _skill_validation_statuses(case),
        "learning_review_decisions": _learning_review_decisions(case),
        "learning_audit_events": _learning_audit_events(case),
        "permission_summary": dict(case.get("permission_summary") or {}),
        "policy_decisions": _policy_decisions(case),
        "skipped_stages": _policy_skipped_stages(case),
        "policy_risk_flags": _policy_risk_flags(case),
        "policy_reasons": _policy_reasons(case),
        "policy_context_budget_signals": _policy_context_budget_signals(case),
        "request_payload_stats_signals": _request_payload_stats_signals(case),
        "node_status_signals": _node_status_signals(case),
        "critic_action_signals": _critic_action_signals(case),
        "memory_retrieval_signals": _memory_retrieval_signals(case),
        "llm_call_count": _case_int_value(case, "llm_call_count"),
        "llm_total_tokens": _case_int_value(case, "llm_total_tokens"),
        "duration_seconds": _case_float_value(case, "duration_seconds"),
        "llm_usage_signals": _llm_usage_signals(case),
        "embedding_providers": _string_values(_embedding_summary(case).get("providers")),
        "embedding_models": _string_values(_embedding_summary(case).get("models")),
        "vector_match_count": int(_embedding_summary(case).get("vector_match_count") or 0),
        "vector_memory_entry_count": int(
            _embedding_summary(case).get("vector_memory_entry_count") or 0
        ),
        "embedding_fallback_count": int(_embedding_summary(case).get("fallback_count") or 0),
        "embedding_error_count": int(_embedding_summary(case).get("error_count") or 0),
        "embedding_incompatible_match_count": int(
            _embedding_summary(case).get("incompatible_match_count") or 0
        ),
        "learning_evidence_kinds": _learning_evidence_kinds(case),
        "task_ids": _string_values(case.get("task_ids")),
        "thread_ids": _string_values(case.get("thread_ids")),
        "completion_failure_layers": _completion_layers(case),
        "model_capability_suspected": bool(case.get("model_capability_suspected")),
        "learning_evidence_count": _learning_evidence_count(case),
        "diagnostics_path": _artifact_path(case, "diagnostics"),
        "result_path": _artifact_path(case, "case_result"),
    }


def query_evidence_index(
    payload: dict[str, Any],
    *,
    statuses: list[str] | None = None,
    failure_layers: list[str] | None = None,
    skills: list[str] | None = None,
    procedure_ids: list[str] | None = None,
    habit_ids: list[str] | None = None,
    lineage_skills: list[str] | None = None,
    promotion_generation_types: list[str] | None = None,
    promotion_source_targets: list[str] | None = None,
    scenario_ids: list[str] | None = None,
    categories: list[str] | None = None,
    requires: list[str] | None = None,
    skip_reasons: list[str] | None = None,
    permissions: list[str] | None = None,
    completion_layers: list[str] | None = None,
    policy_decisions: list[str] | None = None,
    skipped_stages: list[str] | None = None,
    policy_risk_flags: list[str] | None = None,
    policy_reasons: list[str] | None = None,
    policy_context_budgets: list[str] | None = None,
    request_payload_stats: list[str] | None = None,
    node_statuses: list[str] | None = None,
    critic_actions: list[str] | None = None,
    memory_retrieval_signals: list[str] | None = None,
    llm_usage_signals: list[str] | None = None,
    learning_kinds: list[str] | None = None,
    learning_validation_statuses: list[str] | None = None,
    skill_validation_statuses: list[str] | None = None,
    learning_review_decisions: list[str] | None = None,
    learning_audit_events: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    cases = [item for item in payload.get("cases", []) if isinstance(item, dict)]
    filters = {
        "status": _split_filter_values(statuses),
        "failure_layer": _split_filter_values(failure_layers),
        "skill": _split_filter_values(skills),
        "procedure": _split_filter_values(procedure_ids),
        "habit": _split_filter_values(habit_ids),
        "lineage_skill": _split_filter_values(lineage_skills),
        "promotion_generation_type": _split_filter_values(promotion_generation_types),
        "promotion_source_target": _split_filter_values(promotion_source_targets),
        "scenario": _split_filter_values(scenario_ids),
        "category": _split_filter_values(categories),
        "requires": _split_filter_values(requires),
        "skip_reason": _split_filter_values(skip_reasons),
        "permission": _split_filter_values(permissions),
        "completion_layer": _split_filter_values(completion_layers),
        "policy_decision": _split_filter_values(policy_decisions),
        "skipped_stage": _split_filter_values(skipped_stages),
        "policy_risk_flag": _split_filter_values(policy_risk_flags),
        "policy_reason": _split_filter_values(policy_reasons),
        "policy_context_budget": _split_filter_values(policy_context_budgets),
        "request_payload_stats": _split_filter_values(request_payload_stats),
        "node_status": _split_filter_values(node_statuses),
        "critic_action": _split_filter_values(critic_actions),
        "memory_retrieval": _split_filter_values(memory_retrieval_signals),
        "llm_usage": _split_filter_values(llm_usage_signals),
        "learning_kind": _split_filter_values(learning_kinds),
        "learning_validation": _split_filter_values(learning_validation_statuses),
        "skill_validation": _split_filter_values(skill_validation_statuses),
        "learning_review_decision": _split_filter_values(learning_review_decisions),
        "learning_audit_event": _split_filter_values(learning_audit_events),
    }
    matched = [
        case
        for case in cases
        if _case_matches_evidence_filters(
            case,
            statuses=filters["status"],
            failure_layers=filters["failure_layer"],
            skills=filters["skill"],
            procedure_ids=filters["procedure"],
            habit_ids=filters["habit"],
            lineage_skills=filters["lineage_skill"],
            promotion_generation_types=filters["promotion_generation_type"],
            promotion_source_targets=filters["promotion_source_target"],
            scenario_ids=filters["scenario"],
            categories=filters["category"],
            requires=filters["requires"],
            skip_reasons=filters["skip_reason"],
            permissions=filters["permission"],
            completion_layers=filters["completion_layer"],
            policy_decisions=filters["policy_decision"],
            skipped_stages=filters["skipped_stage"],
            policy_risk_flags=filters["policy_risk_flag"],
            policy_reasons=filters["policy_reason"],
            policy_context_budgets=filters["policy_context_budget"],
            request_payload_stats=filters["request_payload_stats"],
            node_statuses=filters["node_status"],
            critic_actions=filters["critic_action"],
            memory_retrieval_signals=filters["memory_retrieval"],
            llm_usage_signals=filters["llm_usage"],
            learning_kinds=filters["learning_kind"],
            learning_validation_statuses=filters["learning_validation"],
            skill_validation_statuses=filters["skill_validation"],
            learning_review_decisions=filters["learning_review_decision"],
            learning_audit_events=filters["learning_audit_event"],
        )
    ]
    returned = matched if limit is None or limit <= 0 else matched[:limit]
    summary = {
        "source_run_count": payload.get("run_count", len(payload.get("runs", []))),
        "total_index_cases": len(cases),
        "matched_cases": len(matched),
        "returned_cases": len(returned),
        "by_status": _count_case_values(matched, lambda case: [case.get("status", "unknown")]),
        "by_scenario": _count_case_values(
            matched,
            lambda case: [case.get("scenario_id", "unknown")],
        ),
        "by_category": _count_case_values(
            matched,
            lambda case: [case.get("category", "uncategorized")],
        ),
        "by_requires": _count_case_values(matched, _requires_values),
        "by_skip_reason": _count_case_values(
            matched,
            lambda case: [case.get("skip_reason")] if case.get("skip_reason") else [],
        ),
        "by_failure_layer": _count_case_values(
            matched,
            lambda case: [case.get("failure_layer", "unknown")],
        ),
        "by_skill": _count_case_values(
            matched,
            lambda case: _string_values(case.get("used_skills")),
        ),
        "by_procedure": _count_case_values(matched, _procedure_ids),
        "by_habit": _count_case_values(matched, _habit_ids),
        "by_lineage_skill": _count_case_values(matched, _lineage_skill_names),
        "by_promotion_generation_type": _count_case_values(
            matched,
            _promotion_generation_types,
        ),
        "by_promotion_source_target": _count_case_values(
            matched,
            _promotion_source_targets,
        ),
        "by_permission": _count_case_values(matched, _permission_values),
        "by_completion_layer": _count_case_values(matched, _completion_layers),
        "by_policy_decision": _count_case_values(matched, _policy_decisions),
        "by_skipped_stage": _count_case_values(matched, _policy_skipped_stages),
        "by_policy_risk_flag": _count_case_values(matched, _policy_risk_flags),
        "by_policy_reason": _count_case_values(matched, _policy_reasons),
        "by_policy_context_budget": _count_case_values(
            matched,
            _policy_context_budget_signals,
        ),
        "by_request_payload_stats": _count_case_values(
            matched,
            _request_payload_stats_signals,
        ),
        "by_node_status": _count_case_values(matched, _node_status_signals),
        "by_critic_action": _count_case_values(matched, _critic_action_signals),
        "by_memory_retrieval": _count_case_values(matched, _memory_retrieval_signals),
        "by_llm_usage": _count_case_values(matched, _llm_usage_signals),
        "by_learning_kind": _count_case_values(matched, _learning_evidence_kinds),
        "by_learning_validation": _count_case_values(matched, _learning_validation_statuses),
        "by_skill_validation": _count_case_values(matched, _skill_validation_statuses),
        "by_learning_review_decision": _count_case_values(
            matched,
            _learning_review_decisions,
        ),
        "by_learning_audit_event": _count_case_values(matched, _learning_audit_events),
    }
    return {
        "schema_version": EVIDENCE_QUERY_SCHEMA_VERSION,
        "source_schema_version": payload.get("schema_version"),
        "run_root": payload.get("run_root"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": filters,
        "summary": summary,
        "cases": [_compact_evidence_case(case) for case in returned],
    }


def _markdown_value_list(values: list[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "-"


def _markdown_count_line(label: str, counts: dict[str, int]) -> str:
    if not counts:
        return f"- {label}: -"
    pairs = ", ".join(f"`{key}`={value}" for key, value in counts.items())
    return f"- {label}: {pairs}"


def _markdown_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("\n", " ").replace("|", "\\|")


def render_evidence_query_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    lines = [
        "# Eval Evidence Query",
        "",
        f"- run_root: `{payload.get('run_root')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- source_schema_version: `{payload.get('source_schema_version')}`",
        f"- total_index_cases: {summary.get('total_index_cases', 0)}",
        f"- matched_cases: {summary.get('matched_cases', 0)}",
        f"- returned_cases: {summary.get('returned_cases', 0)}",
        "",
        "## Filters",
        "",
    ]
    active_filter_count = 0
    for key in [
        "status",
        "failure_layer",
        "skill",
        "procedure",
        "habit",
        "lineage_skill",
        "promotion_generation_type",
        "promotion_source_target",
        "scenario",
        "category",
        "requires",
        "skip_reason",
        "permission",
        "completion_layer",
        "policy_decision",
        "skipped_stage",
        "policy_risk_flag",
        "policy_reason",
        "policy_context_budget",
        "request_payload_stats",
        "node_status",
        "critic_action",
        "memory_retrieval",
        "llm_usage",
        "learning_kind",
        "learning_validation",
        "skill_validation",
        "learning_review_decision",
        "learning_audit_event",
    ]:
        values = _string_values(filters.get(key))
        if values:
            active_filter_count += 1
            lines.append(f"- {key}: {_markdown_value_list(values)}")
    if active_filter_count == 0:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Summary",
            "",
            _markdown_count_line("status", summary.get("by_status", {})),
            _markdown_count_line("scenario", summary.get("by_scenario", {})),
            _markdown_count_line("category", summary.get("by_category", {})),
            _markdown_count_line("requires", summary.get("by_requires", {})),
            _markdown_count_line("skip_reason", summary.get("by_skip_reason", {})),
            _markdown_count_line("failure_layer", summary.get("by_failure_layer", {})),
            _markdown_count_line("skill", summary.get("by_skill", {})),
            _markdown_count_line("procedure", summary.get("by_procedure", {})),
            _markdown_count_line("habit", summary.get("by_habit", {})),
            _markdown_count_line("lineage_skill", summary.get("by_lineage_skill", {})),
            _markdown_count_line(
                "promotion_generation_type",
                summary.get("by_promotion_generation_type", {}),
            ),
            _markdown_count_line(
                "promotion_source_target",
                summary.get("by_promotion_source_target", {}),
            ),
            _markdown_count_line("permission", summary.get("by_permission", {})),
            _markdown_count_line("completion_layer", summary.get("by_completion_layer", {})),
            _markdown_count_line("policy_decision", summary.get("by_policy_decision", {})),
            _markdown_count_line("skipped_stage", summary.get("by_skipped_stage", {})),
            _markdown_count_line(
                "policy_risk_flag",
                summary.get("by_policy_risk_flag", {}),
            ),
            _markdown_count_line("policy_reason", summary.get("by_policy_reason", {})),
            _markdown_count_line(
                "policy_context_budget",
                summary.get("by_policy_context_budget", {}),
            ),
            _markdown_count_line(
                "request_payload_stats",
                summary.get("by_request_payload_stats", {}),
            ),
            _markdown_count_line("node_status", summary.get("by_node_status", {})),
            _markdown_count_line("critic_action", summary.get("by_critic_action", {})),
            _markdown_count_line(
                "memory_retrieval",
                summary.get("by_memory_retrieval", {}),
            ),
            _markdown_count_line("llm_usage", summary.get("by_llm_usage", {})),
            _markdown_count_line("learning_kind", summary.get("by_learning_kind", {})),
            _markdown_count_line(
                "learning_validation",
                summary.get("by_learning_validation", {}),
            ),
            _markdown_count_line("skill_validation", summary.get("by_skill_validation", {})),
            _markdown_count_line(
                "learning_review_decision",
                summary.get("by_learning_review_decision", {}),
            ),
            _markdown_count_line(
                "learning_audit_event",
                summary.get("by_learning_audit_event", {}),
            ),
            "",
            "## Cases",
            "",
            "| Run | Scenario | Category | Requires | Status | Skip Reason | Failure Layer | Completion Layers | Policy Decisions | Skipped Stages | Policy Risk Flags | Policy Reasons | Policy Context Budget | Request Payload Stats | Node Status | Critic Actions | Memory Retrieval | LLM Usage | Skills | Procedures | Habits | Lineage Skills | Promotion Types | Promotion Targets | Learning Validation | Skill Validation | Review Decisions | Audit Events | Permissions | Learning Kinds | Diagnostics |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for case in payload.get("cases", []):
        if not isinstance(case, dict):
            continue
        diagnostics = "-"
        if case.get("diagnostics_path"):
            diagnostics = f"[diagnostics]({case.get('diagnostics_path')})"
        lines.append(
            f"| `{_markdown_cell(case.get('run_id'))}` | "
            f"`{_markdown_cell(case.get('scenario_id'))}` | "
            f"{_markdown_cell(case.get('category'))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('requires'))))} | "
            f"{_markdown_cell(case.get('status'))} | "
            f"{_markdown_cell(case.get('skip_reason'))} | "
            f"{_markdown_cell(case.get('failure_layer'))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('completion_failure_layers'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_decisions'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('skipped_stages'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_risk_flags'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_reasons'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_context_budget_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('request_payload_stats_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('node_status_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('critic_action_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('memory_retrieval_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('llm_usage_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('used_skills'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('procedure_ids'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('habit_ids'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('lineage_skill_names'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('promotion_generation_types'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('promotion_source_targets'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('learning_validation_statuses'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('skill_validation_statuses'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('learning_review_decisions'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('learning_audit_events'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(list((case.get('permission_summary') or {}).keys()))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('learning_evidence_kinds'))))} | "
            f"{diagnostics} |"
        )
    return "\n".join(lines) + "\n"


def _dashboard_query_command(filter_name: str, value: str | None = None) -> str:
    base = "poetry run python scripts/run_real_world_evals.py --query-evidence-index"
    if value is None:
        return base
    return f"{base} --{filter_name} {value}"


def _dashboard_filter_value(value: str) -> str:
    return shlex.quote(str(value))


def _dashboard_scenario_run_command(scenario_id: str, requires: list[str]) -> str:
    required = {str(item).strip().lower() for item in requires if str(item).strip()}
    parts = ["poetry run python scripts/run_real_world_evals.py"]
    if "network" in required:
        parts.append("--include-network")
    if "l4" in required:
        parts.append("--include-l4")
    if "complex" in required:
        parts.append("--include-complex")
    parts.extend(["--scenario", shlex.quote(str(scenario_id))])
    return " ".join(parts)


def _dashboard_hotspot_signal(value: str) -> bool:
    signal = str(value).strip()
    return (
        signal.endswith(":items:21+")
        or signal.endswith(":json_chars:10000-49999")
        or signal.endswith(":json_chars:50000+")
    )


def _failure_layer_repair_hint(layer: str) -> str:
    hints = {
        "routing": "先看 interaction router 与 turn trace。",
        "planning": "先看 plan artifact、planner prompt 输入和 selected skill。",
        "permission": "先看 permission summary、policy decision 和 max_permission。",
        "skill_or_tool": "先看 node output、skill contract、executor 错误和输入 payload。",
        "task_flow_or_output": "先看 workspace artifacts、completion evaluation 和 expected output。",
        "memory_retrieval": "先看 vector recall、procedure match 和 context snapshot。",
        "completion_evaluation": "先看 completion/evaluation.json 和 failure attribution。",
        "model_capability": "先确认工具证据已给足，再记录模型能力边界。",
        "assertion": "先核对 scenario assertions 与真实产物。",
        "infrastructure": "先看命令退出码、超时、环境和依赖。",
        "none": "通过或跳过 case；无需失败修复入口。",
    }
    return hints.get(layer, "先看 diagnostics.md、result.json 和 workspace artifacts。")


def _learning_lineage_query(payload: dict[str, Any], *, limit: int) -> dict[str, Any]:
    base = query_evidence_index(payload, limit=0)
    cases = [
        case
        for case in base.get("cases", [])
        if case.get("procedure_ids") or case.get("habit_ids") or case.get("lineage_skill_names")
    ]
    returned = cases if limit <= 0 else cases[:limit]
    summary = dict(base.get("summary") or {})
    summary["matched_cases"] = len(cases)
    summary["returned_cases"] = len(returned)
    summary["by_status"] = _count_case_values(
        returned, lambda case: [case.get("status", "unknown")]
    )
    summary["by_scenario"] = _count_case_values(
        returned, lambda case: [case.get("scenario_id", "unknown")]
    )
    summary["by_category"] = _count_case_values(
        returned,
        lambda case: [case.get("category", "uncategorized")],
    )
    summary["by_requires"] = _count_case_values(returned, _requires_values)
    summary["by_skip_reason"] = _count_case_values(
        returned,
        lambda case: [case.get("skip_reason")] if case.get("skip_reason") else [],
    )
    summary["by_failure_layer"] = _count_case_values(
        returned,
        lambda case: [case.get("failure_layer", "unknown")],
    )
    summary["by_skill"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("used_skills")),
    )
    summary["by_procedure"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("procedure_ids")),
    )
    summary["by_habit"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("habit_ids")),
    )
    summary["by_lineage_skill"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("lineage_skill_names")),
    )
    summary["by_promotion_generation_type"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("promotion_generation_types")),
    )
    summary["by_promotion_source_target"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("promotion_source_targets")),
    )
    summary["by_policy_decision"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("policy_decisions")),
    )
    summary["by_skipped_stage"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("skipped_stages")),
    )
    summary["by_policy_risk_flag"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("policy_risk_flags")),
    )
    summary["by_policy_reason"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("policy_reasons")),
    )
    summary["by_policy_context_budget"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("policy_context_budget_signals")),
    )
    summary["by_request_payload_stats"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("request_payload_stats_signals")),
    )
    summary["by_node_status"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("node_status_signals")),
    )
    summary["by_critic_action"] = _count_case_values(
        returned,
        lambda case: _string_values(case.get("critic_action_signals")),
    )
    summary["by_llm_usage"] = _count_case_values(returned, _llm_usage_signals)
    return {
        "schema_version": EVIDENCE_QUERY_SCHEMA_VERSION,
        "source_schema_version": payload.get("schema_version"),
        "run_root": payload.get("run_root"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {"learning_lineage": ["any"]},
        "summary": summary,
        "cases": returned,
    }


def _dashboard_filtered_query(
    payload: dict[str, Any],
    *,
    filters: dict[str, list[str]],
    predicate: Any,
    limit: int,
) -> dict[str, Any]:
    source_cases = [item for item in payload.get("cases", []) if isinstance(item, dict)]
    matched_source = [case for case in source_cases if predicate(case)]
    matched = [_compact_evidence_case(case) for case in matched_source]
    returned = matched if limit <= 0 else matched[:limit]
    summary = {
        "source_run_count": payload.get("run_count", len(payload.get("runs", []))),
        "total_index_cases": len(source_cases),
        "matched_cases": len(matched),
        "returned_cases": len(returned),
        "by_status": _count_case_values(matched, lambda case: [case.get("status", "unknown")]),
        "by_scenario": _count_case_values(
            matched,
            lambda case: [case.get("scenario_id", "unknown")],
        ),
        "by_category": _count_case_values(
            matched,
            lambda case: [case.get("category", "uncategorized")],
        ),
        "by_requires": _count_case_values(matched, _requires_values),
        "by_skip_reason": _count_case_values(
            matched,
            lambda case: [case.get("skip_reason")] if case.get("skip_reason") else [],
        ),
        "by_failure_layer": _count_case_values(
            matched,
            lambda case: [case.get("failure_layer", "unknown")],
        ),
        "by_skill": _count_case_values(
            matched,
            lambda case: _string_values(case.get("used_skills")),
        ),
        "by_procedure": _count_case_values(
            matched,
            lambda case: _string_values(case.get("procedure_ids")),
        ),
        "by_habit": _count_case_values(
            matched,
            lambda case: _string_values(case.get("habit_ids")),
        ),
        "by_lineage_skill": _count_case_values(
            matched,
            lambda case: _string_values(case.get("lineage_skill_names")),
        ),
        "by_promotion_generation_type": _count_case_values(
            matched,
            lambda case: _string_values(case.get("promotion_generation_types")),
        ),
        "by_promotion_source_target": _count_case_values(
            matched,
            lambda case: _string_values(case.get("promotion_source_targets")),
        ),
        "by_permission": _count_case_values(
            matched,
            lambda case: _string_values(list((case.get("permission_summary") or {}).keys())),
        ),
        "by_completion_layer": _count_case_values(
            matched,
            lambda case: _string_values(case.get("completion_failure_layers")),
        ),
        "by_policy_decision": _count_case_values(
            matched,
            lambda case: _string_values(case.get("policy_decisions")),
        ),
        "by_skipped_stage": _count_case_values(
            matched,
            lambda case: _string_values(case.get("skipped_stages")),
        ),
        "by_policy_risk_flag": _count_case_values(
            matched,
            lambda case: _string_values(case.get("policy_risk_flags")),
        ),
        "by_policy_reason": _count_case_values(
            matched,
            lambda case: _string_values(case.get("policy_reasons")),
        ),
        "by_policy_context_budget": _count_case_values(
            matched,
            lambda case: _string_values(case.get("policy_context_budget_signals")),
        ),
        "by_request_payload_stats": _count_case_values(
            matched,
            lambda case: _string_values(case.get("request_payload_stats_signals")),
        ),
        "by_node_status": _count_case_values(
            matched,
            lambda case: _string_values(case.get("node_status_signals")),
        ),
        "by_critic_action": _count_case_values(
            matched,
            lambda case: _string_values(case.get("critic_action_signals")),
        ),
        "by_memory_retrieval": _count_case_values(
            matched,
            lambda case: _string_values(case.get("memory_retrieval_signals")),
        ),
        "by_llm_usage": _count_case_values(matched, _llm_usage_signals),
        "by_learning_kind": _count_case_values(
            matched,
            lambda case: _string_values(case.get("learning_evidence_kinds")),
        ),
        "by_learning_validation": _count_case_values(
            matched,
            lambda case: _string_values(case.get("learning_validation_statuses")),
        ),
        "by_skill_validation": _count_case_values(
            matched,
            lambda case: _string_values(case.get("skill_validation_statuses")),
        ),
        "by_learning_review_decision": _count_case_values(
            matched,
            lambda case: _string_values(case.get("learning_review_decisions")),
        ),
        "by_learning_audit_event": _count_case_values(
            matched,
            lambda case: _string_values(case.get("learning_audit_events")),
        ),
    }
    return {
        "schema_version": EVIDENCE_QUERY_SCHEMA_VERSION,
        "source_schema_version": payload.get("schema_version"),
        "run_root": payload.get("run_root"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": filters,
        "summary": summary,
        "cases": returned,
    }


def _dashboard_regression_commands(
    repair_entrypoints: list[dict[str, Any]],
    views: dict[str, dict[str, Any]],
    dashboard_summary: dict[str, Any],
    source_cases: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    commands = [
        {
            "kind": "scenario_check",
            "label": "Validate scenario definitions",
            "command": "poetry run python scripts/run_real_world_evals.py --check-only",
            "matched_cases": 0,
        },
        {
            "kind": "evidence_index",
            "label": "Rebuild evidence index from eval_runs",
            "command": "poetry run python scripts/run_real_world_evals.py --build-evidence-index",
            "matched_cases": 0,
        },
        {
            "kind": "dashboard",
            "label": "Render evidence dashboard",
            "command": "poetry run python scripts/run_real_world_evals.py --evidence-dashboard",
            "matched_cases": 0,
        },
    ]
    seen = {str(item["command"]) for item in commands}

    for item in repair_entrypoints:
        command = str(item.get("query_command") or "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "repair_query",
                "label": f"Repair {item.get('failure_layer') or 'unknown'} cases",
                "command": command,
                "matched_cases": int(item.get("case_count") or 0),
            }
        )

    failed_scenario_commands: dict[str, dict[str, Any]] = {}
    for case in source_cases or []:
        if not isinstance(case, dict) or case.get("status") != "failed":
            continue
        scenario_id = str(case.get("scenario_id") or "").strip()
        if not scenario_id:
            continue
        command = _dashboard_scenario_run_command(scenario_id, _requires_values(case))
        entry = failed_scenario_commands.setdefault(
            command,
            {
                "kind": "scenario_rerun",
                "label": f"Rerun scenario {scenario_id}",
                "command": command,
                "matched_cases": 0,
            },
        )
        entry["matched_cases"] = int(entry["matched_cases"]) + 1
    for item in sorted(
        failed_scenario_commands.values(),
        key=lambda command: str(command.get("label") or ""),
    ):
        command = str(item.get("command") or "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        commands.append(item)

    for key, view in views.items():
        query = view.get("query") if isinstance(view.get("query"), dict) else {}
        query_summary = query.get("summary") if isinstance(query.get("summary"), dict) else {}
        matched_cases = int(query_summary.get("matched_cases") or 0)
        command = str(view.get("query_command") or "").strip()
        if not command or command in seen or matched_cases <= 0:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "dashboard_view_query",
                "view": key,
                "label": str(view.get("title") or key),
                "command": command,
                "matched_cases": matched_cases,
            }
        )

    for status, count in sorted((dashboard_summary.get("by_status") or {}).items()):
        status = str(status).strip()
        if not status:
            continue
        command = _dashboard_query_command("evidence-status", _dashboard_filter_value(status))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "status_query",
                "label": f"Status {status}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for scenario, count in sorted((dashboard_summary.get("by_scenario") or {}).items()):
        scenario = str(scenario).strip()
        if not scenario:
            continue
        command = _dashboard_query_command("evidence-scenario", _dashboard_filter_value(scenario))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "scenario_query",
                "label": f"Scenario {scenario}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for category, count in sorted((dashboard_summary.get("by_category") or {}).items()):
        category = str(category).strip()
        if not category:
            continue
        command = _dashboard_query_command("evidence-category", _dashboard_filter_value(category))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "category_query",
                "label": f"Category {category}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for requires, count in sorted((dashboard_summary.get("by_requires") or {}).items()):
        requires = str(requires).strip()
        if not requires:
            continue
        command = _dashboard_query_command("evidence-requires", _dashboard_filter_value(requires))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "requires_query",
                "label": f"Requires {requires}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for reason, count in sorted((dashboard_summary.get("by_skip_reason") or {}).items()):
        reason = str(reason).strip()
        if not reason:
            continue
        command = _dashboard_query_command("evidence-skip-reason", _dashboard_filter_value(reason))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "skip_reason_query",
                "label": f"Skip reason {reason}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for skill, count in sorted((dashboard_summary.get("by_skill") or {}).items()):
        skill = str(skill).strip()
        if not skill:
            continue
        command = _dashboard_query_command("evidence-skill", _dashboard_filter_value(skill))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "skill_query",
                "label": f"Skill {skill}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for procedure_id, count in sorted((dashboard_summary.get("by_procedure") or {}).items()):
        procedure_id = str(procedure_id).strip()
        if not procedure_id:
            continue
        command = _dashboard_query_command(
            "evidence-procedure",
            _dashboard_filter_value(procedure_id),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "procedure_query",
                "label": f"Procedure {procedure_id}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for habit_id, count in sorted((dashboard_summary.get("by_habit") or {}).items()):
        habit_id = str(habit_id).strip()
        if not habit_id:
            continue
        command = _dashboard_query_command(
            "evidence-habit",
            _dashboard_filter_value(habit_id),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "habit_query",
                "label": f"Habit {habit_id}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for skill, count in sorted((dashboard_summary.get("by_lineage_skill") or {}).items()):
        skill = str(skill).strip()
        if not skill:
            continue
        command = _dashboard_query_command(
            "evidence-lineage-skill",
            _dashboard_filter_value(skill),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "lineage_skill_query",
                "label": f"Lineage skill {skill}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for generation_type, count in sorted(
        (dashboard_summary.get("by_promotion_generation_type") or {}).items()
    ):
        generation_type = str(generation_type).strip()
        if not generation_type:
            continue
        command = _dashboard_query_command(
            "evidence-promotion-generation-type",
            _dashboard_filter_value(generation_type),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "promotion_generation_type_query",
                "label": f"Promotion generation type {generation_type}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for target, count in sorted(
        (dashboard_summary.get("by_promotion_source_target") or {}).items()
    ):
        if not str(target).strip():
            continue
        command = _dashboard_query_command(
            "evidence-promotion-source-target",
            _dashboard_filter_value(str(target)),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "promotion_source_target_query",
                "label": f"Promotion source target {target}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for permission, count in sorted((dashboard_summary.get("by_permission") or {}).items()):
        permission = str(permission).strip()
        if not permission:
            continue
        command = _dashboard_query_command(
            "evidence-permission", _dashboard_filter_value(permission)
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "permission_query",
                "label": f"Permission {permission}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for layer, count in sorted((dashboard_summary.get("by_completion_layer") or {}).items()):
        layer = str(layer).strip()
        if not layer or layer == "none":
            continue
        command = _dashboard_query_command("evidence-completion-layer", layer)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "completion_layer_query",
                "label": f"Completion layer {layer}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for decision, count in sorted((dashboard_summary.get("by_policy_decision") or {}).items()):
        if not str(decision).strip():
            continue
        command = _dashboard_query_command("evidence-policy-decision", str(decision))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "policy_decision_query",
                "label": f"Policy decision {decision}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for stage, count in sorted((dashboard_summary.get("by_skipped_stage") or {}).items()):
        if not str(stage).strip():
            continue
        command = _dashboard_query_command("evidence-skipped-stage", str(stage))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "skipped_stage_query",
                "label": f"Skipped stage {stage}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for flag, count in sorted((dashboard_summary.get("by_policy_risk_flag") or {}).items()):
        if not str(flag).strip():
            continue
        command = _dashboard_query_command("evidence-policy-risk-flag", str(flag))
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "policy_risk_flag_query",
                "label": f"Policy risk flag {flag}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for reason, count in sorted((dashboard_summary.get("by_policy_reason") or {}).items()):
        reason = str(reason).strip()
        if not reason:
            continue
        command = _dashboard_query_command(
            "evidence-policy-reason",
            _dashboard_filter_value(reason),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "policy_reason_query",
                "label": f"Policy reason {reason}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for signal, count in sorted((dashboard_summary.get("by_policy_context_budget") or {}).items()):
        signal = str(signal).strip()
        if not _dashboard_hotspot_signal(signal):
            continue
        command = _dashboard_query_command(
            "evidence-policy-context-budget",
            _dashboard_filter_value(signal),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "policy_context_budget_query",
                "label": f"Policy context budget {signal}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for signal, count in sorted((dashboard_summary.get("by_request_payload_stats") or {}).items()):
        signal = str(signal).strip()
        if not _dashboard_hotspot_signal(signal):
            continue
        command = _dashboard_query_command(
            "evidence-request-payload-stats",
            _dashboard_filter_value(signal),
        )
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "request_payload_stats_query",
                "label": f"Request payload stats {signal}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for status, count in sorted((dashboard_summary.get("by_node_status") or {}).items()):
        status = str(status).strip()
        if not (
            status.startswith("status:")
            or status.startswith("attempts:")
            or status.startswith("error:")
        ):
            continue
        command = _dashboard_query_command("evidence-node-status", status)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "node_status_query",
                "label": f"Node status {status}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for action, count in sorted((dashboard_summary.get("by_critic_action") or {}).items()):
        action = str(action).strip()
        if not action.startswith("action:"):
            continue
        command = _dashboard_query_command("evidence-critic-action", action)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "critic_action_query",
                "label": f"Critic action {action}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    memory_retrieval_dynamic_signals = {
        "failure_layer",
        "vector_match",
        "vector_memory_entry",
        "fallback",
        "error",
        "incompatible",
    }
    for signal, count in sorted((dashboard_summary.get("by_memory_retrieval") or {}).items()):
        signal = str(signal).strip()
        if signal not in memory_retrieval_dynamic_signals:
            continue
        command = _dashboard_query_command("evidence-memory-retrieval", signal)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "memory_retrieval_query",
                "label": f"Memory retrieval {signal}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for signal, count in sorted((dashboard_summary.get("by_llm_usage") or {}).items()):
        signal = str(signal).strip()
        if not (
            signal in {"llm_called", "no_llm", "llm_unknown"}
            or signal.startswith("tokens:")
            or signal.startswith("duration:")
        ):
            continue
        command = _dashboard_query_command("evidence-llm-usage", signal)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "llm_usage_query",
                "label": f"LLM usage {signal}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for kind, count in sorted((dashboard_summary.get("by_learning_kind") or {}).items()):
        kind = str(kind).strip()
        if not kind:
            continue
        command = _dashboard_query_command("evidence-learning-kind", kind)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "learning_kind_query",
                "label": f"Learning evidence kind {kind}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for status, count in sorted((dashboard_summary.get("by_learning_validation") or {}).items()):
        status = str(status).strip()
        if not status:
            continue
        command = _dashboard_query_command("evidence-learning-validation", status)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "learning_validation_query",
                "label": f"Learning validation {status}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for status, count in sorted((dashboard_summary.get("by_skill_validation") or {}).items()):
        status = str(status).strip()
        if not status:
            continue
        command = _dashboard_query_command("evidence-skill-validation", status)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "skill_validation_query",
                "label": f"Skill validation {status}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for decision, count in sorted(
        (dashboard_summary.get("by_learning_review_decision") or {}).items()
    ):
        decision = str(decision).strip()
        if not decision:
            continue
        command = _dashboard_query_command("evidence-learning-review-decision", decision)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "learning_review_decision_query",
                "label": f"Learning review decision {decision}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    for event, count in sorted((dashboard_summary.get("by_learning_audit_event") or {}).items()):
        event = str(event).strip()
        if not event:
            continue
        command = _dashboard_query_command("evidence-learning-audit-event", event)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "kind": "learning_audit_event_query",
                "label": f"Learning audit event {event}",
                "command": command,
                "matched_cases": int(count or 0),
            }
        )

    return commands


def _has_habit_fast_path(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    fast_paths = learning.get("habit_fast_paths")
    return isinstance(fast_paths, list) and any(isinstance(item, dict) for item in fast_paths)


def _has_learning_match_explanation_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    matches = learning.get("procedure_matches")
    return isinstance(matches, list) and any(isinstance(item, dict) for item in matches)


def _has_permission_governance_evidence(case: dict[str, Any]) -> bool:
    if str(case.get("failure_layer") or "") == "permission":
        return True
    summary = case.get("permission_summary")
    return isinstance(summary, dict) and bool(summary)


def _has_policy_decision_evidence(case: dict[str, Any]) -> bool:
    return bool(_policy_decisions(case))


def _has_policy_risk_flag_evidence(case: dict[str, Any]) -> bool:
    return bool(_policy_risk_flags(case))


def _has_model_capability_evidence(case: dict[str, Any]) -> bool:
    if str(case.get("failure_layer") or "") == "model_capability":
        return True
    if bool(case.get("model_capability_suspected")):
        return True
    return "model_capability" in _string_values(case.get("completion_failure_layers"))


def _has_completion_evaluation_evidence(case: dict[str, Any]) -> bool:
    if str(case.get("failure_layer") or "") == "completion_evaluation":
        return True
    return any(layer and layer != "none" for layer in _completion_layers(case))


def _has_memory_retrieval_evidence(case: dict[str, Any]) -> bool:
    return bool(_memory_retrieval_signals(case))


def _has_llm_usage_evidence(case: dict[str, Any]) -> bool:
    return "llm_called" in _llm_usage_signals(case)


def _has_skill_approval_gate_evidence(case: dict[str, Any]) -> bool:
    generated_skills = _string_values(case.get("used_skills")) + _lineage_skill_names(case)
    if not any(fnmatch.fnmatchcase(skill, "generated.habit.*") for skill in generated_skills):
        return False
    approval_sensitive_permissions = {
        "code_execution",
        "network",
        "system.exec",
        "dependency.install",
        "filesystem.delete",
    }
    for value in _permission_values(case):
        permission = value.split(":", 1)[0]
        if permission in approval_sensitive_permissions:
            return True
    return False


def _has_skill_promotion_coverage_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    used_or_lifecycle_skills = set(_string_values(case.get("used_skills")))
    used_or_lifecycle_skills.update(_learning_values(case, "skill_lifecycle_records", "skill_name"))
    for lineage in learning.get("skill_lineages", []):
        if not isinstance(lineage, dict):
            continue
        skill_name = str(lineage.get("skill_name") or "")
        if not fnmatch.fnmatchcase(skill_name, "generated.habit.*"):
            continue
        if skill_name not in used_or_lifecycle_skills:
            continue
        if str(lineage.get("generation_type") or ""):
            return True
        if _string_values(lineage.get("source_targets")):
            return True
    return False


def _has_learning_governance_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    corrections = learning.get("learning_corrections")
    if isinstance(corrections, list) and any(isinstance(item, dict) for item in corrections):
        return True
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        records = []
    governance_review_statuses = {
        "edited",
        "edited_requires_review",
        "correction_required",
        "decayed_requires_review",
        "needs_changes",
        "rejected",
        "approved",
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "") in {"disabled", "deprecated"}:
            return True
        if int(record.get("review_count") or 0) > 0:
            return True
        if int(record.get("audit_event_count") or 0) > 0:
            return True
        if str(record.get("review_status") or "") in governance_review_statuses:
            return True
    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        if str(record.get("lifecycle_status") or "") == "deprecated":
            return True
        if "deprecate" in _string_values(record.get("registration_actions")):
            return True
        if "deprecated" in _string_values(record.get("registration_statuses")):
            return True
    return False


def _has_learning_history_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False

    corrections = learning.get("learning_corrections")
    if isinstance(corrections, list):
        for record in corrections:
            if isinstance(record, dict) and (
                record.get("procedure_id")
                or record.get("trigger")
                or record.get("new_review_status")
            ):
                return True

    experience_records = learning.get("experience_records")
    if isinstance(experience_records, list):
        for record in experience_records:
            if isinstance(record, dict) and (
                record.get("experience_id") or record.get("task_id") or record.get("status")
            ):
                return True

    records = learning.get("procedure_memory_records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            if int(record.get("audit_event_count") or 0) > 0:
                return True
            if int(record.get("review_count") or 0) > 0:
                return True
            if _string_values(record.get("audit_events")):
                return True
            if _string_values(record.get("review_decisions")):
                return True
            if record.get("latest_audit_event") or record.get("latest_review_decision"):
                return True

    skill_records = learning.get("skill_lifecycle_records")
    if isinstance(skill_records, list):
        for record in skill_records:
            if not isinstance(record, dict):
                continue
            if _string_values(record.get("registration_actions")):
                return True
            if _string_values(record.get("registration_statuses")):
                return True
            if _string_values(record.get("approval_statuses")):
                return True
            if bool(record.get("validation_passed")):
                return True
            if record.get("lifecycle_status") or record.get("draft_status"):
                return True
    return False


def _has_learning_edit_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        return False
    edit_review_statuses = {
        "edited",
        "edited_requires_review",
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("review_status") or "") in edit_review_statuses:
            return True
        if str(record.get("latest_audit_event") or "") == "edited":
            return True
        if "edited" in _string_values(record.get("audit_events")):
            return True
    return False


def _has_learning_request_changes_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        return False
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("review_status") or "") == "needs_changes":
            return True
        if str(record.get("latest_review_decision") or "") == "needs_changes":
            return True
        if "needs_changes" in _string_values(record.get("review_decisions")):
            return True
    return False


def _has_learning_decay_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        return False
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("review_status") or "") == "decayed_requires_review":
            return True
        if str(record.get("latest_audit_event") or "") == "decayed":
            return True
        if "decayed" in _string_values(record.get("audit_events")):
            return True
    return False


def _has_learning_correction_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    corrections = learning.get("learning_corrections")
    if isinstance(corrections, list) and any(isinstance(item, dict) for item in corrections):
        return True
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        return False
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("review_status") or "") == "correction_required":
            return True
        if "validator_failed" in _string_values(record.get("audit_events")):
            return True
        if str(record.get("latest_audit_event") or "") == "validator_failed":
            return True
    return False


def _has_learning_disable_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("latest_audit_event") or "") == "disabled":
            return True
        review_status = str(record.get("review_status") or "")
        if (
            str(record.get("status") or "") in {"disabled", "deprecated"}
            and record.get("previous_status")
            and review_status != "rejected"
        ):
            return True

    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        lifecycle_status = str(record.get("lifecycle_status") or "")
        if lifecycle_status == "deprecated":
            return True
        if lifecycle_status == "active":
            continue
        if "deprecate" in _string_values(record.get("registration_actions")):
            return True
        if "deprecated" in _string_values(record.get("registration_statuses")):
            return True
    return False


def _has_learning_restore_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        audit_events = _string_values(record.get("audit_events"))
        if "enabled" in audit_events:
            return True
        if str(record.get("latest_audit_event") or "") == "enabled":
            return True
        if record.get("restored_status"):
            return True
    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        actions = _string_values(record.get("registration_actions"))
        if str(record.get("lifecycle_status") or "") == "active" and any(
            action in {"activate", "upgrade"} for action in actions
        ):
            if "deprecate" in actions and record.get("latest_registration_note"):
                return True
    return False


def _has_learning_rollback_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        if "rollback" in _string_values(record.get("registration_actions")):
            return True
    return False


def _has_learning_approval_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("review_status") or "") == "approved":
            return True
        if str(record.get("latest_review_decision") or "") == "approved":
            return True
        if "approved" in _string_values(record.get("review_decisions")):
            return True

    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        if "approved" in _string_values(record.get("approval_statuses")):
            return True
    return False


def _has_learning_promotion_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False

    records = learning.get("procedure_memory_records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            audit_events = _string_values(record.get("audit_events"))
            if "promoted_to_habit" in audit_events:
                return True
            if str(record.get("latest_audit_event") or "") == "promoted_to_habit":
                return True

    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        skill_records = []
    skill_lineages = [
        record for record in learning.get("skill_lineages", []) if isinstance(record, dict)
    ]
    lineage_skill_names = {
        str(record.get("skill_name"))
        for record in skill_lineages
        if record.get("skill_name") is not None
    }
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        skill_name = str(record.get("skill_name") or "")
        source = str(record.get("source") or "")
        if source in {"habit_promotion", "habit_promoted"}:
            return True
        if skill_name in lineage_skill_names and (
            bool(_string_values(record.get("registration_actions")))
            or bool(_string_values(record.get("registration_statuses")))
            or bool(_string_values(record.get("approval_statuses")))
            or bool(record.get("validation_passed"))
            or str(record.get("lifecycle_status") or record.get("draft_status") or "")
            in {"active", "validated", "approved"}
        ):
            return True
    return False


def _has_learning_validation_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False

    procedure_records = learning.get("procedure_memory_records")
    if isinstance(procedure_records, list):
        for record in procedure_records:
            if not isinstance(record, dict):
                continue
            if str(record.get("review_status") or "") == "approved":
                return True
            if str(record.get("latest_review_decision") or "") == "approved":
                return True
            if "approved" in _string_values(record.get("review_decisions")):
                return True

    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        validation_results = record.get("validation_passed")
        if isinstance(validation_results, list) and validation_results:
            return True
        if record.get("last_validated_at"):
            return True
    return False


def _has_learning_rejection_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False
    records = learning.get("procedure_memory_records")
    if not isinstance(records, list):
        records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("review_status") or "") == "rejected":
            return True
        if str(record.get("latest_review_decision") or "") == "rejected":
            return True
        if "rejected" in _string_values(record.get("review_decisions")):
            return True

    skill_records = learning.get("skill_lifecycle_records")
    if not isinstance(skill_records, list):
        return False
    for record in skill_records:
        if not isinstance(record, dict):
            continue
        if "rejected" in _string_values(record.get("approval_statuses")):
            return True
    return False


def _has_learning_audit_gap_evidence(case: dict[str, Any]) -> bool:
    learning = case.get("learning_evidence")
    if not isinstance(learning, dict):
        return False

    procedure_records = [
        record
        for record in learning.get("procedure_memory_records", [])
        if isinstance(record, dict)
    ]
    if (
        _learning_values(case, "procedure_matches", "procedure_id")
        or _learning_values(case, "habit_fast_paths", "habit_id")
    ) and not procedure_records:
        return True

    for record in procedure_records:
        status = str(record.get("status") or "")
        has_review_or_audit = (
            int(record.get("review_count") or 0) > 0
            or int(record.get("audit_event_count") or 0) > 0
            or bool(_string_values(record.get("review_decisions")))
            or bool(record.get("latest_review_decision"))
            or bool(record.get("latest_audit_event"))
        )
        if status in {"verified", "habit", "disabled"} and not has_review_or_audit:
            return True

    skill_lineages = [
        record for record in learning.get("skill_lineages", []) if isinstance(record, dict)
    ]
    skill_records = [
        record for record in learning.get("skill_lifecycle_records", []) if isinstance(record, dict)
    ]
    if skill_lineages and not skill_records:
        return True

    lifecycle_names = {
        str(record.get("skill_name"))
        for record in skill_records
        if record.get("skill_name") is not None
    }
    for lineage in skill_lineages:
        skill_name = lineage.get("skill_name")
        if skill_name is not None and str(skill_name) not in lifecycle_names:
            return True

    for record in skill_records:
        lifecycle_status = str(record.get("lifecycle_status") or record.get("draft_status") or "")
        has_lifecycle_evidence = (
            bool(_string_values(record.get("registration_actions")))
            or bool(_string_values(record.get("registration_statuses")))
            or bool(_string_values(record.get("approval_statuses")))
            or bool(record.get("validation_passed"))
        )
        if lifecycle_status in {"active", "validated", "deprecated"} and not has_lifecycle_evidence:
            return True
    return False


def build_evidence_dashboard(
    payload: dict[str, Any],
    *,
    limit: int = 10,
) -> dict[str, Any]:
    all_cases = query_evidence_index(payload, limit=0)
    failure_layers = dict(all_cases.get("summary", {}).get("by_failure_layer", {}))
    repair_entrypoints = [
        {
            "failure_layer": layer,
            "case_count": count,
            "query_command": _dashboard_query_command("evidence-failure-layer", layer),
            "hint": _failure_layer_repair_hint(layer),
        }
        for layer, count in failure_layers.items()
        if str(layer or "").lower() not in {"none", ""}
    ]
    repair_entrypoints.sort(key=lambda item: (-int(item["case_count"]), item["failure_layer"]))
    views = {
        "eval_coverage_cases": {
            "title": "Eval Coverage Cases",
            "description": "集中查看 eval case 的 status、category、requires 和 skip reason，确认新增学习闭环能力进入真实回归矩阵。",
            "query_command": _dashboard_query_command("evidence-status", '"*"'),
            "query": query_evidence_index(payload, statuses=["*"], limit=limit),
        },
        "skipped_eval_cases": {
            "title": "Skipped Eval Cases",
            "description": "集中查看被 requires gate 跳过的 case，确认 network / L4 / complex 场景的跳过原因可被直接复核。",
            "query_command": _dashboard_query_command("evidence-status", "skipped"),
            "query": query_evidence_index(payload, statuses=["skipped"], limit=limit),
        },
        "failed_cases": {
            "title": "Failed Cases",
            "description": "所有未通过 case 的首选修复入口。",
            "query_command": _dashboard_query_command("evidence-status", "failed"),
            "query": query_evidence_index(payload, statuses=["failed"], limit=limit),
        },
        "skill_or_tool_failures": {
            "title": "Skill Or Tool Failures",
            "description": "优先检查 skill contract、executor 输入输出和 node artifacts。",
            "query_command": _dashboard_query_command(
                "evidence-failure-layer",
                "skill_or_tool",
            ),
            "query": query_evidence_index(
                payload,
                failure_layers=["skill_or_tool"],
                limit=limit,
            ),
        },
        "node_execution_cases": {
            "title": "Node Execution Cases",
            "description": "集中查看 TaskGraph node status、skill/action、attempts 和 error 信号，定位执行节点层面的失败或重试路径。",
            "query_command": _dashboard_query_command("evidence-node-status", "recorded"),
            "query": query_evidence_index(
                payload,
                node_statuses=["recorded"],
                limit=limit,
            ),
        },
        "critic_action_cases": {
            "title": "Critic Action Cases",
            "description": "集中查看 Critic verdict action、target 和 summary evidence，定位 accept / retry / replan / degrade 等执行评估路径。",
            "query_command": _dashboard_query_command("evidence-critic-action", "recorded"),
            "query": query_evidence_index(
                payload,
                critic_actions=["recorded"],
                limit=limit,
            ),
        },
        "model_capability_cases": {
            "title": "Model Capability Cases",
            "description": "集中查看模型能力边界或疑似模型能力不足的 case，避免误修成 Agent 流程补丁。",
            "query_command": _dashboard_query_command(
                "evidence-failure-layer",
                "model_capability",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={"model_capability": ["failure_layer", "suspected"]},
                predicate=_has_model_capability_evidence,
                limit=limit,
            ),
        },
        "completion_evaluation_cases": {
            "title": "Completion Evaluation Cases",
            "description": "集中查看 completion evaluator 产出的失败归因，确认任务完成度判断和主 failure layer 能被回归看板直接定位。",
            "query_command": _dashboard_query_command(
                "evidence-completion-layer",
                "recorded",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "completion_evaluation": [
                        "completion_failure_layers",
                        "completion_evaluation.failure_layer",
                    ]
                },
                predicate=_has_completion_evaluation_evidence,
                limit=limit,
            ),
        },
        "memory_retrieval_cases": {
            "title": "Memory Retrieval Cases",
            "description": "集中查看 vector recall、embedding provider/model、fallback、error 和兼容性证据，确认记忆召回问题能被回归看板直接定位。",
            "query_command": _dashboard_query_command(
                "evidence-memory-retrieval",
                "recorded",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "memory_retrieval": [
                        "embedding_summary",
                        "vector_matches",
                        "vector_memory_entries",
                    ]
                },
                predicate=_has_memory_retrieval_evidence,
                limit=limit,
            ),
        },
        "llm_usage_cases": {
            "title": "LLM Usage Cases",
            "description": "集中查看真实触发 LLM 的 case、token 桶和耗时桶，确认 runtime 成本信号进入回归消费入口。",
            "query_command": _dashboard_query_command(
                "evidence-llm-usage",
                "llm_called",
            ),
            "query": query_evidence_index(
                payload,
                llm_usage_signals=["llm_called"],
                limit=limit,
            ),
        },
        "llm_cost_hotspot_cases": {
            "title": "Runtime Cost Hotspot Cases",
            "description": "集中查看高 token 或长耗时 case，优先定位 runtime 成本热点和可优化回归样本。",
            "query_command": _dashboard_query_command(
                "evidence-llm-usage",
                "tokens:10000+,duration:60s+",
            ),
            "query": query_evidence_index(
                payload,
                llm_usage_signals=["tokens:10000+", "duration:60s+"],
                limit=limit,
            ),
        },
        "execution_policy_cases": {
            "title": "Execution Policy Cases",
            "description": "集中查看 ExecutionPolicy 决策证据，确认 direct_execute、plan_required 等路径进入回归消费入口。",
            "query_command": _dashboard_query_command(
                "evidence-policy-decision",
                "direct_execute",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={"policy_decision": ["direct_execute", "plan_required"]},
                predicate=_has_policy_decision_evidence,
                limit=limit,
            ),
        },
        "planner_skipped_cases": {
            "title": "Planner Skipped Cases",
            "description": "集中查看明确跳过 Planner 的 case，确认 fast path 不只是 policy decision，而是实际记录了 skipped_stages。",
            "query_command": _dashboard_query_command(
                "evidence-skipped-stage",
                "planner",
            ),
            "query": query_evidence_index(
                payload,
                skipped_stages=["planner"],
                limit=limit,
            ),
        },
        "policy_risk_flag_cases": {
            "title": "Policy Risk Flag Cases",
            "description": "集中查看 ExecutionPolicy 风险/路由标记，解释 fast path 为什么被允许、为什么回退 Planner 或为什么需要人工治理。",
            "query_command": _dashboard_query_command(
                "evidence-policy-risk-flag",
                '"*"',
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={"policy_risk_flag": ["*"]},
                predicate=_has_policy_risk_flag_evidence,
                limit=limit,
            ),
        },
        "policy_reason_cases": {
            "title": "Policy Reason Cases",
            "description": "集中查看 ExecutionPolicy reason 文本，直接定位 direct_execute 或 plan_required 的触发/回退解释。",
            "query_command": _dashboard_query_command(
                "evidence-policy-reason",
                '"*"',
            ),
            "query": query_evidence_index(
                payload,
                policy_reasons=["*"],
                limit=limit,
            ),
        },
        "policy_context_budget_cases": {
            "title": "Policy Context Budget Cases",
            "description": "集中查看 ExecutionPolicy context_budget 信号，定位 skill registry、召回、agent state 和 fast path bootstrap 字段大小。",
            "query_command": _dashboard_query_command(
                "evidence-policy-context-budget",
                "recorded",
            ),
            "query": query_evidence_index(
                payload,
                policy_context_budgets=["recorded"],
                limit=limit,
            ),
        },
        "request_payload_stats_cases": {
            "title": "Request Payload Stats Cases",
            "description": "集中查看 LLM request_payload_stats 信号，定位 Planner / Critic / Completion 请求字段大小和上下文膨胀热点。",
            "query_command": _dashboard_query_command(
                "evidence-request-payload-stats",
                "recorded",
            ),
            "query": query_evidence_index(
                payload,
                request_payload_stats=["recorded"],
                limit=limit,
            ),
        },
        "permission_governance_cases": {
            "title": "Permission Governance Cases",
            "description": "集中查看 permission grant / deny 与权限失败 case，确认学习结果没有绕过治理门禁。",
            "query_command": _dashboard_query_command("evidence-permission", '"*"'),
            "query": _dashboard_filtered_query(
                payload,
                filters={"permission_governance": ["permission_summary", "permission_failure"]},
                predicate=_has_permission_governance_evidence,
                limit=limit,
            ),
        },
        "habit_fast_path_cases": {
            "title": "Habit Fast Path Cases",
            "description": "跟踪 habit fast path 在回归中的真实命中、回退和 validator evidence。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "habit_fast_paths",
            ),
            "query": query_evidence_index(
                payload,
                learning_kinds=["habit_fast_paths"],
                limit=limit,
            ),
        },
        "learning_match_explanation_cases": {
            "title": "Learning Match Explanation Cases",
            "description": "集中查看 procedure / habit retrieval 的匹配解释，确认 matched item、rejected candidates、confirmation requirement 和 suggestion mode 进入回归消费入口。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_matches",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_match_explanation": [
                        "procedure_matches",
                        "matched_item",
                        "rejected_candidates",
                        "required_confirmation",
                        "suggestion_mode",
                    ]
                },
                predicate=_has_learning_match_explanation_evidence,
                limit=limit,
            ),
        },
        "learning_gate_cases": {
            "title": "Learning Gate Cases",
            "description": "集中查看带 experience record 的 case，确认学习入口绑定 validation evidence。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "experience_records",
            ),
            "query": query_evidence_index(
                payload,
                learning_kinds=["experience_records"],
                limit=limit,
            ),
        },
        "learning_feedback_cases": {
            "title": "Learning Feedback Cases",
            "description": "集中查看 task-ending learning feedback evidence，确认连续任务中的生成、复用、解释和治理提示进入回归消费入口。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "learning_feedbacks",
            ),
            "query": query_evidence_index(
                payload,
                learning_kinds=["learning_feedbacks"],
                limit=limit,
            ),
        },
        "learning_correction_cases": {
            "title": "Learning Correction Cases",
            "description": "集中查看 procedure / habit 复用失败后的纠错、冻结、复审和恢复证据，确认旧记忆不会继续自动复用。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "learning_corrections",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_correction": [
                        "learning_corrections",
                        "procedure_memory_records.correction_required",
                        "procedure_memory_records.validator_failed",
                    ]
                },
                predicate=_has_learning_correction_evidence,
                limit=limit,
            ),
        },
        "learning_governance_cases": {
            "title": "Learning Governance Cases",
            "description": "集中查看 procedure / habit / skill 生命周期、review、disable、enable、deprecate 和 reactivate evidence，确认用户可治理的学习对象进入回归视图。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_memory_records,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_governance": [
                        "procedure_memory_records",
                        "skill_lifecycle_records",
                    ]
                },
                predicate=_has_learning_governance_evidence,
                limit=limit,
            ),
        },
        "learning_history_cases": {
            "title": "Learning History Cases",
            "description": "集中查看可被 learning history 展开的对象级事件流，确认 experience、procedure / habit 和 skill lifecycle 历史进入回归消费入口。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "experience_records,procedure_memory_records,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_history": [
                        "experience_records.recorded_or_validated",
                        "procedure_memory_records.audit_or_review",
                        "skill_lifecycle_records.lifecycle_events",
                    ]
                },
                predicate=_has_learning_history_evidence,
                limit=limit,
            ),
        },
        "learning_approval_cases": {
            "title": "Learning Approval Cases",
            "description": "集中查看 learning approve / approved review evidence，确认用户审批的学习对象进入回归视图且没有绕过后续 activation gate。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_memory_records,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_approval": [
                        "procedure_memory_records.approved",
                        "skill_lifecycle_records.approved",
                    ]
                },
                predicate=_has_learning_approval_evidence,
                limit=limit,
            ),
        },
        "learning_promotion_cases": {
            "title": "Learning Promotion Cases",
            "description": "集中查看 procedure -> habit 和 habit -> skill promotion evidence，确认学习升级建立在 review、validation、lineage 和 lifecycle 证据链之上。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_memory_records,skill_lineages,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_promotion": [
                        "procedure_memory_records.promoted_to_habit",
                        "skill_lineages",
                        "skill_lifecycle_records.habit_promotion",
                    ]
                },
                predicate=_has_learning_promotion_evidence,
                limit=limit,
            ),
        },
        "learning_validation_cases": {
            "title": "Learning Validation Cases",
            "description": "集中查看 procedure / habit approved review evidence 和 skill validation evidence，确认学习对象校验记录能被回归看板直接定位。",
            "query_command": _dashboard_query_command(
                "evidence-learning-validation",
                '"*"',
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_validation": [
                        "procedure_memory_records.review_status.approved",
                        "procedure_memory_records.review_decision.approved",
                        "skill_lifecycle_records.validation_passed",
                        "skill_lifecycle_records.last_validated_at",
                    ]
                },
                predicate=_has_learning_validation_evidence,
                limit=limit,
            ),
        },
        "learning_edit_cases": {
            "title": "Learning Edit Cases",
            "description": "集中查看 procedure edit evidence，确认用户修正过的学习对象进入回归视图且需要复审时不会继续自动复用。",
            "query_command": _dashboard_query_command(
                "evidence-learning-audit-event",
                "edited",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_edit": [
                        "procedure_memory_records.edited",
                    ]
                },
                predicate=_has_learning_edit_evidence,
                limit=limit,
            ),
        },
        "learning_request_changes_cases": {
            "title": "Learning Request Changes Cases",
            "description": "集中查看 learning request-changes / needs_changes review evidence，确认要求修改的 procedure / habit 会退出自动复用并进入复审队列。",
            "query_command": _dashboard_query_command(
                "evidence-learning-review-decision",
                "needs_changes",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_request_changes": [
                        "procedure_memory_records.needs_changes",
                    ]
                },
                predicate=_has_learning_request_changes_evidence,
                limit=limit,
            ),
        },
        "learning_decay_cases": {
            "title": "Learning Decay Cases",
            "description": "集中查看 learning decay evidence，确认过期 procedure / habit 被审计地降级、需要复审且不会继续自动 fast path。",
            "query_command": _dashboard_query_command(
                "evidence-learning-audit-event",
                "decayed",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_decay": [
                        "procedure_memory_records.decayed",
                        "procedure_memory_records.decayed_requires_review",
                    ]
                },
                predicate=_has_learning_decay_evidence,
                limit=limit,
            ),
        },
        "learning_disable_cases": {
            "title": "Learning Disable Cases",
            "description": "集中查看 learning disable / deprecate evidence，确认被用户停用的学习对象进入回归视图且不会继续作为可复用能力。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_memory_records,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_disable": [
                        "procedure_memory_records.disabled",
                        "skill_lifecycle_records.deprecated",
                    ]
                },
                predicate=_has_learning_disable_evidence,
                limit=limit,
            ),
        },
        "learning_restore_cases": {
            "title": "Learning Restore Cases",
            "description": "集中查看 learning enable / restore / reactivation evidence，确认被禁用的学习对象可被审计地恢复。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_memory_records,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_restore": [
                        "procedure_memory_records.enabled",
                        "skill_lifecycle_records.activate_with_note",
                    ]
                },
                predicate=_has_learning_restore_evidence,
                limit=limit,
            ),
        },
        "learning_rollback_cases": {
            "title": "Learning Rollback Cases",
            "description": "集中查看 skill rollback lifecycle evidence，确认学习能力版本回退进入回归视图且可追溯 previous / target version。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={"learning_rollback": ["skill_lifecycle_records.rollback"]},
                predicate=_has_learning_rollback_evidence,
                limit=limit,
            ),
        },
        "learning_rejection_cases": {
            "title": "Learning Rejection Cases",
            "description": "集中查看 learning reject / rejected approval evidence，确认用户拒绝的学习对象进入回归视图且不会被误当作可复用能力。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_memory_records,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_rejection": [
                        "procedure_memory_records.rejected",
                        "skill_lifecycle_records.rejected",
                    ]
                },
                predicate=_has_learning_rejection_evidence,
                limit=limit,
            ),
        },
        "learning_audit_gap_cases": {
            "title": "Learning Audit Gap Cases",
            "description": "集中查看已有 procedure / habit / skill lineage 但缺少 review、audit、validation 或 lifecycle 证据的 case，优先修补学习证据链断点。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "procedure_matches,habit_fast_paths,skill_lineages,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "learning_audit_gap": [
                        "procedure_or_habit_without_memory_record",
                        "skill_lineage_without_lifecycle_record",
                        "lifecycle_without_validation_or_governance_evidence",
                    ]
                },
                predicate=_has_learning_audit_gap_evidence,
                limit=limit,
            ),
        },
        "skill_lifecycle_governance_cases": {
            "title": "Skill Lifecycle Governance Cases",
            "description": "集中查看 skill registry、validation、approval 和 registration lifecycle evidence，确认 skill 治理状态进入回归视图。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "skill_lifecycle_records",
            ),
            "query": query_evidence_index(
                payload,
                learning_kinds=["skill_lifecycle_records"],
                limit=limit,
            ),
        },
        "generated_habit_skill_usage": {
            "title": "Generated Habit Skill Usage",
            "description": "观察 habit-promoted skill 的真实回归使用情况。",
            "query_command": _dashboard_query_command("evidence-skill", "generated.habit.*"),
            "query": query_evidence_index(
                payload,
                skills=["generated.habit.*"],
                limit=limit,
            ),
        },
        "skill_promotion_coverage_cases": {
            "title": "Skill Promotion Coverage Cases",
            "description": "集中查看 habit-promoted skill 的 generation_type 和 source target 覆盖情况，判断新增 promotion 类型或 alias 是否进入真实回归证据。",
            "query_command": _dashboard_query_command(
                "evidence-learning-kind",
                "skill_lineages,skill_lifecycle_records",
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "skill_promotion_coverage": [
                        "generated.habit.*",
                        "skill_lineages.generation_type",
                        "skill_lineages.source_targets",
                    ]
                },
                predicate=_has_skill_promotion_coverage_evidence,
                limit=limit,
            ),
        },
        "skill_approval_gate_cases": {
            "title": "Skill Approval Gate Cases",
            "description": "集中查看 habit-promoted skill 中触发审批敏感权限的 case，确认高风险 promotion 没有绕过人工审批和权限治理。",
            "query_command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill generated.habit.* "
                "--evidence-permission 'system.exec:*'"
            ),
            "query": _dashboard_filtered_query(
                payload,
                filters={
                    "skill_approval_gate": [
                        "generated.habit.*",
                        "approval_sensitive_permission",
                    ]
                },
                predicate=_has_skill_approval_gate_evidence,
                limit=limit,
            ),
        },
        "learning_lineage_cases": {
            "title": "Learning Lineage Cases",
            "description": "含 procedure、habit 或 skill lineage evidence 的 case。",
            "query_command": _dashboard_query_command("evidence-lineage-skill", "*"),
            "query": _learning_lineage_query(payload, limit=limit),
        },
    }
    regression_commands = _dashboard_regression_commands(
        repair_entrypoints,
        views,
        all_cases["summary"],
        all_cases.get("cases", []),
    )
    return {
        "schema_version": EVIDENCE_DASHBOARD_SCHEMA_VERSION,
        "source_schema_version": payload.get("schema_version"),
        "run_root": payload.get("run_root"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "summary": {
            "source_run_count": all_cases["summary"]["source_run_count"],
            "total_index_cases": all_cases["summary"]["total_index_cases"],
            "by_status": all_cases["summary"]["by_status"],
            "by_scenario": all_cases["summary"]["by_scenario"],
            "by_category": all_cases["summary"]["by_category"],
            "by_requires": all_cases["summary"]["by_requires"],
            "by_skip_reason": all_cases["summary"]["by_skip_reason"],
            "by_failure_layer": failure_layers,
            "by_skill": all_cases["summary"]["by_skill"],
            "by_procedure": all_cases["summary"]["by_procedure"],
            "by_habit": all_cases["summary"]["by_habit"],
            "by_lineage_skill": all_cases["summary"]["by_lineage_skill"],
            "by_promotion_generation_type": all_cases["summary"]["by_promotion_generation_type"],
            "by_promotion_source_target": all_cases["summary"]["by_promotion_source_target"],
            "by_permission": all_cases["summary"]["by_permission"],
            "by_completion_layer": all_cases["summary"]["by_completion_layer"],
            "by_policy_decision": all_cases["summary"]["by_policy_decision"],
            "by_skipped_stage": all_cases["summary"]["by_skipped_stage"],
            "by_policy_risk_flag": all_cases["summary"]["by_policy_risk_flag"],
            "by_policy_reason": all_cases["summary"]["by_policy_reason"],
            "by_policy_context_budget": all_cases["summary"]["by_policy_context_budget"],
            "by_request_payload_stats": all_cases["summary"]["by_request_payload_stats"],
            "by_node_status": all_cases["summary"]["by_node_status"],
            "by_critic_action": all_cases["summary"]["by_critic_action"],
            "by_memory_retrieval": all_cases["summary"]["by_memory_retrieval"],
            "by_llm_usage": all_cases["summary"]["by_llm_usage"],
            "by_learning_kind": all_cases["summary"]["by_learning_kind"],
            "by_learning_validation": all_cases["summary"]["by_learning_validation"],
            "by_skill_validation": all_cases["summary"]["by_skill_validation"],
            "by_learning_review_decision": all_cases["summary"]["by_learning_review_decision"],
            "by_learning_audit_event": all_cases["summary"]["by_learning_audit_event"],
            "repair_entrypoint_count": len(repair_entrypoints),
            "regression_command_count": len(regression_commands),
        },
        "repair_entrypoints": repair_entrypoints,
        "regression_commands": regression_commands,
        "views": views,
    }


def _render_dashboard_cases(query: dict[str, Any]) -> list[str]:
    lines = [
        "| Run | Scenario | Category | Requires | Status | Skip Reason | Failure Layer | Policy Decisions | Skipped Stages | Policy Risk Flags | Policy Reasons | Policy Context Budget | Request Payload Stats | Node Status | Critic Actions | LLM Usage | Skills | Lineage | Match | Promotion Types | Promotion Targets | Diagnostics |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for case in query.get("cases", []):
        if not isinstance(case, dict):
            continue
        lineage_values = (
            _string_values(case.get("procedure_ids"))
            + _string_values(case.get("habit_ids"))
            + _string_values(case.get("lineage_skill_names"))
        )
        diagnostics = "-"
        if case.get("diagnostics_path"):
            diagnostics = f"[diagnostics]({case.get('diagnostics_path')})"
        lines.append(
            f"| `{_markdown_cell(case.get('run_id'))}` | "
            f"`{_markdown_cell(case.get('scenario_id'))}` | "
            f"{_markdown_cell(case.get('category'))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('requires'))))} | "
            f"{_markdown_cell(case.get('status'))} | "
            f"{_markdown_cell(case.get('skip_reason'))} | "
            f"{_markdown_cell(case.get('failure_layer'))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_decisions'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('skipped_stages'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_risk_flags'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_reasons'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('policy_context_budget_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('request_payload_stats_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('node_status_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('critic_action_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('llm_usage_signals'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('used_skills'))))} | "
            f"{_markdown_cell(_markdown_value_list(lineage_values))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('procedure_match_summaries'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('promotion_generation_types'))))} | "
            f"{_markdown_cell(_markdown_value_list(_string_values(case.get('promotion_source_targets'))))} | "
            f"{diagnostics} |"
        )
    if len(lines) == 2:
        lines.append(
            "| - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |"
        )
    return lines


def render_evidence_dashboard_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# Eval Evidence Dashboard",
        "",
        f"- run_root: `{payload.get('run_root')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- source_schema_version: `{payload.get('source_schema_version')}`",
        f"- source_run_count: {summary.get('source_run_count', 0)}",
        f"- total_index_cases: {summary.get('total_index_cases', 0)}",
        f"- section_case_limit: {payload.get('limit', 0)}",
        "",
        "## Overview",
        "",
        _markdown_count_line("status", summary.get("by_status", {})),
        _markdown_count_line("scenario", summary.get("by_scenario", {})),
        _markdown_count_line("category", summary.get("by_category", {})),
        _markdown_count_line("requires", summary.get("by_requires", {})),
        _markdown_count_line("skip_reason", summary.get("by_skip_reason", {})),
        _markdown_count_line("failure_layer", summary.get("by_failure_layer", {})),
        _markdown_count_line("skill", summary.get("by_skill", {})),
        _markdown_count_line("procedure", summary.get("by_procedure", {})),
        _markdown_count_line("habit", summary.get("by_habit", {})),
        _markdown_count_line("lineage_skill", summary.get("by_lineage_skill", {})),
        _markdown_count_line(
            "promotion_generation_type",
            summary.get("by_promotion_generation_type", {}),
        ),
        _markdown_count_line(
            "promotion_source_target",
            summary.get("by_promotion_source_target", {}),
        ),
        _markdown_count_line("permission", summary.get("by_permission", {})),
        _markdown_count_line("completion_layer", summary.get("by_completion_layer", {})),
        _markdown_count_line("policy_decision", summary.get("by_policy_decision", {})),
        _markdown_count_line("skipped_stage", summary.get("by_skipped_stage", {})),
        _markdown_count_line(
            "policy_risk_flag",
            summary.get("by_policy_risk_flag", {}),
        ),
        _markdown_count_line("policy_reason", summary.get("by_policy_reason", {})),
        _markdown_count_line(
            "policy_context_budget",
            summary.get("by_policy_context_budget", {}),
        ),
        _markdown_count_line(
            "request_payload_stats",
            summary.get("by_request_payload_stats", {}),
        ),
        _markdown_count_line("node_status", summary.get("by_node_status", {})),
        _markdown_count_line("critic_action", summary.get("by_critic_action", {})),
        _markdown_count_line("memory_retrieval", summary.get("by_memory_retrieval", {})),
        _markdown_count_line("llm_usage", summary.get("by_llm_usage", {})),
        _markdown_count_line("learning_kind", summary.get("by_learning_kind", {})),
        _markdown_count_line(
            "learning_validation",
            summary.get("by_learning_validation", {}),
        ),
        _markdown_count_line("skill_validation", summary.get("by_skill_validation", {})),
        _markdown_count_line(
            "learning_review_decision",
            summary.get("by_learning_review_decision", {}),
        ),
        _markdown_count_line(
            "learning_audit_event",
            summary.get("by_learning_audit_event", {}),
        ),
        "",
        "## Repair Entry Points",
        "",
        "| Failure Layer | Cases | Query | First Inspection |",
        "|---|---:|---|---|",
    ]
    for item in payload.get("repair_entrypoints", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| `{_markdown_cell(item.get('failure_layer'))}` | "
            f"{_markdown_cell(item.get('case_count'))} | "
            f"`{_markdown_cell(item.get('query_command'))}` | "
            f"{_markdown_cell(item.get('hint'))} |"
        )
    if not payload.get("repair_entrypoints"):
        lines.append("| - | 0 | - | - |")

    lines.extend(
        [
            "",
            "## Regression Commands",
            "",
            "| Kind | Scope | Cases | Command |",
            "|---|---|---:|---|",
        ]
    )
    for item in payload.get("regression_commands", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| `{_markdown_cell(item.get('kind'))}` | "
            f"{_markdown_cell(item.get('label'))} | "
            f"{_markdown_cell(item.get('matched_cases', 0))} | "
            f"`{_markdown_cell(item.get('command'))}` |"
        )
    if not payload.get("regression_commands"):
        lines.append("| - | - | 0 | - |")

    views = payload.get("views") if isinstance(payload.get("views"), dict) else {}
    for key in [
        "eval_coverage_cases",
        "skipped_eval_cases",
        "failed_cases",
        "skill_or_tool_failures",
        "node_execution_cases",
        "critic_action_cases",
        "model_capability_cases",
        "completion_evaluation_cases",
        "memory_retrieval_cases",
        "llm_usage_cases",
        "llm_cost_hotspot_cases",
        "execution_policy_cases",
        "planner_skipped_cases",
        "policy_risk_flag_cases",
        "policy_reason_cases",
        "policy_context_budget_cases",
        "request_payload_stats_cases",
        "permission_governance_cases",
        "habit_fast_path_cases",
        "learning_match_explanation_cases",
        "learning_gate_cases",
        "learning_feedback_cases",
        "learning_correction_cases",
        "learning_governance_cases",
        "learning_history_cases",
        "learning_approval_cases",
        "learning_promotion_cases",
        "learning_validation_cases",
        "learning_edit_cases",
        "learning_request_changes_cases",
        "learning_decay_cases",
        "learning_disable_cases",
        "learning_restore_cases",
        "learning_rollback_cases",
        "learning_rejection_cases",
        "learning_audit_gap_cases",
        "skill_lifecycle_governance_cases",
        "generated_habit_skill_usage",
        "skill_promotion_coverage_cases",
        "skill_approval_gate_cases",
        "learning_lineage_cases",
    ]:
        view = views.get(key)
        if not isinstance(view, dict):
            continue
        query = view.get("query") if isinstance(view.get("query"), dict) else {}
        query_summary = query.get("summary") if isinstance(query.get("summary"), dict) else {}
        lines.extend(
            [
                "",
                f"## {view.get('title')}",
                "",
                str(view.get("description") or ""),
                "",
                f"- query: `{view.get('query_command')}`",
                f"- matched_cases: {query_summary.get('matched_cases', 0)}",
                f"- returned_cases: {query_summary.get('returned_cases', 0)}",
                "",
                *_render_dashboard_cases(query),
            ]
        )
    return "\n".join(lines) + "\n"


def render_evidence_dashboard_commands(payload: dict[str, Any]) -> str:
    lines = [
        "# Eval evidence dashboard regression commands",
        f"# run_root: {payload.get('run_root')}",
        f"# generated_at: {payload.get('generated_at')}",
        "",
    ]
    for item in payload.get("regression_commands", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("kind") or "command")
        matched_cases = int(item.get("matched_cases") or 0)
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        lines.append(f"# {label} ({matched_cases} cases)")
        lines.append(command)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_evidence_index_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# Eval Evidence Index",
        "",
        f"- run_root: `{payload.get('run_root')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- runs: {payload.get('run_count', 0)}",
        f"- cases: {payload.get('case_count', 0)}",
        f"- passed: {payload.get('passed', 0)}",
        f"- failed: {payload.get('failed', 0)}",
        f"- skipped: {payload.get('skipped', 0)}",
        "",
        "## Summary",
        "",
        _markdown_count_line("status", summary.get("by_status", {})),
        _markdown_count_line("scenario", summary.get("by_scenario", {})),
        _markdown_count_line("category", summary.get("by_category", {})),
        _markdown_count_line("requires", summary.get("by_requires", {})),
        _markdown_count_line("skip_reason", summary.get("by_skip_reason", {})),
        _markdown_count_line("failure_layer", summary.get("by_failure_layer", {})),
        "",
        "## Cases",
        "",
        "| Run | Scenario | Category | Requires | Status | Skip Reason | Failure Layer | Skills | Learning Evidence | Diagnostics |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for case in payload.get("cases", []):
        artifacts = case.get("artifacts") if isinstance(case.get("artifacts"), list) else []
        diagnostics = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict) and item.get("kind") == "diagnostics"
            ),
            None,
        )
        diagnostics_link = "-"
        if isinstance(diagnostics, dict) and diagnostics.get("exists"):
            diagnostics_link = f"[diagnostics]({diagnostics.get('path')})"
        skills = ", ".join(f"`{item}`" for item in case.get("used_skills", [])) or "-"
        requires = ", ".join(f"`{item}`" for item in _requires_values(case)) or "-"
        lines.append(
            f"| `{case.get('run_id')}` | `{case.get('scenario_id')}` | "
            f"{case.get('category')} | {requires} | {case.get('status')} | "
            f"{case.get('skip_reason') or '-'} | {case.get('failure_layer')} | {skills} | "
            f"{_learning_evidence_count(case)} | {diagnostics_link} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_scenarios(scenarios: list[dict[str, Any]]) -> None:
    for scenario in scenarios:
        requires = ",".join(str(item) for item in scenario.get("requires", [])) or "-"
        print(
            f"{scenario['id']}\t{scenario.get('kind', 'task')}\t"
            f"{scenario.get('category', '-')}\trequires={requires}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real-world OmniAgent eval scenarios.")
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS), help="Scenario JSON file")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT), help="Eval run output root")
    parser.add_argument("--scenario", action="append", help="Run only this scenario id")
    parser.add_argument(
        "--scenario-glob",
        action="append",
        help=(
            "Run scenarios whose id matches this glob; can be repeated or comma-separated. "
            "When combined with --scenario-category, both filters must match."
        ),
    )
    parser.add_argument(
        "--scenario-category",
        action="append",
        help=(
            "Run scenarios whose category matches this value or glob; can be repeated or "
            "comma-separated. When combined with --scenario-glob, both filters must match."
        ),
    )
    parser.add_argument("--include-network", action="store_true", help="Include network scenarios")
    parser.add_argument("--include-l4", action="store_true", help="Include L4 shell scenarios")
    parser.add_argument("--include-complex", action="store_true", help="Include complex scenarios")
    parser.add_argument("--include-all", action="store_true", help="Include every scenario")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview selected and skipped scenarios without executing any task",
    )
    parser.add_argument("--check-only", action="store_true", help="Validate scenario file and exit")
    parser.add_argument(
        "--build-evidence-index",
        action="store_true",
        help="Build a cross-run evidence index from existing eval_runs outputs and exit",
    )
    parser.add_argument(
        "--evidence-index-output",
        help="Output JSON path for --build-evidence-index (default: <run-root>/evidence_index.json)",
    )
    parser.add_argument(
        "--query-evidence-index",
        action="store_true",
        help="Query an existing cross-run evidence index and exit",
    )
    parser.add_argument(
        "--evidence-index-input",
        help=(
            "Input JSON path for --query-evidence-index or --evidence-dashboard "
            "(default: <run-root>/evidence_index.json)"
        ),
    )
    parser.add_argument(
        "--evidence-status",
        action="append",
        help="Filter evidence query by case status; can be repeated or comma-separated",
    )
    parser.add_argument(
        "--evidence-failure-layer",
        action="append",
        help="Filter evidence query by failure layer; supports exact or glob matches",
    )
    parser.add_argument(
        "--evidence-skill",
        action="append",
        help="Filter evidence query by used skill; supports exact or glob matches",
    )
    parser.add_argument(
        "--evidence-procedure",
        action="append",
        help="Filter evidence query by procedure id from matches or skill lineage",
    )
    parser.add_argument(
        "--evidence-habit",
        action="append",
        help="Filter evidence query by habit id from fast paths or skill lineage",
    )
    parser.add_argument(
        "--evidence-lineage-skill",
        action="append",
        help="Filter evidence query by skill name recorded in lineage artifacts",
    )
    parser.add_argument(
        "--evidence-promotion-generation-type",
        action="append",
        help=(
            "Filter evidence query by skill lineage generation type "
            "(for example file, json, csv, sqlite, archive, or process); "
            "supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-promotion-source-target",
        action="append",
        help=(
            "Filter evidence query by habit source target recorded in skill lineage "
            "(for example builtin.file.basic, csv, or tsv); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-scenario",
        action="append",
        help="Filter evidence query by scenario id; supports exact or glob matches",
    )
    parser.add_argument(
        "--evidence-category",
        action="append",
        help="Filter evidence query by scenario category; supports exact or glob matches",
    )
    parser.add_argument(
        "--evidence-requires",
        action="append",
        help="Filter evidence query by scenario requires tag; supports exact or glob matches",
    )
    parser.add_argument(
        "--evidence-skip-reason",
        action="append",
        help="Filter evidence query by skip reason; supports exact or glob matches",
    )
    parser.add_argument(
        "--evidence-permission",
        action="append",
        help=(
            "Filter evidence query by permission summary entry "
            "(for example filesystem.write:granted); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-completion-layer",
        action="append",
        help=(
            "Filter evidence query by completion evaluation failure layer; "
            "use recorded for any non-none completion failure layer"
        ),
    )
    parser.add_argument(
        "--evidence-policy-decision",
        action="append",
        help=(
            "Filter evidence query by ExecutionPolicy decision "
            "(for example direct_execute or plan_required); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-skipped-stage",
        action="append",
        help=(
            "Filter evidence query by stages skipped by ExecutionPolicy "
            "(for example planner); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-policy-risk-flag",
        action="append",
        help=(
            "Filter evidence query by enabled ExecutionPolicy risk or routing flag "
            "(for example network, ambiguous_reference, or planner_context_required); "
            "supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-policy-reason",
        action="append",
        help=(
            "Filter evidence query by ExecutionPolicy reason text; supports exact "
            "or glob matches, for example '*风险标记*'"
        ),
    )
    parser.add_argument(
        "--evidence-policy-context-budget",
        action="append",
        help=(
            "Filter evidence query by ExecutionPolicy context budget signal "
            "(for example recorded or skill_registry_entries:json_chars:50000+); "
            "supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-request-payload-stats",
        action="append",
        help=(
            "Filter evidence query by LLM request_payload_stats signal "
            "(for example recorded, prompt:planner_v1, or "
            "skill_registry_entries:json_chars:10000-49999); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-node-status",
        action="append",
        help=(
            "Filter evidence query by TaskGraph node status signal "
            "(for example recorded, status:completed, skill:builtin.file.basic, "
            "action:write, or error:present); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-critic-action",
        action="append",
        help=(
            "Filter evidence query by Critic verdict action signal "
            "(for example recorded, action:accept, summary:present, or target:<id>); "
            "supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-memory-retrieval",
        action="append",
        help=(
            "Filter evidence query by memory retrieval or embedding signal "
            "(recorded, vector_match, vector_memory_entry, fallback, error, "
            "incompatible, provider:<name>, model:<name>)"
        ),
    )
    parser.add_argument(
        "--evidence-llm-usage",
        action="append",
        help=(
            "Filter evidence query by LLM/runtime cost signal "
            "(llm_called, no_llm, llm_unknown, tokens:0, tokens:1-999, "
            "tokens:1000-9999, tokens:10000+, duration:<1s, duration:1-10s, "
            "duration:10-60s, duration:60s+)"
        ),
    )
    parser.add_argument(
        "--evidence-learning-kind",
        action="append",
        help=(
            "Filter evidence query by learning evidence kind "
            "(procedure_matches, habit_fast_paths, experience_records, learning_feedbacks, "
            "learning_corrections, procedure_memory_records, skill_lineages, "
            "skill_lifecycle_records)"
        ),
    )
    parser.add_argument(
        "--evidence-skill-validation",
        action="append",
        help=(
            "Filter evidence query by skill lifecycle validation status "
            "(passed, failed, or recorded); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-learning-validation",
        action="append",
        help=(
            "Filter evidence query by procedure/habit approved review evidence "
            "or skill validation evidence (procedure, habit, skill, approved, "
            "passed, failed, recorded, or glob patterns)"
        ),
    )
    parser.add_argument(
        "--evidence-learning-review-decision",
        action="append",
        help=(
            "Filter evidence query by procedure/habit review decision "
            "(approved, rejected, needs_changes, etc.); supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-learning-audit-event",
        action="append",
        help=(
            "Filter evidence query by procedure/habit audit event "
            "(edited, decayed, disabled, enabled, promoted_to_habit, etc.); "
            "supports exact or glob matches"
        ),
    )
    parser.add_argument(
        "--evidence-limit",
        type=int,
        default=50,
        help="Maximum cases to include in query output (default: 50; use 0 for all)",
    )
    parser.add_argument(
        "--evidence-json",
        action="store_true",
        help="Emit evidence query output as JSON instead of Markdown",
    )
    parser.add_argument(
        "--evidence-output",
        help="Write evidence query output to this path instead of stdout",
    )
    parser.add_argument(
        "--evidence-dashboard",
        action="store_true",
        help="Render a regression dashboard/runbook from an existing evidence index and exit",
    )
    parser.add_argument(
        "--evidence-dashboard-output",
        help=(
            "Output path for --evidence-dashboard "
            "(default: <run-root>/evidence_dashboard.md or .json)"
        ),
    )
    parser.add_argument(
        "--evidence-dashboard-json",
        action="store_true",
        help="Write the evidence dashboard payload as JSON instead of Markdown",
    )
    parser.add_argument(
        "--evidence-dashboard-commands-output",
        help=(
            "Optional output path for a plain-text command list derived from "
            "--evidence-dashboard regression_commands"
        ),
    )
    parser.add_argument(
        "--evidence-dashboard-limit",
        type=int,
        default=10,
        help="Maximum cases per dashboard section (default: 10; use 0 for all)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_root = Path(args.run_root).resolve()
    evidence_index_payload: dict[str, Any] | None = None
    evidence_index_path: Path | None = None
    if args.build_evidence_index:
        output_json = (
            Path(args.evidence_index_output).resolve()
            if args.evidence_index_output
            else run_root / "evidence_index.json"
        )
        evidence_index_payload = _write_evidence_index(run_root, output_json)
        evidence_index_path = output_json
        print(f"evidence_index: {output_json}")
        print(f"evidence_index_markdown: {output_json.with_suffix('.md')}")
        print(
            f"runs={evidence_index_payload['run_count']} "
            f"cases={evidence_index_payload['case_count']} "
            f"passed={evidence_index_payload['passed']} "
            f"failed={evidence_index_payload['failed']} "
            f"skipped={evidence_index_payload['skipped']}"
        )
        if not args.query_evidence_index and not args.evidence_dashboard:
            return 0
    if args.query_evidence_index:
        if evidence_index_payload is None:
            evidence_index_path = (
                Path(args.evidence_index_input).resolve()
                if args.evidence_index_input
                else run_root / "evidence_index.json"
            )
            if not evidence_index_path.exists():
                print(
                    f"evidence index not found: {evidence_index_path}; "
                    "run with --build-evidence-index first",
                    file=sys.stderr,
                )
                return 2
            loaded_payload = _load_json(evidence_index_path)
            if not isinstance(loaded_payload, dict):
                print(
                    f"evidence index is not a JSON object: {evidence_index_path}", file=sys.stderr
                )
                return 2
            evidence_index_payload = loaded_payload
        query_payload = query_evidence_index(
            evidence_index_payload,
            statuses=args.evidence_status,
            failure_layers=args.evidence_failure_layer,
            skills=args.evidence_skill,
            procedure_ids=args.evidence_procedure,
            habit_ids=args.evidence_habit,
            lineage_skills=args.evidence_lineage_skill,
            promotion_generation_types=args.evidence_promotion_generation_type,
            promotion_source_targets=args.evidence_promotion_source_target,
            scenario_ids=args.evidence_scenario,
            categories=args.evidence_category,
            requires=args.evidence_requires,
            skip_reasons=args.evidence_skip_reason,
            permissions=args.evidence_permission,
            completion_layers=args.evidence_completion_layer,
            policy_decisions=args.evidence_policy_decision,
            skipped_stages=args.evidence_skipped_stage,
            policy_risk_flags=args.evidence_policy_risk_flag,
            policy_reasons=args.evidence_policy_reason,
            policy_context_budgets=args.evidence_policy_context_budget,
            request_payload_stats=args.evidence_request_payload_stats,
            node_statuses=args.evidence_node_status,
            critic_actions=args.evidence_critic_action,
            memory_retrieval_signals=args.evidence_memory_retrieval,
            llm_usage_signals=args.evidence_llm_usage,
            learning_kinds=args.evidence_learning_kind,
            learning_validation_statuses=args.evidence_learning_validation,
            skill_validation_statuses=args.evidence_skill_validation,
            learning_review_decisions=args.evidence_learning_review_decision,
            learning_audit_events=args.evidence_learning_audit_event,
            limit=args.evidence_limit,
        )
        if args.evidence_json:
            rendered = json.dumps(query_payload, ensure_ascii=False, indent=2) + "\n"
        else:
            rendered = render_evidence_query_markdown(query_payload)
        if args.evidence_output:
            output_path = Path(args.evidence_output).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
            summary = query_payload["summary"]
            print(f"evidence_query: {output_path}")
            print(
                f"matched={summary['matched_cases']} returned={summary['returned_cases']} "
                f"source_cases={summary['total_index_cases']}"
            )
        else:
            print(rendered, end="")
        return 0
    if args.evidence_dashboard:
        if evidence_index_payload is None:
            evidence_index_path = (
                Path(args.evidence_index_input).resolve()
                if args.evidence_index_input
                else run_root / "evidence_index.json"
            )
            if not evidence_index_path.exists():
                print(
                    f"evidence index not found: {evidence_index_path}; "
                    "run with --build-evidence-index first",
                    file=sys.stderr,
                )
                return 2
            loaded_payload = _load_json(evidence_index_path)
            if not isinstance(loaded_payload, dict):
                print(
                    f"evidence index is not a JSON object: {evidence_index_path}", file=sys.stderr
                )
                return 2
            evidence_index_payload = loaded_payload
        dashboard_payload = build_evidence_dashboard(
            evidence_index_payload,
            limit=args.evidence_dashboard_limit,
        )
        if args.evidence_dashboard_json:
            rendered = json.dumps(dashboard_payload, ensure_ascii=False, indent=2) + "\n"
            default_output = run_root / "evidence_dashboard.json"
        else:
            rendered = render_evidence_dashboard_markdown(dashboard_payload)
            default_output = run_root / "evidence_dashboard.md"
        output_path = (
            Path(args.evidence_dashboard_output).resolve()
            if args.evidence_dashboard_output
            else default_output
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        commands_path = None
        if args.evidence_dashboard_commands_output:
            commands_path = Path(args.evidence_dashboard_commands_output).resolve()
            commands_path.parent.mkdir(parents=True, exist_ok=True)
            commands_path.write_text(
                render_evidence_dashboard_commands(dashboard_payload),
                encoding="utf-8",
            )
        summary = dashboard_payload["summary"]
        print(f"evidence_dashboard: {output_path}")
        if commands_path is not None:
            print(f"evidence_dashboard_commands: {commands_path}")
        print(
            f"source_cases={summary['total_index_cases']} "
            f"repair_entrypoints={summary['repair_entrypoint_count']} "
            f"regression_commands={summary['regression_command_count']}"
        )
        return 0
    scenario_path = Path(args.scenarios).resolve()
    scenarios = _normalize_scenarios(scenario_path)
    if args.list:
        _print_scenarios(scenarios)
        return 0
    if args.check_only:
        print(f"validated {len(scenarios)} scenarios from {scenario_path}")
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    selected, results = _select_scenarios(scenarios, args)

    if not selected:
        _write_summary(run_dir, results)
        print(f"no scenarios selected; summary: {run_dir / 'summary.md'}")
        return 0

    if args.dry_run:
        preview_results = [
            _selection_result(scenario, status="selected") for scenario in selected
        ] + results
        _write_summary(run_dir, preview_results)
        print(f"dry_run: {run_dir / 'summary.md'}")
        print(
            f"selected={len(selected)} skipped={len(results)} "
            "passed=0 failed=0 executed=0"
        )
        return 0

    for scenario in selected:
        results.append(_run_one(scenario=scenario, run_dir=run_dir, args=args))

    _write_summary(run_dir, results)
    failed = sum(1 for item in results if item["status"] == "failed")
    print(f"summary: {run_dir / 'summary.md'}")
    print(
        f"passed={sum(1 for item in results if item['status'] == 'passed')} "
        f"failed={failed} skipped={sum(1 for item in results if item['status'] == 'skipped')}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

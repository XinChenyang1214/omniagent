#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
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
        if kind not in {"task", "chat_sequence"}:
            raise ValueError(f"{scenario_id}: unsupported kind {kind}")
        if kind == "task" and not item.get("goal"):
            raise ValueError(f"{scenario_id}: task scenario requires goal")
        if kind == "chat_sequence" and not item.get("turns"):
            raise ValueError(f"{scenario_id}: chat_sequence requires turns")
        normalized.append(item)
    return normalized


def _scenario_skip_reason(scenario: dict[str, Any], args: argparse.Namespace) -> str | None:
    selected = set(args.scenario or [])
    scenario_id = str(scenario["id"])
    if selected and scenario_id not in selected:
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
                        evidence["vector_matches"].append(
                            _summarize_vector_match(task_id, match)
                        )
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
                "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
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
        {
            str(item.get("embedding_provider"))
            for item in items
            if item.get("embedding_provider")
        }
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
        "embedding_incompatible_match_count": embedding_summary.get(
            "incompatible_match_count", 0
        ),
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
                item["preview"] = _short_text(path.read_text(encoding="utf-8", errors="replace"), 600)
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
        lines.extend(["## Skill Usage", "", ", ".join(f"`{item}`" for item in evidence["used_skills"]), ""])

    if evidence.get("policy_decisions"):
        lines.extend(["## Execution Policy", ""])
        rows: list[list[Any]] = []
        for item in evidence.get("policy_decisions", []):
            risk_flags = item.get("risk_flags") if isinstance(item.get("risk_flags"), dict) else {}
            enabled_risks = ", ".join(
                name for name, enabled in risk_flags.items() if enabled
            ) or "-"
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

    embedding_summary = evidence.get("embedding_summary") if isinstance(evidence.get("embedding_summary"), dict) else {}
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
        lines.extend(["## LLM Calls", "", f"- count: `{len(evidence['llm_calls'])}`", f"- total_tokens: `{total_tokens}`", ""])
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


def _evaluate_assertions(
    *,
    scenario: dict[str, Any],
    workspace: Path,
    executions: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> tuple[bool, list[str], str]:
    assertions = scenario.get("assertions", {})
    issues: list[str] = []
    successes = [_task_success(item.get("json")) for item in executions]
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
    required_route_actions = {
        str(item) for item in assertions.get("required_route_action_any", [])
    }
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

    required_statuses = {
        str(item) for item in assertions.get("required_node_status_any", [])
    }
    if required_statuses:
        seen_statuses = {
            str(item.get("status")) for item in evidence.get("node_statuses", [])
        }
        if not (required_statuses & seen_statuses):
            issues.append(
                "missing required node status; expected any of "
                f"{sorted(required_statuses)}, got {sorted(seen_statuses)}"
            )

    denied_required = {
        str(item) for item in assertions.get("required_permission_denied_any", [])
    }
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
            isinstance(item.get("constraints"), dict)
            and item["constraints"].get(key) == value
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
            isinstance(item.get("metadata"), dict)
            and item["metadata"].get(key) == value
            for item in request_snapshots
        ):
            issues.append(
                f"missing request metadata {key}={value!r}; "
                f"seen={[item.get('metadata') for item in request_snapshots]}"
            )

    max_llm_calls = assertions.get("max_llm_calls")
    if max_llm_calls is not None and len(evidence.get("llm_calls", [])) > int(max_llm_calls):
        issues.append(
            f"too many LLM calls: {len(evidence.get('llm_calls', []))} > {max_llm_calls}"
        )

    vector_matches = evidence.get("vector_matches", [])
    min_vector_match_count = assertions.get("min_vector_match_count")
    if min_vector_match_count is not None and len(vector_matches) < int(min_vector_match_count):
        issues.append(
            f"too few vector matches: {len(vector_matches)} < {min_vector_match_count}"
        )
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
        issues.append(
            f"too many vector matches: {len(vector_matches)} > {max_vector_match_count}"
        )
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
                issues.append(
                    f"forbidden workspace glob matched {pattern}: {matches}"
                )

    diagnosis = _diagnose(issues, executions, evidence)
    diagnosis_issue_count = len(issues)
    allowed_diagnoses = {str(item) for item in assertions.get("diagnosis_any", [])}
    if allowed_diagnoses and diagnosis not in allowed_diagnoses:
        issues.append(
            f"unexpected diagnosis: expected any of {sorted(allowed_diagnoses)}, got {diagnosis}"
        )
    required_layers = {
        str(item) for item in assertions.get("required_failure_layer_any", [])
    }
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


def _diagnose(
    issues: list[str], executions: list[dict[str, Any]], evidence: dict[str, Any]
) -> str:
    if any(item.get("timed_out") for item in executions):
        return "runtime_timeout"
    if any(item.get("json") is None for item in executions):
        return "cli_or_infrastructure"
    if issues and any(
        turn.get("route_input_type") in {"chitchat", "knowledge_query", "status_query", "system_request"}
        for turn in evidence.get("turns", [])
    ):
        return "router_or_interaction_dispatch"
    if any("missing required skill usage" in issue for issue in issues):
        return "planner_or_skill_selection"
    if any("forbidden permissions" in issue for issue in issues):
        return "permission_guardrail"
    if any("missing required permission denial" in issue for issue in issues):
        return "permission_detection"
    denied = [
        item for item in evidence.get("permissions", []) if item.get("status") == "denied"
    ]
    if denied:
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
        "status": "passed" if passed else "failed",
        "diagnosis": diagnosis,
        "failure_layer": _failure_layer(diagnosis, passed=passed),
        "issues": issues,
        "workspace": str(workspace),
        "case_dir": str(case_dir),
        "task_ids": task_ids,
        "thread_ids": thread_ids,
        "exit_codes": [item.get("exit_code") for item in executions],
        "duration_seconds": round(sum(float(item.get("duration_seconds", 0)) for item in executions), 3),
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
    summary = {
        "run_dir": str(run_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
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
    parser.add_argument("--include-network", action="store_true", help="Include network scenarios")
    parser.add_argument("--include-l4", action="store_true", help="Include L4 shell scenarios")
    parser.add_argument("--include-complex", action="store_true", help="Include complex scenarios")
    parser.add_argument("--include-all", action="store_true", help="Include every scenario")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    parser.add_argument("--check-only", action="store_true", help="Validate scenario file and exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario_path = Path(args.scenarios).resolve()
    scenarios = _normalize_scenarios(scenario_path)
    if args.list:
        _print_scenarios(scenarios)
        return 0
    if args.check_only:
        print(f"validated {len(scenarios)} scenarios from {scenario_path}")
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.run_root).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    selected: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        reason = _scenario_skip_reason(scenario, args)
        if reason:
            if reason != "not selected":
                results.append(
                    {
                        "id": scenario["id"],
                        "category": scenario.get("category", "uncategorized"),
                        "kind": scenario.get("kind", "task"),
                        "status": "skipped",
                        "diagnosis": reason,
                        "failure_layer": "none",
                        "issues": [],
                        "task_ids": [],
                        "thread_ids": [],
                    }
                )
            continue
        selected.append(scenario)

    if not selected:
        _write_summary(run_dir, results)
        print(f"no scenarios selected; summary: {run_dir / 'summary.md'}")
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

from __future__ import annotations

import importlib.util
import json
import sqlite3
import zipfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_real_world_evals.py"
SPEC = importlib.util.spec_from_file_location("run_real_world_evals", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
evals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evals)


def test_router_misroute_gets_routing_diagnosis_and_layer() -> None:
    issues = [
        "scenario expected every task turn to succeed",
        "missing file: notes/project.md",
    ]
    executions = [
        {
            "json": {
                "route": {
                    "input_type": "status_query",
                    "reasoning": "matched status keyword",
                }
            },
            "timed_out": False,
        }
    ]
    evidence = {
        "turns": [{"route_input_type": "status_query"}],
        "permissions": [],
        "node_statuses": [],
    }

    diagnosis = evals._diagnose(issues, executions, evidence)

    assert diagnosis == "router_or_interaction_dispatch"
    assert evals._failure_layer(diagnosis) == "routing"


def test_extract_turn_summaries_captures_route_task_and_response() -> None:
    executions = [
        {
            "exit_code": 0,
            "duration_seconds": 1.25,
            "json_error": None,
            "json": {
                "thread": {"thread_id": "thread_123"},
                "route": {
                    "input_type": "new_task",
                    "task_action": "create",
                    "confidence": 0.9,
                    "reasoning": "task verb",
                    "metadata": {"route_source": "rule"},
                },
                "assistant_message": "created the requested file",
                "task_report": {
                    "task": {
                        "task_id": "task_123",
                        "status": "completed",
                        "plan_summary": "write report",
                    },
                    "result": {"success": True, "status": "completed"},
                },
            },
        }
    ]

    turns = evals._extract_turn_summaries(executions)

    assert turns == [
        {
            "turn": 1,
            "exit_code": 0,
            "duration_seconds": 1.25,
            "json_error": None,
            "thread_id": "thread_123",
            "route_input_type": "new_task",
            "route_source": "rule",
            "route_action": "create",
            "route_confidence": 0.9,
            "route_reasoning": "task verb",
            "task_id": "task_123",
            "task_status": "completed",
            "task_plan_summary": "write report",
            "result_success": True,
            "result_status": "completed",
            "assistant_message": "created the requested file",
        }
    ]


def test_prepare_workspace_creates_sqlite_seed_database(tmp_path: Path) -> None:
    scenario = {
        "id": "sqlite_seed",
        "setup_sqlite": [
            {
                "path": "inputs/app.sqlite3",
                "schema": ["CREATE TABLE metrics(name TEXT, value INTEGER)"],
                "rows": {
                    "metrics": {
                        "columns": ["name", "value"],
                        "values": [["alpha", 4], ["beta", 6]],
                    }
                },
            }
        ],
    }

    workspace = evals._prepare_workspace(tmp_path, scenario)

    conn = sqlite3.connect(workspace / "inputs/app.sqlite3")
    try:
        assert conn.execute("SELECT sum(value) FROM metrics").fetchone()[0] == 10
    finally:
        conn.close()


def test_prepare_workspace_creates_zip_archive_with_raw_entry_names(tmp_path: Path) -> None:
    scenario = {
        "id": "archive_seed",
        "setup_archives": [
            {
                "path": "inputs/unsafe.zip",
                "entries": [
                    {"name": "../escape.txt", "content": "bad\n"},
                    {"name": "safe/readme.txt", "content": "ok\n"},
                ],
            }
        ],
    }

    workspace = evals._prepare_workspace(tmp_path, scenario)

    with zipfile.ZipFile(workspace / "inputs/unsafe.zip") as archive:
        assert archive.namelist() == ["../escape.txt", "safe/readme.txt"]
        assert archive.read("../escape.txt").decode("utf-8") == "bad\n"


def test_case_diagnostics_includes_repair_layer_and_workspace_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    case_dir = tmp_path / "case"
    (workspace / "reports").mkdir(parents=True)
    (workspace / "reports" / "out.md").write_text("status: draft\n", encoding="utf-8")
    (workspace / "tasks").mkdir()
    (workspace / "tasks" / "internal.txt").write_text("skip me", encoding="utf-8")
    case_dir.mkdir()

    executions = [
        {
            "command": ["python", "-m", "omniagent"],
            "exit_code": 0,
            "duration_seconds": 1.0,
            "stdout": "{}",
            "stderr": "",
            "json": {
                "thread": {"thread_id": "thread_abc"},
                "route": {
                    "input_type": "status_query",
                    "task_action": "status",
                    "reasoning": "matched status keyword",
                    "metadata": {"route_source": "rule"},
                },
                "assistant_message": "no active task",
            },
            "json_error": None,
        }
    ]
    evidence = {
        "turns": evals._extract_turn_summaries(executions),
        "used_skills": [],
        "permissions": [],
        "node_statuses": [],
        "critic_actions": [],
        "llm_calls": [],
    }
    result = {
        "id": "demo",
        "status": "failed",
        "diagnosis": "router_or_interaction_dispatch",
        "failure_layer": "routing",
        "issues": ["missing file: notes/project.md"],
        "duration_seconds": 1.0,
    }

    evals._write_case_diagnostics(
        case_dir=case_dir,
        scenario={
            "id": "demo",
            "kind": "chat_sequence",
            "category": "conversation",
            "turns": [{"message": "Create notes/project.md"}],
        },
        result=result,
        executions=executions,
        evidence=evidence,
        workspace=workspace,
    )

    text = (case_dir / "diagnostics.md").read_text(encoding="utf-8")
    assert "- failure_layer: `routing`" in text
    assert "先检查 interaction router" in text
    assert "turn_01_stdout.txt" in text
    assert "`reports/out.md`" in text
    assert "tasks/internal.txt" not in text
    assert result["workspace_files"] == [
        {"path": "reports/out.md", "size": 14, "preview": "status: draft\\n"}
    ]


def test_expected_permission_denial_can_assert_diagnosis_layer(tmp_path: Path) -> None:
    executions = [
        {
            "json": {"result": {"success": False}},
            "timed_out": False,
        }
    ]
    evidence = {
        "turns": [],
        "used_skills": ["builtin.http.client"],
        "permissions": [
            {
                "permission": "network",
                "status": "denied",
            }
        ],
        "node_statuses": [{"status": "ready"}],
        "llm_calls": [],
    }

    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario={
            "assertions": {
                "must_not_succeed": True,
                "required_permission_denied_any": ["network"],
                "required_failure_layer_any": ["permission"],
                "diagnosis_any": ["permission_policy_or_scenario_config"],
                "required_node_status_any": ["ready"],
            }
        },
        workspace=tmp_path,
        executions=executions,
        evidence=evidence,
    )

    assert passed is True
    assert issues == []
    assert diagnosis == "permission_policy_or_scenario_config"


def test_assertions_detect_forbidden_skill_and_workspace_glob(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "out.md").write_text("ok", encoding="utf-8")
    (tmp_path / "debug.log").write_text("unexpected", encoding="utf-8")
    executions = [{"json": {"result": {"success": True}}, "timed_out": False}]
    evidence = {
        "turns": [],
        "used_skills": ["builtin.python.basic"],
        "permissions": [],
        "node_statuses": [{"status": "completed"}],
        "llm_calls": [],
    }

    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario={
            "assertions": {
                "must_succeed": True,
                "forbidden_skill_any": ["builtin.python.basic"],
                "file_count_max": 1,
                "forbidden_workspace_glob": ["*.log"],
            }
        },
        workspace=tmp_path,
        executions=executions,
        evidence=evidence,
    )

    assert passed is False
    assert diagnosis == "assertion_failure"
    assert any("forbidden skills were used" in issue for issue in issues)
    assert any("too many workspace files" in issue for issue in issues)
    assert any("forbidden workspace glob matched" in issue for issue in issues)


def test_assertions_check_full_assistant_messages(tmp_path: Path) -> None:
    executions = [
        {
            "json": {
                "result": {"success": False},
                "assistant_message": "I cannot complete this because inputs/data.json is invalid.",
            },
            "timed_out": False,
        }
    ]
    evidence = {
        "turns": [],
        "used_skills": [],
        "permissions": [],
        "node_statuses": [],
        "llm_calls": [],
    }

    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario={
            "assertions": {
                "must_not_succeed": True,
                "assistant_message_contains": ["invalid"],
                "assistant_message_not_contains": ["completed successfully"],
            }
        },
        workspace=tmp_path,
        executions=executions,
        evidence=evidence,
    )

    assert passed is True
    assert issues == []
    assert diagnosis == "passed"


def test_collect_evidence_reads_completion_and_embedding_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = workspace / "tasks" / "task_123"
    (task_dir / "completion").mkdir(parents=True)
    (task_dir / "llm_calls").mkdir()
    (workspace / "MEMORY" / "vector").mkdir(parents=True)
    (task_dir / "completion" / "evaluation.json").write_text(
        json.dumps(
            {
                "task_satisfied": False,
                "completion_score": 0.4,
                "summary": "模型未正确推理",
                "missing_requirements": ["需要合并两个约束"],
                "evidence": ["工具结果已给出"],
                "primary_failure_layer": "model_capability",
                "model_capability_suspected": True,
                "recommended_next_action": "record_model_limit",
                "confidence": 0.8,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (task_dir / "llm_calls" / "planner.json").write_text(
        json.dumps(
            {
                "prompt_name": "planner_v1",
                "model": "qwen",
                "usage": {"total_tokens": 10},
                "request_payload": {
                    "vector_matches": [
                        {
                            "entry_id": "task_old",
                            "source_type": "task",
                            "score": 0.91,
                            "embedding_provider": "local_model",
                            "embedding_model": "bge-small-zh-v1.5",
                            "embedding_dimensions": 512,
                            "embedding_version": "local_model_onnx_v1",
                            "embedding_model_fingerprint": "abc",
                            "embedding_fallback_used": False,
                            "match_details": {
                                "vector_compatible": True,
                                "vector_score": 0.88,
                                "matched_concepts": ["file"],
                            },
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (workspace / "MEMORY" / "vector" / "task_old.json").write_text(
        json.dumps(
            {
                "entry_id": "task_old",
                "source_type": "task",
                "metadata": {"task_id": "task_old"},
                "embedding": [0.1, 0.2],
                "embedding_provider": "local_model",
                "embedding_model": "bge-small-zh-v1.5",
                "embedding_dimensions": 512,
                "embedding_version": "local_model_onnx_v1",
                "embedding_model_fingerprint": "abc",
                "embedding_fallback_used": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    evidence = evals._collect_evidence(workspace, ["task_123"], [], [])

    assert evidence["completion_evaluations"][0]["primary_failure_layer"] == "model_capability"
    assert evidence["completion_evaluations"][0]["model_capability_suspected"] is True
    assert evidence["vector_matches"][0]["entry_id"] == "task_old"
    assert evidence["vector_matches"][0]["vector_compatible"] is True
    assert evidence["vector_memory_entries"][0]["embedding_model_fingerprint"] == "abc"
    assert evidence["embedding_summary"] == {
        "providers": ["local_model"],
        "models": ["bge-small-zh-v1.5"],
        "vector_match_count": 1,
        "vector_memory_entry_count": 1,
        "fallback_count": 0,
        "error_count": 0,
        "incompatible_match_count": 0,
    }


def test_completion_model_capability_updates_diagnosis_and_layer() -> None:
    evidence = {
        "turns": [],
        "permissions": [],
        "node_statuses": [],
        "completion_evaluations": [
            {
                "primary_failure_layer": "model_capability",
                "model_capability_suspected": True,
            }
        ],
        "embedding_summary": {},
    }

    diagnosis = evals._diagnose(["file report.md does not contain expected text"], [], evidence)

    assert diagnosis == "model_capability"
    assert evals._failure_layer(diagnosis) == "model_capability"


def test_embedding_assertions_and_diagnostics_sections(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    case_dir = tmp_path / "case"
    workspace.mkdir()
    case_dir.mkdir()
    executions = [
        {
            "command": ["python", "-m", "omniagent"],
            "exit_code": 0,
            "duration_seconds": 1.0,
            "stdout": "{}",
            "stderr": "",
            "json": {"result": {"success": True}},
            "json_error": None,
        }
    ]
    evidence = {
        "turns": [],
        "used_skills": [],
        "permissions": [],
        "node_statuses": [],
        "critic_actions": [],
        "completion_evaluations": [
            {
                "task_id": "task_123",
                "task_satisfied": True,
                "completion_score": 1.0,
                "primary_failure_layer": "none",
                "model_capability_suspected": False,
                "confidence": 0.9,
                "summary": "任务完成",
                "missing_requirements": [],
                "evidence": ["产物存在"],
            }
        ],
        "vector_matches": [
            {
                "task_id": "task_123",
                "entry_id": "task_old",
                "score": 0.8,
                "embedding_provider": "local_model",
                "embedding_model": "bge-small-zh-v1.5",
                "embedding_dimensions": 512,
                "vector_compatible": True,
                "vector_score": 0.7,
                "embedding_fallback_used": False,
            }
        ],
        "vector_memory_entries": [],
        "embedding_summary": {
            "providers": ["local_model"],
            "models": ["bge-small-zh-v1.5"],
            "vector_match_count": 1,
            "vector_memory_entry_count": 0,
            "fallback_count": 0,
            "error_count": 0,
            "incompatible_match_count": 0,
        },
        "llm_calls": [],
    }
    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario={
            "assertions": {
                "must_succeed": True,
                "min_vector_match_count": 1,
                "required_vector_match_entry_any": ["task_old"],
                "require_vector_compatible": True,
                "required_embedding_provider_any": ["local_model"],
                "forbid_embedding_fallback": True,
                "forbid_embedding_errors": True,
            }
        },
        workspace=workspace,
        executions=executions,
        evidence=evidence,
    )
    assert passed is True
    assert issues == []
    assert diagnosis == "passed"

    result = {
        "id": "demo",
        "status": "passed",
        "diagnosis": "passed",
        "failure_layer": "none",
        "issues": [],
        "duration_seconds": 1.0,
        "failure_attribution": evals._build_failure_attribution(
            diagnosis="passed",
            passed=True,
            evidence=evidence,
        ),
    }
    evals._write_case_diagnostics(
        case_dir=case_dir,
        scenario={"id": "demo", "kind": "task", "goal": "write report"},
        result=result,
        executions=executions,
        evidence=evidence,
        workspace=workspace,
    )

    text = (case_dir / "diagnostics.md").read_text(encoding="utf-8")
    assert "## Completion Evaluation" in text
    assert "## Embedding And Vector Recall" in text
    assert "## Failure Attribution" in text
    assert "bge-small-zh-v1.5" in text


def test_complex_scenarios_require_explicit_flag() -> None:
    class Args:
        scenario = None
        include_network = False
        include_l4 = False
        include_complex = False
        include_all = False

    scenario = {"id": "complex_case", "requires": ["complex"], "max_permission": "L2"}

    assert evals._scenario_skip_reason(scenario, Args()) == "requires --include-complex"

    Args.include_complex = True
    assert evals._scenario_skip_reason(scenario, Args()) is None

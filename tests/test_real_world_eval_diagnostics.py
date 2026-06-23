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


def test_task_intake_assertions_use_collected_assessment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario = {
        "id": "general_intake",
        "assertions": {
            "required_intake_task_type_any": ["file_organization"],
            "required_intake_risk_level_any": ["low"],
            "required_intake_tool_any": ["builtin.file.basic"],
            "required_intake_validation_strategy_any": ["artifact_file_check"],
            "required_intake_confirmation_required": False,
            "required_experience_status_any": ["succeeded"],
            "required_policy_risk_flag_any": ["delete"],
            "required_llm_prompt_any": ["planner_v1"],
        },
    }
    executions = [{"json": {"result": {"success": True}}, "timed_out": False}]
    evidence = {
        "experience_records": [
            {
                "task_id": "task_intake",
                "experience_id": "exp_intake",
                "status": "succeeded",
            }
        ],
        "policy_decisions": [
            {
                "task_id": "task_intake",
                "policy_decision": "plan_required",
                "risk_flags": {"delete": True},
            }
        ],
        "task_intake_assessments": [
            {
                "task_id": "task_intake",
                "task_type": "file_organization",
                "risk_level": "low",
                "tools": ["builtin.file.basic"],
                "validation_strategy": "artifact_file_check",
                "confirmation_required": False,
            }
        ],
        "llm_calls": [{"prompt_name": "planner_v1"}],
        "permissions": [],
        "node_statuses": [],
        "completion_evaluations": [],
    }

    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario=scenario,
        workspace=workspace,
        executions=executions,
        evidence=evidence,
    )

    assert passed is True
    assert issues == []
    assert diagnosis == "passed"


def test_permission_denied_experience_drives_permission_diagnosis(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario = {
        "id": "general_permission_boundary",
        "assertions": {
            "must_not_succeed": True,
            "required_experience_status_any": ["permission_denied"],
            "required_policy_risk_flag_any": ["delete"],
            "required_failure_layer_any": ["permission"],
        },
    }
    executions = [{"json": {"result": {"success": False}}, "timed_out": False}]
    evidence = {
        "experience_records": [
            {
                "task_id": "task_permission",
                "experience_id": "exp_permission",
                "status": "permission_denied",
                "failure_layer": "permission",
                "error_code": "permission_denied",
                "permission": "filesystem.delete",
            }
        ],
        "policy_decisions": [
            {
                "task_id": "task_permission",
                "policy_decision": "plan_required",
                "risk_flags": {"delete": True},
            }
        ],
        "permissions": [],
        "node_statuses": [],
        "completion_evaluations": [],
    }

    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario=scenario,
        workspace=workspace,
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


def test_assertions_check_learning_correction_evidence(tmp_path: Path) -> None:
    executions = [
        {"json": {"result": {"success": True}}, "timed_out": False},
        {"json": {"result": {"success": False}}, "timed_out": False},
    ]
    evidence = {
        "turns": [],
        "used_skills": [],
        "permissions": [],
        "node_statuses": [],
        "llm_calls": [],
        "learning_corrections": [
            {
                "procedure_id": "proc_1",
                "object_type": "procedure",
                "trigger": "validator_failure",
                "new_status": "draft",
                "new_review_status": "correction_required",
                "frozen": True,
            }
        ],
    }

    passed, issues, diagnosis = evals._evaluate_assertions(
        scenario={
            "assertions": {
                "min_successful_turns": 1,
                "min_failed_turns": 1,
                "required_learning_correction_trigger_any": ["validator_failure"],
                "required_learning_correction_new_status_any": ["draft"],
                "required_learning_correction_review_status_any": ["correction_required"],
                "required_learning_correction_object_type_any": ["procedure"],
                "require_learning_correction_frozen": True,
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


def test_run_selection_supports_category_and_scenario_glob() -> None:
    class Args:
        scenario = None
        scenario_glob = None
        scenario_category = None
        include_network = False
        include_l4 = False
        include_complex = False
        include_all = False

    scenario = {
        "id": "general_agent_usability_file_organization_tool_choice",
        "category": "general_agent_usability",
        "requires": [],
        "max_permission": "L2",
    }
    other_category = {
        "id": "general_agent_usability_file_organization_tool_choice",
        "category": "legacy",
        "requires": [],
        "max_permission": "L2",
    }
    other_id = {
        "id": "general_agent_usability_file_organization",
        "category": "general_agent_usability",
        "requires": [],
        "max_permission": "L2",
    }

    Args.scenario_category = ["general_agent_usability"]
    assert evals._scenario_skip_reason(scenario, Args()) is None
    assert evals._scenario_skip_reason(other_category, Args()) == "not selected"

    Args.scenario_glob = ["general_agent_usability_*_tool_choice"]
    assert evals._scenario_skip_reason(scenario, Args()) is None
    assert evals._scenario_skip_reason(other_id, Args()) == "not selected"

    Args.scenario = ["exact_outside_category"]
    exact = {
        "id": "exact_outside_category",
        "category": "legacy",
        "requires": [],
        "max_permission": "L2",
    }
    assert evals._scenario_skip_reason(exact, Args()) is None


def test_dry_run_writes_selection_summary_without_executing(tmp_path: Path, capsys) -> None:
    scenario_path = tmp_path / "scenarios.json"
    run_root = tmp_path / "runs"
    scenario_path.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "id": "general_agent_usability_file_organization_tool_choice",
                        "category": "general_agent_usability",
                        "kind": "task",
                        "goal": "copy inputs/a.txt to outputs/a.txt",
                    },
                    {
                        "id": "other_case",
                        "category": "legacy",
                        "kind": "task",
                        "goal": "write reports/other.md",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = evals.main(
        [
            "--scenarios",
            str(scenario_path),
            "--run-root",
            str(run_root),
            "--scenario-category",
            "general_agent_usability",
            "--scenario-glob",
            "*_tool_choice",
            "--expect-selected-count",
            "1",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "selected=1" in output
    assert "executed=0" in output
    run_dirs = list(run_root.iterdir())
    assert len(run_dirs) == 1
    summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["selected"] == 1
    assert summary["metadata"]["selection"]["scenario_category"] == [
        "general_agent_usability"
    ]
    assert summary["metadata"]["selection"]["scenario_glob"] == ["*_tool_choice"]
    assert summary["metadata"]["execution"]["dry_run"] is True
    assert summary["metadata"]["llm"]["provider"]
    assert summary["results"][0]["status"] == "selected"
    assert summary["results"][0]["id"] == "general_agent_usability_file_organization_tool_choice"
    assert not (run_dirs[0] / "workspaces").exists()


def test_expected_selected_count_mismatch_fails_before_execution(
    tmp_path: Path, capsys
) -> None:
    scenario_path = tmp_path / "scenarios.json"
    run_root = tmp_path / "runs"
    scenario_path.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "id": "general_agent_usability_file_organization_tool_choice",
                        "category": "general_agent_usability",
                        "kind": "task",
                        "goal": "copy inputs/a.txt to outputs/a.txt",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = evals.main(
        [
            "--scenarios",
            str(scenario_path),
            "--run-root",
            str(run_root),
            "--scenario-category",
            "general_agent_usability",
            "--expect-selected-count",
            "2",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "selected_count_mismatch: expected=2 actual=1" in captured.err
    run_dirs = list(run_root.iterdir())
    assert len(run_dirs) == 1
    summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["selected"] == 1
    assert summary["metadata"]["selection"]["expect_selected_count"] == 2
    assert summary["results"][0]["status"] == "selected"
    assert not (run_dirs[0] / "workspaces").exists()


def test_require_real_llm_rejects_test_provider_before_execution(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    scenario_path = tmp_path / "scenarios.json"
    run_root = tmp_path / "runs"
    scenario_path.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "id": "general_agent_usability_file_organization_tool_choice",
                        "category": "general_agent_usability",
                        "kind": "task",
                        "goal": "copy inputs/a.txt to outputs/a.txt",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENT_LLM_PROVIDER", "test")

    exit_code = evals.main(
        [
            "--scenarios",
            str(scenario_path),
            "--run-root",
            str(run_root),
            "--scenario-category",
            "general_agent_usability",
            "--expect-selected-count",
            "1",
            "--require-real-llm",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "require_real_llm_failed" in captured.err
    assert "llm.provider=test" in captured.err
    assert not run_root.exists()


def test_evidence_index_enriches_legacy_skipped_summary_with_scenario_metadata(
    tmp_path: Path,
) -> None:
    case = evals._summarize_result_for_index(
        run_root=tmp_path,
        summary_path=tmp_path / "run_legacy" / "summary.json",
        summary_result={
            "id": "network_case",
            "status": "skipped",
            "diagnosis": "requires --include-network",
            "failure_layer": "none",
            "issues": [],
        },
        scenario_metadata={
            "network_case": {
                "category": "network",
                "kind": "task",
                "requires": ["network"],
            }
        },
    )

    assert case["category"] == "network"
    assert case["kind"] == "task"
    assert case["requires"] == ["network"]
    assert case["skip_reason"] == "requires --include-network"


def test_evidence_index_summarizes_runs_and_learning_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "20260612_000000"
    case_dir = run_dir / "cases" / "learning_loop_case"
    workspace = run_dir / "workspaces" / "learning_loop_case"
    task_id = "task_123"
    task_dir = workspace / "tasks" / task_id
    (task_dir / "procedure").mkdir(parents=True)
    (task_dir / "experience").mkdir(parents=True)
    (task_dir / "learning").mkdir(parents=True)
    (workspace / "skills" / "managed" / "generated.habit.demo").mkdir(parents=True)
    (workspace / "procedures" / "disabled").mkdir(parents=True)
    case_dir.mkdir(parents=True)

    (task_dir / "procedure" / "procedure_match.json").write_text(
        json.dumps(
            {
                "matched": True,
                "matched_procedure_id": "proc_1",
                "matched_status": "habit",
                "suggestion_mode": "habit_fast_path_candidate",
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "procedure" / "habit_fast_path.json").write_text(
        json.dumps(
            {
                "decision": "habit_fast_path_candidate",
                "habit_id": "proc_1",
                "source_experience_ids": ["exp_1"],
                "validator_evidence_refs": ["tasks/task_source/completion/evaluation.json"],
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "experience" / "experience_record.json").write_text(
        json.dumps(
            {
                "experience_id": "exp_2",
                "status": "succeeded",
                "validation": {"verdict": "passed"},
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "learning" / "feedback.json").write_text(
        json.dumps(
            {
                "schema_version": "learning-feedback/v1",
                "task_id": task_id,
                "experience": {
                    "experience_id": "exp_2",
                    "status": "succeeded",
                    "validation_verdict": "passed",
                    "learnable": True,
                },
                "procedure": {
                    "matched_id": "proc_1",
                    "matched_object_type": "habit",
                },
                "habit_fast_path": {"decision": "habit_fast_path_candidate"},
                "reuse": {"status": "habit_fast_path", "reusable_now": True},
                "review": {"required": False, "target_type": "habit", "target_id": "proc_1"},
                "summary_lines": ["habit feedback"],
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "learning" / "correction.json").write_text(
        json.dumps(
            {
                "schema_version": "learning-correction/v1",
                "task_id": task_id,
                "experience_id": "exp_2",
                "procedure_id": "proc_1",
                "object_type": "procedure",
                "trigger": "validator_failure",
                "previous_status": "verified",
                "new_status": "draft",
                "previous_review_status": "approved",
                "new_review_status": "correction_required",
                "failure_count": 1,
                "frozen": True,
                "evidence_refs": ["tasks/task_123/completion/evaluation.json"],
                "reason": "validator failed",
                "next_actions": ["omniagent learning approve --type procedure proc_1"],
            }
        ),
        encoding="utf-8",
    )
    (workspace / "skills" / "managed" / "generated.habit.demo" / "lineage.json").write_text(
        json.dumps(
            {
                "skill_name": "generated.habit.demo",
                "version": "1.0.0",
                "habit_id": "proc_1",
                "procedure_id": "proc_1",
                "generation_type": "file",
                "source_targets": ["builtin.file.basic"],
                "source_experience_ids": ["exp_1"],
            }
        ),
        encoding="utf-8",
    )
    db_path = run_dir / "data" / "learning_loop_case.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE skill_registry (
                skill_name TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                version TEXT NOT NULL,
                description TEXT NOT NULL,
                permission_requirements_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                location TEXT NOT NULL,
                scope TEXT NOT NULL,
                sandbox_level TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                success_rate REAL NOT NULL DEFAULT 0.0,
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                activation_source TEXT,
                activation_target TEXT,
                last_validated_at TEXT,
                last_approved_at TEXT,
                last_seen_at TEXT NOT NULL,
                contracts_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE skill_registration_records (
                registration_id TEXT PRIMARY KEY,
                skill_name TEXT NOT NULL,
                version TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                source_path TEXT NOT NULL,
                target_path TEXT,
                previous_version TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO skill_registry (
                skill_name, source, version, description, permission_requirements_json,
                tags_json, location, scope, sandbox_level, lifecycle_status, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "generated.habit.demo",
                "habit_promotion",
                "1.0.0",
                "demo",
                json.dumps(["filesystem.write"]),
                json.dumps(["generated", "deprecated"]),
                "workspace/skills/managed/generated.habit.demo",
                "workspace",
                "workspace",
                "deprecated",
                "2026-06-12T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO skill_registration_records (
                registration_id, skill_name, version, action, status, source_path,
                target_path, previous_version, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "reg_1",
                "generated.habit.demo",
                "1.0.0",
                "deprecate",
                "deprecated",
                "workspace/skills/managed/generated.habit.demo",
                None,
                "1.0.0",
                json.dumps({"note": "stale skill"}),
                "2026-06-12T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    (workspace / "procedures" / "disabled" / "proc_1.json").write_text(
        json.dumps(
            {
                "schema_version": "procedure-memory/v1",
                "procedure_id": "proc_1",
                "name": "disabled habit",
                "status": "disabled",
                "review_status": "disabled",
                "risk_level": "low",
                "source_experience_ids": ["exp_1"],
                "success_count": 3,
                "failure_count": 0,
                "audit_notes": [
                    {
                        "event": "disabled",
                        "previous_status": "habit",
                        "note": "stale",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = {
        "id": "learning_loop_case",
        "category": "learning_loop",
        "kind": "task",
        "status": "passed",
        "diagnosis": "passed",
        "failure_layer": "none",
        "issues": [],
        "workspace": str(workspace),
        "case_dir": str(case_dir),
        "task_ids": [task_id],
        "thread_ids": [],
        "duration_seconds": 1.0,
        "evidence": {
            "used_skills": ["generated.habit.demo"],
            "permissions": [{"permission": "filesystem.write", "status": "granted"}],
            "policy_decisions": [{"policy_decision": "plan_required"}],
            "completion_evaluations": [
                {
                    "primary_failure_layer": "none",
                    "model_capability_suspected": False,
                }
            ],
            "embedding_summary": {"providers": ["local_model"]},
            "llm_calls": [{"total_tokens": 42}],
        },
    }
    (case_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (case_dir / "diagnostics.md").write_text("# diagnostics\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "created_at": "2026-06-12T00:00:00+00:00",
                "total": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "results": [result],
            }
        ),
        encoding="utf-8",
    )

    output_json = tmp_path / "evidence_index.json"
    payload = evals._write_evidence_index(tmp_path, output_json)

    assert payload["schema_version"] == "eval-evidence-index/v1"
    assert payload["run_count"] == 1
    assert payload["case_count"] == 1
    assert payload["summary"]["by_status"] == {"passed": 1}
    assert payload["summary"]["by_scenario"] == {"learning_loop_case": 1}
    assert payload["summary"]["by_category"] == {"learning_loop": 1}
    assert payload["summary"]["by_requires"] == {}
    assert payload["summary"]["by_skip_reason"] == {}
    assert payload["summary"]["by_failure_layer"] == {"none": 1}
    case = payload["cases"][0]
    assert case["used_skills"] == ["generated.habit.demo"]
    assert case["permission_summary"] == {"filesystem.write:granted": 1}
    assert case["llm_total_tokens"] == 42
    assert case["learning_evidence"]["procedure_matches"][0]["procedure_id"] == "proc_1"
    assert case["learning_evidence"]["habit_fast_paths"][0]["habit_id"] == "proc_1"
    assert case["learning_evidence"]["experience_records"][0]["experience_id"] == "exp_2"
    feedback = case["learning_evidence"]["learning_feedbacks"][0]
    assert feedback["schema_version"] == "learning-feedback/v1"
    assert feedback["reuse_status"] == "habit_fast_path"
    assert feedback["review_target_type"] == "habit"
    correction = case["learning_evidence"]["learning_corrections"][0]
    assert correction["schema_version"] == "learning-correction/v1"
    assert correction["procedure_id"] == "proc_1"
    assert correction["trigger"] == "validator_failure"
    assert correction["new_review_status"] == "correction_required"
    assert correction["frozen"] is True
    assert case["learning_evidence"]["procedure_memory_records"][0]["procedure_id"] == "proc_1"
    assert case["learning_evidence"]["procedure_memory_records"][0]["object_type"] == "habit"
    assert case["learning_evidence"]["procedure_memory_records"][0]["status"] == "disabled"
    assert case["learning_evidence"]["procedure_memory_records"][0]["previous_status"] == "habit"
    assert case["learning_evidence"]["procedure_memory_records"][0]["audit_event_count"] == 1
    assert case["learning_evidence"]["procedure_memory_records"][0]["audit_events"] == ["disabled"]
    assert case["learning_evidence"]["procedure_memory_records"][0]["latest_audit_event"] == (
        "disabled"
    )
    assert case["learning_evidence"]["skill_lineages"][0]["skill_name"] == "generated.habit.demo"
    assert case["learning_evidence"]["skill_lineages"][0]["generation_type"] == "file"
    assert case["learning_evidence"]["skill_lineages"][0]["source_targets"] == [
        "builtin.file.basic"
    ]
    skill_record = case["learning_evidence"]["skill_lifecycle_records"][0]
    assert skill_record["skill_name"] == "generated.habit.demo"
    assert skill_record["lifecycle_status"] == "deprecated"
    assert skill_record["registration_actions"] == ["deprecate"]
    assert skill_record["registration_statuses"] == ["deprecated"]
    assert skill_record["latest_registration_note"] == "stale skill"
    assert output_json.exists()
    assert output_json.with_suffix(".md").exists()
    markdown = output_json.with_suffix(".md").read_text(encoding="utf-8")
    assert "- scenario: `learning_loop_case`=1" in markdown
    assert "| `20260612_000000` | `learning_loop_case` | learning_loop |" in markdown


def test_evidence_index_query_filters_and_summarizes_learning_lineage() -> None:
    index = {
        "schema_version": "eval-evidence-index/v1",
        "run_root": "/tmp/eval_runs",
        "run_count": 2,
        "cases": [
            {
                "run_id": "run_a",
                "scenario_id": "learning_loop_case",
                "category": "learning_loop",
                "kind": "task",
                "requires": ["complex"],
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "duration_seconds": 1.25,
                "llm_call_count": 1,
                "llm_total_tokens": 42,
                "task_ids": ["task_1"],
                "thread_ids": [],
                "used_skills": ["generated.habit.demo"],
                "permission_summary": {"filesystem.write:granted": 1},
                "policy_decisions": [
                    {
                        "policy_decision": "direct_execute",
                        "reason": "direct execute static gates passed",
                        "skipped_stages": ["planner"],
                        "context_budget": {
                            "task_request": 592,
                            "payload": 177,
                            "skill_registry_entries": {
                                "items": 15,
                                "json_chars": 56119,
                            },
                            "planner_skill_registry_entries": {
                                "items": 6,
                                "json_chars": 23308,
                            },
                            "agent_state_summary": {
                                "items": 9,
                                "json_chars": 1441,
                            },
                        },
                    }
                ],
                "request_payload_stats": [
                    {
                        "scope": "task",
                        "task_id": "task_1",
                        "prompt_name": "planner_v1",
                        "json_chars": 28884,
                        "fields": {
                            "memory_summaries": {"items": 0, "json_chars": 2},
                            "workflow_matches": {"items": 2, "json_chars": 1248},
                            "skill_registry_entries": {
                                "items": 6,
                                "json_chars": 27511,
                            },
                        },
                    }
                ],
                "node_statuses": [
                    {
                        "task_id": "task_1",
                        "node_id": "node_write_report",
                        "type": "action",
                        "status": "completed",
                        "executor_hint": "file",
                        "skill_name": "generated.habit.demo",
                        "action": "write",
                        "attempts": 1,
                    }
                ],
                "critic_actions": [
                    {
                        "task_id": "task_1",
                        "target_id": "task_1",
                        "action": "accept",
                        "summary": "Task completed successfully.",
                    }
                ],
                "completion_failure_layers": ["none"],
                "embedding_summary": {
                    "providers": ["local_model"],
                    "models": ["bge-small"],
                    "vector_match_count": 1,
                    "vector_memory_entry_count": 1,
                    "fallback_count": 0,
                    "error_count": 0,
                    "incompatible_match_count": 0,
                },
                "learning_evidence": {
                    "procedure_matches": [
                        {
                            "procedure_id": "proc_1",
                            "matched": True,
                            "suggestion_mode": "habit_fast_path_candidate",
                            "required_confirmation": False,
                            "rejected_candidate_count": 0,
                        }
                    ],
                    "habit_fast_paths": [{"habit_id": "habit_1"}],
                    "experience_records": [],
                    "learning_feedbacks": [
                        {
                            "task_id": "task_1",
                            "schema_version": "learning-feedback/v1",
                            "reuse_status": "habit_fast_path",
                            "review_target_type": "habit",
                        }
                    ],
                    "learning_corrections": [
                        {
                            "task_id": "task_1",
                            "schema_version": "learning-correction/v1",
                            "procedure_id": "proc_1",
                            "object_type": "procedure",
                            "trigger": "validator_failure",
                            "previous_status": "verified",
                            "new_status": "draft",
                            "new_review_status": "correction_required",
                            "failure_count": 1,
                            "frozen": True,
                        }
                    ],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "habit_1",
                            "object_type": "habit",
                            "review_decisions": ["approved"],
                            "latest_review_decision": "approved",
                            "audit_events": ["promoted_to_habit"],
                            "latest_audit_event": "promoted_to_habit",
                        }
                    ],
                    "skill_lineages": [
                        {
                            "skill_name": "generated.habit.demo",
                            "procedure_id": "proc_1",
                            "habit_id": "habit_1",
                            "generation_type": "file",
                            "source_targets": ["builtin.file.basic"],
                        }
                    ],
                    "skill_lifecycle_records": [
                        {
                            "skill_name": "generated.habit.demo",
                            "version": "1.0.0",
                            "validation_passed": [True],
                        }
                    ],
                },
                "artifacts": [
                    {
                        "kind": "diagnostics",
                        "path": "/tmp/eval_runs/run_a/cases/learning_loop_case/diagnostics.md",
                        "exists": True,
                    }
                ],
            },
            {
                "run_id": "run_b",
                "scenario_id": "json_report_case",
                "category": "data",
                "kind": "task",
                "status": "failed",
                "failure_layer": "skill_or_tool",
                "diagnosis": "skill_execution_failed",
                "used_skills": ["builtin.data.json"],
                "policy_decisions": [
                    {
                        "policy_decision": "plan_required",
                        "reason": "risk flag requires planner context",
                        "risk_flags": {"planner_context_required": True},
                    }
                ],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "skill_lineages": [],
                },
                "artifacts": [],
            },
        ],
    }

    query = evals.query_evidence_index(
        index,
        statuses=["passed"],
        failure_layers=["none"],
        skills=["generated.habit.*"],
        procedure_ids=["proc_1"],
        habit_ids=["habit_1"],
        lineage_skills=["generated.habit.demo"],
        promotion_generation_types=["file"],
        promotion_source_targets=["builtin.file.*"],
        scenario_ids=["learning_*"],
        categories=["learning_*"],
        requires=["complex"],
        permissions=["filesystem.write:*"],
        completion_layers=["none"],
        policy_decisions=["direct_execute"],
        skipped_stages=["planner"],
        policy_reasons=["direct execute*"],
        policy_context_budgets=["skill_registry_entries:json_chars:50000+"],
        request_payload_stats=["skill_registry_entries:json_chars:10000-49999"],
        node_statuses=["status:completed"],
        critic_actions=["action:accept"],
        memory_retrieval_signals=["vector_match"],
        llm_usage_signals=["llm_called"],
        learning_kinds=["habit_fast_paths"],
        learning_validation_statuses=["passed"],
        skill_validation_statuses=["passed"],
        learning_review_decisions=["approved"],
        learning_audit_events=["promoted_to_habit"],
    )
    generation_miss = evals.query_evidence_index(index, promotion_generation_types=["json"])
    target_miss = evals.query_evidence_index(index, promotion_source_targets=["tsv"])
    risk_query = evals.query_evidence_index(
        index,
        policy_risk_flags=["planner_context_required"],
    )
    reason_query = evals.query_evidence_index(index, policy_reasons=["*risk flag*"])
    parsed = evals.build_parser().parse_args(
        [
            "--query-evidence-index",
            "--evidence-completion-layer",
            "none",
            "--evidence-policy-decision",
            "direct_execute",
            "--evidence-skipped-stage",
            "planner",
            "--evidence-policy-risk-flag",
            "planner_context_required",
            "--evidence-policy-reason",
            "*risk flag*",
            "--evidence-policy-context-budget",
            "skill_registry_entries:json_chars:50000+",
            "--evidence-request-payload-stats",
            "skill_registry_entries:json_chars:10000-49999",
            "--evidence-node-status",
            "status:completed",
            "--evidence-critic-action",
            "action:accept",
            "--evidence-memory-retrieval",
            "vector_match",
            "--evidence-llm-usage",
            "llm_called",
            "--evidence-category",
            "learning_loop",
            "--evidence-requires",
            "complex",
            "--evidence-skip-reason",
            "requires --include-network",
            "--evidence-learning-validation",
            "passed",
            "--evidence-skill-validation",
            "passed",
            "--evidence-learning-review-decision",
            "approved",
            "--evidence-learning-audit-event",
            "promoted_to_habit",
            "--evidence-promotion-generation-type",
            "file",
            "--evidence-promotion-source-target",
            "builtin.file.*",
        ]
    )
    assert parsed.evidence_completion_layer == ["none"]
    assert parsed.evidence_policy_decision == ["direct_execute"]
    assert parsed.evidence_skipped_stage == ["planner"]
    assert parsed.evidence_policy_risk_flag == ["planner_context_required"]
    assert parsed.evidence_policy_reason == ["*risk flag*"]
    assert parsed.evidence_policy_context_budget == ["skill_registry_entries:json_chars:50000+"]
    assert parsed.evidence_request_payload_stats == [
        "skill_registry_entries:json_chars:10000-49999"
    ]
    assert parsed.evidence_node_status == ["status:completed"]
    assert parsed.evidence_critic_action == ["action:accept"]
    assert parsed.evidence_memory_retrieval == ["vector_match"]
    assert parsed.evidence_llm_usage == ["llm_called"]
    assert parsed.evidence_category == ["learning_loop"]
    assert parsed.evidence_requires == ["complex"]
    assert parsed.evidence_skip_reason == ["requires --include-network"]
    assert parsed.evidence_learning_validation == ["passed"]
    assert parsed.evidence_skill_validation == ["passed"]
    assert parsed.evidence_learning_review_decision == ["approved"]
    assert parsed.evidence_learning_audit_event == ["promoted_to_habit"]
    assert parsed.evidence_promotion_generation_type == ["file"]
    assert parsed.evidence_promotion_source_target == ["builtin.file.*"]

    assert query["schema_version"] == "eval-evidence-query/v1"
    assert query["filters"]["skill"] == ["generated.habit.*"]
    assert query["filters"]["category"] == ["learning_*"]
    assert query["filters"]["requires"] == ["complex"]
    assert query["filters"]["permission"] == ["filesystem.write:*"]
    assert query["filters"]["completion_layer"] == ["none"]
    assert query["filters"]["policy_decision"] == ["direct_execute"]
    assert query["filters"]["skipped_stage"] == ["planner"]
    assert query["filters"]["policy_risk_flag"] == []
    assert query["filters"]["policy_reason"] == ["direct execute*"]
    assert query["filters"]["policy_context_budget"] == ["skill_registry_entries:json_chars:50000+"]
    assert query["filters"]["request_payload_stats"] == [
        "skill_registry_entries:json_chars:10000-49999"
    ]
    assert query["filters"]["node_status"] == ["status:completed"]
    assert query["filters"]["critic_action"] == ["action:accept"]
    assert query["filters"]["memory_retrieval"] == ["vector_match"]
    assert query["filters"]["llm_usage"] == ["llm_called"]
    assert query["filters"]["learning_kind"] == ["habit_fast_paths"]
    assert query["filters"]["learning_validation"] == ["passed"]
    assert query["filters"]["skill_validation"] == ["passed"]
    assert query["filters"]["learning_review_decision"] == ["approved"]
    assert query["filters"]["learning_audit_event"] == ["promoted_to_habit"]
    assert query["filters"]["promotion_generation_type"] == ["file"]
    assert query["filters"]["promotion_source_target"] == ["builtin.file.*"]
    assert query["summary"]["total_index_cases"] == 2
    assert query["summary"]["matched_cases"] == 1
    assert generation_miss["summary"]["matched_cases"] == 0
    assert target_miss["summary"]["matched_cases"] == 0
    assert query["summary"]["by_status"] == {"passed": 1}
    assert query["summary"]["by_scenario"] == {"learning_loop_case": 1}
    assert query["summary"]["by_category"] == {"learning_loop": 1}
    assert query["summary"]["by_requires"] == {"complex": 1}
    assert query["summary"]["by_skip_reason"] == {}
    assert query["summary"]["by_skill"] == {"generated.habit.demo": 1}
    assert query["summary"]["by_procedure"] == {"proc_1": 1}
    assert query["summary"]["by_habit"] == {"habit_1": 1}
    assert query["summary"]["by_lineage_skill"] == {"generated.habit.demo": 1}
    assert query["summary"]["by_promotion_generation_type"] == {"file": 1}
    assert query["summary"]["by_promotion_source_target"] == {"builtin.file.basic": 1}
    assert query["summary"]["by_completion_layer"] == {"none": 1}
    assert query["summary"]["by_policy_decision"] == {"direct_execute": 1}
    assert query["summary"]["by_skipped_stage"] == {"planner": 1}
    assert query["summary"]["by_policy_risk_flag"] == {}
    assert query["summary"]["by_policy_reason"] == {"direct execute static gates passed": 1}
    assert query["summary"]["by_policy_context_budget"] == {
        "agent_state_summary:items:6-20": 1,
        "agent_state_summary:json_chars:1000-9999": 1,
        "payload:json_chars:1-999": 1,
        "planner_skill_registry_entries:items:6-20": 1,
        "planner_skill_registry_entries:json_chars:10000-49999": 1,
        "recorded": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:50000+": 1,
        "task_request:json_chars:1-999": 1,
    }
    assert query["summary"]["by_request_payload_stats"] == {
        "memory_summaries:items:0": 1,
        "memory_summaries:json_chars:1-999": 1,
        "prompt:planner_v1": 1,
        "recorded": 1,
        "scope:task": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:10000-49999": 1,
        "total:json_chars:10000-49999": 1,
        "workflow_matches:items:1-5": 1,
        "workflow_matches:json_chars:1000-9999": 1,
    }
    assert query["summary"]["by_node_status"] == {
        "action:write": 1,
        "attempts:1": 1,
        "executor:file": 1,
        "recorded": 1,
        "skill:generated.habit.demo": 1,
        "status:completed": 1,
        "type:action": 1,
    }
    assert query["summary"]["by_critic_action"] == {
        "action:accept": 1,
        "recorded": 1,
        "summary:present": 1,
        "target:task_1": 1,
    }
    assert query["summary"]["by_memory_retrieval"] == {
        "model:bge-small": 1,
        "provider:local_model": 1,
        "recorded": 1,
        "vector_match": 1,
        "vector_memory_entry": 1,
    }
    assert query["summary"]["by_llm_usage"] == {
        "duration:1-10s": 1,
        "llm_called": 1,
        "tokens:1-999": 1,
    }
    assert query["summary"]["by_learning_validation"] == {
        "approved": 1,
        "habit": 1,
        "passed": 1,
        "recorded": 1,
        "skill": 1,
    }
    assert query["summary"]["by_skill_validation"] == {"passed": 1}
    assert query["summary"]["by_learning_review_decision"] == {"approved": 1}
    assert query["summary"]["by_learning_audit_event"] == {"promoted_to_habit": 1}
    assert query["summary"]["by_permission"] == {"filesystem.write:granted": 1}
    assert query["summary"]["by_learning_kind"] == {
        "habit_fast_paths": 1,
        "learning_corrections": 1,
        "learning_feedbacks": 1,
        "procedure_matches": 1,
        "procedure_memory_records": 1,
        "skill_lifecycle_records": 1,
        "skill_lineages": 1,
    }
    assert risk_query["filters"]["policy_risk_flag"] == ["planner_context_required"]
    assert risk_query["summary"]["matched_cases"] == 1
    assert risk_query["summary"]["by_policy_decision"] == {"plan_required": 1}
    assert risk_query["summary"]["by_policy_risk_flag"] == {"planner_context_required": 1}
    assert risk_query["summary"]["by_policy_reason"] == {"risk flag requires planner context": 1}
    assert risk_query["cases"][0]["policy_risk_flags"] == ["planner_context_required"]
    assert reason_query["filters"]["policy_reason"] == ["*risk flag*"]
    assert reason_query["summary"]["matched_cases"] == 1
    assert reason_query["summary"]["by_policy_reason"] == {"risk flag requires planner context": 1}
    assert reason_query["cases"][0]["policy_reasons"] == ["risk flag requires planner context"]
    assert query["cases"] == [
        {
            "run_id": "run_a",
            "scenario_id": "learning_loop_case",
            "category": "learning_loop",
            "kind": "task",
            "requires": ["complex"],
            "status": "passed",
            "skip_reason": None,
            "failure_layer": "none",
            "diagnosis": "passed",
            "used_skills": ["generated.habit.demo"],
            "procedure_ids": ["proc_1"],
            "habit_ids": ["habit_1"],
            "lineage_skill_names": ["generated.habit.demo"],
            "promotion_generation_types": ["file"],
            "promotion_source_targets": ["builtin.file.basic"],
            "procedure_match_summaries": [
                "proc_1 matched mode=habit_fast_path_candidate confirm=false rejected=0"
            ],
            "learning_validation_statuses": [
                "habit",
                "approved",
                "recorded",
                "skill",
                "passed",
            ],
            "skill_validation_statuses": ["passed"],
            "learning_review_decisions": ["approved"],
            "learning_audit_events": ["promoted_to_habit"],
            "permission_summary": {"filesystem.write:granted": 1},
            "policy_decisions": ["direct_execute"],
            "skipped_stages": ["planner"],
            "policy_risk_flags": [],
            "policy_reasons": ["direct execute static gates passed"],
            "policy_context_budget_signals": [
                "recorded",
                "task_request:json_chars:1-999",
                "payload:json_chars:1-999",
                "skill_registry_entries:items:6-20",
                "skill_registry_entries:json_chars:50000+",
                "planner_skill_registry_entries:items:6-20",
                "planner_skill_registry_entries:json_chars:10000-49999",
                "agent_state_summary:items:6-20",
                "agent_state_summary:json_chars:1000-9999",
            ],
            "request_payload_stats_signals": [
                "recorded",
                "scope:task",
                "prompt:planner_v1",
                "total:json_chars:10000-49999",
                "memory_summaries:items:0",
                "memory_summaries:json_chars:1-999",
                "workflow_matches:items:1-5",
                "workflow_matches:json_chars:1000-9999",
                "skill_registry_entries:items:6-20",
                "skill_registry_entries:json_chars:10000-49999",
            ],
            "node_status_signals": [
                "recorded",
                "status:completed",
                "type:action",
                "skill:generated.habit.demo",
                "executor:file",
                "action:write",
                "attempts:1",
            ],
            "critic_action_signals": [
                "recorded",
                "action:accept",
                "target:task_1",
                "summary:present",
            ],
            "memory_retrieval_signals": [
                "vector_match",
                "recorded",
                "vector_memory_entry",
                "provider:local_model",
                "model:bge-small",
            ],
            "llm_call_count": 1,
            "llm_total_tokens": 42,
            "duration_seconds": 1.25,
            "llm_usage_signals": [
                "llm_called",
                "tokens:1-999",
                "duration:1-10s",
            ],
            "embedding_providers": ["local_model"],
            "embedding_models": ["bge-small"],
            "vector_match_count": 1,
            "vector_memory_entry_count": 1,
            "embedding_fallback_count": 0,
            "embedding_error_count": 0,
            "embedding_incompatible_match_count": 0,
            "learning_evidence_kinds": [
                "habit_fast_paths",
                "learning_corrections",
                "learning_feedbacks",
                "procedure_matches",
                "procedure_memory_records",
                "skill_lifecycle_records",
                "skill_lineages",
            ],
            "task_ids": ["task_1"],
            "thread_ids": [],
            "completion_failure_layers": ["none"],
            "model_capability_suspected": False,
            "learning_evidence_count": 7,
            "diagnostics_path": "/tmp/eval_runs/run_a/cases/learning_loop_case/diagnostics.md",
            "result_path": None,
        }
    ]

    markdown = evals.render_evidence_query_markdown(query)

    assert "# Eval Evidence Query" in markdown
    assert "- matched_cases: 1" in markdown
    assert "`generated.habit.demo`=1" in markdown
    assert "- permission: `filesystem.write:*`" in markdown
    assert "- completion_layer: `none`" in markdown
    assert "- policy_decision: `direct_execute`" in markdown
    assert "- skipped_stage: `planner`" in markdown
    assert "policy_risk_flag" in markdown
    assert "- policy_reason: `direct execute*`" in markdown
    assert "- policy_context_budget: `skill_registry_entries:json_chars:50000+`" in markdown
    assert "- request_payload_stats: `skill_registry_entries:json_chars:10000-49999`" in markdown
    assert "- node_status: `status:completed`" in markdown
    assert "- critic_action: `action:accept`" in markdown
    assert "- memory_retrieval: `vector_match`" in markdown
    assert "- llm_usage: `llm_called`" in markdown
    assert "- category: `learning_*`" in markdown
    assert "- requires: `complex`" in markdown
    assert "- learning_validation: `passed`" in markdown
    assert "- skill_validation: `passed`" in markdown
    assert "- learning_review_decision: `approved`" in markdown
    assert "- learning_audit_event: `promoted_to_habit`" in markdown
    assert "`habit_fast_paths`=1" in markdown
    assert "`file`=1" in markdown
    assert "`direct_execute`=1" in markdown
    assert "`planner`=1" in markdown
    assert "`direct execute static gates passed`=1" in markdown
    assert "`skill_registry_entries:json_chars:50000+`=1" in markdown
    assert "`skill_registry_entries:json_chars:10000-49999`=1" in markdown
    assert "`status:completed`=1" in markdown
    assert "`action:accept`=1" in markdown
    assert "`provider:local_model`=1" in markdown
    assert "`vector_match`=1" in markdown
    assert "`tokens:1-999`=1" in markdown
    assert "`passed`=1" in markdown
    assert "`approved`=1" in markdown
    assert "`promoted_to_habit`=1" in markdown
    assert "[diagnostics](/tmp/eval_runs/run_a/cases/learning_loop_case/diagnostics.md)" in markdown


def test_evidence_dashboard_scenario_rerun_command_adds_required_flags() -> None:
    assert evals._dashboard_scenario_run_command("plain_case", []) == (
        "poetry run python scripts/run_real_world_evals.py --scenario plain_case"
    )
    assert evals._dashboard_scenario_run_command(
        "network case",
        ["network"],
    ) == (
        "poetry run python scripts/run_real_world_evals.py "
        "--include-network --scenario 'network case'"
    )
    assert evals._dashboard_scenario_run_command(
        "l4_complex_case",
        ["l4", "complex"],
    ) == (
        "poetry run python scripts/run_real_world_evals.py "
        "--include-l4 --include-complex --scenario l4_complex_case"
    )


def test_evidence_dashboard_builds_common_runbook_views() -> None:
    index = {
        "schema_version": "eval-evidence-index/v1",
        "run_root": "/tmp/eval_runs",
        "run_count": 3,
        "cases": [
            {
                "run_id": "run_failed",
                "scenario_id": "config_case",
                "category": "data",
                "kind": "task",
                "status": "failed",
                "failure_layer": "skill_or_tool",
                "diagnosis": "skill_execution_failed",
                "duration_seconds": 12.5,
                "llm_call_count": 2,
                "llm_total_tokens": 1200,
                "used_skills": ["builtin.data.config"],
                "completion_failure_layers": ["skill_or_tool"],
                "embedding_summary": {
                    "providers": ["local_model"],
                    "models": ["bge-small"],
                    "vector_match_count": 1,
                    "vector_memory_entry_count": 0,
                    "fallback_count": 0,
                    "error_count": 0,
                    "incompatible_match_count": 0,
                },
                "permission_summary": {"filesystem.write:granted": 1},
                "node_statuses": [
                    {
                        "task_id": "task_config",
                        "node_id": "node_config_read",
                        "type": "action",
                        "status": "failed",
                        "executor_hint": "config",
                        "skill_name": "builtin.data.config",
                        "action": "read",
                        "attempts": 2,
                        "error_message": "missing required key",
                    }
                ],
                "critic_actions": [
                    {
                        "task_id": "task_config",
                        "target_id": "node_config_read",
                        "action": "retry",
                        "summary": "Retry after missing config key.",
                    }
                ],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "skill_lineages": [],
                },
                "artifacts": [
                    {
                        "kind": "diagnostics",
                        "path": "/tmp/eval_runs/run_failed/cases/config_case/diagnostics.md",
                        "exists": True,
                    }
                ],
            },
            {
                "run_id": "run_learning",
                "scenario_id": "learning_loop_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": ["generated.habit.demo"],
                "policy_decisions": [
                    {
                        "policy_decision": "direct_execute",
                        "reason": "direct execute static gates passed",
                        "skipped_stages": ["planner"],
                        "context_budget": {
                            "task_request": 592,
                            "payload": 177,
                            "skill_registry_entries": {
                                "items": 15,
                                "json_chars": 56119,
                            },
                            "planner_skill_registry_entries": {
                                "items": 6,
                                "json_chars": 23308,
                            },
                            "agent_state_summary": {
                                "items": 9,
                                "json_chars": 1441,
                            },
                        },
                    }
                ],
                "request_payload_stats": [
                    {
                        "scope": "task",
                        "task_id": "task_1",
                        "prompt_name": "planner_v1",
                        "json_chars": 28884,
                        "fields": {
                            "memory_summaries": {"items": 0, "json_chars": 2},
                            "workflow_matches": {"items": 2, "json_chars": 1248},
                            "skill_registry_entries": {
                                "items": 6,
                                "json_chars": 27511,
                            },
                        },
                    }
                ],
                "learning_evidence": {
                    "procedure_matches": [
                        {
                            "procedure_id": "proc_1",
                            "matched": True,
                            "suggestion_mode": "habit_fast_path_candidate",
                            "required_confirmation": False,
                            "rejected_candidate_count": 0,
                        }
                    ],
                    "habit_fast_paths": [{"habit_id": "habit_1"}],
                    "experience_records": [{"experience_id": "exp_1"}],
                    "learning_feedbacks": [
                        {
                            "task_id": "task_1",
                            "schema_version": "learning-feedback/v1",
                            "reuse_status": "habit_fast_path",
                            "review_target_type": "habit",
                        }
                    ],
                    "learning_corrections": [
                        {
                            "task_id": "task_1",
                            "schema_version": "learning-correction/v1",
                            "procedure_id": "proc_1",
                            "object_type": "procedure",
                            "trigger": "validator_failure",
                            "previous_status": "verified",
                            "new_status": "draft",
                            "new_review_status": "correction_required",
                            "failure_count": 1,
                            "frozen": True,
                        }
                    ],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "habit_1",
                            "object_type": "habit",
                            "status": "habit",
                            "previous_status": "habit",
                            "restored_status": "habit",
                            "review_status": "approved",
                            "review_count": 1,
                            "review_decisions": ["approved"],
                            "latest_review_decision": "approved",
                            "audit_event_count": 3,
                            "audit_events": ["promoted_to_habit", "disabled", "enabled"],
                            "latest_audit_event": "enabled",
                        }
                    ],
                    "skill_lineages": [
                        {
                            "skill_name": "generated.habit.demo",
                            "procedure_id": "proc_1",
                            "habit_id": "habit_1",
                            "generation_type": "file",
                            "source_targets": ["builtin.file.basic"],
                        }
                    ],
                    "skill_lifecycle_records": [
                        {
                            "skill_name": "generated.habit.demo",
                            "version": "1.0.0",
                            "source": "habit_promotion",
                            "lifecycle_status": "active",
                            "approval_statuses": ["approved"],
                            "validation_passed": [True],
                            "registration_actions": ["deprecate", "activate"],
                            "registration_statuses": ["deprecated", "active"],
                            "latest_registration_note": "restore skill",
                        }
                    ],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_model",
                "scenario_id": "reasoning_case",
                "category": "complex",
                "kind": "task",
                "requires": ["complex"],
                "status": "failed",
                "failure_layer": "model_capability",
                "diagnosis": "model_capability",
                "duration_seconds": 70.0,
                "llm_call_count": 1,
                "llm_total_tokens": 15000,
                "used_skills": ["builtin.file.basic"],
                "policy_decisions": [
                    {
                        "policy_decision": "plan_required",
                        "reason": "risk flag requires planner context",
                        "risk_flags": {"planner_context_required": True},
                    }
                ],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "skill_lineages": [],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_edit",
                "scenario_id": "learning_edit_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "proc_edited",
                            "object_type": "procedure",
                            "status": "draft",
                            "previous_status": "habit",
                            "review_status": "edited_requires_review",
                            "review_count": 1,
                            "review_decisions": ["needs_changes"],
                            "latest_review_decision": "needs_changes",
                            "audit_event_count": 2,
                            "audit_events": ["edited", "reviewed"],
                            "latest_audit_event": "edited",
                        }
                    ],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_rejection",
                "scenario_id": "learning_rejection_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "habit_rejected",
                            "object_type": "habit",
                            "status": "disabled",
                            "previous_status": "habit",
                            "review_status": "rejected",
                            "review_count": 1,
                            "review_decisions": ["rejected"],
                            "latest_review_decision": "rejected",
                            "audit_event_count": 1,
                            "audit_events": ["reviewed"],
                            "latest_audit_event": "reviewed",
                        }
                    ],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [
                        {
                            "skill_name": "generated.habit.rejected",
                            "version": "1.0.0",
                            "lifecycle_status": "validated",
                            "approval_statuses": ["rejected"],
                            "registration_actions": [],
                            "registration_statuses": [],
                        }
                    ],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_disable",
                "scenario_id": "learning_disable_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "habit_disabled",
                            "object_type": "habit",
                            "status": "disabled",
                            "previous_status": "habit",
                            "review_status": "disabled",
                            "review_count": 0,
                            "audit_event_count": 1,
                            "audit_events": ["disabled"],
                            "latest_audit_event": "disabled",
                        }
                    ],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [
                        {
                            "skill_name": "generated.habit.disabled",
                            "version": "1.0.0",
                            "lifecycle_status": "deprecated",
                            "approval_statuses": [],
                            "registration_actions": ["deprecate"],
                            "registration_statuses": ["deprecated"],
                            "latest_registration_note": "disabled after review",
                        }
                    ],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_rollback",
                "scenario_id": "learning_rollback_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": ["imported.learning_echo"],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [
                        {
                            "skill_name": "imported.learning_echo",
                            "version": "0.1.0",
                            "lifecycle_status": "active",
                            "approval_statuses": [],
                            "registration_actions": ["rollback"],
                            "registration_statuses": ["active"],
                        }
                    ],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_procedure_validation",
                "scenario_id": "procedure_learning_validation_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "proc_validated",
                            "object_type": "procedure",
                            "status": "verified",
                            "previous_status": "draft",
                            "review_status": "approved",
                            "review_count": 1,
                            "review_decisions": ["approved"],
                            "latest_review_decision": "approved",
                            "audit_event_count": 0,
                            "audit_events": [],
                        }
                    ],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_audit_gap",
                "scenario_id": "learning_audit_gap_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [{"procedure_id": "proc_gap"}],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [],
                    "skill_lineages": [
                        {
                            "skill_name": "generated.habit.audit_gap",
                            "procedure_id": "proc_gap",
                            "habit_id": "habit_gap",
                        }
                    ],
                    "skill_lifecycle_records": [],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_match_gate",
                "scenario_id": "learning_match_gate_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [
                        {
                            "procedure_id": "proc_review",
                            "matched": True,
                            "status": "verified",
                            "suggestion_mode": "suggest_only",
                            "required_confirmation": True,
                            "rejected_candidate_count": 1,
                        }
                    ],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_git_gate",
                "scenario_id": "git_skill_approval_gate",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": ["generated.habit.git_status"],
                "permission_summary": {"system.exec:granted": 1},
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "skill_lineages": [],
                },
                "artifacts": [],
            },
            {
                "run_id": "run_skipped",
                "scenario_id": "network_skip_case",
                "category": "network",
                "kind": "task",
                "requires": ["network"],
                "status": "skipped",
                "skip_reason": "requires --include-network",
                "failure_layer": "none",
                "diagnosis": "requires --include-network",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "skill_lineages": [],
                },
                "artifacts": [],
            },
        ],
    }

    dashboard = evals.build_evidence_dashboard(index, limit=5)

    assert dashboard["schema_version"] == "eval-evidence-dashboard/v1"
    assert dashboard["summary"]["total_index_cases"] == 12
    assert dashboard["summary"]["by_status"] == {"failed": 2, "passed": 9, "skipped": 1}
    assert dashboard["summary"]["by_scenario"] == {
        "config_case": 1,
        "git_skill_approval_gate": 1,
        "learning_audit_gap_case": 1,
        "learning_disable_case": 1,
        "learning_edit_case": 1,
        "learning_loop_case": 1,
        "learning_match_gate_case": 1,
        "learning_rejection_case": 1,
        "learning_rollback_case": 1,
        "network_skip_case": 1,
        "procedure_learning_validation_case": 1,
        "reasoning_case": 1,
    }
    assert dashboard["summary"]["by_category"] == {
        "complex": 1,
        "data": 1,
        "learning_loop": 9,
        "network": 1,
    }
    assert dashboard["summary"]["by_requires"] == {"complex": 1, "network": 1}
    assert dashboard["summary"]["by_skip_reason"] == {"requires --include-network": 1}
    assert dashboard["summary"]["by_skill"] == {
        "builtin.data.config": 1,
        "builtin.file.basic": 1,
        "generated.habit.demo": 1,
        "generated.habit.git_status": 1,
        "imported.learning_echo": 1,
    }
    assert dashboard["summary"]["by_procedure"] == {
        "proc_1": 1,
        "proc_edited": 1,
        "proc_gap": 1,
        "proc_review": 1,
        "proc_validated": 1,
    }
    assert dashboard["summary"]["by_habit"] == {
        "habit_1": 1,
        "habit_disabled": 1,
        "habit_gap": 1,
        "habit_rejected": 1,
    }
    assert dashboard["summary"]["by_lineage_skill"] == {
        "generated.habit.audit_gap": 1,
        "generated.habit.demo": 1,
        "generated.habit.disabled": 1,
        "generated.habit.rejected": 1,
        "imported.learning_echo": 1,
    }
    assert dashboard["summary"]["by_promotion_generation_type"] == {"file": 1}
    assert dashboard["summary"]["by_promotion_source_target"] == {"builtin.file.basic": 1}
    assert dashboard["summary"]["by_permission"] == {
        "filesystem.write:granted": 1,
        "system.exec:granted": 1,
    }
    assert dashboard["summary"]["by_completion_layer"] == {"skill_or_tool": 1}
    assert dashboard["summary"]["by_policy_decision"] == {
        "direct_execute": 1,
        "plan_required": 1,
    }
    assert dashboard["summary"]["by_skipped_stage"] == {"planner": 1}
    assert dashboard["summary"]["by_policy_risk_flag"] == {"planner_context_required": 1}
    assert dashboard["summary"]["by_policy_reason"] == {
        "direct execute static gates passed": 1,
        "risk flag requires planner context": 1,
    }
    assert dashboard["summary"]["by_policy_context_budget"] == {
        "agent_state_summary:items:6-20": 1,
        "agent_state_summary:json_chars:1000-9999": 1,
        "payload:json_chars:1-999": 1,
        "planner_skill_registry_entries:items:6-20": 1,
        "planner_skill_registry_entries:json_chars:10000-49999": 1,
        "recorded": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:50000+": 1,
        "task_request:json_chars:1-999": 1,
    }
    assert dashboard["summary"]["by_request_payload_stats"] == {
        "memory_summaries:items:0": 1,
        "memory_summaries:json_chars:1-999": 1,
        "prompt:planner_v1": 1,
        "recorded": 1,
        "scope:task": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:10000-49999": 1,
        "total:json_chars:10000-49999": 1,
        "workflow_matches:items:1-5": 1,
        "workflow_matches:json_chars:1000-9999": 1,
    }
    assert dashboard["summary"]["by_node_status"] == {
        "action:read": 1,
        "attempts:2": 1,
        "error:present": 1,
        "executor:config": 1,
        "recorded": 1,
        "skill:builtin.data.config": 1,
        "status:failed": 1,
        "type:action": 1,
    }
    assert dashboard["summary"]["by_critic_action"] == {
        "action:retry": 1,
        "recorded": 1,
        "summary:present": 1,
        "target:node_config_read": 1,
    }
    assert dashboard["summary"]["by_memory_retrieval"] == {
        "model:bge-small": 1,
        "provider:local_model": 1,
        "recorded": 1,
        "vector_match": 1,
    }
    assert dashboard["summary"]["by_llm_usage"] == {
        "duration:10-60s": 1,
        "duration:60s+": 1,
        "llm_called": 2,
        "llm_unknown": 10,
        "tokens:1000-9999": 1,
        "tokens:10000+": 1,
    }
    assert dashboard["summary"]["by_learning_kind"] == {
        "experience_records": 1,
        "habit_fast_paths": 1,
        "learning_corrections": 1,
        "learning_feedbacks": 1,
        "procedure_matches": 3,
        "procedure_memory_records": 5,
        "skill_lifecycle_records": 4,
        "skill_lineages": 2,
    }
    assert dashboard["summary"]["by_learning_validation"] == {
        "approved": 2,
        "habit": 1,
        "passed": 1,
        "procedure": 1,
        "recorded": 2,
        "skill": 1,
    }
    assert dashboard["summary"]["by_skill_validation"] == {"passed": 1}
    assert dashboard["summary"]["by_learning_review_decision"] == {
        "approved": 2,
        "needs_changes": 1,
        "rejected": 1,
    }
    assert dashboard["summary"]["by_learning_audit_event"] == {
        "disabled": 2,
        "edited": 1,
        "enabled": 1,
        "promoted_to_habit": 1,
        "reviewed": 2,
    }
    assert dashboard["summary"]["repair_entrypoint_count"] == 2
    assert dashboard["summary"]["regression_command_count"] == len(dashboard["regression_commands"])
    command_values = [item["command"] for item in dashboard["regression_commands"]]
    assert len(command_values) == len(set(command_values))
    assert "poetry run python scripts/run_real_world_evals.py --check-only" in command_values
    assert (
        "poetry run python scripts/run_real_world_evals.py --build-evidence-index" in command_values
    )
    assert "poetry run python scripts/run_real_world_evals.py --evidence-dashboard" in (
        command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --scenario config_case" in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --include-complex --scenario reasoning_case"
        in command_values
    )
    assert sorted(
        (item for item in dashboard["regression_commands"] if item.get("kind") == "scenario_rerun"),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "scenario_rerun",
            "label": "Rerun scenario config_case",
            "command": ("poetry run python scripts/run_real_world_evals.py --scenario config_case"),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_rerun",
            "label": "Rerun scenario reasoning_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--include-complex --scenario reasoning_case"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-status failed"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage llm_called"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage tokens:10000+,duration:60s+"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-status passed"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-category learning_loop"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-requires network"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skip-reason 'requires --include-network'"
        in command_values
    )
    assert [
        item for item in dashboard["regression_commands"] if item.get("kind") == "status_query"
    ] == [
        {
            "kind": "status_query",
            "label": "Status passed",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-status passed"
            ),
            "matched_cases": 9,
        }
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-scenario learning_loop_case"
        in command_values
    )
    assert sorted(
        (item for item in dashboard["regression_commands"] if item.get("kind") == "scenario_query"),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "scenario_query",
            "label": "Scenario config_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario config_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario git_skill_approval_gate",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario git_skill_approval_gate"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_audit_gap_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_audit_gap_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_disable_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_disable_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_edit_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_edit_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_loop_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_loop_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_match_gate_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_match_gate_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_rejection_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_rejection_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario learning_rollback_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario learning_rollback_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario network_skip_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario network_skip_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario procedure_learning_validation_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario procedure_learning_validation_case"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "scenario_query",
            "label": "Scenario reasoning_case",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-scenario reasoning_case"
            ),
            "matched_cases": 1,
        },
    ]
    assert sorted(
        (item for item in dashboard["regression_commands"] if item.get("kind") == "category_query"),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "category_query",
            "label": "Category complex",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-category complex"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "category_query",
            "label": "Category data",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-category data"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "category_query",
            "label": "Category learning_loop",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-category learning_loop"
            ),
            "matched_cases": 9,
        },
        {
            "kind": "category_query",
            "label": "Category network",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-category network"
            ),
            "matched_cases": 1,
        },
    ]
    assert sorted(
        (item for item in dashboard["regression_commands"] if item.get("kind") == "requires_query"),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "requires_query",
            "label": "Requires complex",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-requires complex"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "requires_query",
            "label": "Requires network",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-requires network"
            ),
            "matched_cases": 1,
        },
    ]
    assert [
        item for item in dashboard["regression_commands"] if item.get("kind") == "skip_reason_query"
    ] == [
        {
            "kind": "skip_reason_query",
            "label": "Skip reason requires --include-network",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skip-reason 'requires --include-network'"
            ),
            "matched_cases": 1,
        }
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skill builtin.data.config"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skill generated.habit.demo"
        in command_values
    )
    assert sorted(
        (item for item in dashboard["regression_commands"] if item.get("kind") == "skill_query"),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "skill_query",
            "label": "Skill builtin.data.config",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill builtin.data.config"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "skill_query",
            "label": "Skill builtin.file.basic",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill builtin.file.basic"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "skill_query",
            "label": "Skill generated.habit.demo",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill generated.habit.demo"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "skill_query",
            "label": "Skill generated.habit.git_status",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill generated.habit.git_status"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "skill_query",
            "label": "Skill imported.learning_echo",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill imported.learning_echo"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-procedure proc_1"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-habit habit_1"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "procedure_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "procedure_query",
            "label": "Procedure proc_1",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-procedure proc_1"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "procedure_query",
            "label": "Procedure proc_edited",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-procedure proc_edited"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "procedure_query",
            "label": "Procedure proc_gap",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-procedure proc_gap"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "procedure_query",
            "label": "Procedure proc_review",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-procedure proc_review"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "procedure_query",
            "label": "Procedure proc_validated",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-procedure proc_validated"
            ),
            "matched_cases": 1,
        },
    ]
    assert sorted(
        (item for item in dashboard["regression_commands"] if item.get("kind") == "habit_query"),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "habit_query",
            "label": "Habit habit_1",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-habit habit_1"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "habit_query",
            "label": "Habit habit_disabled",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-habit habit_disabled"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "habit_query",
            "label": "Habit habit_gap",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-habit habit_gap"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "habit_query",
            "label": "Habit habit_rejected",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-habit habit_rejected"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-lineage-skill generated.habit.demo"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "lineage_skill_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "lineage_skill_query",
            "label": "Lineage skill generated.habit.audit_gap",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-lineage-skill generated.habit.audit_gap"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "lineage_skill_query",
            "label": "Lineage skill generated.habit.demo",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-lineage-skill generated.habit.demo"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "lineage_skill_query",
            "label": "Lineage skill generated.habit.disabled",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-lineage-skill generated.habit.disabled"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "lineage_skill_query",
            "label": "Lineage skill generated.habit.rejected",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-lineage-skill generated.habit.rejected"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "lineage_skill_query",
            "label": "Lineage skill imported.learning_echo",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-lineage-skill imported.learning_echo"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-promotion-source-target builtin.file.basic"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-promotion-generation-type file"
        in command_values
    )
    assert [
        item
        for item in dashboard["regression_commands"]
        if item.get("kind") == "promotion_generation_type_query"
    ] == [
        {
            "kind": "promotion_generation_type_query",
            "label": "Promotion generation type file",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-promotion-generation-type file"
            ),
            "matched_cases": 1,
        }
    ]
    assert (
        'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-permission "*"'
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-permission filesystem.write:granted"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-permission system.exec:granted"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "permission_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "permission_query",
            "label": "Permission filesystem.write:granted",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-permission filesystem.write:granted"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "permission_query",
            "label": "Permission system.exec:granted",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-permission system.exec:granted"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-completion-layer skill_or_tool"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-completion-layer none"
        not in command_values
    )
    assert [
        item
        for item in dashboard["regression_commands"]
        if item.get("kind") == "completion_layer_query"
    ] == [
        {
            "kind": "completion_layer_query",
            "label": "Completion layer skill_or_tool",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-completion-layer skill_or_tool"
            ),
            "matched_cases": 1,
        }
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-decision direct_execute"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skipped-stage planner"
        in command_values
    )
    assert (
        'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-risk-flag "*"'
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-risk-flag planner_context_required"
        in command_values
    )
    assert (
        'poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-reason "*"'
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-context-budget recorded"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-request-payload-stats recorded"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-reason 'direct execute static gates passed'"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-context-budget skill_registry_entries:json_chars:50000+"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-request-payload-stats skill_registry_entries:json_chars:10000-49999"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "policy_reason_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "policy_reason_query",
            "label": "Policy reason direct execute static gates passed",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-policy-reason "
                "'direct execute static gates passed'"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "policy_reason_query",
            "label": "Policy reason risk flag requires planner context",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-policy-reason "
                "'risk flag requires planner context'"
            ),
            "matched_cases": 1,
        },
    ]
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "policy_context_budget_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "policy_context_budget_query",
            "label": (
                "Policy context budget planner_skill_registry_entries:json_chars:10000-49999"
            ),
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-policy-context-budget "
                "planner_skill_registry_entries:json_chars:10000-49999"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "policy_context_budget_query",
            "label": ("Policy context budget skill_registry_entries:json_chars:50000+"),
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-policy-context-budget "
                "skill_registry_entries:json_chars:50000+"
            ),
            "matched_cases": 1,
        },
    ]
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "request_payload_stats_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "request_payload_stats_query",
            "label": ("Request payload stats skill_registry_entries:json_chars:10000-49999"),
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-request-payload-stats "
                "skill_registry_entries:json_chars:10000-49999"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "request_payload_stats_query",
            "label": "Request payload stats total:json_chars:10000-49999",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-request-payload-stats "
                "total:json_chars:10000-49999"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-request-payload-stats recorded"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status recorded"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status status:failed"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status attempts:2"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status error:present"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status skill:builtin.data.config"
        not in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "node_status_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "node_status_query",
            "label": "Node status attempts:2",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-node-status attempts:2"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "node_status_query",
            "label": "Node status error:present",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-node-status error:present"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "node_status_query",
            "label": "Node status status:failed",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-node-status status:failed"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-critic-action recorded"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-critic-action action:retry"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-critic-action target:node_config_read"
        not in command_values
    )
    assert [
        item
        for item in dashboard["regression_commands"]
        if item.get("kind") == "critic_action_query"
    ] == [
        {
            "kind": "critic_action_query",
            "label": "Critic action action:retry",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-critic-action action:retry"
            ),
            "matched_cases": 1,
        }
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-memory-retrieval vector_match"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-memory-retrieval provider:local_model"
        not in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-memory-retrieval model:bge-small"
        not in command_values
    )
    assert [
        item
        for item in dashboard["regression_commands"]
        if item.get("kind") == "memory_retrieval_query"
    ] == [
        {
            "kind": "memory_retrieval_query",
            "label": "Memory retrieval vector_match",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-memory-retrieval vector_match"
            ),
            "matched_cases": 1,
        }
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage tokens:10000+"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage duration:60s+"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage llm_unknown"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "llm_usage_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "llm_usage_query",
            "label": "LLM usage duration:10-60s",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-llm-usage duration:10-60s"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "llm_usage_query",
            "label": "LLM usage duration:60s+",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-llm-usage duration:60s+"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "llm_usage_query",
            "label": "LLM usage llm_unknown",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-llm-usage llm_unknown"
            ),
            "matched_cases": 10,
        },
        {
            "kind": "llm_usage_query",
            "label": "LLM usage tokens:1000-9999",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-llm-usage tokens:1000-9999"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "llm_usage_query",
            "label": "LLM usage tokens:10000+",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-llm-usage tokens:10000+"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind procedure_memory_records"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind skill_lineages"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "learning_kind_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "learning_kind_query",
            "label": "Learning evidence kind procedure_memory_records",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-kind procedure_memory_records"
            ),
            "matched_cases": 5,
        },
        {
            "kind": "learning_kind_query",
            "label": "Learning evidence kind skill_lineages",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-kind skill_lineages"
            ),
            "matched_cases": 2,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-validation approved"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-validation passed"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "learning_validation_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "learning_validation_query",
            "label": "Learning validation approved",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-validation approved"
            ),
            "matched_cases": 2,
        },
        {
            "kind": "learning_validation_query",
            "label": "Learning validation habit",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-validation habit"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "learning_validation_query",
            "label": "Learning validation passed",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-validation passed"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "learning_validation_query",
            "label": "Learning validation procedure",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-validation procedure"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "learning_validation_query",
            "label": "Learning validation recorded",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-validation recorded"
            ),
            "matched_cases": 2,
        },
        {
            "kind": "learning_validation_query",
            "label": "Learning validation skill",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-validation skill"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skill-validation passed"
        in command_values
    )
    assert [
        item
        for item in dashboard["regression_commands"]
        if item.get("kind") == "skill_validation_query"
    ] == [
        {
            "kind": "skill_validation_query",
            "label": "Skill validation passed",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-skill-validation passed"
            ),
            "matched_cases": 1,
        }
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-review-decision approved"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-review-decision needs_changes"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "learning_review_decision_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "learning_review_decision_query",
            "label": "Learning review decision approved",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-review-decision approved"
            ),
            "matched_cases": 2,
        },
        {
            "kind": "learning_review_decision_query",
            "label": "Learning review decision rejected",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-review-decision rejected"
            ),
            "matched_cases": 1,
        },
    ]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-audit-event edited"
        in command_values
    )
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-audit-event disabled"
        in command_values
    )
    assert sorted(
        (
            item
            for item in dashboard["regression_commands"]
            if item.get("kind") == "learning_audit_event_query"
        ),
        key=lambda item: item["label"],
    ) == [
        {
            "kind": "learning_audit_event_query",
            "label": "Learning audit event disabled",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-audit-event disabled"
            ),
            "matched_cases": 2,
        },
        {
            "kind": "learning_audit_event_query",
            "label": "Learning audit event enabled",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-audit-event enabled"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "learning_audit_event_query",
            "label": "Learning audit event promoted_to_habit",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-audit-event promoted_to_habit"
            ),
            "matched_cases": 1,
        },
        {
            "kind": "learning_audit_event_query",
            "label": "Learning audit event reviewed",
            "command": (
                "poetry run python scripts/run_real_world_evals.py "
                "--query-evidence-index --evidence-learning-audit-event reviewed"
            ),
            "matched_cases": 2,
        },
    ]
    repair_layers = {item["failure_layer"] for item in dashboard["repair_entrypoints"]}
    assert repair_layers == {"model_capability", "skill_or_tool"}
    coverage_query = dashboard["views"]["eval_coverage_cases"]["query"]
    assert coverage_query["summary"]["matched_cases"] == 12
    assert coverage_query["summary"]["by_status"] == {
        "failed": 2,
        "passed": 9,
        "skipped": 1,
    }
    assert coverage_query["summary"]["by_requires"] == {"complex": 1, "network": 1}
    assert coverage_query["summary"]["by_skip_reason"] == {"requires --include-network": 1}
    skipped_query = dashboard["views"]["skipped_eval_cases"]["query"]
    assert skipped_query["summary"]["matched_cases"] == 1
    assert skipped_query["cases"][0]["scenario_id"] == "network_skip_case"
    assert skipped_query["cases"][0]["requires"] == ["network"]
    assert skipped_query["cases"][0]["skip_reason"] == "requires --include-network"
    assert dashboard["views"]["failed_cases"]["query"]["summary"]["matched_cases"] == 2
    assert (
        dashboard["views"]["skill_or_tool_failures"]["query"]["cases"][0]["scenario_id"]
        == "config_case"
    )
    node_query = dashboard["views"]["node_execution_cases"]["query"]
    assert node_query["filters"]["node_status"] == ["recorded"]
    assert node_query["summary"]["matched_cases"] == 1
    assert node_query["summary"]["by_node_status"] == {
        "action:read": 1,
        "attempts:2": 1,
        "error:present": 1,
        "executor:config": 1,
        "recorded": 1,
        "skill:builtin.data.config": 1,
        "status:failed": 1,
        "type:action": 1,
    }
    assert node_query["cases"][0]["scenario_id"] == "config_case"
    assert node_query["cases"][0]["node_status_signals"] == [
        "recorded",
        "status:failed",
        "type:action",
        "skill:builtin.data.config",
        "executor:config",
        "action:read",
        "attempts:2",
        "error:present",
    ]
    critic_query = dashboard["views"]["critic_action_cases"]["query"]
    assert critic_query["filters"]["critic_action"] == ["recorded"]
    assert critic_query["summary"]["matched_cases"] == 1
    assert critic_query["summary"]["by_critic_action"] == {
        "action:retry": 1,
        "recorded": 1,
        "summary:present": 1,
        "target:node_config_read": 1,
    }
    assert critic_query["cases"][0]["scenario_id"] == "config_case"
    assert critic_query["cases"][0]["critic_action_signals"] == [
        "recorded",
        "action:retry",
        "target:node_config_read",
        "summary:present",
    ]
    model_query = dashboard["views"]["model_capability_cases"]["query"]
    assert model_query["summary"]["matched_cases"] == 1
    assert model_query["cases"][0]["scenario_id"] == "reasoning_case"
    assert model_query["cases"][0]["failure_layer"] == "model_capability"
    completion_query = dashboard["views"]["completion_evaluation_cases"]["query"]
    assert completion_query["summary"]["matched_cases"] == 1
    assert completion_query["cases"][0]["scenario_id"] == "config_case"
    assert completion_query["cases"][0]["completion_failure_layers"] == ["skill_or_tool"]
    memory_query = dashboard["views"]["memory_retrieval_cases"]["query"]
    assert memory_query["summary"]["matched_cases"] == 1
    assert memory_query["cases"][0]["scenario_id"] == "config_case"
    assert memory_query["cases"][0]["memory_retrieval_signals"] == [
        "vector_match",
        "recorded",
        "provider:local_model",
        "model:bge-small",
    ]
    llm_query = dashboard["views"]["llm_usage_cases"]["query"]
    assert llm_query["summary"]["matched_cases"] == 2
    assert llm_query["summary"]["by_llm_usage"] == {
        "duration:10-60s": 1,
        "duration:60s+": 1,
        "llm_called": 2,
        "tokens:1000-9999": 1,
        "tokens:10000+": 1,
    }
    assert llm_query["cases"][0]["scenario_id"] == "config_case"
    assert llm_query["cases"][0]["llm_call_count"] == 2
    assert llm_query["cases"][0]["llm_total_tokens"] == 1200
    assert llm_query["cases"][0]["llm_usage_signals"] == [
        "llm_called",
        "tokens:1000-9999",
        "duration:10-60s",
    ]
    hotspot_query = dashboard["views"]["llm_cost_hotspot_cases"]["query"]
    assert hotspot_query["filters"]["llm_usage"] == ["tokens:10000+", "duration:60s+"]
    assert hotspot_query["summary"]["matched_cases"] == 1
    assert hotspot_query["summary"]["by_llm_usage"] == {
        "duration:60s+": 1,
        "llm_called": 1,
        "tokens:10000+": 1,
    }
    assert hotspot_query["cases"][0]["scenario_id"] == "reasoning_case"
    assert hotspot_query["cases"][0]["llm_total_tokens"] == 15000
    assert hotspot_query["cases"][0]["llm_usage_signals"] == [
        "llm_called",
        "tokens:10000+",
        "duration:60s+",
    ]
    policy_query = dashboard["views"]["execution_policy_cases"]["query"]
    assert policy_query["filters"]["policy_decision"] == ["direct_execute", "plan_required"]
    assert policy_query["summary"]["matched_cases"] == 2
    assert policy_query["summary"]["by_policy_decision"] == {
        "direct_execute": 1,
        "plan_required": 1,
    }
    assert policy_query["summary"]["by_skipped_stage"] == {"planner": 1}
    assert policy_query["summary"]["by_policy_risk_flag"] == {"planner_context_required": 1}
    assert policy_query["summary"]["by_policy_reason"] == {
        "direct execute static gates passed": 1,
        "risk flag requires planner context": 1,
    }
    assert policy_query["summary"]["by_policy_context_budget"] == {
        "agent_state_summary:items:6-20": 1,
        "agent_state_summary:json_chars:1000-9999": 1,
        "payload:json_chars:1-999": 1,
        "planner_skill_registry_entries:items:6-20": 1,
        "planner_skill_registry_entries:json_chars:10000-49999": 1,
        "recorded": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:50000+": 1,
        "task_request:json_chars:1-999": 1,
    }
    assert policy_query["summary"]["by_request_payload_stats"] == {
        "memory_summaries:items:0": 1,
        "memory_summaries:json_chars:1-999": 1,
        "prompt:planner_v1": 1,
        "recorded": 1,
        "scope:task": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:10000-49999": 1,
        "total:json_chars:10000-49999": 1,
        "workflow_matches:items:1-5": 1,
        "workflow_matches:json_chars:1000-9999": 1,
    }
    assert policy_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert policy_query["cases"][0]["policy_decisions"] == ["direct_execute"]
    assert policy_query["cases"][0]["skipped_stages"] == ["planner"]
    assert policy_query["cases"][0]["policy_risk_flags"] == []
    assert policy_query["cases"][0]["policy_reasons"] == ["direct execute static gates passed"]
    assert policy_query["cases"][0]["policy_context_budget_signals"] == [
        "recorded",
        "task_request:json_chars:1-999",
        "payload:json_chars:1-999",
        "skill_registry_entries:items:6-20",
        "skill_registry_entries:json_chars:50000+",
        "planner_skill_registry_entries:items:6-20",
        "planner_skill_registry_entries:json_chars:10000-49999",
        "agent_state_summary:items:6-20",
        "agent_state_summary:json_chars:1000-9999",
    ]
    assert policy_query["cases"][0]["request_payload_stats_signals"] == [
        "recorded",
        "scope:task",
        "prompt:planner_v1",
        "total:json_chars:10000-49999",
        "memory_summaries:items:0",
        "memory_summaries:json_chars:1-999",
        "workflow_matches:items:1-5",
        "workflow_matches:json_chars:1000-9999",
        "skill_registry_entries:items:6-20",
        "skill_registry_entries:json_chars:10000-49999",
    ]
    assert policy_query["cases"][1]["scenario_id"] == "reasoning_case"
    assert policy_query["cases"][1]["policy_decisions"] == ["plan_required"]
    assert policy_query["cases"][1]["policy_risk_flags"] == ["planner_context_required"]
    assert policy_query["cases"][1]["policy_reasons"] == ["risk flag requires planner context"]
    skipped_query = dashboard["views"]["planner_skipped_cases"]["query"]
    assert skipped_query["filters"]["skipped_stage"] == ["planner"]
    assert skipped_query["summary"]["matched_cases"] == 1
    assert skipped_query["summary"]["by_skipped_stage"] == {"planner": 1}
    assert skipped_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert skipped_query["cases"][0]["skipped_stages"] == ["planner"]
    risk_query = dashboard["views"]["policy_risk_flag_cases"]["query"]
    assert risk_query["filters"]["policy_risk_flag"] == ["*"]
    assert risk_query["summary"]["matched_cases"] == 1
    assert risk_query["summary"]["by_policy_risk_flag"] == {"planner_context_required": 1}
    assert risk_query["cases"][0]["scenario_id"] == "reasoning_case"
    assert risk_query["cases"][0]["policy_risk_flags"] == ["planner_context_required"]
    reason_query = dashboard["views"]["policy_reason_cases"]["query"]
    assert reason_query["filters"]["policy_reason"] == ["*"]
    assert reason_query["summary"]["matched_cases"] == 2
    assert reason_query["summary"]["by_policy_reason"] == {
        "direct execute static gates passed": 1,
        "risk flag requires planner context": 1,
    }
    assert reason_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert reason_query["cases"][0]["policy_reasons"] == ["direct execute static gates passed"]
    assert reason_query["cases"][1]["scenario_id"] == "reasoning_case"
    assert reason_query["cases"][1]["policy_reasons"] == ["risk flag requires planner context"]
    budget_query = dashboard["views"]["policy_context_budget_cases"]["query"]
    assert budget_query["filters"]["policy_context_budget"] == ["recorded"]
    assert budget_query["summary"]["matched_cases"] == 1
    assert budget_query["summary"]["by_policy_context_budget"] == {
        "agent_state_summary:items:6-20": 1,
        "agent_state_summary:json_chars:1000-9999": 1,
        "payload:json_chars:1-999": 1,
        "planner_skill_registry_entries:items:6-20": 1,
        "planner_skill_registry_entries:json_chars:10000-49999": 1,
        "recorded": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:50000+": 1,
        "task_request:json_chars:1-999": 1,
    }
    assert budget_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert budget_query["cases"][0]["policy_context_budget_signals"] == [
        "recorded",
        "task_request:json_chars:1-999",
        "payload:json_chars:1-999",
        "skill_registry_entries:items:6-20",
        "skill_registry_entries:json_chars:50000+",
        "planner_skill_registry_entries:items:6-20",
        "planner_skill_registry_entries:json_chars:10000-49999",
        "agent_state_summary:items:6-20",
        "agent_state_summary:json_chars:1000-9999",
    ]
    payload_stats_query = dashboard["views"]["request_payload_stats_cases"]["query"]
    assert payload_stats_query["filters"]["request_payload_stats"] == ["recorded"]
    assert payload_stats_query["summary"]["matched_cases"] == 1
    assert payload_stats_query["summary"]["by_request_payload_stats"] == {
        "memory_summaries:items:0": 1,
        "memory_summaries:json_chars:1-999": 1,
        "prompt:planner_v1": 1,
        "recorded": 1,
        "scope:task": 1,
        "skill_registry_entries:items:6-20": 1,
        "skill_registry_entries:json_chars:10000-49999": 1,
        "total:json_chars:10000-49999": 1,
        "workflow_matches:items:1-5": 1,
        "workflow_matches:json_chars:1000-9999": 1,
    }
    assert payload_stats_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert payload_stats_query["cases"][0]["request_payload_stats_signals"] == [
        "recorded",
        "scope:task",
        "prompt:planner_v1",
        "total:json_chars:10000-49999",
        "memory_summaries:items:0",
        "memory_summaries:json_chars:1-999",
        "workflow_matches:items:1-5",
        "workflow_matches:json_chars:1000-9999",
        "skill_registry_entries:items:6-20",
        "skill_registry_entries:json_chars:10000-49999",
    ]
    assert (
        dashboard["views"]["generated_habit_skill_usage"]["query"]["cases"][0]["scenario_id"]
        == "learning_loop_case"
    )
    coverage_query = dashboard["views"]["skill_promotion_coverage_cases"]["query"]
    assert coverage_query["summary"]["matched_cases"] == 1
    assert coverage_query["summary"]["by_promotion_generation_type"] == {"file": 1}
    assert coverage_query["summary"]["by_promotion_source_target"] == {"builtin.file.basic": 1}
    assert coverage_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert coverage_query["cases"][0]["promotion_generation_types"] == ["file"]
    assert coverage_query["cases"][0]["promotion_source_targets"] == ["builtin.file.basic"]
    lineage_query = dashboard["views"]["learning_lineage_cases"]["query"]
    assert lineage_query["summary"]["matched_cases"] == 8
    assert lineage_query["cases"][0]["procedure_ids"] == ["proc_1"]
    permission_query = dashboard["views"]["permission_governance_cases"]["query"]
    assert permission_query["summary"]["matched_cases"] == 2
    assert permission_query["cases"][0]["scenario_id"] == "config_case"
    approval_gate_query = dashboard["views"]["skill_approval_gate_cases"]["query"]
    assert approval_gate_query["summary"]["matched_cases"] == 1
    assert approval_gate_query["cases"][0]["scenario_id"] == "git_skill_approval_gate"
    assert approval_gate_query["cases"][0]["permission_summary"] == {"system.exec:granted": 1}
    habit_query = dashboard["views"]["habit_fast_path_cases"]["query"]
    assert habit_query["summary"]["matched_cases"] == 1
    assert habit_query["cases"][0]["habit_ids"] == ["habit_1"]
    match_query = dashboard["views"]["learning_match_explanation_cases"]["query"]
    assert match_query["summary"]["matched_cases"] == 3
    assert match_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert match_query["cases"][0]["procedure_match_summaries"] == [
        "proc_1 matched mode=habit_fast_path_candidate confirm=false rejected=0"
    ]
    assert match_query["cases"][1]["scenario_id"] == "learning_audit_gap_case"
    assert match_query["cases"][1]["procedure_match_summaries"] == [
        "proc_gap unknown mode=- confirm=false rejected=0"
    ]
    assert match_query["cases"][2]["scenario_id"] == "learning_match_gate_case"
    assert match_query["cases"][2]["procedure_ids"] == ["proc_review"]
    assert match_query["cases"][2]["procedure_match_summaries"] == [
        "proc_review matched mode=suggest_only confirm=true rejected=1"
    ]
    learning_gate_query = dashboard["views"]["learning_gate_cases"]["query"]
    assert learning_gate_query["summary"]["matched_cases"] == 1
    assert learning_gate_query["cases"][0]["learning_evidence_kinds"] == [
        "experience_records",
        "habit_fast_paths",
        "learning_corrections",
        "learning_feedbacks",
        "procedure_matches",
        "procedure_memory_records",
        "skill_lifecycle_records",
        "skill_lineages",
    ]
    feedback_query = dashboard["views"]["learning_feedback_cases"]["query"]
    assert feedback_query["summary"]["matched_cases"] == 1
    assert feedback_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert "learning_feedbacks" in feedback_query["cases"][0]["learning_evidence_kinds"]
    correction_query = dashboard["views"]["learning_correction_cases"]["query"]
    assert correction_query["summary"]["matched_cases"] == 1
    assert correction_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert "learning_corrections" in correction_query["cases"][0]["learning_evidence_kinds"]
    governance_query = dashboard["views"]["learning_governance_cases"]["query"]
    assert governance_query["summary"]["matched_cases"] == 5
    assert governance_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert governance_query["cases"][0]["habit_ids"] == ["habit_1"]
    assert governance_query["cases"][0]["lineage_skill_names"] == ["generated.habit.demo"]
    assert governance_query["cases"][0]["learning_evidence_kinds"] == [
        "experience_records",
        "habit_fast_paths",
        "learning_corrections",
        "learning_feedbacks",
        "procedure_matches",
        "procedure_memory_records",
        "skill_lifecycle_records",
        "skill_lineages",
    ]
    history_query = dashboard["views"]["learning_history_cases"]["query"]
    assert history_query["summary"]["matched_cases"] == 6
    assert history_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert history_query["cases"][0]["learning_evidence_kinds"] == [
        "experience_records",
        "habit_fast_paths",
        "learning_corrections",
        "learning_feedbacks",
        "procedure_matches",
        "procedure_memory_records",
        "skill_lifecycle_records",
        "skill_lineages",
    ]
    assert history_query["cases"][-1]["scenario_id"] == "learning_rollback_case"
    assert history_query["cases"][-1]["learning_evidence_kinds"] == ["skill_lifecycle_records"]
    approval_query = dashboard["views"]["learning_approval_cases"]["query"]
    assert approval_query["summary"]["matched_cases"] == 2
    assert approval_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert approval_query["cases"][0]["habit_ids"] == ["habit_1"]
    assert approval_query["cases"][0]["lineage_skill_names"] == ["generated.habit.demo"]
    assert approval_query["cases"][0]["learning_evidence_kinds"] == [
        "experience_records",
        "habit_fast_paths",
        "learning_corrections",
        "learning_feedbacks",
        "procedure_matches",
        "procedure_memory_records",
        "skill_lifecycle_records",
        "skill_lineages",
    ]
    promotion_query = dashboard["views"]["learning_promotion_cases"]["query"]
    assert promotion_query["summary"]["matched_cases"] == 1
    assert promotion_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert promotion_query["cases"][0]["habit_ids"] == ["habit_1"]
    assert promotion_query["cases"][0]["lineage_skill_names"] == ["generated.habit.demo"]
    assert promotion_query["cases"][0]["learning_evidence_kinds"] == [
        "experience_records",
        "habit_fast_paths",
        "learning_corrections",
        "learning_feedbacks",
        "procedure_matches",
        "procedure_memory_records",
        "skill_lifecycle_records",
        "skill_lineages",
    ]
    validation_query = dashboard["views"]["learning_validation_cases"]["query"]
    assert validation_query["summary"]["matched_cases"] == 2
    assert validation_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert validation_query["cases"][0]["lineage_skill_names"] == ["generated.habit.demo"]
    assert validation_query["cases"][0]["learning_evidence_kinds"] == [
        "experience_records",
        "habit_fast_paths",
        "learning_corrections",
        "learning_feedbacks",
        "procedure_matches",
        "procedure_memory_records",
        "skill_lifecycle_records",
        "skill_lineages",
    ]
    assert validation_query["cases"][1]["scenario_id"] == ("procedure_learning_validation_case")
    assert validation_query["cases"][1]["procedure_ids"] == ["proc_validated"]
    assert validation_query["cases"][1]["learning_evidence_kinds"] == ["procedure_memory_records"]
    edit_query = dashboard["views"]["learning_edit_cases"]["query"]
    assert edit_query["summary"]["matched_cases"] == 1
    assert edit_query["cases"][0]["scenario_id"] == "learning_edit_case"
    assert edit_query["cases"][0]["procedure_ids"] == ["proc_edited"]
    assert edit_query["cases"][0]["learning_evidence_kinds"] == ["procedure_memory_records"]
    request_changes_query = dashboard["views"]["learning_request_changes_cases"]["query"]
    assert request_changes_query["summary"]["matched_cases"] == 1
    assert request_changes_query["cases"][0]["scenario_id"] == "learning_edit_case"
    assert request_changes_query["cases"][0]["procedure_ids"] == ["proc_edited"]
    assert request_changes_query["cases"][0]["learning_evidence_kinds"] == [
        "procedure_memory_records"
    ]
    disable_query = dashboard["views"]["learning_disable_cases"]["query"]
    assert disable_query["summary"]["matched_cases"] == 1
    assert disable_query["cases"][0]["scenario_id"] == "learning_disable_case"
    assert disable_query["cases"][0]["habit_ids"] == ["habit_disabled"]
    assert disable_query["cases"][0]["lineage_skill_names"] == ["generated.habit.disabled"]
    assert disable_query["cases"][0]["learning_evidence_kinds"] == [
        "procedure_memory_records",
        "skill_lifecycle_records",
    ]
    restore_query = dashboard["views"]["learning_restore_cases"]["query"]
    assert restore_query["summary"]["matched_cases"] == 1
    assert restore_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert restore_query["cases"][0]["habit_ids"] == ["habit_1"]
    assert restore_query["cases"][0]["lineage_skill_names"] == ["generated.habit.demo"]
    rollback_query = dashboard["views"]["learning_rollback_cases"]["query"]
    assert rollback_query["summary"]["matched_cases"] == 1
    assert rollback_query["cases"][0]["scenario_id"] == "learning_rollback_case"
    assert rollback_query["cases"][0]["lineage_skill_names"] == ["imported.learning_echo"]
    assert rollback_query["cases"][0]["learning_evidence_kinds"] == ["skill_lifecycle_records"]
    rejection_query = dashboard["views"]["learning_rejection_cases"]["query"]
    assert rejection_query["summary"]["matched_cases"] == 1
    assert rejection_query["cases"][0]["scenario_id"] == "learning_rejection_case"
    assert rejection_query["cases"][0]["habit_ids"] == ["habit_rejected"]
    assert rejection_query["cases"][0]["lineage_skill_names"] == ["generated.habit.rejected"]
    assert rejection_query["cases"][0]["learning_evidence_kinds"] == [
        "procedure_memory_records",
        "skill_lifecycle_records",
    ]
    audit_gap_query = dashboard["views"]["learning_audit_gap_cases"]["query"]
    assert audit_gap_query["summary"]["matched_cases"] == 2
    assert audit_gap_query["cases"][0]["scenario_id"] == "learning_audit_gap_case"
    assert audit_gap_query["cases"][0]["procedure_ids"] == ["proc_gap"]
    assert audit_gap_query["cases"][0]["habit_ids"] == ["habit_gap"]
    assert audit_gap_query["cases"][0]["lineage_skill_names"] == ["generated.habit.audit_gap"]
    assert audit_gap_query["cases"][0]["learning_evidence_kinds"] == [
        "procedure_matches",
        "skill_lineages",
    ]
    lifecycle_query = dashboard["views"]["skill_lifecycle_governance_cases"]["query"]
    assert lifecycle_query["summary"]["matched_cases"] == 4
    assert lifecycle_query["cases"][0]["scenario_id"] == "learning_loop_case"
    assert lifecycle_query["cases"][0]["lineage_skill_names"] == ["generated.habit.demo"]
    assert "skill_lifecycle_records" in lifecycle_query["cases"][0]["learning_evidence_kinds"]

    markdown = evals.render_evidence_dashboard_markdown(dashboard)

    assert "# Eval Evidence Dashboard" in markdown
    assert "## Repair Entry Points" in markdown
    assert "## Regression Commands" in markdown
    assert "## Eval Coverage Cases" in markdown
    assert "## Skipped Eval Cases" in markdown
    assert "skip_reason" in markdown
    assert "`poetry run python scripts/run_real_world_evals.py --build-evidence-index`" in markdown
    assert (
        "`poetry run python scripts/run_real_world_evals.py --include-complex --scenario reasoning_case`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-failure-layer skill_or_tool`"
        in markdown
    )
    assert "## Model Capability Cases" in markdown
    assert "## Node Execution Cases" in markdown
    assert "## Critic Action Cases" in markdown
    assert "## Completion Evaluation Cases" in markdown
    assert "## Memory Retrieval Cases" in markdown
    assert "## LLM Usage Cases" in markdown
    assert "## Runtime Cost Hotspot Cases" in markdown
    assert "## Execution Policy Cases" in markdown
    assert "## Planner Skipped Cases" in markdown
    assert "## Policy Risk Flag Cases" in markdown
    assert "## Policy Reason Cases" in markdown
    assert "## Policy Context Budget Cases" in markdown
    assert "## Request Payload Stats Cases" in markdown
    assert "## Permission Governance Cases" in markdown
    assert "## Habit Fast Path Cases" in markdown
    assert "## Learning Match Explanation Cases" in markdown
    assert "## Learning Gate Cases" in markdown
    assert "## Learning Correction Cases" in markdown
    assert "## Learning Governance Cases" in markdown
    assert "## Learning History Cases" in markdown
    assert "## Learning Approval Cases" in markdown
    assert "## Learning Promotion Cases" in markdown
    assert "## Learning Validation Cases" in markdown
    assert "## Learning Edit Cases" in markdown
    assert "## Learning Request Changes Cases" in markdown
    assert "## Learning Decay Cases" in markdown
    assert "## Learning Disable Cases" in markdown
    assert "## Learning Restore Cases" in markdown
    assert "## Learning Rollback Cases" in markdown
    assert "## Learning Rejection Cases" in markdown
    assert "## Learning Audit Gap Cases" in markdown
    assert "## Skill Lifecycle Governance Cases" in markdown
    assert "## Skill Promotion Coverage Cases" in markdown
    assert "promotion_generation_type" in markdown
    assert "promotion_source_target" in markdown
    assert "policy_decision" in markdown
    assert "skipped_stage" in markdown
    assert "policy_risk_flag" in markdown
    assert "policy_reason" in markdown
    assert "policy_context_budget" in markdown
    assert "request_payload_stats" in markdown
    assert "node_status" in markdown
    assert "critic_action" in markdown
    assert "learning_validation" in markdown
    assert "skill_validation" in markdown
    assert "learning_review_decision" in markdown
    assert "learning_audit_event" in markdown
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind learning_corrections`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind procedure_memory_records,skill_lifecycle_records`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-completion-layer recorded`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-memory-retrieval recorded`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage llm_called`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage tokens:10000+,duration:60s+`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-decision direct_execute`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skipped-stage planner`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-risk-flag planner_context_required`"
        in markdown
    )
    assert (
        '`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-reason "*"`'
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-context-budget recorded`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-request-payload-stats recorded`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status recorded`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-critic-action recorded`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind procedure_matches`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind experience_records,procedure_memory_records,skill_lifecycle_records`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind procedure_memory_records,skill_lineages,skill_lifecycle_records`"
        in markdown
    )
    assert (
        '`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-validation "*"`'
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-kind skill_lifecycle_records`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-audit-event edited`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-review-decision needs_changes`"
        in markdown
    )
    assert (
        "`poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-learning-audit-event decayed`"
        in markdown
    )
    assert "learning_rejection_case" in markdown
    assert "learning_rollback_case" in markdown
    assert "learning_edit_case" in markdown
    assert "learning_disable_case" in markdown
    assert "## Generated Habit Skill Usage" in markdown
    assert "## Skill Approval Gate Cases" in markdown
    assert "`generated.habit.git_status`" in markdown
    assert "`generated.habit.demo`" in markdown


def test_evidence_dashboard_builds_learning_decay_view() -> None:
    index = {
        "schema_version": "eval-evidence-index/v1",
        "run_root": "/tmp/eval_runs",
        "run_count": 1,
        "cases": [
            {
                "run_id": "run_decay",
                "scenario_id": "learning_decay_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "proc_decayed",
                            "object_type": "procedure",
                            "status": "verified",
                            "previous_status": "habit",
                            "review_status": "decayed_requires_review",
                            "review_count": 0,
                            "audit_event_count": 1,
                            "audit_events": ["decayed"],
                            "latest_audit_event": "decayed",
                        }
                    ],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [],
                },
                "artifacts": [],
            }
        ],
    }

    dashboard = evals.build_evidence_dashboard(index, limit=5)

    decay_query = dashboard["views"]["learning_decay_cases"]["query"]
    assert decay_query["summary"]["matched_cases"] == 1
    assert decay_query["cases"][0]["scenario_id"] == "learning_decay_case"
    assert decay_query["cases"][0]["procedure_ids"] == ["proc_decayed"]
    assert decay_query["cases"][0]["learning_evidence_kinds"] == ["procedure_memory_records"]

    governance_query = dashboard["views"]["learning_governance_cases"]["query"]
    assert governance_query["summary"]["matched_cases"] == 1
    assert governance_query["cases"][0]["scenario_id"] == "learning_decay_case"

    markdown = evals.render_evidence_dashboard_markdown(dashboard)
    assert "## Learning Decay Cases" in markdown
    assert "learning_decay_case" in markdown


def test_evidence_dashboard_builds_learning_request_changes_view() -> None:
    index = {
        "schema_version": "eval-evidence-index/v1",
        "run_root": "/tmp/eval_runs",
        "run_count": 1,
        "cases": [
            {
                "run_id": "run_request_changes",
                "scenario_id": "learning_request_changes_case",
                "category": "learning_loop",
                "kind": "task",
                "status": "passed",
                "failure_layer": "none",
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {
                    "procedure_matches": [],
                    "habit_fast_paths": [],
                    "experience_records": [],
                    "procedure_memory_records": [
                        {
                            "procedure_id": "proc_needs_changes",
                            "object_type": "procedure",
                            "status": "draft",
                            "previous_status": "habit",
                            "review_status": "needs_changes",
                            "review_count": 1,
                            "review_decisions": ["needs_changes"],
                            "latest_review_decision": "needs_changes",
                            "audit_event_count": 1,
                            "audit_events": ["reviewed"],
                            "latest_audit_event": "reviewed",
                        }
                    ],
                    "skill_lineages": [],
                    "skill_lifecycle_records": [],
                },
                "artifacts": [],
            }
        ],
    }

    dashboard = evals.build_evidence_dashboard(index, limit=5)

    request_changes_query = dashboard["views"]["learning_request_changes_cases"]["query"]
    assert request_changes_query["summary"]["matched_cases"] == 1
    assert request_changes_query["cases"][0]["scenario_id"] == "learning_request_changes_case"
    assert request_changes_query["cases"][0]["procedure_ids"] == ["proc_needs_changes"]
    assert request_changes_query["cases"][0]["learning_evidence_kinds"] == [
        "procedure_memory_records"
    ]

    edit_query = dashboard["views"]["learning_edit_cases"]["query"]
    assert edit_query["summary"]["matched_cases"] == 0

    governance_query = dashboard["views"]["learning_governance_cases"]["query"]
    assert governance_query["summary"]["matched_cases"] == 1
    assert governance_query["cases"][0]["scenario_id"] == "learning_request_changes_case"

    markdown = evals.render_evidence_dashboard_markdown(dashboard)
    assert "## Learning Request Changes Cases" in markdown
    assert "learning_request_changes_case" in markdown


def test_evidence_dashboard_ignores_empty_failure_layers_for_repair_commands() -> None:
    index = {
        "schema_version": "eval-evidence-index/v1",
        "run_root": "/tmp/eval_runs",
        "run_count": 1,
        "cases": [
            {
                "run_id": "run_passed",
                "scenario_id": "legacy_none_layer",
                "category": "legacy",
                "kind": "task",
                "status": "passed",
                "failure_layer": None,
                "diagnosis": "passed",
                "used_skills": [],
                "learning_evidence": {},
                "artifacts": [],
            },
            {
                "run_id": "run_failed",
                "scenario_id": "tool_failure",
                "category": "data",
                "kind": "task",
                "status": "failed",
                "failure_layer": "skill_or_tool",
                "diagnosis": "skill_execution_failed",
                "used_skills": ["builtin.data.config"],
                "learning_evidence": {},
                "artifacts": [],
            },
        ],
    }

    dashboard = evals.build_evidence_dashboard(index, limit=5)

    assert [item["failure_layer"] for item in dashboard["repair_entrypoints"]] == ["skill_or_tool"]
    commands = [item["command"] for item in dashboard["regression_commands"]]
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-failure-layer None"
        not in commands
    )


def test_evidence_dashboard_cli_writes_regression_command_file(tmp_path: Path) -> None:
    index = {
        "schema_version": "eval-evidence-index/v1",
        "run_root": str(tmp_path),
        "run_count": 1,
        "cases": [
            {
                "run_id": "run_failed",
                "scenario_id": "tool_failure",
                "category": "data",
                "kind": "task",
                "status": "failed",
                "failure_layer": "skill_or_tool",
                "diagnosis": "skill_execution_failed",
                "used_skills": ["builtin.data.config"],
                "learning_evidence": {},
                "artifacts": [],
            }
        ],
    }
    index_path = tmp_path / "evidence_index.json"
    dashboard_path = tmp_path / "dashboard.md"
    commands_path = tmp_path / "dashboard.commands"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    parsed = evals.build_parser().parse_args(
        [
            "--evidence-dashboard",
            "--evidence-dashboard-commands-output",
            str(commands_path),
        ]
    )
    assert parsed.evidence_dashboard_commands_output == str(commands_path)

    exit_code = evals.main(
        [
            "--evidence-dashboard",
            "--evidence-index-input",
            str(index_path),
            "--evidence-dashboard-output",
            str(dashboard_path),
            "--evidence-dashboard-commands-output",
            str(commands_path),
        ]
    )

    assert exit_code == 0
    assert "## Regression Commands" in dashboard_path.read_text(encoding="utf-8")
    commands = commands_path.read_text(encoding="utf-8")
    assert "# Eval evidence dashboard regression commands" in commands
    assert "poetry run python scripts/run_real_world_evals.py --check-only" in commands
    assert (
        "poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-failure-layer skill_or_tool"
        in commands
    )
    assert "poetry run python scripts/run_real_world_evals.py --scenario tool_failure" in commands

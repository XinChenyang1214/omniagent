from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from omniagent.execution_engine import ExecutionEngine
from omniagent.models import (
    BootstrapContext,
    CriticAction,
    CriticVerdict,
    ExecutionResult,
    PermissionLevel,
    SkillRegistryEntry,
    SkillSelection,
    Task,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskNodeType,
    TaskRequest,
    TaskRunContext,
    TaskStatus,
)
from omniagent.permissions import PermissionManager


def _task(tmp_path: Path) -> Task:
    return Task(
        task_id="task_test",
        goal="test",
        mode="semi_auto",
        source="cli",
        status=TaskStatus.RUNNING,
        executor="auto",
        skill_name=None,
        payload={},
        workspace_path=str(tmp_path),
    )


def _engine() -> ExecutionEngine:
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.dispatcher = MagicMock()
    engine.store = MagicMock()
    engine.permission_manager = PermissionManager()
    engine.graph_memory = None
    return engine


def test_control_flow_nodes_do_not_inherit_preferred_skill_selection() -> None:
    engine = _engine()
    node = TaskNode(
        node_id="node_finalize",
        type=TaskNodeType.FINALIZE,
        goal="summarize",
    )
    request = TaskRequest(
        goal="fetch a URL",
        preferred_skills=["builtin.http.client"],
    )

    selection = engine._select_skill_for_node(node, request)

    engine.dispatcher.skill_registry.select.assert_not_called()
    assert selection.selected_skill is None
    assert selection.registry_match is None
    assert selection.explanation["source"] == "control_node"


def test_finalize_permission_stays_l1_even_when_task_prefers_network_skill(tmp_path: Path) -> None:
    engine = _engine()
    node = TaskNode(
        node_id="node_finalize",
        type=TaskNodeType.FINALIZE,
        goal="summarize",
    )
    request = TaskRequest(
        goal="fetch a URL",
        task_id="task_test",
        preferred_skills=["builtin.http.client"],
        constraints=TaskConstraints(max_permission=PermissionLevel.L2, allow_network=False),
    )
    selection = engine._select_skill_for_node(node, request)

    result = engine._check_node_permission(request, node, _task(tmp_path), selection=selection)

    assert result is None
    engine.store.record_permission.assert_called_once()
    decision = engine.store.record_permission.call_args.args[0]
    assert decision.permission == "filesystem.read"
    assert str(decision.level) == "L1"
    assert decision.status == "granted"
    assert node.metadata["permission_context"] == {
        "permission": "filesystem.read",
        "level": "L1",
        "status": "granted",
        "scope": "node",
        "resource_kind": None,
        "resource_ref": None,
        "sandbox_level": "workspace",
    }


def test_action_nodes_still_use_registry_selection() -> None:
    engine = _engine()
    expected_selection = SkillSelection(
        node_id="node_fetch",
        selected_skill="builtin.http.client",
        executor_hint=None,
        registry_match=SkillRegistryEntry(
            skill_name="builtin.http.client",
            source="builtin",
            version="0.1.0",
            description="HTTP client",
            permission_requirements=["network"],
            tags=["http"],
            location="",
            scope="builtin",
            sandbox_level="workspace",
            success_count=0,
            failure_count=0,
        ),
    )
    engine.dispatcher.skill_registry.select.return_value = expected_selection
    node = TaskNode(
        node_id="node_fetch",
        type=TaskNodeType.ACTION,
        goal="fetch",
        input_payload={"action": "get", "url": "https://example.com"},
    )
    request = TaskRequest(
        goal="fetch a URL",
        preferred_skills=["builtin.http.client"],
    )

    selection = engine._select_skill_for_node(node, request)

    engine.dispatcher.skill_registry.select.assert_called_once_with(
        node, ["builtin.http.client"]
    )
    assert selection is expected_selection


def test_executor_only_action_nodes_do_not_inherit_preferred_skills() -> None:
    engine = _engine()
    node = TaskNode(
        node_id="node_generate",
        type=TaskNodeType.ACTION,
        goal="generate text",
        executor_hint="llm_native",
        input_payload={"prompt": "rewrite this"},
    )
    request = TaskRequest(
        goal="rewrite text",
        preferred_skills=["builtin.file.basic"],
    )

    selection = engine._select_skill_for_node(node, request)

    engine.dispatcher.skill_registry.select.assert_not_called()
    assert selection.selected_skill is None
    assert selection.registry_match is None
    assert selection.executor_hint == "llm_native"
    assert selection.explanation["source"] == "executor_hint"


def test_run_node_injects_dependency_outputs_before_dispatch(tmp_path: Path) -> None:
    engine = _engine()
    task = _task(tmp_path)
    request = TaskRequest(goal="帮我看一下今天的天气情况", task_id=task.task_id)
    fetch = TaskNode(
        node_id="node_fetch_weather",
        type=TaskNodeType.ACTION,
        goal="fetch weather",
        status=TaskNodeStatus.COMPLETED,
        output={"status": 200, "body": "郑州今日天气：晴，25°C"},
    )
    format_node = TaskNode(
        node_id="node_format_weather",
        type=TaskNodeType.ACTION,
        goal="format weather",
        dependencies=["node_fetch_weather"],
        executor_hint="llm_native",
        input_payload={"prompt": "整理天气数据"},
    )
    graph = TaskGraph(goal="weather", nodes=[fetch, format_node], dependencies=[])
    selection = SkillSelection(
        node_id=format_node.node_id,
        selected_skill=None,
        executor_hint="llm_native",
        explanation={"source": "executor_hint"},
    )
    engine._select_skill_for_node = MagicMock(return_value=selection)
    engine._check_node_permission = MagicMock(return_value=None)
    engine._emit_node_event = MagicMock()
    engine.artifact_manager = MagicMock()
    engine.dispatcher.dispatch.return_value = (
        ExecutionResult(
            success=True,
            status="completed",
            message="weather formatted",
            output={"content": "郑州今日天气：晴，25°C"},
            executor="llm_native",
        ),
        None,
    )
    context = TaskRunContext(
        task=task,
        task_request=request,
        workspace_root=tmp_path,
        task_dir=tmp_path / "tasks" / task.task_id,
        bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
        loaded_skills={},
        sandbox=MagicMock(),
        task_graph=graph,
    )

    result = engine._run_node(
        task=task,
        task_request=request,
        node=format_node,
        task_graph=graph,
        context=context,
        event_bus=MagicMock(),
        critic_verdicts=[],
    )

    assert result is None
    dispatched_node = engine.dispatcher.dispatch.call_args.args[0]
    assert dispatched_node.input_payload["dependency_outputs"] == {
        "node_fetch_weather": {"status": 200, "body": "郑州今日天气：晴，25°C"}
    }
    assert dispatched_node.metadata["dependency_outputs_injected"] == ["node_fetch_weather"]
    assert format_node.status == TaskNodeStatus.COMPLETED


def test_run_node_compacts_large_dependency_outputs_before_dispatch(tmp_path: Path) -> None:
    engine = _engine()
    task = _task(tmp_path)
    request = TaskRequest(goal="抓取网页并总结", task_id=task.task_id)
    large_body = "天气数据" * 1000
    fetch = TaskNode(
        node_id="node_fetch",
        type=TaskNodeType.ACTION,
        goal="fetch",
        status=TaskNodeStatus.COMPLETED,
        output={
            "status": 200,
            "body": large_body,
            "body_preview": large_body[:4096],
            "body_truncated": True,
        },
    )
    summarize = TaskNode(
        node_id="node_summarize",
        type=TaskNodeType.ACTION,
        goal="summarize",
        dependencies=["node_fetch"],
        executor_hint="llm_native",
        input_payload={"prompt": "总结网页"},
    )
    graph = TaskGraph(goal="summarize web", nodes=[fetch, summarize], dependencies=[])
    selection = SkillSelection(
        node_id=summarize.node_id,
        selected_skill=None,
        executor_hint="llm_native",
        explanation={"source": "executor_hint"},
    )
    engine._select_skill_for_node = MagicMock(return_value=selection)
    engine._check_node_permission = MagicMock(return_value=None)
    engine._emit_node_event = MagicMock()
    engine.artifact_manager = MagicMock()
    engine.dispatcher.dispatch.return_value = (
        ExecutionResult(
            success=True,
            status="completed",
            message="summarized",
            output={"content": "summary"},
            executor="llm_native",
        ),
        None,
    )
    context = TaskRunContext(
        task=task,
        task_request=request,
        workspace_root=tmp_path,
        task_dir=tmp_path / "tasks" / task.task_id,
        bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
        loaded_skills={},
        sandbox=MagicMock(),
        task_graph=graph,
    )

    result = engine._run_node(
        task=task,
        task_request=request,
        node=summarize,
        task_graph=graph,
        context=context,
        event_bus=MagicMock(),
        critic_verdicts=[],
    )

    assert result is None
    dispatched_node = engine.dispatcher.dispatch.call_args.args[0]
    injected = dispatched_node.input_payload["dependency_outputs"]["node_fetch"]
    assert "body" not in injected
    assert injected["body_omitted"] == "body_preview already present"
    assert injected["body_preview"].endswith("...<truncated>")
    assert len(injected["body_preview"]) < len(large_body)


def test_accepted_failed_node_does_not_trigger_recovery(tmp_path: Path) -> None:
    _assert_terminal_failed_node_does_not_trigger_recovery(tmp_path, CriticAction.ACCEPT)


def test_failed_node_fail_verdict_does_not_trigger_recovery(tmp_path: Path) -> None:
    _assert_terminal_failed_node_does_not_trigger_recovery(tmp_path, CriticAction.FAIL)


def _assert_terminal_failed_node_does_not_trigger_recovery(
    tmp_path: Path,
    action: CriticAction,
) -> None:
    engine = _engine()
    task = _task(tmp_path)
    request = TaskRequest(goal="fail honestly", task_id=task.task_id)
    node = TaskNode(
        node_id="node_json",
        type=TaskNodeType.ACTION,
        goal="read invalid json",
        skill_name="builtin.data.json",
        input_payload={"action": "report", "path": "inputs/broken.json"},
    )
    graph = TaskGraph(goal="fail honestly", nodes=[node], dependencies=[])
    selection = SkillSelection(
        node_id=node.node_id,
        selected_skill="builtin.data.json",
        executor_hint=None,
    )
    engine._select_skill_for_node = MagicMock(return_value=selection)
    engine._check_node_permission = MagicMock(return_value=None)
    engine._emit_node_event = MagicMock()
    engine.artifact_manager = MagicMock()
    engine.critic = MagicMock()
    engine.critic.evaluate_node.return_value = (
        CriticVerdict(
            target_type="node",
            target_id=node.node_id,
            action=action,
            summary="terminal failed node verdict",
            rationale="critic says this failed node is terminal",
            confidence=0.95,
        ),
        MagicMock(),
    )
    engine.dispatcher.dispatch.return_value = (
        ExecutionResult(
            success=False,
            status="failed",
            message="invalid json",
            output={"errors": ["invalid json"]},
            executor="builtin.data.json",
        ),
        "builtin.data.json",
    )
    engine.dispatcher.recover_incompatible_skill = MagicMock()
    engine.dispatcher.skill_registry.record_execution = MagicMock()
    context = TaskRunContext(
        task=task,
        task_request=request,
        workspace_root=tmp_path,
        task_dir=tmp_path / "tasks" / task.task_id,
        bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
        loaded_skills={},
        sandbox=MagicMock(),
        task_graph=graph,
    )

    result = engine._run_node(
        task=task,
        task_request=request,
        node=node,
        task_graph=graph,
        context=context,
        event_bus=MagicMock(),
        critic_verdicts=[],
    )

    assert result is not None
    assert not result.success
    assert node.status == TaskNodeStatus.FAILED
    engine.dispatcher.recover_incompatible_skill.assert_not_called()

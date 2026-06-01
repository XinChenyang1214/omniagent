from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.completion_evaluator import CompletionEvaluator
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import (
    CompletionEvaluation,
    ExecutionResult,
    LLMResponse,
    PermissionLevel,
    Task,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskNodeType,
    TaskRequest,
    TaskStatus,
)


class CompletionEvaluationModelTest(unittest.TestCase):
    def test_from_dict_clamps_scores_and_normalizes_lists(self) -> None:
        evaluation = CompletionEvaluation.from_dict(
            {
                "task_id": "task_1",
                "task_satisfied": True,
                "completion_score": 2.0,
                "summary": "ok",
                "missing_requirements": ["none"],
                "unexpected_side_effects": "not-list",
                "evidence": [1, "done"],
                "primary_failure_layer": "",
                "model_capability_suspected": False,
                "recommended_next_action": "",
                "confidence": -1.0,
            }
        )

        self.assertEqual(evaluation.completion_score, 1.0)
        self.assertEqual(evaluation.confidence, 0.0)
        self.assertEqual(evaluation.unexpected_side_effects, [])
        self.assertEqual(evaluation.evidence, ["1", "done"])
        self.assertEqual(evaluation.primary_failure_layer, "none")
        self.assertEqual(evaluation.recommended_next_action, "none")

    def test_from_dict_clears_model_capability_flag_for_other_layers(self) -> None:
        evaluation = CompletionEvaluation.from_dict(
            {
                "task_id": "task_1",
                "task_satisfied": False,
                "completion_score": 0.0,
                "summary": "payload contradicted the goal",
                "missing_requirements": [],
                "unexpected_side_effects": [],
                "evidence": ["input_payload wrote gate: fail"],
                "primary_failure_layer": "planner_or_skill_selection",
                "model_capability_suspected": True,
                "recommended_next_action": "fix_planner",
                "confidence": 1.0,
            }
        )

        self.assertFalse(evaluation.model_capability_suspected)


class CompletionEvaluatorPayloadTest(unittest.TestCase):
    def test_node_input_payload_is_included_in_evaluation_evidence(self) -> None:
        captured: dict[str, object] = {}

        class CapturingLLM:
            def generate_structured(self, **kwargs):
                captured["request_payload"] = kwargs["request_payload"]
                return LLMResponse(
                    provider="test",
                    model="test",
                    prompt_name="completion_evaluator_v1",
                    request_id="capture",
                    raw_output="{}",
                    structured_output={
                        "task_id": "task_payload",
                        "task_satisfied": True,
                        "completion_score": 1.0,
                        "summary": "ok",
                        "missing_requirements": [],
                        "unexpected_side_effects": [],
                        "evidence": ["captured"],
                        "primary_failure_layer": "none",
                        "model_capability_suspected": False,
                        "recommended_next_action": "none",
                        "confidence": 1.0,
                    },
                )

        prompts = type("Prompts", (), {"load": lambda self, name: ""})()
        evaluator = CompletionEvaluator(CapturingLLM(), prompts)
        task = Task(
            task_id="task_payload",
            goal="Create reports/quality_gate.md with gate: pass.",
            mode="semi_auto",
            source="test",
            status=TaskStatus.COMPLETED,
            executor="file",
            skill_name=None,
            payload={},
            workspace_path=".",
            expected_output="reports/quality_gate.md contains gate: pass.",
        )
        node = TaskNode(
            node_id="node_write",
            type=TaskNodeType.ACTION,
            goal="Write quality gate",
            executor_hint="file",
            status=TaskNodeStatus.COMPLETED,
            input_payload={
                "action": "write",
                "path": "reports/quality_gate.md",
                "content": "gate: fail\nowner: qa\n",
            },
            output={"path": "reports/quality_gate.md", "size": 21},
        )

        evaluator.evaluate_task(
            task=task,
            task_graph=TaskGraph(goal=task.goal, nodes=[node]),
            execution_result=ExecutionResult(
                success=True,
                status="completed",
                message="done",
                output={"completed_nodes": ["node_write"]},
                executor="task_graph",
            ),
            critic_verdicts=[],
            workflow_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
        )

        request_payload = captured["request_payload"]
        node_payload = request_payload["task_graph"]["nodes"][0]["input_payload"]  # type: ignore[index]
        self.assertEqual(node_payload["content"], "gate: fail\nowner: qa\n")


class CompletionEvaluationRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_root = Path(self.tempdir.name)
        self.workspace = self.project_root / "workspace"
        self.env = {
            "OMNIAGENT_SQLITE_PATH": str(self.project_root / "data" / "omniagent.sqlite3"),
            "OMNIAGENT_LOG_PATH": str(self.project_root / "artifacts" / "omniagent.log"),
            "OMNIAGENT_JSON_LOGS": "false",
            "OMNIAGENT_LLM_PROVIDER": "test",
        }

    def _build_gateway(self) -> RuntimeGateway:
        with patch.dict(os.environ, self.env, clear=False):
            config = AppConfig.from_env(self.project_root)
        return RuntimeGateway(config, self.workspace)

    def test_task_run_writes_completion_evaluation_artifact(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            gateway = self._build_gateway()
            report = gateway.submit(
                TaskRequest(
                    goal="列出工作区文件",
                    executor="file",
                    payload={"action": "list", "path": "."},
                    constraints=TaskConstraints(max_permission=PermissionLevel.L2),
                )
        )

        self.assertIsNotNone(report.completion_evaluation)
        evaluation = report.completion_evaluation
        if evaluation is None:
            self.fail("completion evaluation missing")
        self.assertTrue(evaluation.task_satisfied)
        self.assertEqual(evaluation.primary_failure_layer, "none")

        task_dir = self.workspace / "tasks" / report.task.task_id
        evaluation_path = task_dir / "completion" / "evaluation.json"
        self.assertTrue(evaluation_path.exists())
        payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["task_id"], report.task.task_id)
        self.assertEqual(payload["completion_score"], 1.0)

        llm_prompts = {
            json.loads(path.read_text(encoding="utf-8"))["prompt_name"]
            for path in (task_dir / "llm_calls").glob("*.json")
        }
        self.assertIn("completion_evaluator_v1", llm_prompts)

    def test_unsatisfied_completion_evaluation_overrides_success(self) -> None:
        class UnsatisfiedEvaluator:
            def evaluate_task(self, **kwargs):
                task = kwargs["task"]
                evaluation = CompletionEvaluation(
                    task_id=task.task_id,
                    task_satisfied=False,
                    completion_score=0.4,
                    summary="缺少实时天气证据",
                    missing_requirements=["实时天气"],
                    evidence=["抓取结果不可用"],
                    primary_failure_layer="tool_or_skill",
                    recommended_next_action="fix_tool_or_skill",
                    confidence=0.9,
                )
                response = LLMResponse(
                    provider="test",
                    model="test",
                    prompt_name="completion_evaluator_v1",
                    request_id="completion_unsatisfied",
                    raw_output="{}",
                    structured_output=evaluation.to_dict(),
                )
                return evaluation, response

        with patch.dict(os.environ, self.env, clear=False):
            gateway = self._build_gateway()
            gateway.completion_evaluator = UnsatisfiedEvaluator()
            gateway.runtime.completion_evaluator = gateway.completion_evaluator
            report = gateway.submit(
                TaskRequest(
                    goal="列出工作区文件",
                    executor="file",
                    payload={"action": "list", "path": "."},
                    constraints=TaskConstraints(max_permission=PermissionLevel.L2),
                )
            )

        self.assertFalse(report.result.success)
        self.assertEqual(report.task.status, TaskStatus.FAILED)
        self.assertEqual(report.result.error_code, "completion_unsatisfied")
        self.assertIn("缺少实时天气证据", report.result.message)
        self.assertIsNotNone(report.completion_evaluation)

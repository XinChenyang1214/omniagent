"""Tests for META-003 (Planner decision field) + META-004 (LLMNativeExecutor).

Covers the four Planner decision outcomes — ``plan`` / ``decline_not_a_task``
/ ``clarify`` / ``missing_capability`` — and the pure LLM-native text
executor that lets Planner avoid the ``python print(literal_string)``
anti-pattern for text-generation tasks.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omniagent.config import AppConfig
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.llm_native import LLMNativeExecutor
from omniagent.gateway import RuntimeGateway
from omniagent.models import (
    BootstrapContext,
    LLMResponse,
    PermissionLevel,
    PlanResult,
    Task,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskRequest,
    TaskRunContext,
    TaskStatus,
)
from omniagent.planner import Planner
from tests.llm_test_helper import StubLLMAdapter


class PlannerDecisionBranchTest(unittest.TestCase):
    """The runtime must short-circuit non-``plan`` Planner decisions."""

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

    def _plan_with_decision(
        self,
        *,
        decision: str,
        summary: str,
        decline_reason: str | None = None,
        clarify_question: str | None = None,
        missing_capability: dict | None = None,
    ) -> tuple[PlanResult, LLMResponse]:
        plan = PlanResult(
            summary=summary,
            graph=TaskGraph(goal="", summary="", nodes=[]),
            reasoning=f"test plan returning decision={decision}",
            risk_level="low",
            required_capabilities=[],
            decision=decision,
            decline_reason=decline_reason,
            clarify_question=clarify_question,
            missing_capability=missing_capability,
        )
        response = LLMResponse(
            provider="test",
            model="test-model",
            prompt_name="planner_v1",
            request_id="test_decision",
            raw_output="{}",
            structured_output=plan.to_dict(),
        )
        return plan, response

    def test_decline_not_a_task_completes_with_decline_reason(self) -> None:
        gateway = self._build_gateway()
        plan, response = self._plan_with_decision(
            decision="decline_not_a_task",
            summary="拒绝闲聊",
            decline_reason="这是日常问候而非任务，由对话层回复即可",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, response)):
            report = gateway.submit(
                TaskRequest(
                    goal="晚安",
                    constraints=TaskConstraints(max_permission=PermissionLevel.L1),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.COMPLETED)
        self.assertTrue(report.result.success)
        self.assertIn("这是日常问候", report.result.message)
        self.assertEqual(report.result.executor, "planner_decision")
        self.assertEqual(report.result.output["decision"], "decline_not_a_task")
        event_types = [e.event_type for e in report.events]
        self.assertIn("task.declined", event_types)
        # No engine execution: no node.started / node.completed events should fire
        self.assertNotIn("task.node.started", event_types)

    def test_clarify_completes_with_clarify_question(self) -> None:
        gateway = self._build_gateway()
        plan, response = self._plan_with_decision(
            decision="clarify",
            summary="需要澄清具体的数据源",
            clarify_question="请问您说的『那些数据』指的是哪些文件或目录？",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, response)):
            report = gateway.submit(
                TaskRequest(
                    goal="处理那些数据",
                    constraints=TaskConstraints(max_permission=PermissionLevel.L1),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.COMPLETED)
        self.assertTrue(report.result.success)
        self.assertIn("那些数据", report.result.message)
        self.assertEqual(report.result.output["decision"], "clarify")
        event_types = [e.event_type for e in report.events]
        self.assertIn("task.clarify_requested", event_types)

    def test_missing_capability_fails_with_skill_error(self) -> None:
        gateway = self._build_gateway()
        plan, response = self._plan_with_decision(
            decision="missing_capability",
            summary="缺少推特 API 能力",
            missing_capability={
                "action": "post_tweet",
                "executor_class": "api",
                "description": "需要接入 Twitter/X API 的 OAuth 凭据和发推接口",
            },
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, response)):
            report = gateway.submit(
                TaskRequest(
                    goal="把这段话发到我的推特",
                    constraints=TaskConstraints(max_permission=PermissionLevel.L1),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.FAILED)
        self.assertFalse(report.result.success)
        self.assertIn("Twitter", report.result.message)
        self.assertEqual(report.result.error_code, ErrorCode.SKILL_ERROR)
        self.assertEqual(
            report.result.output["missing_capability"]["action"],
            "post_tweet",
        )
        event_types = [e.event_type for e in report.events]
        self.assertIn("task.missing_capability", event_types)

    def test_planner_error_marks_task_failed_instead_of_leaving_planning(self) -> None:
        gateway = self._build_gateway()
        error = OmniAgentError(
            ErrorCode.LLM_ERROR,
            "LLM 调用失败 (openai): LLM call deadline exceeded",
            details={"attempts": 1, "latency_ms": 30000.0},
        )

        with patch.object(gateway.planner, "plan", side_effect=error):
            report = gateway.submit(
                TaskRequest(
                    goal="给我出一份本周周末在郑州游玩的攻略，考虑天气情况",
                    constraints=TaskConstraints(max_permission=PermissionLevel.L1),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.FAILED)
        self.assertFalse(report.result.success)
        self.assertEqual(report.result.executor, "planner")
        self.assertEqual(report.result.error_code, ErrorCode.LLM_ERROR)
        self.assertEqual(report.result.output["phase"], "planning")
        event_types = [e.event_type for e in report.events]
        self.assertIn("task.failed", event_types)

        payload = gateway.show_task(report.task.task_id)
        self.assertEqual(payload["task"].status, TaskStatus.FAILED)
        self.assertEqual(payload["task"].error_code, ErrorCode.LLM_ERROR)
        self.assertIn("deadline exceeded", payload["task"].error_message)
        self.assertTrue(
            (self.workspace / "tasks" / report.task.task_id / "execution_result.json").exists()
        )


class PlannerDirectFilePlanTest(unittest.TestCase):
    def test_direct_file_payload_plan_bypasses_llm_and_preserves_payload(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal="Create reports/quality_gate.md with gate: pass.",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/quality_gate.md",
                    "content": "gate: fail\n",
                    "overwrite": True,
                },
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(response.request_id, "rule_based_explicit_file_payload_plan")
        self.assertEqual(plan.graph.nodes[0].executor_hint, "file")
        self.assertEqual(plan.graph.nodes[0].input_payload["content"], "gate: fail\n")

    def test_direct_skill_payload_plan_bypasses_llm_and_preserves_payload(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal="Use builtin.browser.basic to fetch inputs/blocked.html.",
                skill_name="builtin.browser.basic",
                payload={"action": "fetch", "url": "inputs/blocked.html"},
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(response.request_id, "rule_based_explicit_skill_payload_plan")
        self.assertEqual(plan.graph.nodes[0].skill_name, "builtin.browser.basic")
        self.assertEqual(plan.graph.nodes[0].input_payload["url"], "inputs/blocked.html")

    def test_direct_line_replace_plan_bypasses_llm_for_explicit_file_request(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Update only the status line in configs/service.md so it says "
                    "status: reviewed. Keep the service line unchanged."
                ),
                preferred_skills=["builtin.file.basic"],
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(plan.graph.nodes[0].skill_name, "builtin.file.basic")
        self.assertEqual(plan.graph.nodes[0].input_payload["action"], "replace_line")
        self.assertEqual(plan.graph.nodes[0].input_payload["path"], "configs/service.md")
        self.assertEqual(plan.graph.nodes[0].input_payload["line_prefix"], "status:")
        self.assertEqual(plan.graph.nodes[0].input_payload["content"], "status: reviewed")

    def test_direct_line_replace_plan_uses_recent_artifact_path_for_same_file(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Actually change only the risk line in that same plan to risk: low. "
                    "Keep phase and owner unchanged."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
            recent_task_artifacts=[
                {
                    "task_id": "task_previous",
                    "status": "completed",
                    "plan_summary": "Append 'risk: medium' to plans/release.md",
                    "nodes": [],
                }
            ],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        payload = plan.graph.nodes[0].input_payload
        self.assertEqual(payload["action"], "replace_line")
        self.assertEqual(payload["path"], "plans/release.md")
        self.assertEqual(payload["line_prefix"], "risk:")
        self.assertEqual(payload["content"], "risk: low")

    def test_network_permission_plan_bypasses_llm_when_network_disallowed(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal="Fetch https://example.com and summarize the page title.",
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(response.request_id, "rule_based_network_permission_plan")
        node = plan.graph.nodes[0]
        self.assertEqual(node.skill_name, "builtin.http.client")
        self.assertEqual(node.input_payload["action"], "get")
        self.assertEqual(node.input_payload["url"], "https://example.com")

    def test_direct_exact_one_line_file_write_bypasses_llm(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Read the file from my previous message and create notes/summary.md "
                    "with exactly one line: copied_metric: 42."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        node = plan.graph.nodes[0]
        self.assertEqual(node.skill_name, "builtin.file.basic")
        self.assertEqual(
            node.input_payload,
            {
                "action": "write",
                "path": "notes/summary.md",
                "content": "copied_metric: 42.\n",
                "overwrite": False,
            },
        )

    def test_direct_multi_line_file_write_bypasses_llm(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Create reports/status.md in the workspace. The file must contain "
                    "these three lines: title: OmniAgent eval, status: ready, "
                    "owner: autonomous_eval. Then read the file back."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(
            plan.graph.nodes[0].input_payload,
            {
                "action": "write",
                "path": "reports/status.md",
                "content": (
                    "title: OmniAgent eval\n"
                    "status: ready\n"
                    "owner: autonomous_eval\n"
                ),
                "overwrite": False,
            },
        )
        self.assertEqual([node.node_id for node in plan.graph.nodes], [
            "node_write_file",
            "node_read_file",
            "node_finalize",
        ])
        self.assertEqual(plan.graph.nodes[1].input_payload, {
            "action": "read",
            "path": "reports/status.md",
        })
        self.assertEqual(plan.graph.nodes[1].dependencies, ["node_write_file"])
        self.assertEqual(plan.graph.nodes[2].dependencies, ["node_read_file"])

    def test_direct_exact_multi_line_file_write_bypasses_llm(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Create ops/alpha.md with exactly three lines: service: alpha, "
                    "owner: platform, severity: medium."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(
            plan.graph.nodes[0].input_payload,
            {
                "action": "write",
                "path": "ops/alpha.md",
                "content": "service: alpha\nowner: platform\nseverity: medium\n",
                "overwrite": False,
            },
        )

    def test_direct_first_file_replace_and_append_uses_oldest_recent_artifact(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "In the first service file, change only severity to severity: high "
                    "and append modified: yes. Do not change the second service file."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
            recent_task_artifacts=[
                {
                    "task_id": "task_beta",
                    "created_at": "2026-05-21T08:36:29+00:00",
                    "plan_summary": "Write exact text to ops/beta.md",
                    "nodes": [],
                },
                {
                    "task_id": "task_alpha",
                    "created_at": "2026-05-21T08:34:33+00:00",
                    "plan_summary": "Write exact text to ops/alpha.md",
                    "nodes": [],
                },
            ],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual([node.node_id for node in plan.graph.nodes], [
            "node_replace_line",
            "node_append_file",
            "node_finalize",
        ])
        self.assertEqual(
            plan.graph.nodes[0].input_payload,
            {
                "action": "replace_line",
                "path": "ops/alpha.md",
                "line_prefix": "severity:",
                "content": "severity: high",
            },
        )
        self.assertEqual(
            plan.graph.nodes[1].input_payload,
            {
                "action": "append",
                "path": "ops/alpha.md",
                "content": "modified: yes\n",
            },
        )

    def test_direct_field_report_file_write_bypasses_llm(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Now summarize the file I modified before that status question into "
                    "reports/latest_modified_service.md with selected_service: alpha, "
                    "selected_owner: platform, selected_severity: high, selected_modified: yes."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(
            plan.graph.nodes[0].input_payload,
            {
                "action": "write",
                "path": "reports/latest_modified_service.md",
                "content": (
                    "selected_service: alpha\n"
                    "selected_owner: platform\n"
                    "selected_severity: high\n"
                    "selected_modified: yes\n"
                ),
                "overwrite": False,
            },
        )

    def test_direct_exact_these_lines_reads_source_file_before_write(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Use relevant vector memory if available. Read inputs/incident_notes.md "
                    "and create reports/incident_brief.md with exactly these lines: "
                    "incident: checkout latency, priority: high, owner: platform, "
                    "followup: rollback checklist."
                ),
                preferred_skills=["builtin.file.basic"],
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual([node.node_id for node in plan.graph.nodes], [
            "node_read_source_file",
            "node_write_file",
            "node_finalize",
        ])
        self.assertEqual(plan.graph.nodes[0].input_payload, {
            "action": "read",
            "path": "inputs/incident_notes.md",
        })
        self.assertEqual(
            plan.graph.nodes[1].input_payload,
            {
                "action": "write",
                "path": "reports/incident_brief.md",
                "content": (
                    "incident: checkout latency\n"
                    "priority: high\n"
                    "owner: platform\n"
                    "followup: rollback checklist\n"
                ),
                "overwrite": False,
            },
        )
        self.assertEqual(plan.graph.nodes[1].dependencies, ["node_read_source_file"])

    def test_direct_report_must_contain_fields_reads_source_file_before_write(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Read inputs/release_checklist.md and create reports/release_readiness.md. "
                    "The report must contain release: mobile-2.1, owner: growth, "
                    "rollback: feature flag mobile_21, and launch_window: "
                    "2026-05-20 09:00 UTC. Do not omit any required field."
                ),
                preferred_skills=["builtin.file.basic"],
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual([node.node_id for node in plan.graph.nodes], [
            "node_read_source_file",
            "node_write_file",
            "node_finalize",
        ])
        self.assertEqual(
            plan.graph.nodes[1].input_payload,
            {
                "action": "write",
                "path": "reports/release_readiness.md",
                "content": (
                    "release: mobile-2.1\n"
                    "owner: growth\n"
                    "rollback: feature flag mobile_21\n"
                    "launch_window: 2026-05-20 09:00 UTC\n"
                ),
                "overwrite": False,
            },
        )

    def test_direct_append_line_plan_wins_over_previous_exact_write_goal(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Create notes/status_flow.md with exactly one line: step_one: created.\n"
                    "继续要求: Continue with that same file and append a second line: "
                    "step_two: continued."
                ),
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        node = plan.graph.nodes[0]
        self.assertEqual(node.skill_name, "builtin.file.basic")
        self.assertEqual(
            node.input_payload,
            {
                "action": "append",
                "path": "notes/status_flow.md",
                "content": "step_two: continued.\n",
            },
        )

    def test_direct_append_line_plan_accepts_same_file_phrase(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal="继续：append a second line to the same file: continued_target: current.",
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
            recent_task_artifacts=[
                {
                    "task_id": "task_previous",
                    "status": "completed",
                    "plan_summary": "Write exact text to notes/current_task.md",
                    "nodes": [],
                }
            ],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(
            plan.graph.nodes[0].input_payload,
            {
                "action": "append",
                "path": "notes/current_task.md",
                "content": "continued_target: current.\n",
            },
        )

    def test_direct_csv_report_plan_bypasses_llm_for_explicit_csv_sum(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Using the CSV skill, calculate total revenue from the revenue column "
                    "in inputs/sales_missing_column.csv and write reports/missing_revenue.md."
                ),
                preferred_skills=["builtin.data.csv"],
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        payload = plan.graph.nodes[0].input_payload
        self.assertEqual(plan.graph.nodes[0].skill_name, "builtin.data.csv")
        self.assertEqual(payload["action"], "report")
        self.assertEqual(payload["path"], "inputs/sales_missing_column.csv")
        self.assertEqual(payload["output_path"], "reports/missing_revenue.md")
        self.assertEqual(payload["metrics"][0]["name"], "total_revenue")
        self.assertEqual(payload["metrics"][0]["column"], "revenue")

    def test_direct_config_report_plan_bypasses_llm_for_preferred_config_skill(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Use builtin.data.config to read configs/service.toml. Write "
                    "reports/config_missing.md containing service_name: billing, "
                    "owner: platform, and retries: 3. If any required key is missing, "
                    "fail honestly and do not create the report."
                ),
                preferred_skills=["builtin.data.config"],
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(response.request_id, "rule_based_config_report_plan")
        node = plan.graph.nodes[0]
        self.assertEqual(node.skill_name, "builtin.data.config")
        self.assertEqual(node.input_payload["path"], "configs/service.toml")
        self.assertEqual(node.input_payload["output_path"], "reports/config_missing.md")
        self.assertEqual(
            node.input_payload["fields"],
            [
                {"label": "service_name", "query": "service.name"},
                {"label": "owner", "query": "owner"},
                {"label": "retries", "query": "retries"},
            ],
        )

    def test_direct_json_report_plan_bypasses_llm_for_preferred_json_skill(self) -> None:
        llm = MagicMock()
        prompts = MagicMock()
        planner = Planner(llm=llm, prompts=prompts)

        plan, response = planner.plan(
            task_request=TaskRequest(
                goal=(
                    "Use the available JSON skill to extract user.name from "
                    "inputs/broken.json and write reports/broken_profile.md. "
                    "If the JSON is invalid, fail honestly and do not create the report."
                ),
                preferred_skills=["builtin.data.json"],
            ),
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            memory_summaries=[],
            workflow_matches=[],
            vector_matches=[],
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=[],
        )

        llm.generate_structured.assert_not_called()
        self.assertEqual(response.provider, "rule_based")
        self.assertEqual(response.request_id, "rule_based_json_report_plan")
        node = plan.graph.nodes[0]
        self.assertEqual(node.skill_name, "builtin.data.json")
        self.assertEqual(
            node.input_payload,
            {
                "action": "report",
                "path": "inputs/broken.json",
                "fields": [{"label": "name", "query": "user.name"}],
                "output_path": "reports/broken_profile.md",
            },
        )


class LLMNativeExecutorUnitTest(unittest.TestCase):
    """Direct unit test for ``LLMNativeExecutor.execute``.

    Bypasses the full runtime so we can validate the executor in isolation:
    it should call the adapter with prompt_name=llm_native_v1, persist the
    response as a markdown artifact, and return a successful ExecutionResult
    with the text in ``output['content']``.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.task_dir = Path(self.tempdir.name) / "tasks" / "task_test"
        self.task_dir.mkdir(parents=True)

    def _make_context(self, *, goal: str, payload: dict | None = None) -> TaskRunContext:
        request = TaskRequest(
            goal=goal,
            payload=payload or {},
            executor="llm_native",
            metadata={"node_id": "node_action_1"},
        )
        task = Task(
            task_id=request.task_id,
            goal=goal,
            mode="semi_auto",
            source="cli",
            status=TaskStatus.RUNNING,
            executor="llm_native",
            skill_name=None,
            payload=request.payload,
            workspace_path=str(self.task_dir.parent.parent),
        )
        bootstrap = BootstrapContext(
            documents={"AGENT.md": "# agent rules\n使用中文回答"},
            memory_summaries=[],
            workflow_summaries=[],
        )
        return TaskRunContext(
            task=task,
            task_request=request,
            workspace_root=self.task_dir.parent.parent,
            task_dir=self.task_dir,
            bootstrap=bootstrap,
            loaded_skills={},
            sandbox=None,
        )

    def test_execute_returns_text_and_writes_artifact(self) -> None:
        adapter = StubLLMAdapter()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(goal="写一首关于春天的小诗")

        result = executor.execute(context)

        self.assertTrue(result.success)
        self.assertEqual(result.executor, "llm_native")
        self.assertIn("写一首关于春天的小诗", result.message)
        self.assertIn("content", result.output)
        self.assertTrue(result.output["content"])
        # Artifact written under task_dir/artifacts/<node_id>_response.md
        self.assertEqual(len(result.artifacts), 1)
        artifact_path = Path(result.artifacts[0].path)
        self.assertTrue(artifact_path.exists())
        self.assertEqual(artifact_path.name, "node_action_1_response.md")
        self.assertIn("写一首关于春天的小诗", artifact_path.read_text(encoding="utf-8"))

    def test_execute_accepts_prompt_override_from_payload(self) -> None:
        adapter = StubLLMAdapter()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(
            goal="春天主题",
            payload={"prompt": "请用七言绝句形式写春天"},
        )

        result = executor.execute(context)

        self.assertTrue(result.success)
        # StubLLMAdapter echoes the effective goal, so prompt override wins
        self.assertIn("七言绝句", result.message)

    def test_execute_fails_when_llm_returns_empty_response(self) -> None:
        class EmptyAdapter(StubLLMAdapter):
            def generate_structured(self, **kwargs):
                base = super().generate_structured(**kwargs)
                # Force an empty response to exercise the no-text fallback
                base.structured_output = {"response": "", "reasoning": ""}
                return base

        executor = LLMNativeExecutor(EmptyAdapter())
        context = self._make_context(goal="测试空响应")
        result = executor.execute(context)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("未返回可用文本", result.message)
        self.assertEqual(result.error_code, ErrorCode.EXECUTION_ERROR)

    def test_execute_refuses_fresh_data_task_without_external_context(self) -> None:
        adapter = MagicMock()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(
            goal="给我出一份本周周末在郑州游玩的攻略，考虑天气情况",
            payload={"prompt": "结合郑州5月中旬典型气候生成攻略"},
        )

        result = executor.execute(context)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("实时/最新外部信息", result.message)
        self.assertEqual(result.output["issue"], "missing_external_context")
        adapter.generate_structured.assert_not_called()

    def test_execute_accepts_fresh_data_task_with_dependency_outputs_context(self) -> None:
        class CapturingAdapter(StubLLMAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.request_payload = {}

            def generate_structured(self, **kwargs):
                self.request_payload = kwargs["request_payload"]
                return super().generate_structured(**kwargs)

        adapter = CapturingAdapter()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(
            goal="帮我看一下今天的天气情况",
            payload={
                "prompt": "将上一步获取的天气原始数据整理为清晰的中文天气报告",
                "dependency_outputs": {
                    "node_fetch_weather": {
                        "status": 200,
                        "body": "郑州今日天气：晴，温度25°C，湿度40%，东北风2级",
                    }
                },
            },
        )

        result = executor.execute(context)

        self.assertTrue(result.success)
        self.assertEqual(adapter.call_count, 1)
        self.assertIn("dependency_outputs", adapter.request_payload["context"])

    def test_execute_refuses_fresh_data_task_with_blocked_external_context(self) -> None:
        adapter = MagicMock()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(
            goal="搜索郑州本周末天气和最新游玩攻略",
            payload={
                "prompt": "根据网页内容整理攻略",
                "weather_content": "<html><title>百度安全验证</title></html>",
                "guide_content": "网络不给力，请稍后重试",
            },
        )

        result = executor.execute(context)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "failed")
        self.assertIn("安全验证", result.message)
        self.assertEqual(result.output["issue"], "blocked_external_context")
        adapter.generate_structured.assert_not_called()

    def test_execute_refuses_year_specific_review_task_without_external_context(self) -> None:
        adapter = MagicMock()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(
            goal="网上搜一下大家对 AirPods Max 的评价，时间要求必须是2026年的评价",
            payload={"prompt": "总结2026年的用户评价"},
        )

        result = executor.execute(context)

        self.assertFalse(result.success)
        self.assertEqual(result.output["issue"], "missing_external_context")
        adapter.generate_structured.assert_not_called()

    def test_execute_refuses_redirect_shell_external_context(self) -> None:
        adapter = MagicMock()
        executor = LLMNativeExecutor(adapter)
        context = self._make_context(
            goal="搜索郑州本周末天气和最新游玩攻略",
            payload={
                "prompt": "根据网页内容整理攻略",
                "guide_content": (
                    '<html><script>location.replace(location.href.replace("https://","http://"));</script>'
                    '<meta http-equiv="refresh" content="0;url=http://www.baidu.com/"></html>'
                ),
            },
        )

        result = executor.execute(context)

        self.assertFalse(result.success)
        self.assertEqual(result.output["issue"], "blocked_external_context")
        adapter.generate_structured.assert_not_called()


class LLMNativeExecutorRoutingTest(unittest.TestCase):
    """End-to-end test: a planned graph with ``executor_hint='llm_native'``
    should be routed through ``LLMNativeExecutor`` by the dispatcher without
    triggering code_execution permission (unlike the python wrapper path)."""

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

    def test_llm_native_node_routes_through_executor_and_stays_l1(self) -> None:
        gateway = self._build_gateway()
        plan = PlanResult(
            summary="生成一首关于春天的小诗",
            graph=TaskGraph(
                goal="写一首春天的诗",
                summary="llm_native 单节点",
                nodes=[
                    TaskNode(
                        node_id="node_action_1",
                        type=TaskNodeType.ACTION,
                        goal="写一首关于春天的小诗",
                        executor_hint="llm_native",
                        input_payload={},
                    ),
                    TaskNode(
                        node_id="node_finalize",
                        type=TaskNodeType.FINALIZE,
                        goal="汇总结果",
                        dependencies=["node_action_1"],
                    ),
                ],
            ),
            reasoning="pure text generation task",
            risk_level="low",
            required_capabilities=["llm_native"],
            decision="plan",
        )
        response = LLMResponse(
            provider="test",
            model="test-model",
            prompt_name="planner_v1",
            request_id="test_llm_native",
            raw_output="{}",
            structured_output=plan.to_dict(),
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, response)):
            report = gateway.submit(
                TaskRequest(
                    goal="写一首关于春天的小诗",
                    # L1 is deliberately too low for code_execution; if the
                    # runtime ever wraps this as python by mistake, we'll
                    # see a permission failure here instead of success.
                    constraints=TaskConstraints(max_permission=PermissionLevel.L1),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.COMPLETED, report.result.message)
        self.assertTrue(report.result.success)
        # First action node should have been executed via llm_native executor
        payload = gateway.show_task(report.task.task_id)
        action_node = next(n for n in payload["task_nodes"] if n.node_id == "node_action_1")
        self.assertEqual(action_node.status.value, "completed")
        # No code_execution permission should have been requested — llm_native
        # is the whole point of META-004: pure-text tasks skip L3.
        permission_names = [p["permission"] for p in payload["permissions"]]
        self.assertNotIn("code_execution", permission_names)


if __name__ == "__main__":
    unittest.main()

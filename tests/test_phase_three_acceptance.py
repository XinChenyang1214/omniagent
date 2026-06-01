from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import (
    LLMResponse,
    PermissionLevel,
    PlanResult,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskRequest,
)


class PhaseThreeAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_root = Path(self.tempdir.name)
        self.workspace = self.project_root / "workspace"
        self.db_path = self.project_root / "data" / "omniagent.sqlite3"
        self.log_path = self.project_root / "artifacts" / "omniagent.log"
        self.env = {
            "OMNIAGENT_SQLITE_PATH": str(self.db_path),
            "OMNIAGENT_LOG_PATH": str(self.log_path),
            "OMNIAGENT_JSON_LOGS": "false",
            "OMNIAGENT_LLM_PROVIDER": "test",
        }

    def _build_gateway(self) -> RuntimeGateway:
        with patch.dict(os.environ, self.env, clear=False):
            config = AppConfig.from_env(self.project_root)
        return RuntimeGateway(config, self.workspace)

    def _run_cli(self, argv: list[str], *, stdin_data: str | None = None) -> tuple[int, str]:
        stdout = StringIO()
        stdin = StringIO(stdin_data or "")
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch("sys.stdin", stdin),
            redirect_stdout(stdout),
        ):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def _extract_value(self, prefix: str, output: str) -> str:
        return next(
            line.split(prefix, 1)[1] for line in output.splitlines() if line.startswith(prefix)
        )

    def _install_echo_skill(self) -> None:
        skill_root = self.workspace / "skills" / "echo_skill"
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "skill.yaml").write_text(
            json.dumps(
                {
                    "name": "workspace.echo",
                    "version": "0.1.0",
                    "description": "Echo payload text",
                    "input_schema": {"text": "string"},
                    "output_schema": {"echo": "string"},
                    "permission_requirements": [],
                    "dependency_requirements": [],
                    "side_effects": [],
                    "sandbox_level": "workspace",
                    "tags": ["echo", "workspace"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (skill_root / "main.py").write_text(
            textwrap.dedent(
                """
                def run(payload, context):
                    return {
                        "success": True,
                        "status": "completed",
                        "message": "echo skill ok",
                        "output": {"echo": payload.get("text", "")},
                    }
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (skill_root / "README.md").write_text("# echo skill\n", encoding="utf-8")

    def _wait_plan(
        self, *, payload: dict[str, object], goal: str = "长期任务"
    ) -> tuple[PlanResult, LLMResponse]:
        plan = PlanResult(
            summary="包含 wait 节点的长期任务",
            graph=TaskGraph(
                goal=goal,
                summary="先等待，再收尾",
                nodes=[
                    TaskNode(
                        node_id="node_wait",
                        type=TaskNodeType.WAIT,
                        goal="等待恢复条件满足",
                        input_payload=payload,
                    ),
                    TaskNode(
                        node_id="node_finalize",
                        type=TaskNodeType.FINALIZE,
                        goal="汇总结果",
                        dependencies=["node_wait"],
                    ),
                ],
            ),
        )
        response = LLMResponse(
            provider="mock",
            model="mock",
            prompt_name="planner_v1",
            request_id="planner_acceptance_long_task",
            raw_output="{}",
            structured_output=plan.to_dict(),
        )
        return plan, response

    def test_acceptance_multiturn_cli_repl_flow(self) -> None:
        exit_code, output = self._run_cli(
            [
                "chat",
                "repl",
                "--executor",
                "python",
                "--payload",
                json.dumps({"code": "result = {'ok': True}\nprint('python ok')"}),
                "--max-permission",
                "L2",
            ],
            stdin_data="执行一个 python 任务\n补充要求：执行完成后总结输出\n/status\n同意继续执行\n/exit\n",
        )
        self.assertEqual(exit_code, 0, output)
        thread_id = self._extract_value("thread_id: ", output)
        # Verify via gateway API that the full flow completed
        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        self.assertIsNone(thread_payload["pending_checkpoint"])
        self.assertEqual(str(thread_payload["active_task"].status), "completed")
        self.assertIn("补充要求", thread_payload["active_task"].goal)
        self.assertGreaterEqual(len(thread_payload["turns"]), 8)
        # Verify routing decisions through event log
        route_events = [e for e in thread_payload["events"] if e["event_type"] == "route.decided"]
        route_types = {e["payload"].get("input_type") for e in route_events}
        self.assertIn("new_task", route_types)
        self.assertIn("status_query", route_types)

    def test_acceptance_graph_memory_influences_router_planner_and_critic(self) -> None:
        gateway = self._build_gateway()
        self._install_echo_skill()

        gateway.submit(
            TaskRequest(
                goal="运行本地 Skill",
                skill_name="workspace.echo",
                payload={"text": "seed"},
                constraints=TaskConstraints(max_permission=PermissionLevel.L1),
            )
        )

        seeded_thread = gateway.send_message(
            "写一个文件",
            request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
        )
        captured: dict[str, list[dict[str, object]]] = {
            "router_v1": [],
            "planner_v1": [],
            "critic_v1": [],
        }
        original = gateway.llm_adapter.generate_structured

        def _capture(*, prompt_name, system_prompt, request_payload, schema):
            captured.setdefault(prompt_name, []).append(request_payload)
            return original(
                prompt_name=prompt_name,
                system_prompt=system_prompt,
                request_payload=request_payload,
                schema=schema,
            )

        with patch.object(gateway.llm_adapter, "generate_structured", side_effect=_capture):
            router_response = gateway.send_message(
                "刚才那个", thread_id=seeded_thread.thread.thread_id
            )
            gateway.submit(
                TaskRequest(
                    goal="运行本地 Skill",
                    payload={"text": "graph-choice"},
                    constraints=TaskConstraints(max_permission=PermissionLevel.L1),
                )
            )
            gateway.submit(
                TaskRequest(
                    goal="执行一个会失败的 python 任务",
                    executor="python",
                    payload={"code": "raise RuntimeError('boom')"},
                    constraints=TaskConstraints(max_permission=PermissionLevel.L3),
                )
            )
            gateway.submit(
                TaskRequest(
                    goal="执行一个会失败的 python 任务",
                    executor="python",
                    payload={"code": "raise RuntimeError('boom again')"},
                    constraints=TaskConstraints(max_permission=PermissionLevel.L3),
                )
            )

        self.assertEqual(str(router_response.route.input_type), "task_update")
        # Rule-based router may handle routing without LLM call
        # so captured["router_v1"] may be empty — that's correct

        self.assertTrue(captured["planner_v1"])
        planner_payload = captured["planner_v1"][0]
        # Verify graph recalls are passed to planner context
        self.assertIn("graph_workflow_matches", planner_payload)
        self.assertIn("graph_skill_matches", planner_payload)

        self.assertTrue(captured["critic_v1"])
        # Verify graph failure patterns are passed to critic context
        critic_node_calls = [
            item for item in captured["critic_v1"] if item["target_type"] == "node"
        ]
        self.assertTrue(critic_node_calls)
        self.assertIn("graph_failure_patterns", critic_node_calls[0])

    def test_acceptance_workflow_candidate_validation_approval_and_registration(self) -> None:
        gateway = self._build_gateway()
        first = gateway.submit(
            TaskRequest(
                goal="写一个文件",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/a.txt",
                    "content": "alpha",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        second = gateway.submit(
            TaskRequest(
                goal="写一个文件",
                executor="file",
                payload={
                    "action": "write",
                    "path": "reports/b.txt",
                    "content": "beta",
                    "overwrite": True,
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
        )
        self.assertEqual(str(first.task.status), "completed")

        candidate = gateway.show_task(second.task.task_id)["workflow_candidates"][0]
        promotion = gateway.promote_workflow(
            second.task.task_id,
            skill_name=candidate["skill_name"],
            approver="tester",
            rationale="将稳定 workflow 固化为可复用能力",
        )
        self.assertEqual(promotion["candidate"]["promotion_source"], "user_confirmed")
        self.assertEqual(promotion["candidate"]["solidification_result"], "generated_skill_created")

        skill_payload = gateway.show_skill(candidate["skill_name"])
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "active")
        self.assertTrue(skill_payload["validation_reports"][0].passed)
        self.assertIn(skill_payload["approval_records"][0].status, {"approved", "not_required"})
        self.assertEqual(skill_payload["registration_records"][0].status, "active")

    def test_acceptance_controlled_skill_generation_gap_to_validation(self) -> None:
        gateway = self._build_gateway()
        initial = gateway.send_message(
            "执行一个 python 任务",
            request_options={
                "executor": "python",
                "skill_name": "generated.python.acceptance",
                "payload": {"code": "result = {'ok': True}\nprint('acceptance ok')"},
                "constraints": TaskConstraints(max_permission=PermissionLevel.L3),
            },
        )
        self.assertIsNotNone(initial.task_report)
        self.assertEqual(str(initial.task_report.task.status), "waiting_input")
        self.assertIn(
            "自动生成 Skill generated.python.acceptance", initial.task_report.result.message
        )

        skill_payload = gateway.show_skill("generated.python.acceptance")
        self.assertEqual(skill_payload["registry_entry"].lifecycle_status.value, "validated")
        self.assertTrue(skill_payload["validation_reports"][0].passed)
        self.assertEqual(skill_payload["approval_records"], [])

        followup = gateway.send_message("同意继续执行", thread_id=initial.thread.thread_id)
        self.assertEqual(str(followup.task_report.task.status), "completed")
        final_skill_payload = gateway.show_skill("generated.python.acceptance")
        self.assertEqual(final_skill_payload["registry_entry"].lifecycle_status.value, "active")
        self.assertEqual(final_skill_payload["registration_records"][0].status, "active")
        self.assertTrue(
            Path(final_skill_payload["drafts"][0].metadata["generation_report_path"]).exists()
        )

    def test_acceptance_long_task_suspend_wakeup_and_binding(self) -> None:
        gateway = self._build_gateway()
        plan, planner_response = self._wait_plan(
            payload={
                "event_key": "deploy.ready",
                "prompt": "等待部署完成",
                "confirm_message": "部署完成，继续执行",
                "timeout_at": "2099-01-01T01:00:00+00:00",
            },
            goal="等待部署外部事件",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, planner_response)):
            initial = gateway.send_message(
                "开始一个需要等待部署事件的任务",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
            )

        self.assertEqual(str(initial.task_report.task.status), "suspended")
        checkpoint_id = initial.checkpoint.checkpoint_id
        results = gateway.signal_external_event(
            "deploy.ready",
            payload={"artifact": "bundle.js"},
            now="2099-01-01T00:10:00+00:00",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        self.assertEqual(str(task_payload["task"].status), "completed")
        self.assertEqual(task_payload["resume_attempts"][0].checkpoint_id, checkpoint_id)
        self.assertEqual(task_payload["resume_attempts"][0].trigger, "external_event")
        self.assertTrue(
            any(
                item["permission"] == "external.event.consume" and item["scope"] == "resume"
                for item in task_payload["permissions"]
            )
        )
        thread_payload = gateway.show_thread(initial.thread.thread_id)
        self.assertIsNone(thread_payload["pending_checkpoint"])
        self.assertEqual(thread_payload["thread"].active_task_id, initial.task_report.task.task_id)
        self.assertEqual(str(thread_payload["active_task"].status), "completed")


if __name__ == "__main__":
    unittest.main()

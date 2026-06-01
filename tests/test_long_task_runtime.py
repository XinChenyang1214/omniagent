from __future__ import annotations

import os
import tempfile
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
)


class LongTaskRuntimeIntegrationTest(unittest.TestCase):
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

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with patch.dict(os.environ, self.env, clear=False), redirect_stdout(stdout):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

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
            request_id="planner_long_task",
            raw_output="{}",
            structured_output=plan.to_dict(),
        )
        return plan, response

    def test_timer_wait_node_can_suspend_and_resume_via_scheduler_cli(self) -> None:
        gateway = self._build_gateway()
        plan, planner_response = self._wait_plan(
            payload={
                "wait_until": "2099-01-01T00:05:00+00:00",
                "prompt": "等待定时唤醒",
                "confirm_message": "定时器唤醒成功",
                "timeout_at": "2099-01-01T00:30:00+00:00",
            },
            goal="等待一个未来时间点",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, planner_response)):
            initial = gateway.send_message(
                "开始一个需要稍后继续的任务",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
            )

        self.assertIsNotNone(initial.task_report)
        self.assertEqual(str(initial.task_report.task.status), "suspended")
        self.assertIsNotNone(initial.checkpoint)
        self.assertEqual(str(initial.checkpoint.checkpoint_type), "timer")

        exit_code, output = self._run_cli(["scheduler", "run", "--at", "2099-01-01T00:06:00+00:00"])
        self.assertEqual(exit_code, 0, output)
        self.assertIn("trigger: timer", output)
        self.assertIn("status: completed", output)

        fresh_gateway = self._build_gateway()
        task_payload = fresh_gateway.show_task(initial.task_report.task.task_id)
        self.assertEqual(str(task_payload["task"].status), "completed")
        wait_node = next(node for node in task_payload["task_nodes"] if node.type.value == "wait")
        self.assertEqual(wait_node.output["resume_trigger"], "timer")
        self.assertEqual(task_payload["resume_attempts"][0].trigger, "timer")
        thread_payload = fresh_gateway.show_thread(initial.thread.thread_id)
        self.assertIsNone(thread_payload["pending_checkpoint"])
        self.assertEqual(str(thread_payload["active_task"].status), "completed")

    def test_external_event_wait_node_can_resume_via_signal(self) -> None:
        gateway = self._build_gateway()
        plan, planner_response = self._wait_plan(
            payload={
                "event_key": "build.ready",
                "prompt": "等待构建完成事件",
                "confirm_message": "外部事件已到达",
                "timeout_at": "2099-01-01T01:00:00+00:00",
            },
            goal="等待外部构建结果",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, planner_response)):
            initial = gateway.send_message(
                "开始一个等待外部事件的任务",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
            )

        self.assertIsNotNone(initial.checkpoint)
        self.assertEqual(str(initial.checkpoint.checkpoint_type), "external_event")

        results = gateway.signal_external_event(
            "build.ready",
            payload={"artifact": "bundle.js"},
            now="2099-01-01T00:10:00+00:00",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        wait_node = next(node for node in task_payload["task_nodes"] if node.type.value == "wait")
        self.assertEqual(wait_node.output["resume_trigger"], "external_event")
        self.assertEqual(wait_node.output["event_payload"]["artifact"], "bundle.js")
        self.assertEqual(task_payload["resume_attempts"][0].trigger, "external_event")
        thread_payload = gateway.show_thread(initial.thread.thread_id)
        self.assertIsNone(thread_payload["pending_checkpoint"])
        self.assertIn(
            "external_event.received", [event["event_type"] for event in thread_payload["events"]]
        )

    def test_external_event_resume_can_be_blocked_by_permission_policy(self) -> None:
        gateway = self._build_gateway()
        plan, planner_response = self._wait_plan(
            payload={
                "event_key": "build.restricted",
                "prompt": "等待受限事件",
                "timeout_at": "2099-01-01T01:00:00+00:00",
            },
            goal="等待需要更高恢复权限的事件",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, planner_response)):
            initial = gateway.send_message(
                "开始一个受限外部事件任务",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L1)},
            )

        results = gateway.signal_external_event(
            "build.restricted",
            payload={"artifact": "restricted.zip"},
            now="2099-01-01T00:10:00+00:00",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "permission_denied")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        self.assertEqual(str(task_payload["task"].status), "suspended")
        blocked_permission = next(
            item
            for item in task_payload["permissions"]
            if item["permission"] == "external.event.consume"
        )
        self.assertEqual(blocked_permission["status"], "denied")
        self.assertEqual(blocked_permission["scope"], "resume")
        self.assertEqual(task_payload["resume_attempts"][0].status, "permission_denied")
        thread_payload = gateway.show_thread(initial.thread.thread_id)
        self.assertIsNotNone(thread_payload["pending_checkpoint"])

    def test_wait_timeout_marks_task_failed_and_records_notification(self) -> None:
        gateway = self._build_gateway()
        plan, planner_response = self._wait_plan(
            payload={
                "event_key": "deploy.ready",
                "prompt": "等待部署完成事件",
                "timeout_at": "2099-01-01T00:20:00+00:00",
            },
            goal="等待会超时的部署事件",
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, planner_response)):
            initial = gateway.send_message(
                "开始一个可能会超时的任务",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
            )

        results = gateway.run_scheduler(now="2099-01-01T00:30:00+00:00")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "timed_out")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        self.assertEqual(str(task_payload["task"].status), "failed")
        self.assertTrue(
            any(event.event_type == "task.resume_timeout" for event in task_payload["events"])
        )
        self.assertEqual(task_payload["resume_attempts"][0].status, "timed_out")
        thread_payload = gateway.show_thread(initial.thread.thread_id)
        self.assertIsNone(thread_payload["pending_checkpoint"])
        self.assertIn(
            "task.resume_timeout", [event["event_type"] for event in thread_payload["events"]]
        )


if __name__ == "__main__":
    unittest.main()

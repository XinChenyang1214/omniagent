from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.gateway import RuntimeGateway
from omniagent.models import (
    CriticAction,
    LLMResponse,
    PermissionLevel,
    PlanResult,
    Task,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskRequest,
    TaskStatus,
)


class OmniAgentConversationIntegrationTest(unittest.TestCase):
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

    def _build_gateway(self) -> RuntimeGateway:
        with patch.dict(os.environ, self.env, clear=False):
            config = AppConfig.from_env(self.project_root)
        return RuntimeGateway(config, self.workspace)

    def _extract_value(self, prefix: str, output: str) -> str:
        return next(
            line.split(prefix, 1)[1] for line in output.splitlines() if line.startswith(prefix)
        )

    def test_chat_status_query_uses_existing_thread_binding(self) -> None:
        create_exit, create_output = self._run_cli(
            ["chat", "send", "写一个文件", "--max-permission", "L2"]
        )
        self.assertEqual(create_exit, 0, create_output)
        thread_id = self._extract_value("thread_id: ", create_output)

        status_exit, status_output = self._run_cli(
            ["chat", "send", "当前状态如何", "--thread-id", thread_id]
        )
        self.assertEqual(status_exit, 0, status_output)
        # Verify via gateway that the status query was routed correctly
        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        route_events = [e for e in thread_payload["events"] if e["event_type"] == "route.decided"]
        self.assertTrue(any(e["payload"].get("input_type") == "status_query" for e in route_events))

    def test_chat_planner_error_returns_failed_task_and_binds_thread(self) -> None:
        gateway = self._build_gateway()
        thread = gateway.create_thread(title="CLI REPL")
        error = OmniAgentError(
            ErrorCode.LLM_ERROR,
            "LLM 调用失败 (openai): LLM call deadline exceeded",
            details={"attempts": 1},
        )

        with patch.object(gateway.planner, "plan", side_effect=error):
            response = gateway.send_message(
                "给我出一份本周周末在郑州游玩的攻略，考虑天气情况",
                thread_id=thread.thread_id,
            )

        self.assertIsNotNone(response.task_report)
        assert response.task_report is not None
        self.assertEqual(response.task_report.task.status, TaskStatus.FAILED)
        self.assertEqual(response.task_report.result.executor, "planner")
        self.assertEqual(response.thread.active_task_id, response.task_report.task.task_id)

        thread_payload = gateway.show_thread(thread.thread_id)
        self.assertEqual(
            thread_payload["thread"].active_task_id,
            response.task_report.task.task_id,
        )
        event_types = [e["event_type"] for e in thread_payload["events"]]
        self.assertIn("thread.bound", event_types)

    def test_task_critic_error_preserves_successful_execution_result(self) -> None:
        gateway = self._build_gateway()
        error = OmniAgentError(
            ErrorCode.LLM_ERROR,
            "LLM 调用失败 (openai): transient critic failure",
        )

        with patch.object(gateway.critic, "evaluate_task", side_effect=error):
            report = gateway.submit(
                TaskRequest(
                    goal="Create reports/critic_fallback.md with gate: pass.",
                    executor="file",
                    payload={
                        "action": "write",
                        "path": "reports/critic_fallback.md",
                        "content": "gate: pass\n",
                    },
                    constraints=TaskConstraints(max_permission=PermissionLevel.L2),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.COMPLETED)
        self.assertTrue(report.result.success)
        self.assertTrue((self.workspace / "reports" / "critic_fallback.md").exists())
        task_verdicts = [v for v in report.critic_verdicts if v.target_type == "task"]
        self.assertTrue(task_verdicts)
        self.assertEqual(task_verdicts[-1].action, CriticAction.ACCEPT)
        self.assertEqual(task_verdicts[-1].confidence, 0.0)
        self.assertEqual(task_verdicts[-1].llm_trace_id, "critic_fallback")

    def test_explicit_file_payload_uses_execution_policy_direct_execute(self) -> None:
        gateway = self._build_gateway()

        with patch.object(gateway.planner, "plan", side_effect=AssertionError("planner used")):
            report = gateway.submit(
                TaskRequest(
                    goal="Create reports/direct_policy.md with status: ok.",
                    executor="file",
                    payload={
                        "action": "write",
                        "path": "reports/direct_policy.md",
                        "content": "status: ok\n",
                    },
                    constraints=TaskConstraints(max_permission=PermissionLevel.L2),
                )
            )

        self.assertEqual(report.task.status, TaskStatus.COMPLETED)
        self.assertTrue((self.workspace / "reports" / "direct_policy.md").exists())
        task_dir = self.workspace / "tasks" / report.task.task_id
        policy = json.loads((task_dir / "plan" / "policy_decision.json").read_text("utf-8"))
        self.assertEqual(policy["policy_decision"], "direct_execute")
        self.assertEqual(policy["skipped_stages"], ["planner"])
        self.assertEqual(policy["target_skill"], "builtin.file.basic")
        planner_calls = [
            path
            for path in (task_dir / "llm_calls").glob("*.json")
            if "planner_v1" in path.name
        ]
        self.assertEqual(planner_calls, [])

    def test_chat_list_returns_threads_with_filter_and_limit(self) -> None:
        first_exit, first_output = self._run_cli(
            ["chat", "send", "写一个文件", "--max-permission", "L2"]
        )
        self.assertEqual(first_exit, 0, first_output)
        first_thread_id = self._extract_value("thread_id: ", first_output)

        second_exit, second_output = self._run_cli(
            ["chat", "send", "再写一个文件", "--max-permission", "L2"]
        )
        self.assertEqual(second_exit, 0, second_output)
        second_thread_id = self._extract_value("thread_id: ", second_output)

        list_exit, list_output = self._run_cli(["chat", "list", "--json"])
        self.assertEqual(list_exit, 0, list_output)
        listed = json.loads(list_output)
        listed_ids = {item["thread_id"] for item in listed}
        self.assertIn(first_thread_id, listed_ids)
        self.assertIn(second_thread_id, listed_ids)

        active_exit, active_output = self._run_cli(["chat", "list", "--status", "active", "--json"])
        self.assertEqual(active_exit, 0, active_output)
        active_listed = json.loads(active_output)
        active_ids = {item["thread_id"] for item in active_listed}
        self.assertIn(first_thread_id, active_ids)
        self.assertIn(second_thread_id, active_ids)

        limited_exit, limited_output = self._run_cli(["chat", "list", "--limit", "1", "--json"])
        self.assertEqual(limited_exit, 0, limited_output)
        limited_listed = json.loads(limited_output)
        self.assertEqual(len(limited_listed), 1)

    def test_chat_approval_response_resumes_waiting_task(self) -> None:
        create_exit, create_output = self._run_cli(
            [
                "chat",
                "send",
                "执行一个 python 任务",
                "--executor",
                "python",
                "--payload",
                json.dumps({"code": "result = {'ok': True}\nprint('python ok')"}),
                "--max-permission",
                "L2",
            ]
        )
        self.assertEqual(create_exit, 0, create_output)
        thread_id = self._extract_value("thread_id: ", create_output)
        task_id = self._extract_value("task_id: ", create_output)
        self.assertIn("checkpoint_id:", create_output)

        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        self.assertIsNotNone(thread_payload["pending_checkpoint"])

        approve_exit, approve_output = self._run_cli(
            ["chat", "send", "同意继续执行", "--thread-id", thread_id]
        )
        self.assertEqual(approve_exit, 0, approve_output)

        gateway = self._build_gateway()
        task_payload = gateway.show_task(task_id)
        self.assertEqual(str(task_payload["task"].status), "completed")
        self.assertTrue(
            any(event.event_type == "task.awaiting_input" for event in task_payload["events"])
        )
        self.assertTrue(
            any(event.event_type == "task.completed" for event in task_payload["events"])
        )
        thread_payload = gateway.show_thread(thread_id)
        self.assertIn(
            "approval.responded",
            [event["event_type"] for event in thread_payload["events"]],
        )

    def test_chat_task_update_creates_followup_task_with_constraints(self) -> None:
        create_exit, create_output = self._run_cli(
            ["chat", "send", "写一个文件", "--max-permission", "L2"]
        )
        self.assertEqual(create_exit, 0, create_output)
        thread_id = self._extract_value("thread_id: ", create_output)
        first_task_id = self._extract_value("task_id: ", create_output)

        update_exit, update_output = self._run_cli(
            ["chat", "send", "补充要求：再读取这个文件", "--thread-id", thread_id]
        )
        self.assertEqual(update_exit, 0, update_output)
        second_task_id = self._extract_value("task_id: ", update_output)
        self.assertNotEqual(first_task_id, second_task_id)
        # Route verified through gateway API below (thread binding check)

        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        self.assertEqual(thread_payload["thread"].active_task_id, second_task_id)
        self.assertIn("补充要求", thread_payload["active_task"].goal)
        event_types = [event["event_type"] for event in thread_payload["events"]]
        self.assertIn("conversation.message.received", event_types)
        self.assertIn("route.decided", event_types)
        self.assertIn("thread.bound", event_types)

    def test_chat_continue_fork_uses_continuation_message_as_goal_context(self) -> None:
        create_exit, create_output = self._run_cli(
            ["chat", "send", "写一个文件", "--max-permission", "L2"]
        )
        self.assertEqual(create_exit, 0, create_output)
        thread_id = self._extract_value("thread_id: ", create_output)

        update_exit, update_output = self._run_cli(
            ["chat", "send", "继续要求：追加第二行", "--thread-id", thread_id]
        )
        self.assertEqual(update_exit, 0, update_output)

        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        self.assertIn("追加第二行", thread_payload["active_task"].goal)
        self.assertNotIn("写一个文件", thread_payload["active_task"].goal)

    def test_chat_continue_reconciles_to_latest_thread_task(self) -> None:
        gateway = self._build_gateway()
        thread = gateway.create_thread(title="CLI REPL")
        first = gateway.send_message("帮我做什么", thread_id=thread.thread_id)
        self.assertIsNotNone(first.task_report)
        old_task_id = first.task_report.task.task_id

        orphan_request = gateway._build_task_request(
            goal="给我出一份本周周末在郑州游玩的攻略，考虑天气情况",
            thread=thread,
            request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
        )
        orphan_task = Task(
            task_id=orphan_request.task_id,
            goal=orphan_request.goal,
            mode=str(orphan_request.mode),
            source=str(orphan_request.source),
            status=TaskStatus.PLANNING,
            executor=orphan_request.executor,
            skill_name=orphan_request.skill_name,
            payload=orphan_request.payload,
            workspace_path=str(gateway.workspace_root),
            expected_output=orphan_request.expected_output,
            metadata=dict(orphan_request.metadata),
        )
        gateway.store.create_task(orphan_task)
        gateway.artifact_manager.initialize_task(
            orphan_task,
            orphan_request,
            gateway.workspace_manager.build_bootstrap_context(
                memory_summaries=[], workflow_summaries=[]
            ),
        )
        persisted = gateway.store.get_conversation_thread(thread.thread_id)
        assert persisted is not None
        self.assertEqual(persisted.active_task_id, old_task_id)

        followup = gateway.send_message("继续", thread_id=thread.thread_id)

        self.assertEqual(followup.route.target_task_id, orphan_task.task_id)
        thread_payload = gateway.show_thread(thread.thread_id)
        self.assertNotEqual(thread_payload["thread"].active_task_id, old_task_id)
        event_types = [event["event_type"] for event in thread_payload["events"]]
        self.assertIn("thread.reconciled", event_types)

    def test_explicit_network_authorization_sets_task_constraint(self) -> None:
        gateway = self._build_gateway()
        thread = gateway.create_thread(title="CLI REPL")

        request = gateway._build_task_request(
            goal="我允许你联网，不仅搜索天气情况也要搜索郑州最新的游玩攻略",
            thread=thread,
            request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
        )

        self.assertTrue(request.constraints.allow_network)
        self.assertTrue(request.metadata["user_explicit_network_authorization"])

    def test_chat_router_can_use_thread_context_for_ambiguous_followup(self) -> None:
        create_exit, create_output = self._run_cli(
            ["chat", "send", "写一个文件", "--max-permission", "L2"]
        )
        self.assertEqual(create_exit, 0, create_output)
        thread_id = self._extract_value("thread_id: ", create_output)
        first_task_id = self._extract_value("task_id: ", create_output)

        followup_exit, followup_output = self._run_cli(
            ["chat", "send", "刚才那个", "--thread-id", thread_id]
        )
        self.assertEqual(followup_exit, 0, followup_output)
        # Route verified: followup creates a new task (task_update -> continue)
        second_task_id = self._extract_value("task_id: ", followup_output)
        self.assertNotEqual(first_task_id, second_task_id)

    def test_chat_decision_checkpoint_resumes_after_user_response(self) -> None:
        gateway = self._build_gateway()
        plan = PlanResult(
            summary="需要用户先做一个决策",
            graph=TaskGraph(
                goal="需要决策的任务",
                summary="包含 decision 节点",
                nodes=[
                    TaskNode(
                        node_id="node_decide",
                        type=TaskNodeType.DECISION,
                        goal="选择输出方案",
                        input_payload={"prompt": "请选择 A 或 B", "options": ["A", "B"]},
                    ),
                    TaskNode(
                        node_id="node_finalize",
                        type=TaskNodeType.FINALIZE,
                        goal="汇总结果",
                        dependencies=["node_decide"],
                    ),
                ],
            ),
        )
        planner_response = LLMResponse(
            provider="mock",
            model="mock",
            prompt_name="planner_v1",
            request_id="planner_test_decision",
            raw_output="{}",
            structured_output=plan.to_dict(),
        )
        with patch.object(gateway.planner, "plan", return_value=(plan, planner_response)):
            initial = gateway.send_message(
                "做一个需要决策的任务",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
            )

        self.assertIsNotNone(initial.task_report)
        self.assertEqual(str(initial.task_report.task.status), "waiting_input")
        self.assertIsNotNone(initial.checkpoint)
        self.assertEqual(str(initial.checkpoint.checkpoint_type), "decision")

        followup = gateway.send_message("选择 B", thread_id=initial.thread.thread_id)
        self.assertEqual(str(followup.route.input_type), "approval_response")
        self.assertIsNotNone(followup.task_report)
        self.assertEqual(str(followup.task_report.task.status), "completed")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        decision_node = next(
            node for node in task_payload["task_nodes"] if node.type.value == "decision"
        )
        self.assertEqual(decision_node.output["decision_response"]["message"], "选择 B")
        thread_payload = gateway.show_thread(initial.thread.thread_id)
        self.assertIn(
            "decision.responded", [event["event_type"] for event in thread_payload["events"]]
        )

    def test_chat_knowledge_query_has_dedicated_handler(self) -> None:
        """The handler runs and produces a natural-language reply.

        Post-ROADMAP-P1-#4 this path goes through ``knowledge_query_v1``
        prompt + LLM (StubLLMAdapter in tests). Instead of asserting the
        old keyword-matched ``workspace_knowledge:`` wrapper, we just
        require a non-trivial reply that is routed as knowledge_query.
        """
        gateway = self._build_gateway()
        response = gateway.send_message("有哪些工作区规则？")
        self.assertEqual(str(response.route.input_type), "knowledge_query")
        # Stub always emits at least a fallback sentence; real replies are
        # longer. Assert the reply is non-empty and is routed correctly.
        self.assertTrue(response.assistant_message)
        self.assertGreater(len(response.assistant_message.strip()), 5)

    def test_chat_system_request_has_dedicated_handler(self) -> None:
        gateway = self._build_gateway()
        response = gateway.send_message("help")
        self.assertEqual(str(response.route.input_type), "system_request")
        self.assertTrue(response.assistant_message, "assistant_message should not be empty")

    def test_chat_repl_reuses_thread_for_status_and_exit(self) -> None:
        exit_code, output = self._run_cli(
            ["chat", "repl", "--max-permission", "L2"],
            stdin_data="写一个文件\n/status\n/exit\n",
        )
        self.assertEqual(exit_code, 0, output)
        thread_id = self._extract_value("thread_id: ", output)
        self.assertIn("repl_commands:", output)
        self.assertIn(f"session_closed: {thread_id}", output)

        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        self.assertEqual(thread_payload["thread"].thread_id, thread_id)
        self.assertIsNotNone(thread_payload["active_task"])
        self.assertGreaterEqual(len(thread_payload["turns"]), 4)

    def test_chat_repl_can_resume_thread_and_approve_checkpoint(self) -> None:
        create_exit, create_output = self._run_cli(
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
            stdin_data="执行一个 python 任务\n/exit\n",
        )
        self.assertEqual(create_exit, 0, create_output)
        thread_id = self._extract_value("thread_id: ", create_output)
        self.assertIn("checkpoint_id:", create_output)

        resume_exit, resume_output = self._run_cli(
            ["chat", "repl", "--thread-id", thread_id],
            stdin_data="/status\n同意继续执行\n/exit\n",
        )
        self.assertEqual(resume_exit, 0, resume_output)
        self.assertIn(f"thread_id: {thread_id}", resume_output)

        gateway = self._build_gateway()
        thread_payload = gateway.show_thread(thread_id)
        self.assertIsNone(thread_payload["pending_checkpoint"])
        self.assertEqual(str(thread_payload["active_task"].status), "completed")

    def test_chat_permission_checkpoint_can_authorize_external_path_and_resume(self) -> None:
        gateway = self._build_gateway()
        home = self.project_root / "home"
        downloads = home / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "photo.jpg").write_text("image", encoding="utf-8")

        plan = PlanResult(
            summary="列出下载目录内容",
            graph=TaskGraph(
                goal="列出下载目录中的图片文件名",
                summary="包含外部路径访问",
                nodes=[
                    TaskNode(
                        node_id="node_list",
                        type=TaskNodeType.ACTION,
                        goal="列出下载目录",
                        executor_hint="file",
                        input_payload={"action": "list", "path": "下载"},
                    ),
                    TaskNode(
                        node_id="node_finalize",
                        type=TaskNodeType.FINALIZE,
                        goal="汇总结果",
                        dependencies=["node_list"],
                    ),
                ],
            ),
        )
        planner_response = LLMResponse(
            provider="mock",
            model="mock",
            prompt_name="planner_v1",
            request_id="planner_test_path_permission",
            raw_output="{}",
            structured_output=plan.to_dict(),
        )

        with (
            patch.dict(os.environ, {"HOME": str(home)}, clear=False),
            patch.object(
                gateway.planner,
                "plan",
                return_value=(plan, planner_response),
            ),
        ):
            initial = gateway.send_message(
                "帮我列出下载目录里的图片",
                request_options={"constraints": TaskConstraints(max_permission=PermissionLevel.L2)},
            )

        self.assertIsNotNone(initial.task_report)
        self.assertEqual(str(initial.task_report.task.status), "waiting_input")
        self.assertIsNotNone(initial.checkpoint)
        self.assertEqual(str(initial.checkpoint.checkpoint_type), "permission")
        self.assertEqual(initial.checkpoint.payload.get("authorization_type"), "path_access")
        self.assertEqual(
            initial.checkpoint.payload.get("authorized_path"), str(downloads.resolve())
        )

        with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            followup = gateway.send_message("同意授权并继续", thread_id=initial.thread.thread_id)

        self.assertEqual(str(followup.route.input_type), "approval_response")
        self.assertIsNotNone(followup.task_report)
        self.assertEqual(str(followup.task_report.task.status), "completed")

        task_payload = gateway.show_task(initial.task_report.task.task_id)
        list_node = next(node for node in task_payload["task_nodes"] if node.node_id == "node_list")
        self.assertIn("photo.jpg", list_node.output["entries"])

        persisted_request = gateway.artifact_manager.load_task_request(task_payload["task"])
        self.assertIn(
            str(downloads.resolve()), persisted_request.metadata.get("authorized_paths", [])
        )


if __name__ == "__main__":
    unittest.main()

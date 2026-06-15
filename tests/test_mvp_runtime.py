from __future__ import annotations

import json
import os
import sys
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
from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest


class OmniAgentPhaseTwoIntegrationTest(unittest.TestCase):
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

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with patch.dict(os.environ, self.env, clear=False), redirect_stdout(stdout):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def _build_gateway(self) -> RuntimeGateway:
        with patch.dict(os.environ, self.env, clear=False):
            config = AppConfig.from_env(self.project_root)
        return RuntimeGateway(config, self.workspace)

    def _extract_task_id(self, output: str) -> str:
        return next(
            line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("task_id: ")
        )

    def test_multistep_task_generates_task_graph_and_executes(self) -> None:
        exit_code, output = self._run_cli(
            ["run", "先写一个文件然后读取这个文件", "--max-permission", "L2"]
        )
        self.assertEqual(exit_code, 0, output)
        task_id = self._extract_task_id(output)

        gateway = self._build_gateway()
        payload = gateway.show_task(task_id)
        graph = payload["task_graph"]
        self.assertIsNotNone(graph)
        self.assertGreaterEqual(len(graph.nodes), 2)
        action_nodes = [node for node in payload["task_nodes"] if node.type.value == "action"]
        self.assertTrue(all(node.status.value == "completed" for node in action_nodes))
        self.assertTrue(any(event.event_type == "task.node.started" for event in payload["events"]))
        self.assertTrue(
            any(event.event_type == "task.node.completed" for event in payload["events"])
        )
        self.assertIn("event: task.node.started", output)
        self.assertIn("event: task.node.completed", output)

    def test_critic_triggers_retry_and_replan_on_failure(self) -> None:
        exit_code, output = self._run_cli(
            [
                "run",
                "执行一个会失败的 python 任务",
                "--executor",
                "python",
                "--payload",
                json.dumps({"code": "raise RuntimeError('boom')"}),
                "--max-permission",
                "L3",
            ]
        )
        self.assertEqual(exit_code, 1, output)
        task_id = self._extract_task_id(output)

        gateway = self._build_gateway()
        payload = gateway.show_task(task_id)
        verdict_actions = [verdict.action.value for verdict in payload["critic_verdicts"]]
        self.assertIn("retry", verdict_actions)
        self.assertIn("replan", verdict_actions)
        self.assertTrue(any("_replan_" in node.node_id for node in payload["task_nodes"]))
        self.assertTrue(
            any(event.event_type == "task.node.retrying" for event in payload["events"])
        )
        self.assertTrue(
            any(event.event_type == "task.node.replanned" for event in payload["events"])
        )

    def test_planner_uses_workflow_and_vector_recall(self) -> None:
        first_exit, _ = self._run_cli(["run", "写一个历史文件", "--max-permission", "L2"])
        self.assertEqual(first_exit, 0)

        second_exit, second_output = self._run_cli(
            ["run", "再次写一个历史文件然后读取这个文件", "--max-permission", "L2"]
        )
        self.assertEqual(second_exit, 0, second_output)
        task_id = self._extract_task_id(second_output)

        gateway = self._build_gateway()
        payload = gateway.show_task(task_id)
        self.assertTrue(payload["task"].plan_summary, "plan_summary should not be empty")
        first_action = next(node for node in payload["task_nodes"] if node.type.value == "action")
        total_hints = first_action.metadata.get(
            "workflow_hint_count", 0
        ) + first_action.metadata.get("vector_hint_count", 0)
        self.assertGreaterEqual(total_hints, 1)

    def test_skill_registry_selection_and_workspace_skill_execution(self) -> None:
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
            ),
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

        exit_code, output = self._run_cli(
            [
                "run",
                "运行本地 Skill",
                "--skill",
                "workspace.echo",
                "--payload",
                json.dumps({"text": "hello skill"}),
                "--max-permission",
                "L1",
            ]
        )
        self.assertEqual(exit_code, 0, output)
        task_id = self._extract_task_id(output)

        gateway = self._build_gateway()
        payload = gateway.show_task(task_id)
        action_node = next(node for node in payload["task_nodes"] if node.type.value == "action")
        self.assertEqual(action_node.metadata.get("selected_skill"), "workspace.echo")
        self.assertEqual(action_node.output["echo"], "hello skill")
        registry = {entry.skill_name: entry for entry in payload["skill_registry"]}
        self.assertIn("workspace.echo", registry)
        self.assertGreaterEqual(registry["workspace.echo"].success_count, 1)

        # Verify the first task's skill is registered and has execution stats
        registry = {entry.skill_name: entry for entry in payload["skill_registry"]}
        self.assertIn("workspace.echo", registry)
        self.assertGreaterEqual(registry["workspace.echo"].success_count, 1)

    def test_cli_can_confirm_workflow_promotion(self) -> None:
        first_exit, _ = self._run_cli(
            [
                "run",
                "写一个文件",
                "--executor",
                "file",
                "--payload",
                json.dumps(
                    {
                        "action": "write",
                        "path": "reports/a.txt",
                        "content": "alpha",
                        "overwrite": True,
                    }
                ),
                "--max-permission",
                "L2",
            ]
        )
        self.assertEqual(first_exit, 0)
        second_exit, second_output = self._run_cli(
            [
                "run",
                "写一个文件",
                "--executor",
                "file",
                "--payload",
                json.dumps(
                    {
                        "action": "write",
                        "path": "reports/b.txt",
                        "content": "beta",
                        "overwrite": True,
                    }
                ),
                "--max-permission",
                "L2",
            ]
        )
        self.assertEqual(second_exit, 0, second_output)
        task_id = self._extract_task_id(second_output)

        promote_exit, promote_output = self._run_cli(
            [
                "workflow",
                "promote",
                task_id,
                "--approver",
                "tester",
                "--rationale",
                "需要将这个稳定 workflow 作为可复用能力保留",
            ]
        )
        self.assertEqual(promote_exit, 0, promote_output)
        self.assertIn("promotion_source: user_confirmed", promote_output)
        self.assertIn("promotion_confirmed_by: tester", promote_output)
        self.assertIn("solidification_result: blocked_requires_habit", promote_output)
        self.assertNotIn("generated_skill_status: active", promote_output)

        gateway = self._build_gateway()
        payload = gateway.show_task(task_id)
        candidate = payload["workflow_candidates"][0]
        self.assertEqual(candidate["promotion_source"], "user_confirmed")
        self.assertTrue(candidate["promotion_confirmed"])
        self.assertEqual(candidate["promotion_confirmed_by"], "tester")
        self.assertEqual(candidate["solidification_result"], "blocked_requires_habit")

    def test_python_workspace_sandbox_blocks_external_commands_but_process_skill_can_run(
        self,
    ) -> None:
        gateway = self._build_gateway()
        blocked = gateway.submit(
            TaskRequest(
                goal="在默认 python sandbox 中执行外部命令",
                executor="python",
                payload={
                    "code": "\n".join(
                        [
                            "import subprocess",
                            "completed = subprocess.run(['echo', 'blocked'], capture_output=True, text=True)",
                            "result = completed.stdout.strip()",
                        ]
                    )
                },
                constraints=TaskConstraints(max_permission=PermissionLevel.L4),
            )
        )
        self.assertEqual(str(blocked.task.status), "failed")
        self.assertIn("sandbox profile 不允许外部命令执行", blocked.result.message)

        skill_root = self.workspace / "skills" / "process_runner"
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "skill.yaml").write_text(
            json.dumps(
                {
                    "name": "workspace.process.runner",
                    "version": "0.1.0",
                    "description": "Run a subprocess inside process sandbox",
                    "input_schema": {},
                    "output_schema": {"result": "string"},
                    "permission_requirements": ["code_execution", "system.exec"],
                    "dependency_requirements": [],
                    "side_effects": [],
                    "sandbox_level": "process",
                    "tags": ["python", "process"],
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
                    response = context["sandbox"].run_python(
                        "import subprocess\\n"
                        "completed = subprocess.run(['echo', 'process-ok'], capture_output=True, text=True)\\n"
                        "result = completed.stdout.strip()\\n"
                    )
                    return {
                        "success": bool(response.get("ok")),
                        "status": "completed" if response.get("ok") else "failed",
                        "message": "process sandbox ok" if response.get("ok") else response.get("error", "failed"),
                        "output": {"result": response.get("result")},
                    }
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (skill_root / "README.md").write_text("# process runner\n", encoding="utf-8")

        allowed = gateway.submit(
            TaskRequest(
                goal="运行 process sandbox skill",
                executor="python",
                skill_name="workspace.process.runner",
                constraints=TaskConstraints(max_permission=PermissionLevel.L4),
            )
        )
        self.assertEqual(str(allowed.task.status), "completed")
        allowed_payload = gateway.show_task(allowed.task.task_id)
        action_node = next(
            node for node in allowed_payload["task_nodes"] if node.type.value == "action"
        )
        self.assertEqual(action_node.output["result"], "process-ok")
        system_exec = next(
            item for item in allowed_payload["permissions"] if item["permission"] == "system.exec"
        )
        self.assertEqual(system_exec["sandbox_level"], "process")

    def test_container_skill_uses_dedicated_python_backend(self) -> None:
        gateway = self._build_gateway()
        skill_root = self.workspace / "skills" / "container_runner"
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "skill.yaml").write_text(
            json.dumps(
                {
                    "name": "workspace.container.runner",
                    "version": "0.1.0",
                    "description": "Run Python code inside container sandbox",
                    "input_schema": {},
                    "output_schema": {"backend": "object", "python": "object"},
                    "permission_requirements": ["code_execution"],
                    "dependency_requirements": [],
                    "side_effects": [],
                    "sandbox_level": "container",
                    "tags": ["python", "container"],
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
                    backend = context["sandbox"].describe()
                    response = context["sandbox"].run_python(
                        "from pathlib import Path\\n"
                        "import sys\\n"
                        "Path('generated').mkdir(exist_ok=True)\\n"
                        "Path('generated/container-backend.txt').write_text('container-ok', encoding='utf-8')\\n"
                        "result = {\\n"
                        "    'executable': sys.executable,\\n"
                        "    'prefix': sys.prefix,\\n"
                        "    'base_prefix': sys.base_prefix,\\n"
                        "    'cwd': str(Path.cwd()),\\n"
                        "}\\n"
                    )
                    return {
                        "success": bool(response.get("ok")),
                        "status": "completed" if response.get("ok") else "failed",
                        "message": "container sandbox ok" if response.get("ok") else response.get("error", "failed"),
                        "output": {
                            "backend": backend,
                            "python": response.get("result"),
                        },
                    }
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (skill_root / "README.md").write_text("# container runner\n", encoding="utf-8")

        allowed = gateway.submit(
            TaskRequest(
                goal="运行 container sandbox skill",
                skill_name="workspace.container.runner",
                constraints=TaskConstraints(max_permission=PermissionLevel.L4),
            )
        )
        self.assertEqual(str(allowed.task.status), "completed")

        allowed_payload = gateway.show_task(allowed.task.task_id)
        action_node = next(
            node for node in allowed_payload["task_nodes"] if node.type.value == "action"
        )
        backend = action_node.output["backend"]
        python_runtime = action_node.output["python"]
        self.assertEqual(backend["backend_name"], "container")
        self.assertNotEqual(backend["runtime_root"], str(self.workspace.resolve()))
        self.assertEqual(python_runtime["cwd"], backend["runtime_root"])
        self.assertEqual(python_runtime["executable"], backend["python_executable"])
        self.assertNotEqual(
            Path(python_runtime["executable"]).resolve(), Path(sys.executable).resolve()
        )
        self.assertNotEqual(python_runtime["prefix"], python_runtime["base_prefix"])
        self.assertTrue((self.workspace / "generated" / "container-backend.txt").exists())
        self.assertEqual(
            (self.workspace / "generated" / "container-backend.txt").read_text(encoding="utf-8"),
            "container-ok",
        )
        self.assertFalse((self.workspace / ".omniagent_container_venv").exists())
        code_exec = next(
            item
            for item in allowed_payload["permissions"]
            if item["permission"] == "code_execution"
        )
        self.assertEqual(code_exec["sandbox_level"], "container")


if __name__ == "__main__":
    unittest.main()

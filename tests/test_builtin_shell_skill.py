from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.models import TaskNode, TaskNodeType
from omniagent.sandbox import build_sandbox
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore


class ShellSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "note.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        (self.workspace / "large.txt").write_text("abcdef\n", encoding="utf-8")
        self.loader = SkillLoader(self.workspace)
        self.definitions = self.loader.load_all()

    def _run(
        self,
        payload: dict,
        *,
        allow_external_commands: bool = True,
        level: str = "workspace",
    ):
        sandbox = build_sandbox(
            self.workspace,
            level=level,
            allow_external_commands=allow_external_commands,
        )
        self.addCleanup(sandbox.close)
        runner = self.loader.load_runner(self.definitions["builtin.shell.safe"])
        return runner(payload, {"sandbox": sandbox})

    def test_shell_contract_loads_run_action(self) -> None:
        metadata = self.definitions["builtin.shell.safe"].metadata
        contracts = metadata.contracts

        self.assertEqual([contract.action for contract in contracts], ["run"])
        self.assertIn("argv", contracts[0].required_input_fields())
        self.assertIn("system.exec", metadata.permission_requirements)
        self.assertEqual(metadata.sandbox_level, "process")

    def test_registry_selects_shell_skill_by_tag_and_action(self) -> None:
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(self.definitions)
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="count lines",
            required_skills=["shell"],
            input_payload={"action": "run", "argv": ["wc", "-l", "note.txt"]},
        )

        selection = registry.select(node)

        self.assertEqual(selection.selected_skill, "builtin.shell.safe")

    def test_external_commands_disabled_returns_permission_error_without_subprocess(self) -> None:
        with patch("subprocess.run") as run:
            result = self._run(
                {"action": "run", "argv": ["ls"]},
                allow_external_commands=False,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "permission_denied")
        run.assert_not_called()

    def test_argv_must_be_array(self) -> None:
        with patch("subprocess.run") as run:
            result = self._run({"action": "run", "argv": "ls"})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "validation_error")
        run.assert_not_called()

    def test_non_whitelisted_command_is_rejected(self) -> None:
        with patch("subprocess.run") as run:
            result = self._run({"action": "run", "argv": ["python", "-V"]})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "validation_error")
        self.assertIn("not allowed", result["message"])
        run.assert_not_called()

    def test_command_path_is_rejected(self) -> None:
        with patch("subprocess.run") as run:
            result = self._run({"action": "run", "argv": ["/bin/ls"]})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "validation_error")
        run.assert_not_called()

    def test_cwd_outside_workspace_is_rejected(self) -> None:
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()

        with patch("subprocess.run") as run:
            result = self._run({"action": "run", "argv": ["ls"], "cwd": str(outside)})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "permission_denied")
        run.assert_not_called()

    def test_whitelisted_command_runs_with_argv_and_shell_false(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["cat", "note.txt"],
                returncode=0,
                stdout="alpha\nbeta\n",
                stderr="",
            )
            result = self._run({"action": "run", "argv": ["cat", "note.txt"], "timeout": 2})

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["stdout"], "alpha\nbeta\n")
        kwargs = run.call_args.kwargs
        self.assertEqual(run.call_args.args[0], ["cat", "note.txt"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertTrue(str(kwargs["cwd"]).endswith("workspace"))

    def test_actual_cat_command_reads_workspace_file(self) -> None:
        result = self._run({"action": "run", "argv": ["cat", "note.txt"]})

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["returncode"], 0)
        self.assertEqual(result["output"]["stdout"], "alpha\nbeta\n")

    def test_stdout_is_truncated_stably(self) -> None:
        result = self._run(
            {
                "action": "run",
                "argv": ["cat", "large.txt"],
                "max_output_chars": 3,
            }
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["stdout"], "abc")
        self.assertTrue(result["output"]["stdout_truncated"])
        self.assertEqual(result["output"]["stdout_size"], 7)

    def test_stderr_is_truncated_for_failed_command(self) -> None:
        result = self._run(
            {
                "action": "run",
                "argv": ["grep", "x", "missing.txt"],
                "max_output_chars": 4,
            }
        )

        self.assertFalse(result["success"])
        self.assertNotEqual(result["output"]["returncode"], 0)
        self.assertTrue(result["output"]["stderr_truncated"])
        self.assertEqual(len(result["output"]["stderr"]), 4)

    def test_timeout_returns_execution_failure_with_partial_output(self) -> None:
        with patch("subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(
                cmd=["cat", "note.txt"],
                timeout=0.1,
                output="partial stdout",
                stderr="partial stderr",
            )
            result = self._run(
                {
                    "action": "run",
                    "argv": ["cat", "note.txt"],
                    "timeout": 0.1,
                    "max_output_chars": 7,
                }
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "execution_error")
        self.assertTrue(result["output"]["timed_out"])
        self.assertEqual(result["output"]["stdout"], "partial")
        self.assertTrue(result["output"]["stdout_truncated"])


if __name__ == "__main__":
    unittest.main()

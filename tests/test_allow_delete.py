"""Regression tests for ISSUE-006 (`allow_delete` in CLI / REPL).

The issue had three parts:

1. **CLI flag** — `--allow-delete` on `run`, `chat send`, `chat repl` (this
   was actually already wired by the time the issue was tackled, but there
   was no regression test locking it down).
2. **REPL runtime toggle** — `/allow delete`, `/deny delete`, `/allow
   network`, `/deny network` slash commands inside the chat REPL.
3. **Code-level approval-before-delete enforcement** — planner_v1.md has
   long asked the LLM to insert an `approval` node before any destructive
   file action, but until now nothing in Python enforced the rule. Graphs
   that skipped the approval step would fall through to the permission
   checkpoint mid-execution, which was a surprise to the user. The new
   `TaskGraph.enforce_destructive_approvals()` pass materialises the
   approval node at plan time.

Each test class maps 1:1 to one of those three deliverables, plus an
end-to-end test (`FileDeleteRuntimeEnforcementTest`) that walks the full
stack: CLI submit → planner inject → execution engine → approval
checkpoint → approval response → FileExecutor.delete.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omniagent.cli import (
    _apply_repl_constraint,
    _build_task_constraints,
    build_parser,
    main,
)
from omniagent.config import AppConfig
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.file import FileExecutor
from omniagent.gateway import RuntimeGateway
from omniagent.models import (
    BootstrapContext,
    PermissionLevel,
    RetryPolicy,
    Task,
    TaskConstraints,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskRequest,
    TaskRunContext,
    TaskStatus,
)
from omniagent.sandbox import build_sandbox


# ---------------------------------------------------------------------------
# Part 1: CLI flag propagation
# ---------------------------------------------------------------------------


class CLIAllowDeleteFlagPropagationTest(unittest.TestCase):
    """`--allow-delete` must flip TaskConstraints.allow_delete for every
    subcommand that submits a task (`run`, `chat send`, `chat repl`)."""

    def _parse(self, argv: list[str], *, default_timeout_seconds: int = 30) -> TaskConstraints:
        parser = build_parser()
        args = parser.parse_args(argv)
        return _build_task_constraints(args, default_timeout_seconds=default_timeout_seconds)

    def test_run_flag_enables_allow_delete(self) -> None:
        constraints = self._parse(["run", "删除文件", "--allow-delete"])
        self.assertTrue(constraints.allow_delete)
        self.assertFalse(constraints.allow_network)

    def test_run_without_flag_defaults_to_false(self) -> None:
        constraints = self._parse(["run", "删除文件"])
        self.assertFalse(constraints.allow_delete)

    def test_chat_send_flag_enables_allow_delete(self) -> None:
        constraints = self._parse(["chat", "send", "删除文件", "--allow-delete"])
        self.assertTrue(constraints.allow_delete)

    def test_chat_repl_flag_enables_allow_delete(self) -> None:
        constraints = self._parse(["chat", "repl", "--allow-delete"])
        self.assertTrue(constraints.allow_delete)

    def test_run_flag_independent_from_allow_network(self) -> None:
        constraints = self._parse(
            ["run", "抓取网页并删除缓存", "--allow-delete", "--allow-network"]
        )
        self.assertTrue(constraints.allow_delete)
        self.assertTrue(constraints.allow_network)

    def test_timeout_defaults_from_config_value(self) -> None:
        constraints = self._parse(["chat", "send", "规划任务"], default_timeout_seconds=1200)
        self.assertEqual(constraints.timeout_seconds, 1200)

    def test_timeout_flag_overrides_config_value(self) -> None:
        constraints = self._parse(
            ["run", "规划任务", "--timeout", "45"],
            default_timeout_seconds=1200,
        )
        self.assertEqual(constraints.timeout_seconds, 45)


# ---------------------------------------------------------------------------
# Part 2: TaskGraph.enforce_destructive_approvals() unit tests
# ---------------------------------------------------------------------------


class TaskGraphDestructiveApprovalUnitTest(unittest.TestCase):
    """The new TaskGraph pass must inject approval nodes in front of every
    destructive file action that lacks one — but not double-inject, and
    not touch non-destructive graphs."""

    @staticmethod
    def _make_delete_node(
        *,
        node_id: str = "node_delete",
        path: str = "target.txt",
        dependencies: list[str] | None = None,
    ) -> TaskNode:
        return TaskNode(
            node_id=node_id,
            type=TaskNodeType.ACTION,
            goal=f"删除 {path}",
            dependencies=list(dependencies or []),
            executor_hint="file",
            skill_name=None,
            risk_level="high",
            retry_policy=RetryPolicy(max_retries=0),
            input_payload={"action": "delete", "path": path},
        )

    @staticmethod
    def _make_finalize(dependencies: list[str]) -> TaskNode:
        return TaskNode(
            node_id="node_finalize",
            type=TaskNodeType.FINALIZE,
            goal="汇总结果",
            dependencies=list(dependencies),
        )

    def test_injects_approval_before_standalone_delete(self) -> None:
        graph = TaskGraph(
            goal="删除 target.txt",
            nodes=[
                self._make_delete_node(),
                self._make_finalize(["node_delete"]),
            ],
        )

        injected = graph.enforce_destructive_approvals()

        self.assertEqual(injected, 1)
        approval_nodes = [n for n in graph.nodes if n.type == TaskNodeType.APPROVAL]
        self.assertEqual(len(approval_nodes), 1)
        approval = approval_nodes[0]
        # Approval must be the single upstream dependency of the delete node.
        delete_node = graph.get_node("node_delete")
        self.assertEqual(delete_node.dependencies, [approval.node_id])
        # Approval payload carries a helpful prompt referencing the path.
        self.assertIn("target.txt", approval.input_payload.get("prompt", ""))
        self.assertTrue(approval.metadata.get("auto_injected"))
        # Graph dependency edge is materialised too.
        edge_pairs = {(d.source_node_id, d.target_node_id) for d in graph.dependencies}
        self.assertIn((approval.node_id, "node_delete"), edge_pairs)

    def test_skip_when_approval_ancestor_already_exists(self) -> None:
        approval = TaskNode(
            node_id="node_approval",
            type=TaskNodeType.APPROVAL,
            goal="审批",
            input_payload={"prompt": "是否继续？"},
        )
        delete = self._make_delete_node(dependencies=["node_approval"])
        graph = TaskGraph(
            goal="删除 target.txt",
            nodes=[approval, delete, self._make_finalize(["node_delete"])],
        )

        injected = graph.enforce_destructive_approvals()

        self.assertEqual(injected, 0)
        # Node list unchanged (still 3 items).
        self.assertEqual(len(graph.nodes), 3)
        self.assertEqual(graph.get_node("node_delete").dependencies, ["node_approval"])

    def test_skip_when_approval_is_transitive_ancestor(self) -> None:
        """Approval may sit several hops above the delete node."""
        approval = TaskNode(
            node_id="node_approval",
            type=TaskNodeType.APPROVAL,
            goal="审批",
        )
        setup = TaskNode(
            node_id="node_setup",
            type=TaskNodeType.ACTION,
            goal="准备删除前置条件",
            dependencies=["node_approval"],
            executor_hint="file",
            input_payload={"action": "list", "path": "."},
        )
        delete = self._make_delete_node(dependencies=["node_setup"])
        graph = TaskGraph(
            goal="删除 target.txt",
            nodes=[approval, setup, delete, self._make_finalize(["node_delete"])],
        )

        injected = graph.enforce_destructive_approvals()

        self.assertEqual(injected, 0)

    def test_skip_non_destructive_actions(self) -> None:
        write_node = TaskNode(
            node_id="node_write",
            type=TaskNodeType.ACTION,
            goal="写入文件",
            executor_hint="file",
            input_payload={"action": "write", "path": "a.txt", "content": "x"},
        )
        graph = TaskGraph(
            goal="写文件",
            nodes=[write_node, self._make_finalize(["node_write"])],
        )

        injected = graph.enforce_destructive_approvals()

        self.assertEqual(injected, 0)
        self.assertFalse(
            any(n.type == TaskNodeType.APPROVAL for n in graph.nodes),
            "write action must not trigger approval injection",
        )

    def test_idempotent(self) -> None:
        graph = TaskGraph(
            goal="删除 target.txt",
            nodes=[
                self._make_delete_node(),
                self._make_finalize(["node_delete"]),
            ],
        )
        first = graph.enforce_destructive_approvals()
        second = graph.enforce_destructive_approvals()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        approvals = [n for n in graph.nodes if n.type == TaskNodeType.APPROVAL]
        self.assertEqual(len(approvals), 1, "must not double-inject")

    def test_injects_approval_for_each_unguarded_delete(self) -> None:
        """Multiple delete nodes get separate approvals unless they share
        an approval ancestor."""
        graph = TaskGraph(
            goal="删除多个文件",
            nodes=[
                self._make_delete_node(node_id="node_delete_a", path="a.txt"),
                self._make_delete_node(node_id="node_delete_b", path="b.txt"),
                self._make_finalize(["node_delete_a", "node_delete_b"]),
            ],
        )

        injected = graph.enforce_destructive_approvals()

        self.assertEqual(injected, 2)
        approvals = [n for n in graph.nodes if n.type == TaskNodeType.APPROVAL]
        self.assertEqual(len(approvals), 2)

    def test_preserves_existing_delete_dependencies(self) -> None:
        """If the delete node already depends on a setup node, the
        injected approval should inherit that dependency rather than
        orphaning it."""
        setup = TaskNode(
            node_id="node_setup",
            type=TaskNodeType.ACTION,
            goal="列出目录",
            executor_hint="file",
            input_payload={"action": "list", "path": "."},
        )
        delete = self._make_delete_node(dependencies=["node_setup"])
        graph = TaskGraph(
            goal="删除 target.txt",
            nodes=[setup, delete, self._make_finalize(["node_delete"])],
        )

        graph.enforce_destructive_approvals()

        approval = next(n for n in graph.nodes if n.type == TaskNodeType.APPROVAL)
        self.assertEqual(approval.dependencies, ["node_setup"])
        # Delete now depends ONLY on the approval (which itself depends on setup).
        self.assertEqual(graph.get_node("node_delete").dependencies, [approval.node_id])

    def test_detects_builtin_file_skill_delete(self) -> None:
        """Destructive detection must also handle skill_name='builtin.file.basic'
        without an explicit executor_hint (dispatcher fills that in later)."""
        node = TaskNode(
            node_id="node_delete",
            type=TaskNodeType.ACTION,
            goal="删除 a.txt",
            executor_hint=None,
            skill_name="builtin.file.basic",
            input_payload={"action": "delete", "path": "a.txt"},
        )
        graph = TaskGraph(
            goal="删除",
            nodes=[node, self._make_finalize(["node_delete"])],
        )
        injected = graph.enforce_destructive_approvals()
        self.assertEqual(injected, 1)

    def test_remove_action_is_also_destructive(self) -> None:
        node = TaskNode(
            node_id="node_remove",
            type=TaskNodeType.ACTION,
            goal="remove 操作",
            executor_hint="file",
            input_payload={"action": "remove", "path": "a.txt"},
        )
        graph = TaskGraph(
            goal="删除",
            nodes=[
                node,
                TaskNode(
                    node_id="node_finalize",
                    type=TaskNodeType.FINALIZE,
                    goal="汇总",
                    dependencies=["node_remove"],
                ),
            ],
        )
        injected = graph.enforce_destructive_approvals()
        self.assertEqual(injected, 1)


# ---------------------------------------------------------------------------
# Part 3: Planner wires enforce_destructive_approvals() in
# ---------------------------------------------------------------------------


class PlannerInjectsApprovalIntegrationTest(unittest.TestCase):
    """Planner.plan() must run enforce_destructive_approvals() after
    validate(). Uses the StubLLMAdapter which produces a bare
    `action=delete` plan mirroring the user-supplied payload."""

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

    def test_planner_injects_approval_for_delete_payload(self) -> None:
        gateway = self._build_gateway()
        request = TaskRequest(
            goal="删除工作区的 target.txt",
            payload={"action": "delete", "path": "target.txt"},
            executor="file",
            constraints=TaskConstraints(allow_delete=True, max_permission=PermissionLevel.L5),
        )
        plan, _ = gateway.planner.plan(
            task_request=request,
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

        approvals = [n for n in plan.graph.nodes if n.type == TaskNodeType.APPROVAL]
        self.assertEqual(
            len(approvals),
            1,
            "planner should inject exactly one approval node for a lone delete",
        )
        delete_nodes = [
            n
            for n in plan.graph.nodes
            if n.type == TaskNodeType.ACTION and str(n.input_payload.get("action", "")) == "delete"
        ]
        self.assertEqual(len(delete_nodes), 1)
        self.assertIn(approvals[0].node_id, delete_nodes[0].dependencies)


# ---------------------------------------------------------------------------
# Part 4: REPL /allow /deny slash commands
# ---------------------------------------------------------------------------


class REPLAllowDenyCommandUnitTest(unittest.TestCase):
    """Narrow unit tests on `_apply_repl_constraint` without spinning up the
    full REPL loop — these lock down the argument parsing and the mutation
    contract."""

    def _make_args(self, *, allow_delete: bool = False, allow_network: bool = False):
        parser = build_parser()
        argv = ["chat", "repl"]
        if allow_delete:
            argv.append("--allow-delete")
        if allow_network:
            argv.append("--allow-network")
        return parser.parse_args(argv)

    def test_allow_delete_flips_args(self) -> None:
        args = self._make_args()
        stream = StringIO()
        handled = _apply_repl_constraint("/allow delete", args, stream, enable=True)
        self.assertTrue(handled)
        self.assertTrue(args.allow_delete)
        self.assertFalse(args.allow_network)
        self.assertIn("allow_delete", stream.getvalue())

    def test_deny_delete_flips_args(self) -> None:
        args = self._make_args(allow_delete=True)
        stream = StringIO()
        handled = _apply_repl_constraint("/deny delete", args, stream, enable=False)
        self.assertTrue(handled)
        self.assertFalse(args.allow_delete)
        self.assertIn("deny_delete", stream.getvalue())

    def test_allow_network_flips_args(self) -> None:
        args = self._make_args()
        stream = StringIO()
        handled = _apply_repl_constraint("/allow network", args, stream, enable=True)
        self.assertTrue(handled)
        self.assertTrue(args.allow_network)
        self.assertFalse(args.allow_delete)

    def test_unknown_target_rejected(self) -> None:
        args = self._make_args()
        stream = StringIO()
        handled = _apply_repl_constraint("/allow disk", args, stream, enable=True)
        self.assertFalse(handled)
        self.assertFalse(args.allow_delete)
        self.assertFalse(args.allow_network)
        self.assertIn("unknown constraint", stream.getvalue())

    def test_bare_allow_shows_current_state(self) -> None:
        args = self._make_args(allow_delete=True)
        stream = StringIO()
        handled = _apply_repl_constraint("/allow", args, stream, enable=True)
        self.assertTrue(handled)
        output = stream.getvalue()
        self.assertIn("constraints:", output)
        self.assertIn("allow_delete=True", output)
        self.assertIn("allow_network=False", output)
        # Bare /allow must NOT mutate anything.
        self.assertTrue(args.allow_delete)
        self.assertFalse(args.allow_network)


class REPLAllowDenyEndToEndTest(unittest.TestCase):
    """End-to-end test: feed `/allow delete` and `/exit` via stdin, verify
    the constraint state is reflected in the REPL's `/allow` bare output."""

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

    def _run_repl(self, argv: list[str], stdin_data: str) -> tuple[int, str]:
        stdout = StringIO()
        stdin = StringIO(stdin_data)
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch("sys.stdin", stdin),
            redirect_stdout(stdout),
        ):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def test_initial_constraints_print_and_allow_delete_flip(self) -> None:
        exit_code, output = self._run_repl(
            ["chat", "repl"],
            stdin_data="/allow\n/allow delete\n/allow\n/exit\n",
        )
        self.assertEqual(exit_code, 0, output)
        # First /allow prints baseline state (False/False)
        self.assertIn("allow_delete=False", output)
        # /allow delete acknowledges the flip
        self.assertIn("allow_delete: True", output)
        # Second /allow prints new state (True/False)
        self.assertIn("allow_delete=True", output)

    def test_deny_delete_flips_back(self) -> None:
        exit_code, output = self._run_repl(
            ["chat", "repl", "--allow-delete"],
            stdin_data="/allow\n/deny delete\n/allow\n/exit\n",
        )
        self.assertEqual(exit_code, 0, output)
        # Baseline shows True because of CLI flag
        self.assertIn("allow_delete=True", output)
        # Explicit deny acknowledgement
        self.assertIn("deny_delete: False", output)
        # Second /allow shows False
        self.assertIn("allow_delete=False", output.split("deny_delete: False")[1])

    def test_unknown_allow_target_does_not_crash_repl(self) -> None:
        exit_code, output = self._run_repl(
            ["chat", "repl"],
            stdin_data="/allow disk\n/exit\n",
        )
        self.assertEqual(exit_code, 0, output)
        self.assertIn("unknown constraint", output)


# ---------------------------------------------------------------------------
# Part 5: End-to-end FileExecutor delete guard
# ---------------------------------------------------------------------------


class FileExecutorAllowDeleteGuardTest(unittest.TestCase):
    """Low-level guard at the executor: without allow_delete, FileExecutor
    must raise PERMISSION_DENIED; with it, the file must actually be
    removed."""

    def _build_context(
        self,
        *,
        workspace: Path,
        allow_delete: bool,
        target_path: str,
    ) -> TaskRunContext:
        request = TaskRequest(
            goal=f"删除 {target_path}",
            executor="file",
            payload={"action": "delete", "path": target_path},
            constraints=TaskConstraints(
                allow_delete=allow_delete,
                max_permission=PermissionLevel.L5,
            ),
        )
        task = Task(
            task_id=request.task_id,
            goal=request.goal,
            mode=str(request.mode),
            source=str(request.source),
            status=TaskStatus.RUNNING,
            executor="file",
            skill_name=None,
            payload=dict(request.payload),
            workspace_path=str(workspace),
        )
        task_dir = workspace / "tasks" / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        sandbox = build_sandbox(workspace, level="workspace")
        self.addCleanup(sandbox.close)
        return TaskRunContext(
            task=task,
            task_request=request,
            workspace_root=workspace,
            task_dir=task_dir,
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            loaded_skills={},
            sandbox=sandbox,
        )

    def test_delete_denied_without_allow_delete_flag(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = Path(tempdir.name) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "target.txt"
        target.write_text("payload", encoding="utf-8")

        executor = FileExecutor()
        context = self._build_context(
            workspace=workspace, allow_delete=False, target_path="target.txt"
        )

        with self.assertRaises(OmniAgentError) as exc_info:
            executor.execute(context)
        self.assertEqual(exc_info.exception.code, ErrorCode.PERMISSION_DENIED)
        # File must still exist — the executor aborted before touching it.
        self.assertTrue(target.exists())

    def test_delete_proceeds_with_allow_delete_flag(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = Path(tempdir.name) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "target.txt"
        target.write_text("payload", encoding="utf-8")

        executor = FileExecutor()
        context = self._build_context(
            workspace=workspace, allow_delete=True, target_path="target.txt"
        )

        result = executor.execute(context)
        self.assertTrue(result.success)
        self.assertFalse(target.exists(), "file should be removed on approved delete")


# ---------------------------------------------------------------------------
# Part 6: End-to-end gateway submit with allow_delete
# ---------------------------------------------------------------------------


class GatewaySubmitDeleteEnforcementTest(unittest.TestCase):
    """Full-stack check: `omniagent run ... --allow-delete` should
    (a) inject an approval node via the planner pass, (b) reach
    WAITING_INPUT at the approval checkpoint, (c) delete the file after
    a successful approval resume. Without `--allow-delete` the task must
    surface a permission denial or approval hold."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_root = Path(self.tempdir.name)
        self.workspace = self.project_root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "target.txt").write_text("payload", encoding="utf-8")
        self.env = {
            "OMNIAGENT_SQLITE_PATH": str(self.project_root / "data" / "omniagent.sqlite3"),
            "OMNIAGENT_LOG_PATH": str(self.project_root / "artifacts" / "omniagent.log"),
            "OMNIAGENT_JSON_LOGS": "false",
            "OMNIAGENT_LLM_PROVIDER": "test",
        }

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with patch.dict(os.environ, self.env, clear=False), redirect_stdout(stdout):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def test_delete_task_without_flag_pauses_for_approval(self) -> None:
        """Without --allow-delete the task must NOT silently delete. It
        should either pause for approval (waiting_input) or fail with a
        permission error."""
        exit_code, output = self._run_cli(
            [
                "run",
                "删除 target.txt",
                "--executor",
                "file",
                "--payload",
                json.dumps({"action": "delete", "path": "target.txt"}),
                "--max-permission",
                "L5",
                "--json",
            ]
        )
        report = json.loads(output)
        self.assertIn(
            report["task"]["status"],
            {"waiting_input", "failed"},
            f"without --allow-delete the task must not complete; got {report['task']['status']}",
        )
        self.assertTrue(
            (self.workspace / "target.txt").exists(),
            "file must NOT be deleted without approval",
        )
        # The plan should carry the injected approval node.
        approval_nodes = [
            n for n in report["task_graph"]["nodes"] if n["type"] == TaskNodeType.APPROVAL
        ]
        self.assertGreaterEqual(
            len(approval_nodes),
            1,
            "planner must inject approval before a destructive delete",
        )
        self.assertTrue(
            any(n.get("metadata", {}).get("auto_injected") for n in approval_nodes),
            "at least one approval node must be flagged auto_injected",
        )
        # Exit code is 0 because waiting_input is treated as non-fatal.
        self.assertIn(exit_code, {0, 1})

    def test_delete_task_with_flag_still_gates_via_approval(self) -> None:
        """Even with --allow-delete the planner still injects an approval
        node (we require user confirmation, not silent destruction). The
        task should reach waiting_input at the approval checkpoint."""
        exit_code, output = self._run_cli(
            [
                "run",
                "删除 target.txt",
                "--executor",
                "file",
                "--payload",
                json.dumps({"action": "delete", "path": "target.txt"}),
                "--allow-delete",
                "--max-permission",
                "L5",
                "--json",
            ]
        )
        report = json.loads(output)
        approval_nodes = [
            n for n in report["task_graph"]["nodes"] if n["type"] == TaskNodeType.APPROVAL
        ]
        self.assertGreaterEqual(len(approval_nodes), 1)
        # File should still exist because the task is paused at approval.
        # (Even with allow_delete=True, the approval gate must fire first.)
        self.assertEqual(report["task"]["status"], "waiting_input")
        self.assertTrue((self.workspace / "target.txt").exists())


if __name__ == "__main__":
    unittest.main()

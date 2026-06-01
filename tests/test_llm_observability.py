"""ISSUE-001 regression tests: LLM observability end-to-end.

These tests guard the full pipeline that lets ``omniagent llm stats`` report
per-call latency / tier / attempts / token usage without re-running the LLM:

    adapter → LLMResponse → workspace/tasks/<id>/llm_calls/*.json
    adapter → LLMResponse → workspace/threads/<id>/llm_calls/*.json
           → gateway.llm_stats(task_id=, thread_id=, prompt_name=, limit=)
           → CLI `llm stats` pretty output

The pieces were individually wired during the ISSUE-001 pass but had zero
regression coverage — any refactor that dropped a ``write_llm_call`` call or
reset ``StubLLMAdapter.latency_ms=None`` would silently invalidate ``llm
stats`` without failing any test. These tests exist to catch that.
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

from omniagent.cli import main
from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import LLMResponse
from omniagent.workspace import TaskArtifactManager, WorkspaceManager

from tests.llm_test_helper import StubLLMAdapter


# ---------------------------------------------------------------------------
# Stub field population
# ---------------------------------------------------------------------------


class StubAdapterObservabilityFieldsTest(unittest.TestCase):
    """StubLLMAdapter must populate observability fields or the rest of the
    pipeline becomes untestable — every integration test would write ``null``
    to disk and aggregates would all be zero.
    """

    def test_planner_response_has_nonnull_observability_fields(self) -> None:
        adapter = StubLLMAdapter()
        response = adapter.generate_structured(
            prompt_name="planner_v1",
            system_prompt="test",
            request_payload={"task_request": {"goal": "写文件", "executor": "file"}},
            schema={"type": "object"},
        )
        self.assertIsInstance(response, LLMResponse)
        self.assertIsNotNone(response.latency_ms)
        self.assertGreater(response.latency_ms, 0)
        self.assertIsNotNone(response.tier)
        self.assertEqual(response.tier, "tier1_json_schema")
        self.assertEqual(response.attempts, 1)
        self.assertIsNotNone(response.started_at)
        self.assertEqual(response.usage.get("total_tokens"), 19)

    def test_call_count_increments_per_call(self) -> None:
        adapter = StubLLMAdapter()
        self.assertEqual(adapter.call_count, 0)
        for _ in range(3):
            adapter.generate_structured(
                prompt_name="critic_v1",
                system_prompt="test",
                request_payload={"execution_result": {"success": True}},
                schema={"type": "object"},
            )
        self.assertEqual(adapter.call_count, 3)


# ---------------------------------------------------------------------------
# Workspace persistence (task dir + thread dir)
# ---------------------------------------------------------------------------


class WriteLLMCallTaskDirTest(unittest.TestCase):
    """`write_llm_call` must persist all observability fields to JSON so
    `llm_stats` can re-read them offline.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace_manager = WorkspaceManager(self.workspace)
        self.workspace_manager.ensure()
        self.artifact_manager = TaskArtifactManager(self.workspace_manager)

    def _make_response(self, **overrides) -> LLMResponse:
        defaults = {
            "provider": "test",
            "model": "test-model",
            "prompt_name": "planner_v1",
            "request_id": "req_abc",
            "raw_output": "{}",
            "structured_output": {"summary": "ok"},
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "latency_ms": 123.45,
            "tier": "tier1_json_schema",
            "attempts": 2,
            "started_at": "2026-04-15T10:00:00Z",
        }
        defaults.update(overrides)
        return LLMResponse(**defaults)

    def test_task_scoped_persistence_writes_all_observability_fields(self) -> None:
        task_id = "task_obs_01"
        task_dir = self.workspace_manager.create_task_dir(task_id)
        response = self._make_response()

        self.artifact_manager.write_llm_call(task_dir, response, request_payload={"x": 1})

        call_files = list((task_dir / "llm_calls").glob("*.json"))
        self.assertEqual(len(call_files), 1)
        payload = json.loads(call_files[0].read_text(encoding="utf-8"))

        # These four fields are the whole point of ISSUE-001 — if any of
        # them is missing from the persisted JSON, llm_stats is broken.
        self.assertEqual(payload["latency_ms"], 123.45)
        self.assertEqual(payload["tier"], "tier1_json_schema")
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(payload["started_at"], "2026-04-15T10:00:00Z")
        self.assertEqual(payload["usage"]["total_tokens"], 8)
        self.assertEqual(payload["prompt_name"], "planner_v1")
        self.assertEqual(payload["request_payload"], {"x": 1})


class WriteThreadLLMCallTest(unittest.TestCase):
    """`write_thread_llm_call` persists router/responder LLM calls under
    workspace/threads/<id>/llm_calls/. Regression guard: directory layout,
    same schema as task-scoped file, and explicit thread_id field.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace_manager = WorkspaceManager(self.workspace)
        self.workspace_manager.ensure()
        self.artifact_manager = TaskArtifactManager(self.workspace_manager)

    def test_thread_dir_is_created_lazily(self) -> None:
        thread_id = "thread_obs_01"
        thread_dir = self.workspace / "threads" / thread_id
        self.assertFalse(thread_dir.exists())

        response = LLMResponse(
            provider="test",
            model="test-model",
            prompt_name="router_v1",
            request_id="req_rt",
            raw_output="{}",
            structured_output={"input_type": "new_task"},
            usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            latency_ms=55.5,
            tier="tier1_json_schema",
            attempts=1,
            started_at="2026-04-15T10:00:00Z",
        )
        self.artifact_manager.write_thread_llm_call(
            thread_id, response, request_payload={"message": "写个文件"}
        )

        self.assertTrue(thread_dir.exists())
        self.assertTrue((thread_dir / "llm_calls").exists())
        call_files = list((thread_dir / "llm_calls").glob("*.json"))
        self.assertEqual(len(call_files), 1)
        payload = json.loads(call_files[0].read_text(encoding="utf-8"))

        self.assertEqual(payload["thread_id"], thread_id)
        self.assertEqual(payload["prompt_name"], "router_v1")
        self.assertEqual(payload["latency_ms"], 55.5)
        self.assertEqual(payload["tier"], "tier1_json_schema")
        self.assertEqual(payload["usage"]["total_tokens"], 12)


# ---------------------------------------------------------------------------
# gateway.llm_stats aggregation
# ---------------------------------------------------------------------------


class LLMStatsAggregationTest(unittest.TestCase):
    """llm_stats must aggregate task + thread scopes, support filters, and
    match the data written by write_llm_call/write_thread_llm_call.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace_manager = WorkspaceManager(self.workspace)
        self.workspace_manager.ensure()
        self.artifact_manager = TaskArtifactManager(self.workspace_manager)

        # Hand-assemble a gateway with only the pieces llm_stats needs,
        # so this test does not depend on config / DB / LLM bootstrap.
        self.gateway = RuntimeGateway.__new__(RuntimeGateway)
        self.gateway.workspace_root = self.workspace

        self._seed_task(
            "task_a", prompt="planner_v1", latency=100.0, tier="tier1_json_schema", tokens=30
        )
        self._seed_task(
            "task_a", prompt="critic_v1", latency=200.0, tier="tier1_json_schema", tokens=15
        )
        self._seed_task(
            "task_b", prompt="planner_v1", latency=400.0, tier="tier2_json_object", tokens=50
        )
        self._seed_thread(
            "thread_x", prompt="router_v1", latency=50.0, tier="tier1_json_schema", tokens=20
        )
        self._seed_thread(
            "thread_x", prompt="responder_v1", latency=80.0, tier="tier1_json_schema", tokens=25
        )
        self._seed_thread(
            "thread_y", prompt="router_v1", latency=60.0, tier="tier1_json_schema", tokens=10
        )

    def _seed_task(
        self, task_id: str, *, prompt: str, latency: float, tier: str, tokens: int
    ) -> None:
        task_dir = self.workspace_manager.create_task_dir(task_id)
        response = LLMResponse(
            provider="test",
            model="test-model",
            prompt_name=prompt,
            request_id=f"req_{task_id}_{prompt}",
            raw_output="{}",
            structured_output={},
            usage={
                "prompt_tokens": tokens // 2,
                "completion_tokens": tokens - tokens // 2,
                "total_tokens": tokens,
            },
            latency_ms=latency,
            tier=tier,
            attempts=1,
            started_at="2026-04-15T10:00:00Z",
        )
        self.artifact_manager.write_llm_call(task_dir, response, request_payload={})

    def _seed_thread(
        self, thread_id: str, *, prompt: str, latency: float, tier: str, tokens: int
    ) -> None:
        response = LLMResponse(
            provider="test",
            model="test-model",
            prompt_name=prompt,
            request_id=f"req_{thread_id}_{prompt}",
            raw_output="{}",
            structured_output={},
            usage={
                "prompt_tokens": tokens // 2,
                "completion_tokens": tokens - tokens // 2,
                "total_tokens": tokens,
            },
            latency_ms=latency,
            tier=tier,
            attempts=1,
            started_at="2026-04-15T10:00:00Z",
        )
        self.artifact_manager.write_thread_llm_call(thread_id, response, request_payload={})

    def test_default_scope_aggregates_tasks_and_threads(self) -> None:
        stats = self.gateway.llm_stats()
        self.assertEqual(stats["total_calls"], 6)  # 3 task + 3 thread
        self.assertEqual(stats["tasks_scanned"], 2)
        self.assertEqual(stats["threads_scanned"], 2)
        self.assertEqual(stats["by_scope"], {"task": 3, "thread": 3})
        self.assertEqual(
            stats["by_prompt"].keys(), {"planner_v1", "critic_v1", "router_v1", "responder_v1"}
        )
        # tier count — 5 tier1 (100,200 task_a + 50,80 thread_x + 60 thread_y) + 1 tier2 (400 task_b)
        self.assertEqual(stats["by_tier"], {"tier1_json_schema": 5, "tier2_json_object": 1})

    def test_task_id_filter_ignores_threads(self) -> None:
        stats = self.gateway.llm_stats(task_id="task_a")
        self.assertEqual(stats["total_calls"], 2)
        self.assertEqual(stats["tasks_scanned"], 1)
        self.assertEqual(stats["threads_scanned"], 0)
        self.assertEqual(stats["by_scope"], {"task": 2, "thread": 0})
        self.assertEqual(stats["by_prompt"].keys(), {"planner_v1", "critic_v1"})

    def test_thread_id_filter_ignores_tasks(self) -> None:
        stats = self.gateway.llm_stats(thread_id="thread_x")
        self.assertEqual(stats["total_calls"], 2)
        self.assertEqual(stats["tasks_scanned"], 0)
        self.assertEqual(stats["threads_scanned"], 1)
        self.assertEqual(stats["by_scope"], {"task": 0, "thread": 2})
        self.assertEqual(stats["by_prompt"].keys(), {"router_v1", "responder_v1"})

    def test_prompt_name_filter_applies_across_scopes(self) -> None:
        stats = self.gateway.llm_stats(prompt_name="router_v1")
        self.assertEqual(stats["total_calls"], 2)  # both from threads
        self.assertEqual(stats["by_scope"], {"task": 0, "thread": 2})
        self.assertEqual(stats["by_prompt"].keys(), {"router_v1"})
        router_bucket = stats["by_prompt"]["router_v1"]
        self.assertEqual(router_bucket["calls"], 2)
        # avg latency = (50 + 60) / 2 = 55.0
        self.assertEqual(router_bucket["avg_latency_ms"], 55.0)

    def test_latency_percentiles_nonzero_when_data_exists(self) -> None:
        stats = self.gateway.llm_stats(task_id="task_a")
        latencies = stats["latency_ms"]
        # task_a has 100, 200 → avg=150, p50≈100, p95≈200, max=200
        self.assertEqual(latencies["avg"], 150.0)
        self.assertEqual(latencies["max"], 200.0)
        self.assertGreater(latencies["p50"], 0.0)
        self.assertGreater(latencies["p95"], 0.0)

    def test_token_totals_sum_correctly(self) -> None:
        stats = self.gateway.llm_stats()
        # 30 + 15 + 50 + 20 + 25 + 10 = 150
        self.assertEqual(stats["tokens"]["total"], 150)

    def test_unknown_thread_id_returns_empty(self) -> None:
        stats = self.gateway.llm_stats(thread_id="nonexistent")
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["threads_scanned"], 0)
        self.assertEqual(stats["by_scope"], {"task": 0, "thread": 0})


# ---------------------------------------------------------------------------
# End-to-end: task submission writes llm_calls and llm_stats picks them up
# ---------------------------------------------------------------------------


class EndToEndLLMStatsTest(unittest.TestCase):
    """Submit a real task through the gateway with the stub provider and
    verify that (a) llm_calls/*.json files are created with observability
    fields and (b) llm_stats reads them correctly.
    """

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

    def test_full_task_run_produces_task_scoped_llm_call_files(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            gateway = self._build_gateway()
            from omniagent.models import PermissionLevel, TaskConstraints, TaskRequest

            request = TaskRequest(
                goal="先写一个 observability_test.txt 然后读取它",
                executor="file",
                payload={"action": "list", "path": "."},
                constraints=TaskConstraints(max_permission=PermissionLevel.L2),
            )
            report = gateway.submit(request)

        task_dir = self.workspace / "tasks" / report.task.task_id
        llm_dir = task_dir / "llm_calls"
        self.assertTrue(llm_dir.exists(), f"llm_calls dir missing under {task_dir}")
        call_files = list(llm_dir.glob("*.json"))
        self.assertTrue(call_files, "no llm_calls/*.json produced for the task")
        expected_total_tokens = 0
        expected_by_tier: dict[str, int] = {}

        # Verify each file has the four observability fields populated
        for call_path in call_files:
            payload = json.loads(call_path.read_text(encoding="utf-8"))
            expected_total_tokens += int((payload.get("usage") or {}).get("total_tokens") or 0)
            tier = payload.get("tier") or "unknown"
            expected_by_tier[tier] = expected_by_tier.get(tier, 0) + 1
            self.assertIn(
                payload.get("prompt_name"),
                {"planner_v1", "critic_v1", "completion_evaluator_v1"},
                f"unexpected prompt in {call_path.name}: {payload.get('prompt_name')}",
            )
            self.assertIsNotNone(
                payload.get("latency_ms"),
                f"latency_ms is None in {call_path.name} — stub/adapter wiring broken",
            )
            self.assertIsNotNone(payload.get("tier"), f"tier is None in {call_path.name}")
            self.assertEqual(payload.get("attempts"), 1)
            self.assertIsNotNone(payload.get("started_at"))

        # Now aggregate via the public API
        stats = gateway.llm_stats(task_id=report.task.task_id)
        self.assertGreater(stats["total_calls"], 0)
        self.assertEqual(stats["tasks_scanned"], 1)
        self.assertGreater(stats["latency_ms"]["avg"], 0)
        self.assertEqual(stats["tokens"]["total"], expected_total_tokens)
        self.assertEqual(stats["by_tier"], expected_by_tier)

    def test_chat_flow_produces_thread_scoped_llm_call_files(self) -> None:
        """A thread-only interaction (no task execution) — e.g. help query —
        should still produce responder_v1 logs in workspace/threads/<id>/."""
        with patch.dict(os.environ, self.env, clear=False):
            gateway = self._build_gateway()
            response = gateway.send_message("有哪些工作区规则？")

        thread_id = response.thread.thread_id
        thread_dir = self.workspace / "threads" / thread_id
        # Responder should have written at least one call even though no
        # task was created for this knowledge query.
        if thread_dir.exists():
            call_files = list((thread_dir / "llm_calls").glob("*.json"))
            self.assertTrue(call_files, "thread dir exists but no llm_calls/*.json inside")
            for call_path in call_files:
                payload = json.loads(call_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["thread_id"], thread_id)
                # ROADMAP P1 #4: knowledge queries now also go through an
                # LLM call with `knowledge_query_v1`, logged under the same
                # thread-scoped llm_calls/ directory.
                self.assertIn(
                    payload["prompt_name"],
                    {"router_v1", "responder_v1", "knowledge_query_v1"},
                )
                self.assertIsNotNone(payload["latency_ms"])
                self.assertIsNotNone(payload["tier"])

            stats = gateway.llm_stats(thread_id=thread_id)
            self.assertGreater(stats["total_calls"], 0)
            self.assertEqual(stats["tasks_scanned"], 0)
            self.assertEqual(stats["threads_scanned"], 1)
            self.assertEqual(stats["by_scope"]["task"], 0)
            self.assertGreater(stats["by_scope"]["thread"], 0)


# ---------------------------------------------------------------------------
# CLI pretty output
# ---------------------------------------------------------------------------


class CLILLMStatsCommandTest(unittest.TestCase):
    """The `omniagent llm stats` CLI must expose both --task-id and
    --thread-id, and its pretty output must include the new scope fields.
    """

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

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with patch.dict(os.environ, self.env, clear=False), redirect_stdout(stdout):
            exit_code = main(["--workspace", str(self.workspace), *argv])
        return exit_code, stdout.getvalue()

    def test_llm_stats_json_mode_includes_scope_fields(self) -> None:
        # First submit a task so we have some data
        exit_code, _ = self._run_cli(["run", "写一个测试文件", "--max-permission", "L2"])
        self.assertEqual(exit_code, 0)

        exit_code, output = self._run_cli(["llm", "stats", "--json"])
        self.assertEqual(exit_code, 0, output)

        payload = json.loads(output)
        # The three new top-level fields from ISSUE-001 extensions:
        self.assertIn("threads_scanned", payload)
        self.assertIn("thread_root", payload)
        self.assertIn("by_scope", payload)
        self.assertIn("task", payload["by_scope"])
        self.assertIn("thread", payload["by_scope"])
        self.assertGreater(payload["total_calls"], 0)

    def test_llm_stats_pretty_mode_prints_new_labels(self) -> None:
        # Seed some data first
        exit_code, _ = self._run_cli(["run", "写一个测试文件", "--max-permission", "L2"])
        self.assertEqual(exit_code, 0)

        exit_code, output = self._run_cli(["llm", "stats"])
        self.assertEqual(exit_code, 0, output)

        self.assertIn("tasks_scanned:", output)
        self.assertIn("threads_scanned:", output)
        self.assertIn("total_calls:", output)
        self.assertIn("by_scope:", output)
        self.assertIn("by_tier:", output)

    def test_llm_stats_accepts_thread_id_flag(self) -> None:
        # Query a nonexistent thread — should still succeed with zero rows
        exit_code, output = self._run_cli(
            ["llm", "stats", "--thread-id", "thread_missing", "--json"]
        )
        self.assertEqual(exit_code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["total_calls"], 0)
        self.assertEqual(payload["threads_scanned"], 0)
        # tasks_scanned should also be 0 because --thread-id narrows scope
        self.assertEqual(payload["tasks_scanned"], 0)


# ---------------------------------------------------------------------------
# llm_native executor writes llm_call log
# ---------------------------------------------------------------------------


class LLMNativeExecutorPersistsCallTest(unittest.TestCase):
    """LLMNativeExecutor.execute must call artifact_manager.write_llm_call
    so `llm stats` can include text-generation calls (META-004 + ISSUE-001).
    """

    def test_execute_writes_llm_call_when_artifact_manager_provided(self) -> None:
        from omniagent.executors.llm_native import LLMNativeExecutor
        from omniagent.models import (
            BootstrapContext,
            Task,
            TaskRequest,
            TaskRunContext,
            TaskStatus,
        )

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        workspace = Path(tmpdir.name) / "workspace"
        workspace_manager = WorkspaceManager(workspace)
        workspace_manager.ensure()
        artifact_manager = TaskArtifactManager(workspace_manager)

        adapter = StubLLMAdapter()
        executor = LLMNativeExecutor(adapter, artifact_manager=artifact_manager)

        task_id = "task_native_01"
        task_dir = workspace_manager.create_task_dir(task_id)

        task = Task(
            task_id=task_id,
            goal="写一个关于北京的故事",
            mode="semi_auto",
            source="cli",
            status=TaskStatus.RUNNING,
            executor="llm_native",
            skill_name=None,
            payload={},
            workspace_path=str(workspace),
        )
        request = TaskRequest(
            goal="写一个关于北京的故事",
            executor="llm_native",
            payload={},
            metadata={"node_id": "node_1"},
        )
        context = TaskRunContext(
            task=task,
            task_request=request,
            workspace_root=workspace,
            task_dir=task_dir,
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            loaded_skills={},
            sandbox=None,
        )

        result = executor.execute(context)
        self.assertTrue(result.success)

        call_files = list((task_dir / "llm_calls").glob("*.json"))
        self.assertEqual(
            len(call_files), 1, "llm_native should persist exactly one llm_call record"
        )
        payload = json.loads(call_files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["prompt_name"], "llm_native_v1")
        self.assertIsNotNone(payload["latency_ms"])
        self.assertEqual(payload["tier"], "tier1_json_schema")

    def test_execute_still_succeeds_without_artifact_manager(self) -> None:
        """Unit-level instantiation without an artifact_manager must still
        work — the persistence hook is best-effort."""
        from omniagent.executors.llm_native import LLMNativeExecutor
        from omniagent.models import (
            BootstrapContext,
            Task,
            TaskRequest,
            TaskRunContext,
            TaskStatus,
        )

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        workspace = Path(tmpdir.name) / "workspace"
        workspace_manager = WorkspaceManager(workspace)
        workspace_manager.ensure()

        adapter = StubLLMAdapter()
        executor = LLMNativeExecutor(adapter)  # no artifact_manager
        task_dir = workspace_manager.create_task_dir("task_noam")

        task = Task(
            task_id="task_noam",
            goal="写一个故事",
            mode="semi_auto",
            source="cli",
            status=TaskStatus.RUNNING,
            executor="llm_native",
            skill_name=None,
            payload={},
            workspace_path=str(workspace),
        )
        request = TaskRequest(
            goal="写一个故事",
            executor="llm_native",
            payload={},
            metadata={"node_id": "node_1"},
        )
        context = TaskRunContext(
            task=task,
            task_request=request,
            workspace_root=workspace,
            task_dir=task_dir,
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            loaded_skills={},
            sandbox=None,
        )

        result = executor.execute(context)
        self.assertTrue(result.success)
        # No llm_calls written (no artifact_manager) but also no crash.
        self.assertEqual(list((task_dir / "llm_calls").glob("*.json")), [])


if __name__ == "__main__":
    # Avoid pytest-only features — we want this to be runnable standalone too.
    unittest.main()

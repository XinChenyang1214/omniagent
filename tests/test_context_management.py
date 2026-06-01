from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from omniagent.critic import Critic
from omniagent.embeddings import EmbeddingResult
from omniagent.gateway import RuntimeGateway
from omniagent.graph_memory import GraphMemoryStore
from omniagent.hybrid_memory import HybridMemoryRetriever
from omniagent.memory import FileMemoryManager
from omniagent.models import ExecutionResult, LLMResponse, Task, TaskNode, TaskNodeType, TaskStatus
from omniagent.runtime import EmbeddedRuntime
from omniagent.storage import SQLiteStore
from omniagent.vector_recall import VectorRecallEngine
from omniagent.workspace import TaskArtifactManager, WorkspaceManager


class StaticEmbeddingProvider:
    provider = "fake"
    model = "fake-model"
    version = "fake-v1"

    def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=[1.0, 0.0],
            provider=self.provider,
            model=self.model,
            dimensions=2,
            version=self.version,
            latency_ms=0.01,
        )


def test_vector_recall_strips_raw_embedding_from_prompt_payload(tmp_path: Path) -> None:
    manager = FileMemoryManager(
        tmp_path / "workspace",
        vector_engine=VectorRecallEngine(StaticEmbeddingProvider()),
    )
    manager.write_vector_entry(
        entry_id="task_vector",
        text="写一个文件并保存到工作区",
        source_type="task",
        metadata={"task_id": "task_vector", "status": "completed"},
    )

    stored = json.loads((manager.vector_root / "task_vector.json").read_text(encoding="utf-8"))
    assert "embedding" in stored

    matches = manager.recall_vector("写文件", limit=1)

    assert matches[0]["entry_id"] == "task_vector"
    assert "embedding" not in matches[0]
    assert matches[0]["embedding_provider"] == "fake"
    assert matches[0]["match_details"]["vector_compatible"]


def test_file_workflow_recall_filters_failed_workflows(tmp_path: Path) -> None:
    manager = FileMemoryManager(tmp_path / "workspace")
    (manager.workflow_root / "task_ok.json").write_text(
        json.dumps(
            {
                "task_id": "task_ok",
                "goal": "读取 markdown 文件",
                "status": "completed",
                "plan_summary": "读取文件并输出摘要",
                "result_message": "完成",
                "created_at": "2026-05-20T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (manager.workflow_root / "task_failed.json").write_text(
        json.dumps(
            {
                "task_id": "task_failed",
                "goal": "读取 markdown 文件",
                "status": "failed",
                "plan_summary": "错误流程",
                "result_message": "失败",
                "created_at": "2026-05-21T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    matches = manager.recall_workflows("读取 markdown 文件", limit=5)

    assert [item["task_id"] for item in matches] == ["task_ok"]


def test_hybrid_recall_uses_vector_as_supplement_not_duplicate_or_failed_signal() -> None:
    memory = MagicMock()
    graph_memory = MagicMock()
    retriever = HybridMemoryRetriever(memory, graph_memory)
    memory.recall_workflows.return_value = [
        {
            "task_id": "task_same",
            "goal": "读取文件",
            "plan_summary": "读取并整理",
            "result_message": "成功",
            "score": 0.9,
        }
    ]
    memory.recall_vector.return_value = [
        {
            "entry_id": "task_same",
            "text": "读取文件",
            "metadata": {"task_id": "task_same", "status": "completed"},
            "score": 0.95,
        },
        {
            "entry_id": "task_failed",
            "text": "读取文件失败",
            "metadata": {"task_id": "task_failed", "status": "failed"},
            "score": 0.94,
        },
        {
            "entry_id": "task_other",
            "text": "读取配置文件",
            "metadata": {"task_id": "task_other", "status": "completed"},
            "score": 0.8,
        },
    ]
    memory.recall_failure_patterns.return_value = []
    graph_memory.recommend_workflows.return_value = []
    graph_memory.recommend_skills_for_goal.return_value = []
    graph_memory.recommend_failure_patterns.return_value = []

    result = retriever.recall_for_planning("读取文件", limit=5)

    assert [item["entry_id"] for item in result["vector_matches"]] == ["task_other"]
    assert len([item for item in result["recall_explanations"] if item["channel"] == "vector"]) == 1


def test_runtime_context_dedupes_memory_workflow_and_vector_channels() -> None:
    runtime = EmbeddedRuntime.__new__(EmbeddedRuntime)
    memory_summaries = [
        {"task_id": "task_workflow", "summary": "already represented as workflow"},
        {"task_id": "task_memory", "summary": "memory only"},
    ]
    workflow_summaries = [{"task_id": "task_workflow", "summary": "workflow"}]
    vector_matches = [
        {"entry_id": "task_workflow", "metadata": {"task_id": "task_workflow"}},
        {"entry_id": "task_memory", "metadata": {"task_id": "task_memory"}},
        {"entry_id": "task_vector", "metadata": {"task_id": "task_vector"}},
    ]

    deduped_memory = EmbeddedRuntime._dedupe_memory_against_workflows(
        runtime,
        memory_summaries,
        workflow_summaries,
    )
    deduped_vector = EmbeddedRuntime._dedupe_vector_against_context(
        runtime,
        vector_matches,
        memory_summaries=deduped_memory,
        workflow_summaries=workflow_summaries,
    )

    assert [item["task_id"] for item in deduped_memory] == ["task_memory"]
    assert [item["entry_id"] for item in deduped_vector] == ["task_vector"]


class CapturingLLM:
    def __init__(self) -> None:
        self.request_payload: dict[str, Any] | None = None

    def generate_structured(
        self,
        *,
        prompt_name: str,
        system_prompt: str,
        request_payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> LLMResponse:
        self.request_payload = request_payload
        return LLMResponse(
            provider="test",
            model="test",
            prompt_name=prompt_name,
            request_id="req_critic",
            raw_output="{}",
            structured_output={
                "target_type": request_payload["target_type"],
                "target_id": request_payload["target_id"],
                "action": "accept",
                "summary": "ok",
                "rationale": "ok",
                "confidence": 0.9,
                "suggested_retry_policy": None,
                "downgrade_target": None,
                "replan_goal": None,
            },
        )


class PromptStub:
    def load(self, name: str) -> str:
        return f"prompt:{name}"


def test_critic_compacts_large_node_and_result_payloads() -> None:
    llm = CapturingLLM()
    critic = Critic(llm, PromptStub())  # type: ignore[arg-type]
    large_text = "x" * 5000
    node = TaskNode(
        node_id="node_large",
        type=TaskNodeType.ACTION,
        goal="处理大输出",
        input_payload={"body": large_text},
        output={"body": large_text},
    )

    critic.evaluate_node(
        node=node,
        execution_result=ExecutionResult(
            success=False,
            status="failed",
            message="失败",
            output={"body": large_text},
        ),
        failure_patterns=[{"error_message": large_text}],
        workflow_matches=[{"result_message": large_text}],
        graph_failure_patterns=[{"critic_summary": large_text}],
        recall_explanations=[{"reason": large_text}],
        user_intent="处理大输出",
        task_goal="处理大输出",
    )

    assert llm.request_payload is not None
    serialized = json.dumps(llm.request_payload, ensure_ascii=False)
    assert large_text not in serialized
    assert "...<truncated>" in serialized
    assert len(llm.request_payload["node"]["output"]["body"]) < 1300
    assert len(llm.request_payload["execution_result"]["output"]["body"]) < 1300


def test_llm_call_log_records_request_payload_size_stats(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manager = WorkspaceManager(workspace)
    artifact_manager = TaskArtifactManager(manager)
    task_dir = manager.create_task_dir("task_obs")
    response = LLMResponse(
        provider="test",
        model="test",
        prompt_name="planner_v1",
        request_id="req_stats",
        raw_output="{}",
        structured_output={},
    )

    artifact_manager.write_llm_call(
        task_dir,
        response,
        request_payload={"goal": "测试", "vector_matches": [{"text": "x" * 2000}]},
    )

    payload = json.loads(next((task_dir / "llm_calls").glob("*.json")).read_text("utf-8"))

    assert payload["request_payload_stats"]["top_level_keys"] == ["goal", "vector_matches"]
    assert payload["request_payload_stats"]["fields"]["vector_matches"]["items"] == 1
    assert payload["request_payload_stats"]["fields"]["vector_matches"]["json_chars"] > 1000


def test_gateway_reconciles_stale_running_tasks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data.sqlite3")
    graph_memory = GraphMemoryStore(store)
    old_time = (datetime.now(tz=timezone.utc) - timedelta(seconds=1000)).isoformat()
    task = Task(
        task_id="task_stale",
        goal="旧任务",
        mode="semi_auto",
        source="cli",
        status=TaskStatus.RUNNING,
        executor="file",
        skill_name=None,
        payload={},
        workspace_path=str(tmp_path / "workspace"),
        created_at=old_time,
        updated_at=old_time,
    )
    store.create_task(task)
    gateway = RuntimeGateway.__new__(RuntimeGateway)
    gateway.config = SimpleNamespace(gateway=SimpleNamespace(task_timeout_seconds=30))
    gateway.store = store
    gateway.graph_memory = graph_memory

    reconciled = RuntimeGateway._reconcile_stale_runtime_tasks(gateway)

    assert reconciled == ["task_stale"]
    updated = store.get_task("task_stale")
    assert updated is not None
    assert updated.status == TaskStatus.FAILED
    assert updated.error_code == "stale_task_reconciled"
    events = store.list_events("task_stale")
    assert events[-1].event_type == "task.stale_reconciled"

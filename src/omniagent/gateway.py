from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from omniagent.completion_evaluator import CompletionEvaluator
from omniagent.config import AppConfig
from omniagent.critic import Critic
from omniagent.dispatcher import SkillDispatcher
from omniagent.embeddings import build_embedding_provider
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.events import EventBus, EventHandler, EventRecorder
from omniagent.execution_engine import ExecutionEngine
from omniagent.executors.browser import BrowserExecutor
from omniagent.executors.file import FileExecutor
from omniagent.executors.llm_native import LLMNativeExecutor
from omniagent.executors.python import PythonExecutor
from omniagent.graph_memory import GraphMemoryStore
from omniagent.hybrid_memory import HybridMemoryRetriever
from omniagent.interaction import InteractionRouter
from omniagent.llm import build_llm_adapter
from omniagent.memory import FileMemoryManager
from omniagent.models import (
    AutonomyMode,
    CompletionEvaluation,
    ConversationResponse,
    ConversationThread,
    ConversationTurn,
    ConversationTurnRole,
    PendingCheckpoint,
    PendingCheckpointStatus,
    PendingCheckpointType,
    PermissionLevel,
    RouteDecision,
    Task,
    TaskConstraints,
    TaskEvent,
    TaskExecutionReport,
    TaskNodeStatus,
    TaskRequest,
    TaskResumeAttempt,
    TaskSource,
    TaskStatus,
    UserInputEnvelope,
    UserInputType,
    utc_now,
)
from omniagent.permissions import PermissionManager
from omniagent.planner import Planner
from omniagent.prompting import PromptManager
from omniagent.response_generator import ResponseContext, ResponseGenerator
from omniagent.runtime import EmbeddedRuntime
from omniagent.scheduler import TaskScheduler
from omniagent.skill_auto_generation import AutoSkillGenerator
from omniagent.skill_lifecycle import SkillLifecycleManager
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore
from omniagent.vector_recall import SemanticScorer, VectorRecallEngine
from omniagent.workflow_compiler import WorkflowCompiler
from omniagent.workspace import TaskArtifactManager, WorkspaceManager


_NETWORK_DENY_PATTERNS = (
    r"不.{0,3}(允许|要|用).{0,8}(联网|网络访问|访问网络|上网)",
    r"(禁止|别|不要|无需|不用|不需要).{0,8}(联网|网络访问|访问网络|上网)",
)
_NETWORK_ALLOW_PATTERNS = (
    r"(允许|授权|同意|可以|可|请|帮我).{0,10}(联网|网络访问|访问网络|上网|搜索网页|搜索网络)",
    r"(联网|网络访问|访问网络|上网|搜索网页|搜索网络).{0,10}(允许|授权|同意|可以|可)",
)
_THREAD_TASK_LOOKBACK = 100
_STALE_TASK_STATUSES = (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.RETRYING)
_STALE_TASK_MIN_SECONDS = 300


def _explicitly_allows_network(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    if not normalized:
        return False
    if any(re.search(pattern, normalized) for pattern in _NETWORK_DENY_PATTERNS):
        return False
    return any(re.search(pattern, normalized) for pattern in _NETWORK_ALLOW_PATTERNS)


def _copy_task_constraints(constraints: TaskConstraints) -> TaskConstraints:
    return TaskConstraints(
        allow_network=constraints.allow_network,
        allow_delete=constraints.allow_delete,
        allow_memory_read=constraints.allow_memory_read,
        timeout_seconds=constraints.timeout_seconds,
        max_permission=constraints.max_permission,
    )


class RuntimeGateway:
    def __init__(self, config: AppConfig, workspace_root: Path) -> None:
        self.config = config
        self.workspace_root = workspace_root.resolve()
        self.workspace_manager = WorkspaceManager(self.workspace_root)
        self.store = SQLiteStore(config.sqlite.path)
        self.graph_memory = GraphMemoryStore(self.store)
        self.embedding_provider = build_embedding_provider(config.embedding)
        self.memory_manager = FileMemoryManager(
            self.workspace_root,
            vector_engine=VectorRecallEngine(self.embedding_provider),
        )
        self.hybrid_memory = HybridMemoryRetriever(self.memory_manager, self.graph_memory)
        self.workflow_compiler = WorkflowCompiler(self.workspace_root, self.graph_memory)
        self.scheduler = TaskScheduler(self.store)
        self.skill_loader = SkillLoader(self.workspace_root)
        self.skill_registry = SkillRegistry(self.store, self.graph_memory)
        self.skill_lifecycle = SkillLifecycleManager(
            self.store, self.workspace_root, self.workflow_compiler
        )
        self.prompt_manager = PromptManager(config.llm)
        self.llm_adapter = build_llm_adapter(config.llm)
        self.auto_skill_generator = AutoSkillGenerator(
            store=self.store,
            workspace_root=self.workspace_root,
            skill_loader=self.skill_loader,
            skill_lifecycle=self.skill_lifecycle,
            memory_manager=self.memory_manager,
            graph_memory=self.graph_memory,
            llm=self.llm_adapter,
            prompts=self.prompt_manager,
        )
        self.skill_lifecycle.sync_workflow_candidates()
        self.permission_manager = PermissionManager()
        self.llm_health: dict[str, Any] | None = None
        if config.llm.verify_on_startup:
            self.llm_health = self.llm_adapter.check_health()
        # ISSUE-001: artifact_manager must be built before the
        # components that persist LLM responses (llm_native executor,
        # router, responder) so they can capture their own calls.
        self.artifact_manager = TaskArtifactManager(self.workspace_manager)
        self.planner = Planner(self.llm_adapter, self.prompt_manager)
        self.response_generator = ResponseGenerator(
            self.llm_adapter,
            self.prompt_manager,
            self.store,
            artifact_manager=self.artifact_manager,
        )
        self.critic = Critic(self.llm_adapter, self.prompt_manager)
        self.completion_evaluator = CompletionEvaluator(self.llm_adapter, self.prompt_manager)
        self.dispatcher = SkillDispatcher(
            skill_loader=self.skill_loader,
            skill_registry=self.skill_registry,
            auto_skill_generator=self.auto_skill_generator,
            executors={
                "file": FileExecutor(),
                "python": PythonExecutor(),
                "browser": BrowserExecutor(),
                # META-004: LLM itself as an executor. Enables pure
                # text-generation tasks (knowledge questions, creative
                # writing, reasoning) without wrapping them as python
                # print(...) calls.
                "llm_native": LLMNativeExecutor(
                    self.llm_adapter, artifact_manager=self.artifact_manager
                ),
            },
        )
        self.interaction_router = InteractionRouter(
            self.llm_adapter,
            self.prompt_manager,
            artifact_manager=self.artifact_manager,
        )
        self.runtime = EmbeddedRuntime(
            store=self.store,
            workspace_manager=self.workspace_manager,
            artifact_manager=self.artifact_manager,
            memory_manager=self.memory_manager,
            graph_memory=self.graph_memory,
            hybrid_memory=self.hybrid_memory,
            workflow_compiler=self.workflow_compiler,
            skill_lifecycle=self.skill_lifecycle,
            planner=self.planner,
            critic=self.critic,
            completion_evaluator=self.completion_evaluator,
            execution_engine=ExecutionEngine(
                store=self.store,
                artifact_manager=self.artifact_manager,
                dispatcher=self.dispatcher,
                critic=self.critic,
                planner=self.planner,
                permission_manager=self.permission_manager,
                graph_memory=self.graph_memory,
            ),
            permission_manager=self.permission_manager,
            sandbox_config=self.config.sandbox,
        )

    def submit(
        self, request: TaskRequest, *, event_handlers: list[EventHandler] | None = None
    ) -> TaskExecutionReport:
        # META-006: direct CLI submissions (outside send_message) also
        # need a fresh per-turn recall cache, otherwise long-lived Gateway
        # instances would leak recall results across unrelated tasks.
        self.hybrid_memory.begin_turn(f"submit:{request.task_id}:{utc_now()}")
        event_bus, loaded_skills, skill_registry_entries = self._prepare_runtime(event_handlers)
        report = self.runtime.run(
            request,
            event_bus,
            loaded_skills=loaded_skills,
            skill_registry_entries=skill_registry_entries,
        )
        self._ensure_task_thread(request, report)
        return report

    def resume_task(
        self,
        task_id: str,
        *,
        request_overrides: dict[str, Any] | None = None,
        event_handlers: list[EventHandler] | None = None,
    ) -> TaskExecutionReport:
        # META-006: same as submit — fresh cache per resume invocation.
        self.hybrid_memory.begin_turn(f"resume:{task_id}:{utc_now()}")
        event_bus, loaded_skills, skill_registry_entries = self._prepare_runtime(event_handlers)
        return self.runtime.resume(
            task_id,
            event_bus,
            loaded_skills=loaded_skills,
            skill_registry_entries=skill_registry_entries,
            request_overrides=request_overrides,
        )

    def create_thread(self, *, title: str | None = None) -> ConversationThread:
        thread = ConversationThread(title=title or "CLI chat session")
        self.store.save_conversation_thread(thread)
        self.graph_memory.record_thread(thread)
        return thread

    def send_message(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        request_options: dict[str, Any] | None = None,
        event_handlers: list[EventHandler] | None = None,
    ) -> ConversationResponse:
        request_options = request_options or {}
        self._reconcile_stale_runtime_tasks()
        thread = self._resolve_thread(thread_id, message)
        # META-006: invalidate the per-turn recall cache so the router
        # pre-pass and the planner pre-pass share one scan of the graph
        # within this send_message call (and no leakage between turns).
        self.hybrid_memory.begin_turn(f"{thread.thread_id}:{utc_now()}")
        self.store.append_conversation_event(
            thread.thread_id,
            "conversation.message.received",
            {"message": message, "provided_thread_id": thread_id},
        )
        self.graph_memory.record_thread(thread)
        active_task, pending_checkpoint = self._reconcile_thread_runtime_state(thread)

        envelope = UserInputEnvelope(
            message=message,
            thread_id=thread.thread_id,
            metadata={"request_options": request_options},
        )
        thread_memory = self.hybrid_memory.recall_for_thread(
            thread_id=thread.thread_id,
            message=message,
            active_task_goal=active_task.goal if active_task else None,
        )
        route, route_response = self.interaction_router.decide(
            envelope,
            thread=thread,
            active_task=active_task,
            pending_checkpoint=pending_checkpoint,
            thread_context=thread_memory.get("thread_context", thread_memory),
        )
        self.store.append_conversation_event(
            thread.thread_id,
            "route.decided",
            {
                "input_type": str(route.input_type),
                "task_action": route.task_action,
                "target_task_id": route.target_task_id,
                "checkpoint_id": route.checkpoint_id,
                "reasoning": route.reasoning,
            },
        )

        user_turn = ConversationTurn(
            thread_id=thread.thread_id,
            role=ConversationTurnRole.USER,
            message=message,
            input_type=route.input_type,
            route=route.task_action,
            task_id=route.target_task_id,
            checkpoint_id=route.checkpoint_id,
            payload={"reasoning": route.reasoning, "metadata": route.metadata},
        )
        self.store.append_conversation_turn(user_turn)
        self.graph_memory.record_turn(user_turn)

        task_report: TaskExecutionReport | None = None
        checkpoint: PendingCheckpoint | None = None
        assistant_message = ""

        if route.input_type == UserInputType.NEW_TASK:
            request = self._build_task_request(
                goal=route.extracted_goal or message,
                thread=thread,
                request_options=request_options,
            )
            task_report = self.submit(request, event_handlers=event_handlers)
            thread.active_task_id = task_report.task.task_id
            checkpoint = self._sync_checkpoint(thread, task_report)
            self._record_thread_binding(thread, task_report.task.task_id, checkpoint)
            assistant_message = self._summarize_task_report(task_report, checkpoint)
        elif route.input_type == UserInputType.STATUS_QUERY:
            assistant_message = self._format_status(active_task)
        elif route.input_type == UserInputType.FAILURE_QUERY:
            assistant_message = self._format_failure(active_task)
        elif route.input_type == UserInputType.APPROVAL_RESPONSE:
            task_report, checkpoint, assistant_message = self._handle_approval_response(
                thread,
                route,
                pending_checkpoint,
                message=message,
                event_handlers=event_handlers,
            )
        elif route.input_type == UserInputType.TASK_UPDATE:
            target_task = (
                self.store.get_task(route.target_task_id)
                if route.target_task_id
                else active_task
            )
            task_report, checkpoint, assistant_message = self._handle_task_update(
                thread,
                route,
                target_task,
                message,
                event_handlers=event_handlers,
            )
        elif route.input_type == UserInputType.REPLAN_REQUEST:
            target_task = (
                self.store.get_task(route.target_task_id)
                if route.target_task_id
                else active_task
            )
            task_report, checkpoint, assistant_message = self._handle_replan_request(
                thread,
                route,
                target_task,
                event_handlers=event_handlers,
            )
        elif route.input_type == UserInputType.KNOWLEDGE_QUERY:
            assistant_message = self._handle_knowledge_query(
                message, thread=thread, active_task=active_task
            )
        elif route.input_type == UserInputType.SYSTEM_REQUEST:
            assistant_message = self._handle_system_request(
                thread=thread,
                active_task=active_task,
                pending_checkpoint=pending_checkpoint,
            )
        else:
            assistant_message = "当前消息已记录，但暂未匹配到可执行的任务路由。"

        # Generate natural language reply via LLM, keeping structured message as fallback
        conversation_history = self.store.list_conversation_turns(thread.thread_id)
        response_context = ResponseContext(
            route=route,
            user_message=message,
            task_report=task_report,
            checkpoint=checkpoint,
            active_task=active_task,
            conversation_history=conversation_history,
            fallback_message=assistant_message,
        )
        assistant_message = self.response_generator.generate(context=response_context)

        thread.updated_at = utc_now()
        self.store.save_conversation_thread(thread)
        self.graph_memory.record_thread(thread)
        assistant_turn = ConversationTurn(
            thread_id=thread.thread_id,
            role=ConversationTurnRole.ASSISTANT,
            message=assistant_message,
            input_type=route.input_type,
            route=route.task_action,
            task_id=task_report.task.task_id if task_report else route.target_task_id,
            checkpoint_id=checkpoint.checkpoint_id if checkpoint else route.checkpoint_id,
            payload={"reasoning": route.reasoning, "metadata": route.metadata},
        )
        self.store.append_conversation_turn(assistant_turn)
        self.graph_memory.record_turn(assistant_turn)
        return ConversationResponse(
            thread=thread,
            route=route,
            assistant_message=assistant_message,
            task_report=task_report,
            checkpoint=checkpoint,
        )

    def list_tasks(self, *, limit: int = 20, status: str | None = None):
        self._reconcile_stale_runtime_tasks()
        return self.store.list_tasks(limit=limit, status=status)

    def show_task(self, task_id: str) -> dict[str, Any]:
        self._reconcile_stale_runtime_tasks()
        self.skill_lifecycle.sync_workflow_candidates()
        task = self.store.get_task(task_id)
        if task is None:
            return {}
        completion_evaluation = self._load_completion_evaluation(task_id)
        return {
            "task": task,
            "task_graph": self.store.get_task_graph(task_id),
            "task_nodes": self.store.list_task_nodes(task_id),
            "critic_verdicts": self.store.list_critic_verdicts(task_id),
            "completion_evaluation": completion_evaluation,
            "skill_registry": self.store.list_skill_registry_entries(),
            "workflow_candidates": self.workflow_compiler.list_candidates(task_id=task_id),
            "events": self.store.list_events(task_id),
            "permissions": self.store.list_permissions(task_id),
            "execution_logs": self.store.list_execution_logs(task_id),
            "resume_attempts": self.store.list_task_resume_attempts(task_id),
            "graph_memory": self.graph_memory.subgraph_for_task(task_id),
        }

    def _load_completion_evaluation(self, task_id: str) -> CompletionEvaluation | None:
        path = self.artifact_manager.task_dir(task_id) / "completion" / "evaluation.json"
        if not path.exists():
            return None
        try:
            return CompletionEvaluation.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return None

    def show_thread(self, thread_id: str) -> dict[str, Any]:
        self._reconcile_stale_runtime_tasks()
        thread = self.store.get_conversation_thread(thread_id)
        if thread is None:
            return {}
        active_task = self.store.get_task(thread.active_task_id) if thread.active_task_id else None
        pending_checkpoint = (
            self.store.get_pending_checkpoint(thread.pending_checkpoint_id)
            if thread.pending_checkpoint_id
            else None
        )
        return {
            "thread": thread,
            "turns": self.store.list_conversation_turns(thread_id),
            "active_task": active_task,
            "pending_checkpoint": pending_checkpoint,
            "events": self.store.list_conversation_events(thread_id),
            "graph_memory": self.graph_memory.subgraph_for_thread(thread_id),
        }

    def list_threads(self, *, limit: int = 20, status: str | None = None):
        self._reconcile_stale_runtime_tasks()
        return self.store.list_conversation_threads(limit=limit, status=status)

    def check_llm(self) -> dict[str, Any]:
        self.llm_health = self.llm_adapter.check_health()
        return self.llm_health

    def llm_stats(
        self,
        *,
        task_id: str | None = None,
        thread_id: str | None = None,
        prompt_name: str | None = None,
        limit: int = 0,
    ) -> dict[str, Any]:
        """Aggregate observability fields from persisted LLM call logs.

        ISSUE-001: per-call metrics (latency, tier, attempts, usage) live in
        two mirrored locations:

        * task-scoped calls (planner/critic/llm_native/replan) →
          ``workspace/tasks/<task_id>/llm_calls/*.json``
        * thread-scoped calls (router/responder) →
          ``workspace/threads/<thread_id>/llm_calls/*.json``

        Filters:

        * ``task_id``   — only scan this task dir (ignores threads).
        * ``thread_id`` — only scan this thread dir (ignores tasks).
        * neither      — scan all task dirs AND all thread dirs together.
        * ``limit``    — cap how many task dirs are scanned (sorted by mtime);
                         applied to the task scan only.
        * ``prompt_name`` — post-filter each call by ``payload.prompt_name``.
        """
        tasks_root = self.workspace_root / "tasks"
        threads_root = self.workspace_root / "threads"

        scan_tasks = task_id is not None or thread_id is None
        scan_threads = thread_id is not None or task_id is None

        empty_result: dict[str, Any] = {
            "total_calls": 0,
            "tasks_scanned": 0,
            "threads_scanned": 0,
            "task_root": str(tasks_root),
            "thread_root": str(threads_root),
            "by_prompt": {},
            "by_tier": {},
            "by_scope": {"task": 0, "thread": 0},
            "latency_ms": {"avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0},
            "tokens": {"prompt": 0, "completion": 0, "total": 0},
            "attempts": {"avg": 0.0, "max": 0},
        }

        task_dirs: list[Path] = []
        if scan_tasks and tasks_root.exists():
            if task_id:
                candidate = tasks_root / task_id
                if candidate.exists():
                    task_dirs = [candidate]
            else:
                task_dirs = sorted(
                    (p for p in tasks_root.iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if limit > 0:
                    task_dirs = task_dirs[:limit]

        thread_dirs: list[Path] = []
        if scan_threads and threads_root.exists():
            if thread_id:
                candidate = threads_root / thread_id
                if candidate.exists():
                    thread_dirs = [candidate]
            else:
                thread_dirs = sorted(
                    (p for p in threads_root.iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if limit > 0:
                    thread_dirs = thread_dirs[:limit]

        if not task_dirs and not thread_dirs:
            return empty_result

        total_calls = 0
        latencies: list[float] = []
        attempts_list: list[int] = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        by_prompt: dict[str, dict[str, Any]] = {}
        by_tier: dict[str, int] = {}
        scope_counts = {"task": 0, "thread": 0}

        def _ingest(payload: dict[str, Any], scope: str) -> None:
            nonlocal total_calls, prompt_tokens, completion_tokens, total_tokens
            if prompt_name and payload.get("prompt_name") != prompt_name:
                return
            total_calls += 1
            scope_counts[scope] = scope_counts.get(scope, 0) + 1

            latency = payload.get("latency_ms")
            if isinstance(latency, (int, float)):
                latencies.append(float(latency))

            attempts = payload.get("attempts")
            if isinstance(attempts, int):
                attempts_list.append(attempts)

            tier = payload.get("tier") or "unknown"
            by_tier[tier] = by_tier.get(tier, 0) + 1

            usage = payload.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens += int(usage.get("completion_tokens", 0) or 0)
            total_tokens += int(usage.get("total_tokens", 0) or 0)

            pname = payload.get("prompt_name") or "unknown"
            bucket = by_prompt.setdefault(
                pname,
                {"calls": 0, "total_latency_ms": 0.0, "total_tokens": 0},
            )
            bucket["calls"] += 1
            if isinstance(latency, (int, float)):
                bucket["total_latency_ms"] += float(latency)
            bucket["total_tokens"] += int(usage.get("total_tokens", 0) or 0)

        def _scan_dir(roots: list[Path], scope: str) -> None:
            for root in roots:
                llm_dir = root / "llm_calls"
                if not llm_dir.exists():
                    continue
                for call_path in llm_dir.glob("*.json"):
                    try:
                        payload = json.loads(call_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    _ingest(payload, scope)

        _scan_dir(task_dirs, "task")
        _scan_dir(thread_dirs, "thread")

        def _percentile(values: list[float], pct: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
            return round(ordered[idx], 2)

        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
        max_latency = round(max(latencies), 2) if latencies else 0.0
        avg_attempts = round(sum(attempts_list) / len(attempts_list), 3) if attempts_list else 0.0
        max_attempts = max(attempts_list) if attempts_list else 0

        for pname, bucket in by_prompt.items():
            calls = bucket["calls"] or 1
            bucket["avg_latency_ms"] = round(bucket["total_latency_ms"] / calls, 2)

        return {
            "total_calls": total_calls,
            "tasks_scanned": len(task_dirs),
            "threads_scanned": len(thread_dirs),
            "task_root": str(tasks_root),
            "thread_root": str(threads_root),
            "by_prompt": by_prompt,
            "by_tier": by_tier,
            "by_scope": scope_counts,
            "latency_ms": {
                "avg": avg_latency,
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "max": max_latency,
            },
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": total_tokens,
            },
            "attempts": {"avg": avg_attempts, "max": max_attempts},
        }

    def list_skills(self, *, status: str | None = None) -> list[Any]:
        return self.skill_lifecycle.list_skills(status=status)

    def show_skill(self, skill_name: str) -> dict[str, Any]:
        return self.skill_lifecycle.show_skill(skill_name)

    def import_skill(self, skill_path: str) -> dict[str, Any]:
        draft = self.skill_lifecycle.import_skill(Path(skill_path))
        return self.show_skill(draft.skill_name)

    def generate_skill(
        self,
        *,
        skill_name: str,
        generation_type: str,
        goal: str,
        payload_example: dict[str, Any] | None = None,
        trigger: str = "user_request",
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        return self.auto_skill_generator.generate_skill(
            skill_name=skill_name,
            generation_type=generation_type,
            goal=goal,
            payload_example=payload_example or {},
            trigger=trigger,
            task_id=task_id,
            node_id=node_id,
        )

    def validate_skill(self, skill_name: str, *, version: str | None = None) -> dict[str, Any]:
        return self.skill_lifecycle.validate_skill(skill_name, version=version)

    def approve_skill(
        self,
        skill_name: str,
        *,
        version: str | None = None,
        approver: str = "local_user",
        approved: bool = True,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        return self.skill_lifecycle.approve_skill(
            skill_name,
            version=version,
            approver=approver,
            approved=approved,
            rationale=rationale,
        )

    def promote_workflow(
        self,
        task_id: str,
        *,
        skill_name: str | None = None,
        version: str | None = None,
        approver: str = "local_user",
        rationale: str | None = None,
    ) -> dict[str, Any]:
        payload = self.skill_lifecycle.promote_workflow(
            task_id,
            skill_name=skill_name,
            version=version,
            approver=approver,
            rationale=rationale,
        )
        generated_skill = self.auto_skill_generator.generate_for_workflow_candidate(
            payload["candidate"]
        )
        candidate_metadata = {
            "solidification_triggered_at": utc_now(),
        }
        if generated_skill is None:
            candidate_metadata["solidification_result"] = "unsupported_generation_shape"
        else:
            generated_version = None
            if generated_skill.get("draft") is not None:
                generated_version = generated_skill["draft"].version
            elif generated_skill.get("registry_entry") is not None:
                generated_version = generated_skill["registry_entry"].get("version")
            candidate_metadata.update(
                {
                    "solidification_result": "generated_skill_created",
                    "solidified_generated_skill_name": generated_skill["skill_name"],
                    "solidified_generated_version": generated_version,
                    "solidified_generated_status": generated_skill["status"],
                }
            )
        updated_candidate = self.workflow_compiler.update_candidate_status(
            str(payload["candidate"]["skill_name"]),
            version=str(payload["candidate"].get("version"))
            if payload["candidate"].get("version") is not None
            else None,
            status=str(payload["candidate"].get("status")),
            metadata=candidate_metadata,
        )
        if updated_candidate is not None:
            payload["candidate"] = updated_candidate
        payload["generated_skill"] = generated_skill
        self.graph_memory.record_workflow_candidate(payload["candidate"])
        return payload

    def run_scheduler(
        self,
        *,
        now: str | None = None,
        event_handlers: list[EventHandler] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        expired = self.scheduler.expired_checkpoints(now=now)
        expired_ids = {checkpoint.checkpoint_id for checkpoint in expired}
        for checkpoint in expired:
            results.append(self._timeout_checkpoint(checkpoint, now=now))
        for checkpoint in self.scheduler.due_timer_checkpoints(now=now):
            if checkpoint.checkpoint_id in expired_ids:
                continue
            results.append(
                self._resume_pending_checkpoint(
                    checkpoint,
                    trigger="timer",
                    message="scheduler 定时唤醒恢复执行",
                    response_payload={"resumed_at": now or utc_now()},
                    event_handlers=event_handlers,
                )
            )
        return results

    def signal_external_event(
        self,
        event_key: str,
        *,
        payload: dict[str, Any] | None = None,
        now: str | None = None,
        event_handlers: list[EventHandler] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for checkpoint in self.scheduler.checkpoints_for_event(event_key):
            expires_at = self._parse_timestamp(checkpoint.payload.get("expires_at"))
            current = self._parse_timestamp(now or utc_now())
            if expires_at is not None and current is not None and expires_at <= current:
                results.append(self._timeout_checkpoint(checkpoint, now=now))
                continue
            results.append(
                self._resume_pending_checkpoint(
                    checkpoint,
                    trigger="external_event",
                    message=f"收到外部事件 {event_key}，恢复执行",
                    response_payload={
                        "event_key": event_key,
                        "event_payload": payload or {},
                        "resumed_at": now or utc_now(),
                    },
                    event_handlers=event_handlers,
                )
            )
        return results

    def activate_skill(self, skill_name: str, *, version: str | None = None) -> dict[str, Any]:
        return self.skill_lifecycle.activate_skill(skill_name, version=version)

    def rollback_skill(self, skill_name: str, *, to_version: str | None = None) -> dict[str, Any]:
        return self.skill_lifecycle.rollback_skill(skill_name, to_version=to_version)

    def deprecate_skill(self, skill_name: str) -> dict[str, Any]:
        return self.skill_lifecycle.deprecate_skill(skill_name)

    def _prepare_runtime(
        self,
        event_handlers: list[EventHandler] | None,
    ) -> tuple[EventBus, dict[str, Any], list[dict[str, Any]]]:
        loaded_skills = self.skill_loader.load_all()
        self.skill_lifecycle.sync_workflow_candidates()
        self.skill_registry.sync(loaded_skills)
        self._check_executor_skill_consistency()
        for entry in self.skill_registry.list_entries():
            self.graph_memory.record_skill(entry.to_dict())
        event_bus = EventBus()
        event_bus.subscribe("*", EventRecorder(self.store))
        for handler in event_handlers or []:
            event_bus.subscribe("*", handler)
        return (
            event_bus,
            loaded_skills,
            [entry.to_dict() for entry in self.skill_registry.list_entries(active_only=True)],
        )

    def _reconcile_stale_runtime_tasks(self) -> list[str]:
        """Close interrupted transient task states before routing/status reads.

        A previous process can exit after persisting ``planning``/``running``
        but before finalization. Leaving those rows live makes status queries
        and thread routing believe old tasks are still in flight. Use a
        conservative floor so a normal long command is not immediately marked
        stale when the configured per-task timeout is small.
        """

        stale_after_seconds = max(
            _STALE_TASK_MIN_SECONDS,
            int(self.config.gateway.task_timeout_seconds) * 4,
        )
        now_dt = datetime.now(tz=timezone.utc)
        reconciled: list[str] = []
        for status in _STALE_TASK_STATUSES:
            for task in self.store.list_tasks(limit=1000, status=str(status)):
                try:
                    updated_at = self._parse_timestamp(task.updated_at)
                except (TypeError, ValueError):
                    updated_at = None
                if updated_at is None:
                    continue
                age_seconds = (now_dt - updated_at).total_seconds()
                if age_seconds < stale_after_seconds:
                    continue
                now = utc_now()
                message = (
                    f"任务在 {int(age_seconds)} 秒内没有状态更新，"
                    "已按中断运行标记为 failed。"
                )
                task.status = TaskStatus.FAILED
                task.summary = task.summary or message
                task.error_code = "stale_task_reconciled"
                task.error_message = message
                task.updated_at = now
                self.store.update_task(
                    task.task_id,
                    status=task.status,
                    updated_at=task.updated_at,
                    summary=task.summary,
                    plan_summary=task.plan_summary,
                    graph_id=task.graph_id,
                    error_code=task.error_code,
                    error_message=task.error_message,
                )
                self.store.append_event(
                    TaskEvent(
                        event_id=f"evt_{uuid.uuid4().hex[:12]}",
                        task_id=task.task_id,
                        event_type="task.stale_reconciled",
                        status=str(task.status),
                        payload={
                            "previous_status": str(status),
                            "age_seconds": int(age_seconds),
                            "stale_after_seconds": stale_after_seconds,
                            "message": message,
                        },
                    )
                )
                self.graph_memory.record_task(task)
                reconciled.append(task.task_id)
        return reconciled

    def _check_executor_skill_consistency(self) -> None:
        """META-001: verify executor.supported_actions matches skill.yaml contracts.

        For each executor that maps to a builtin skill (via EXECUTOR_TO_BUILTIN),
        the set of action names declared in the executor class MUST equal the
        set of actions declared in the corresponding builtin skill manifest.
        Drift here means a developer added a new action to one side but not
        the other — we fail fast at startup instead of at dispatch time when
        the user is already waiting on a task.

        Executors with an empty `supported_actions` tuple are skipped (they
        don't branch on action, e.g. llm_native).
        """
        from omniagent.skill_registry import EXECUTOR_TO_BUILTIN

        for executor_name, executor in self.dispatcher.executors.items():
            declared_on_executor = set(executor.supported_actions or ())
            if not declared_on_executor:
                continue
            builtin_name = EXECUTOR_TO_BUILTIN.get(executor_name)
            if not builtin_name:
                continue
            entry = self.skill_registry.store.get_skill_registry_entry(builtin_name)
            if entry is None:
                raise OmniAgentError(
                    ErrorCode.SKILL_ERROR,
                    f"Executor {executor_name} 声明 supported_actions={sorted(declared_on_executor)}，"
                    f"但未找到对应 builtin skill: {builtin_name}",
                )
            declared_on_skill = {c.action for c in entry.contracts}
            if declared_on_skill != declared_on_executor:
                missing_in_skill = sorted(declared_on_executor - declared_on_skill)
                missing_in_executor = sorted(declared_on_skill - declared_on_executor)
                raise OmniAgentError(
                    ErrorCode.SKILL_ERROR,
                    f"Executor {executor_name} 与 skill {builtin_name} 的 action 集合不一致: "
                    f"executor 缺失 {missing_in_executor}，skill 缺失 {missing_in_skill}",
                    details={
                        "executor": executor_name,
                        "builtin_skill": builtin_name,
                        "executor_supported_actions": sorted(declared_on_executor),
                        "skill_contract_actions": sorted(declared_on_skill),
                        "missing_in_skill": missing_in_skill,
                        "missing_in_executor": missing_in_executor,
                    },
                )

    def _resolve_thread(self, thread_id: str | None, message: str) -> ConversationThread:
        if thread_id:
            thread = self.store.get_conversation_thread(thread_id)
            if thread is None:
                raise ValueError(f"thread not found: {thread_id}")
            return thread

        return self.create_thread(title=message.strip()[:48] or "Untitled thread")

    def _reconcile_thread_runtime_state(
        self, thread: ConversationThread
    ) -> tuple[Task | None, PendingCheckpoint | None]:
        """Refresh active task/checkpoint pointers before routing a new turn.

        Conversation state is persisted in two places: the thread row and the
        task/checkpoint tables. If a prior turn was interrupted after creating
        a task but before rebinding the thread, the thread row can point at an
        older completed task. Route decisions such as "继续" must use the
        newest task for this thread, otherwise follow-ups attach to stale
        small-talk tasks.
        """
        previous_active_task_id = thread.active_task_id
        previous_checkpoint_id = thread.pending_checkpoint_id

        pending_checkpoint = self._current_pending_checkpoint(thread)
        active_task: Task | None = None
        if pending_checkpoint is not None:
            active_task = self.store.get_task(pending_checkpoint.task_id)
        else:
            recent_tasks = self.store.list_tasks_for_thread(
                thread.thread_id, limit=_THREAD_TASK_LOOKBACK
            )
            if recent_tasks:
                active_task = recent_tasks[0]
            elif thread.active_task_id:
                active_task = self.store.get_task(thread.active_task_id)

        thread.active_task_id = active_task.task_id if active_task else None
        thread.pending_checkpoint_id = (
            pending_checkpoint.checkpoint_id if pending_checkpoint else None
        )

        if (
            previous_active_task_id != thread.active_task_id
            or previous_checkpoint_id != thread.pending_checkpoint_id
        ):
            thread.updated_at = utc_now()
            self.store.save_conversation_thread(thread)
            self.store.append_conversation_event(
                thread.thread_id,
                "thread.reconciled",
                {
                    "previous_active_task_id": previous_active_task_id,
                    "active_task_id": thread.active_task_id,
                    "previous_pending_checkpoint_id": previous_checkpoint_id,
                    "pending_checkpoint_id": thread.pending_checkpoint_id,
                },
            )
            self.graph_memory.record_thread(thread)
        return active_task, pending_checkpoint

    def _current_pending_checkpoint(
        self, thread: ConversationThread
    ) -> PendingCheckpoint | None:
        checkpoint = None
        if thread.pending_checkpoint_id:
            checkpoint = self.store.get_pending_checkpoint(thread.pending_checkpoint_id)
            if checkpoint is not None and checkpoint.status != PendingCheckpointStatus.PENDING:
                checkpoint = None
        if checkpoint is None:
            checkpoint = self.store.get_latest_pending_checkpoint_for_thread(thread.thread_id)
        return checkpoint

    def _ensure_task_thread(self, request: TaskRequest, task_report: TaskExecutionReport) -> None:
        if request.metadata.get("thread_id"):
            return
        if task_report.task.status not in {TaskStatus.WAITING_INPUT, TaskStatus.SUSPENDED}:
            return
        thread = self.create_thread(title=f"task:{task_report.task.task_id}")
        thread.active_task_id = task_report.task.task_id
        checkpoint = self._sync_checkpoint(thread, task_report)
        self._record_thread_binding(thread, task_report.task.task_id, checkpoint)
        thread.updated_at = utc_now()
        self.store.save_conversation_thread(thread)
        self.graph_memory.record_thread(thread)

    # ISSUE-012: metadata keys that describe *a previous task's* lifecycle
    # (approval / permission / path grant). When forking a request for a new
    # task lifecycle, these must be stripped — otherwise the new task thinks
    # it is resuming an old checkpoint and carries the old "立即生成" / "允许l3"
    # reply as if it were its own intent.
    _STALE_APPROVAL_METADATA_KEYS = frozenset(
        {
            "approved_checkpoint_id",
            "checkpoint_response",
            "grant_path_access",
        }
    )

    def _resolve_goal_from_approval(
        self,
        goal: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Reverse-lookup the parent task's goal for approval-triggered tasks.

        Context (ISSUE-012): when a task is created or resumed with
        ``metadata.approved_checkpoint_id`` set, the user's literal approval
        message ("立即生成" / "允许l3" / "同意") must not be treated as the
        task goal. Look up the checkpoint → parent task and use the parent
        task's original goal instead. The user's literal message is preserved
        under ``metadata.user_message`` for audit and for the Responder.

        Returns ``(resolved_goal, updated_metadata)`` — the metadata is always
        a fresh dict (safe to mutate).
        """
        resolved_goal = goal
        resolved_metadata = dict(metadata)
        checkpoint_id = resolved_metadata.get("approved_checkpoint_id")
        if not checkpoint_id:
            return resolved_goal, resolved_metadata
        try:
            checkpoint = self.store.get_pending_checkpoint(str(checkpoint_id))
        except Exception:  # noqa: BLE001 — storage failures must not break task creation
            checkpoint = None
        if checkpoint is None:
            return resolved_goal, resolved_metadata
        parent_task = self.store.get_task(checkpoint.task_id)
        if parent_task is None or not parent_task.goal:
            return resolved_goal, resolved_metadata
        parent_goal = str(parent_task.goal).strip()
        if parent_goal and parent_goal != goal:
            resolved_metadata.setdefault("user_message", goal)
            resolved_metadata.setdefault("parent_task_id", parent_task.task_id)
            resolved_metadata.setdefault("original_goal", parent_goal)
            resolved_goal = parent_goal
        return resolved_goal, resolved_metadata

    def _build_task_request(
        self,
        *,
        goal: str,
        thread: ConversationThread,
        request_options: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> TaskRequest:
        base_metadata = dict(request_options.get("metadata", {}))
        base_metadata["thread_id"] = thread.thread_id
        if metadata:
            base_metadata.update(metadata)
        # ISSUE-012: if the caller passed approval metadata (e.g. via request
        # inheritance from a prior task), rewrite the goal to point at the
        # real parent task goal instead of the user's literal approval reply.
        resolved_goal, base_metadata = self._resolve_goal_from_approval(goal, base_metadata)
        constraints_option = request_options.get("constraints")
        if isinstance(constraints_option, TaskConstraints):
            constraints = _copy_task_constraints(constraints_option)
        elif isinstance(constraints_option, dict):
            constraints = TaskConstraints(
                allow_network=bool(constraints_option.get("allow_network", False)),
                allow_delete=bool(constraints_option.get("allow_delete", False)),
                allow_memory_read=bool(constraints_option.get("allow_memory_read", True)),
                timeout_seconds=int(constraints_option.get("timeout_seconds", 30)),
                max_permission=PermissionLevel(
                    str(constraints_option.get("max_permission", PermissionLevel.L2))
                ),
            )
        else:
            max_permission = request_options.get("max_permission", PermissionLevel.L2)
            constraints = TaskConstraints(
                allow_network=bool(request_options.get("allow_network", False)),
                allow_delete=bool(request_options.get("allow_delete", False)),
                allow_memory_read=bool(request_options.get("allow_memory_read", True)),
                timeout_seconds=int(request_options.get("timeout_seconds", 30)),
                max_permission=PermissionLevel(str(max_permission)),
            )
        if _explicitly_allows_network(goal) or _explicitly_allows_network(resolved_goal):
            constraints.allow_network = True
            base_metadata["user_explicit_network_authorization"] = True
        return TaskRequest(
            goal=resolved_goal,
            expected_output=request_options.get("expected_output"),
            payload=dict(request_options.get("payload", {})),
            executor=str(request_options.get("executor", "auto")),
            skill_name=request_options.get("skill_name"),
            mode=request_options.get("mode", AutonomyMode.SEMI_AUTO),
            source=TaskSource.CONVERSATION,
            context_refs=list(request_options.get("context_refs", [])),
            constraints=constraints,
            preferred_skills=list(request_options.get("preferred_skills", [])),
            metadata=base_metadata,
            execution_mode=str(request_options.get("execution_mode", "plan_and_execute")),
        )

    def _sync_checkpoint(
        self,
        thread: ConversationThread,
        task_report: TaskExecutionReport,
    ) -> PendingCheckpoint | None:
        if task_report.task.status not in {TaskStatus.WAITING_INPUT, TaskStatus.SUSPENDED}:
            thread.pending_checkpoint_id = None
            return None

        checkpoint_payload = dict(task_report.result.output.get("checkpoint", {}))
        checkpoint_type = PendingCheckpointType(
            str(checkpoint_payload.get("checkpoint_type", "approval"))
        )
        checkpoint = PendingCheckpoint(
            thread_id=thread.thread_id,
            task_id=task_report.task.task_id,
            node_id=checkpoint_payload.get("node_id"),
            checkpoint_type=checkpoint_type,
            title=self._checkpoint_title(checkpoint_type),
            prompt=str(
                checkpoint_payload.get("prompt")
                or checkpoint_payload.get("rationale")
                or task_report.result.message
            ),
            required_permission=checkpoint_payload.get("permission"),
            required_level=(
                PermissionLevel(str(checkpoint_payload["level"]))
                if checkpoint_payload.get("level")
                else None
            ),
            payload=checkpoint_payload,
        )
        self.store.save_pending_checkpoint(checkpoint)
        thread.pending_checkpoint_id = checkpoint.checkpoint_id
        return checkpoint

    def _handle_approval_response(
        self,
        thread: ConversationThread,
        route: RouteDecision,
        checkpoint: PendingCheckpoint | None,
        *,
        message: str,
        event_handlers: list[EventHandler] | None,
    ) -> tuple[TaskExecutionReport | None, PendingCheckpoint | None, str]:
        if checkpoint is None:
            thread.pending_checkpoint_id = None
            return None, None, "当前没有待处理的审批 checkpoint。"

        if checkpoint.checkpoint_type in {
            PendingCheckpointType.TIMER,
            PendingCheckpointType.EXTERNAL_EVENT,
        }:
            if route.task_action == "reject":
                checkpoint.status = PendingCheckpointStatus.CANCELLED
                checkpoint.resolved_at = utc_now()
                checkpoint.updated_at = checkpoint.resolved_at
                self.store.save_pending_checkpoint(checkpoint)
                self.store.append_conversation_event(
                    thread.thread_id,
                    "long_task.cancelled",
                    {
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "task_id": checkpoint.task_id,
                        "checkpoint_type": str(checkpoint.checkpoint_type),
                    },
                )
                thread.pending_checkpoint_id = None
                thread.active_task_id = checkpoint.task_id
                self._record_thread_binding(thread, checkpoint.task_id, None)
                self.store.update_task(
                    checkpoint.task_id,
                    status=TaskStatus.CANCELLED,
                    updated_at=utc_now(),
                    summary="用户取消了长期等待任务",
                    error_code=None,
                    error_message="用户取消了长期等待任务",
                )
                self._mark_checkpoint_node_terminal(
                    checkpoint.task_id,
                    checkpoint.node_id,
                    status="failed",
                    message="用户取消了长期等待任务",
                )
                self._append_task_event(
                    checkpoint.task_id,
                    "task.cancelled",
                    TaskStatus.CANCELLED,
                    {
                        "message": "用户取消了长期等待任务",
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "checkpoint_type": str(checkpoint.checkpoint_type),
                    },
                )
                self._record_resume_attempt(
                    checkpoint.task_id,
                    checkpoint.checkpoint_id,
                    trigger="manual_cancel",
                    status="cancelled",
                    message="用户取消了长期等待任务",
                    payload={"checkpoint_type": str(checkpoint.checkpoint_type)},
                )
                return None, checkpoint, "已取消当前长期等待任务。"
            return (
                None,
                checkpoint,
                "当前 checkpoint 需要等待时间点或外部事件，不能通过对话直接恢复；请使用 scheduler run 或 scheduler signal。",
            )

        checkpoint.updated_at = utc_now()
        if checkpoint.checkpoint_type == PendingCheckpointType.DECISION:
            checkpoint.status = PendingCheckpointStatus.RESOLVED
            checkpoint.resolved_at = checkpoint.updated_at
            self.store.save_pending_checkpoint(checkpoint)
            self.store.append_conversation_event(
                thread.thread_id,
                "decision.responded",
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "task_id": checkpoint.task_id,
                    "decision": route.task_action,
                    "message": message,
                },
            )
            thread.pending_checkpoint_id = None
            thread.active_task_id = checkpoint.task_id
            task_report = self.resume_task(
                checkpoint.task_id,
                request_overrides=self._build_checkpoint_overrides(
                    checkpoint, route=route, message=message
                ),
                event_handlers=event_handlers,
            )
            next_checkpoint = self._sync_checkpoint(thread, task_report)
            self._record_thread_binding(thread, checkpoint.task_id, next_checkpoint)
            return (
                task_report,
                next_checkpoint,
                self._summarize_task_report(task_report, next_checkpoint),
            )

        if route.task_action == "reject":
            checkpoint.status = PendingCheckpointStatus.REJECTED
            checkpoint.resolved_at = checkpoint.updated_at
            self.store.save_pending_checkpoint(checkpoint)
            self.store.append_conversation_event(
                thread.thread_id,
                "approval.responded",
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "task_id": checkpoint.task_id,
                    "decision": "reject",
                },
            )
            thread.pending_checkpoint_id = None
            thread.active_task_id = checkpoint.task_id
            task = self.store.get_task(checkpoint.task_id)
            if task is not None:
                self.store.update_task(
                    task.task_id,
                    status=TaskStatus.CANCELLED,
                    updated_at=utc_now(),
                    summary="用户拒绝继续执行",
                    error_code=None,
                    error_message="用户拒绝继续执行",
                )
            return None, checkpoint, "已拒绝当前审批请求，对应任务已停止。"

        checkpoint.status = PendingCheckpointStatus.RESOLVED
        checkpoint.resolved_at = checkpoint.updated_at
        self.store.save_pending_checkpoint(checkpoint)
        self.store.append_conversation_event(
            thread.thread_id,
            "approval.responded",
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "task_id": checkpoint.task_id,
                "decision": "approve",
            },
        )
        thread.pending_checkpoint_id = None
        thread.active_task_id = checkpoint.task_id

        task_report = self.resume_task(
            checkpoint.task_id,
            request_overrides=self._build_checkpoint_overrides(
                checkpoint, route=route, message=message
            ),
            event_handlers=event_handlers,
        )
        next_checkpoint = self._sync_checkpoint(thread, task_report)
        self._record_thread_binding(thread, checkpoint.task_id, next_checkpoint)
        return (
            task_report,
            next_checkpoint,
            self._summarize_task_report(task_report, next_checkpoint),
        )

    def _handle_task_update(
        self,
        thread: ConversationThread,
        route: RouteDecision,
        active_task: Any,
        message: str,
        *,
        event_handlers: list[EventHandler] | None,
    ) -> tuple[TaskExecutionReport | None, PendingCheckpoint | None, str]:
        if active_task is None:
            return None, None, "当前 thread 没有 active task，无法更新。"

        if route.task_action == "cancel":
            self.store.update_task(
                active_task.task_id,
                status=TaskStatus.CANCELLED,
                updated_at=utc_now(),
                summary="用户在会话中取消任务",
                error_code=None,
                error_message="用户在会话中取消任务",
            )
            thread.active_task_id = active_task.task_id
            thread.pending_checkpoint_id = None
            self._record_thread_binding(thread, active_task.task_id, None)
            return None, None, f"任务 {active_task.task_id} 已取消。"

        if route.task_action == "retry":
            request = self._fork_request(
                active_task.task_id, thread=thread, metadata={"parent_task_id": active_task.task_id}
            )
        elif route.task_action == "continue":
            continuation = (route.extracted_goal or message).strip()
            request = self._fork_request(
                active_task.task_id,
                thread=thread,
                goal=continuation or active_task.goal,
                expected_output=continuation or active_task.expected_output,
                metadata={
                    "parent_task_id": active_task.task_id,
                    "update_type": "continue",
                },
            )
        else:
            request = self._fork_request(
                active_task.task_id,
                thread=thread,
                goal=f"{active_task.goal}\n补充要求: {route.extracted_goal or message}",
                metadata={
                    "parent_task_id": active_task.task_id,
                    "update_type": route.task_action or "supplement",
                },
            )

        task_report = self.submit(request, event_handlers=event_handlers)
        thread.active_task_id = task_report.task.task_id
        checkpoint = self._sync_checkpoint(thread, task_report)
        self._record_thread_binding(thread, task_report.task.task_id, checkpoint)
        return task_report, checkpoint, self._summarize_task_report(task_report, checkpoint)

    def _handle_replan_request(
        self,
        thread: ConversationThread,
        route: RouteDecision,
        active_task: Any,
        *,
        event_handlers: list[EventHandler] | None,
    ) -> tuple[TaskExecutionReport | None, PendingCheckpoint | None, str]:
        if active_task is None:
            return None, None, "当前 thread 没有可重规划的 active task。"

        request = self._fork_request(
            active_task.task_id,
            thread=thread,
            goal=f"{active_task.goal}\n请根据以下新要求重新规划并执行: {route.extracted_goal or ''}".strip(),
            metadata={"parent_task_id": active_task.task_id, "phase": "conversation_replan"},
        )
        task_report = self.submit(request, event_handlers=event_handlers)
        thread.active_task_id = task_report.task.task_id
        checkpoint = self._sync_checkpoint(thread, task_report)
        self._record_thread_binding(thread, task_report.task.task_id, checkpoint)
        return task_report, checkpoint, self._summarize_task_report(task_report, checkpoint)

    def _fork_request(
        self,
        task_id: str,
        *,
        thread: ConversationThread,
        goal: str | None = None,
        expected_output: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRequest:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        original = self.artifact_manager.load_task_request(task)
        constraints = TaskConstraints(
            allow_network=original.constraints.allow_network,
            allow_delete=original.constraints.allow_delete,
            allow_memory_read=original.constraints.allow_memory_read,
            timeout_seconds=original.constraints.timeout_seconds,
            max_permission=original.constraints.max_permission,
        )
        effective_goal = goal or original.goal
        if _explicitly_allows_network(effective_goal):
            constraints.allow_network = True
        # ISSUE-012: the previous task's metadata may contain approval /
        # permission keys that were written when it was resumed from a
        # checkpoint. Those belong to the old lifecycle and must not be
        # inherited by a fresh fork — otherwise the new task looks like an
        # ongoing approval, pollutes request.json with stale checkpoint_id,
        # and confuses Planner context.
        inherited_metadata = {
            key: value
            for key, value in original.metadata.items()
            if key not in self._STALE_APPROVAL_METADATA_KEYS
        }
        merged_metadata = {**inherited_metadata, "thread_id": thread.thread_id}
        if _explicitly_allows_network(effective_goal):
            merged_metadata["user_explicit_network_authorization"] = True
        if metadata:
            merged_metadata.update(metadata)
        return TaskRequest(
            goal=effective_goal,
            expected_output=expected_output if expected_output is not None else original.expected_output,
            payload=dict(original.payload),
            executor=original.executor,
            skill_name=original.skill_name,
            mode=original.mode,
            source=TaskSource.CONVERSATION,
            context_refs=list(original.context_refs),
            constraints=constraints,
            preferred_skills=list(original.preferred_skills),
            metadata=merged_metadata,
            execution_mode=original.execution_mode,
        )

    def _build_checkpoint_overrides(
        self,
        checkpoint: PendingCheckpoint,
        *,
        route: RouteDecision,
        message: str,
    ) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        if checkpoint.required_level is not None:
            constraints["max_permission"] = checkpoint.required_level
        if checkpoint.required_permission == "network":
            constraints["allow_network"] = True
        if checkpoint.required_permission == "filesystem.delete":
            constraints["allow_delete"] = True
        if checkpoint.required_permission == "memory.read":
            constraints["allow_memory_read"] = True
        metadata: dict[str, Any] = {
            "approved_checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_response": {
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": str(checkpoint.checkpoint_type),
                "node_id": checkpoint.node_id,
                "task_action": route.task_action,
                "message": message,
                "checkpoint_payload": dict(checkpoint.payload),
            },
        }
        if (
            checkpoint.checkpoint_type == PendingCheckpointType.PERMISSION
            and route.task_action != "reject"
            and str(checkpoint.payload.get("authorization_type", "")) == "path_access"
            and checkpoint.payload.get("authorized_path")
        ):
            metadata["grant_path_access"] = [str(checkpoint.payload["authorized_path"])]
        return {
            "constraints": constraints,
            "metadata": metadata,
        }

    def _build_scheduler_overrides(
        self,
        checkpoint: PendingCheckpoint,
        *,
        trigger: str,
        message: str,
        response_payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "metadata": {
                "scheduler_checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_response": {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "checkpoint_type": str(checkpoint.checkpoint_type),
                    "node_id": checkpoint.node_id,
                    "task_action": "resume",
                    "message": message,
                    **response_payload,
                    "checkpoint_payload": dict(checkpoint.payload),
                    "resume_trigger": trigger,
                },
            }
        }

    def _resume_pending_checkpoint(
        self,
        checkpoint: PendingCheckpoint,
        *,
        trigger: str,
        message: str,
        response_payload: dict[str, Any],
        event_handlers: list[EventHandler] | None,
    ) -> dict[str, Any]:
        thread = self.store.get_conversation_thread(checkpoint.thread_id)
        task = self.store.get_task(checkpoint.task_id)
        if task is not None:
            request = self.artifact_manager.load_task_request(task)
            request.source = TaskSource.SCHEDULER if trigger == "timer" else TaskSource.EVENT
            decision = self.permission_manager.evaluate_checkpoint_resume(
                request,
                checkpoint,
                trigger=trigger,
            )
            self.store.record_permission(decision)
            if decision.status != "granted":
                self._record_resume_attempt(
                    checkpoint.task_id,
                    checkpoint.checkpoint_id,
                    trigger=trigger,
                    status="permission_denied",
                    message=decision.rationale,
                    payload={
                        **response_payload,
                        "permission": decision.permission,
                        "level": str(decision.level),
                    },
                )
                self._append_task_event(
                    checkpoint.task_id,
                    "task.resume_blocked",
                    TaskStatus.SUSPENDED,
                    {
                        "message": decision.rationale,
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "trigger": trigger,
                        "permission": decision.permission,
                        "level": str(decision.level),
                    },
                )
                if thread is not None:
                    self.store.append_conversation_event(
                        thread.thread_id,
                        "task.resume_blocked",
                        {
                            "checkpoint_id": checkpoint.checkpoint_id,
                            "task_id": checkpoint.task_id,
                            "trigger": trigger,
                            "permission": decision.permission,
                            "level": str(decision.level),
                            "message": decision.rationale,
                        },
                    )
                return {
                    "task_id": checkpoint.task_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "trigger": trigger,
                    "status": "permission_denied",
                    "message": decision.rationale,
                }
        checkpoint.updated_at = response_payload.get("resumed_at", utc_now())
        checkpoint.status = PendingCheckpointStatus.RESOLVED
        checkpoint.resolved_at = checkpoint.updated_at
        self.store.save_pending_checkpoint(checkpoint)
        if thread is not None:
            self.store.append_conversation_event(
                thread.thread_id,
                "scheduler.wakeup" if trigger == "timer" else "external_event.received",
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "task_id": checkpoint.task_id,
                    "trigger": trigger,
                    "payload": response_payload,
                },
            )
            thread.pending_checkpoint_id = None
            thread.active_task_id = checkpoint.task_id

        self._append_task_event(
            checkpoint.task_id,
            "task.resuming",
            TaskStatus.RUNNING,
            {
                "message": message,
                "checkpoint_id": checkpoint.checkpoint_id,
                "trigger": trigger,
            },
        )
        task_report = self.resume_task(
            checkpoint.task_id,
            request_overrides=self._build_scheduler_overrides(
                checkpoint,
                trigger=trigger,
                message=message,
                response_payload=response_payload,
            ),
            event_handlers=event_handlers,
        )
        next_checkpoint = None
        if thread is not None:
            next_checkpoint = self._sync_checkpoint(thread, task_report)
            self._record_thread_binding(thread, checkpoint.task_id, next_checkpoint)
            thread.updated_at = utc_now()
            self.store.save_conversation_thread(thread)
            self.graph_memory.record_thread(thread)
        self._record_resume_attempt(
            checkpoint.task_id,
            checkpoint.checkpoint_id,
            trigger=trigger,
            status=str(task_report.task.status),
            message=task_report.result.message,
            payload=response_payload,
        )
        self._append_task_event(
            checkpoint.task_id,
            "task.resumed",
            task_report.task.status,
            {
                "message": task_report.result.message,
                "checkpoint_id": checkpoint.checkpoint_id,
                "trigger": trigger,
                "next_checkpoint_id": next_checkpoint.checkpoint_id if next_checkpoint else None,
            },
        )
        return {
            "task_id": checkpoint.task_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "trigger": trigger,
            "status": str(task_report.task.status),
            "message": task_report.result.message,
        }

    def _timeout_checkpoint(
        self, checkpoint: PendingCheckpoint, *, now: str | None = None
    ) -> dict[str, Any]:
        timestamp = now or utc_now()
        checkpoint.status = PendingCheckpointStatus.CANCELLED
        checkpoint.updated_at = timestamp
        checkpoint.resolved_at = timestamp
        self.store.save_pending_checkpoint(checkpoint)
        task = self.store.get_task(checkpoint.task_id)
        if task is not None:
            self.store.update_task(
                checkpoint.task_id,
                status=TaskStatus.FAILED,
                updated_at=timestamp,
                summary="长期任务等待超时",
                error_code=None,
                error_message="长期任务等待超时",
            )
        self._mark_checkpoint_node_terminal(
            checkpoint.task_id,
            checkpoint.node_id,
            status="failed",
            message="长期任务等待超时",
        )
        thread = self.store.get_conversation_thread(checkpoint.thread_id)
        if thread is not None:
            thread.pending_checkpoint_id = None
            thread.active_task_id = checkpoint.task_id
            self._record_thread_binding(thread, checkpoint.task_id, None)
            thread.updated_at = timestamp
            self.store.save_conversation_thread(thread)
            self.graph_memory.record_thread(thread)
            self.store.append_conversation_event(
                thread.thread_id,
                "task.resume_timeout",
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "task_id": checkpoint.task_id,
                    "checkpoint_type": str(checkpoint.checkpoint_type),
                },
            )
        self._append_task_event(
            checkpoint.task_id,
            "task.resume_timeout",
            TaskStatus.FAILED,
            {
                "message": "长期任务等待超时",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": str(checkpoint.checkpoint_type),
            },
        )
        self._record_resume_attempt(
            checkpoint.task_id,
            checkpoint.checkpoint_id,
            trigger="timeout",
            status="timed_out",
            message="长期任务等待超时",
            payload={"checkpoint_type": str(checkpoint.checkpoint_type), "timed_out_at": timestamp},
        )
        return {
            "task_id": checkpoint.task_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "trigger": "timeout",
            "status": "timed_out",
            "message": "长期任务等待超时",
        }

    def _record_resume_attempt(
        self,
        task_id: str,
        checkpoint_id: str,
        *,
        trigger: str,
        status: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_task_resume_attempt(
            TaskResumeAttempt(
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                trigger=trigger,
                status=status,
                message=message,
                payload=payload or {},
            )
        )

    def _append_task_event(
        self,
        task_id: str,
        event_type: str,
        status: TaskStatus | str,
        payload: dict[str, Any],
    ) -> None:
        self.store.append_event(
            TaskEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                event_type=event_type,
                status=str(status),
                payload=payload,
            )
        )

    def _mark_checkpoint_node_terminal(
        self,
        task_id: str,
        node_id: str | None,
        *,
        status: str,
        message: str,
    ) -> None:
        if not node_id:
            return
        task_graph = self.store.get_task_graph(task_id)
        if task_graph is None:
            return
        try:
            node = task_graph.get_node(node_id)
        except KeyError:
            return
        node.status = TaskNodeStatus(status)
        node.error_message = message
        task_graph.updated_at = utc_now()
        self.store.save_task_graph(task_id, task_graph)

    def _checkpoint_title(self, checkpoint_type: PendingCheckpointType) -> str:
        return {
            PendingCheckpointType.APPROVAL: "等待用户审批",
            PendingCheckpointType.PERMISSION: "等待权限审批",
            PendingCheckpointType.DECISION: "等待用户决策",
            PendingCheckpointType.TIMER: "等待时间点唤醒",
            PendingCheckpointType.EXTERNAL_EVENT: "等待外部事件",
        }.get(checkpoint_type, "等待恢复执行")

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _format_status(self, active_task: Any) -> str:
        if active_task is None:
            return "当前 thread 还没有关联任务。"
        nodes = self.store.list_task_nodes(active_task.task_id)
        completed = sum(1 for node in nodes if str(node.status) == "completed")
        return (
            f"active_task: {active_task.task_id}\n"
            f"status: {active_task.status}\n"
            f"goal: {active_task.goal}\n"
            f"completed_nodes: {completed}/{len(nodes)}\n"
            f"summary: {active_task.summary or 'none'}"
        )

    def _format_failure(self, active_task: Any) -> str:
        if active_task is None:
            return "当前 thread 没有关联的失败任务。"
        verdicts = self.store.list_critic_verdicts(active_task.task_id)
        if not verdicts and active_task.error_message is None:
            return f"任务 {active_task.task_id} 当前没有失败记录。"
        latest = verdicts[-1] if verdicts else None
        return (
            f"task_id: {active_task.task_id}\n"
            f"status: {active_task.status}\n"
            f"error: {active_task.error_message or active_task.summary or 'unknown'}\n"
            f"critic: {latest.action if latest else 'none'} {latest.summary if latest else ''}".strip()
        )

    # ------------------------------------------------------------------
    # ROADMAP P1 #4: knowledge query routed through LLM with natural-language
    # reply, structured-text keyword fallback preserved for offline / LLM-down
    # scenarios.
    # ------------------------------------------------------------------

    _KNOWLEDGE_QUERY_SCHEMA: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {"type": "string"},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "document": {"type": "string"},
                        "line": {"type": ["integer", "null"]},
                        "task_id": {"type": ["string", "null"]},
                    },
                    "required": ["document"],
                },
            },
        },
        "required": ["reply", "sources"],
    }

    def _handle_knowledge_query(
        self,
        message: str,
        *,
        thread: ConversationThread,
        active_task: Any,
    ) -> str:
        docs = self.workspace_manager.load_bootstrap_documents()
        snippets = self._match_document_snippets(message, docs)
        if (
            any(token in message.lower() for token in ["规则", "rule", "policy"])
            and "POLICY.md" in docs
        ):
            policy_hits = self._match_document_snippets(
                message, {"POLICY.md": docs["POLICY.md"]}, limit=2
            )
            for item in reversed(policy_hits):
                if any(
                    existing["document"] == item["document"] and existing["line"] == item["line"]
                    for existing in snippets
                ):
                    continue
                snippets.insert(0, item)
            snippets = snippets[:4]
        memory_hits = self.memory_manager.recall(message, limit=3)
        workflow_hits = self.memory_manager.recall_workflows(message, limit=3)
        thread_context = self.graph_memory.thread_context(thread.thread_id, limit=4)

        llm_reply = self._llm_knowledge_reply(
            message=message,
            thread=thread,
            active_task=active_task,
            docs=docs,
            snippets=snippets,
            memory_hits=memory_hits,
            workflow_hits=workflow_hits,
            thread_preferences=list(thread_context.get("preferences", []))[:5],
        )
        if llm_reply:
            return llm_reply
        # Fallback: deterministic keyword summary so the feature still works
        # offline or when the LLM misbehaves.
        return self._knowledge_query_fallback(
            message=message,
            active_task=active_task,
            snippets=snippets,
            memory_hits=memory_hits,
            workflow_hits=workflow_hits,
            thread_context=thread_context,
        )

    def _llm_knowledge_reply(
        self,
        *,
        message: str,
        thread: ConversationThread,
        active_task: Any,
        docs: dict[str, str],
        snippets: list[dict[str, Any]],
        memory_hits: list[dict[str, Any]],
        workflow_hits: list[dict[str, Any]],
        thread_preferences: list[dict[str, Any]],
    ) -> str | None:
        """Call LLM with knowledge_query_v1 prompt.

        Returns the natural-language reply on success, or ``None`` on any
        failure (missing prompt, LLM error, invalid structured output) so
        the caller can fall through to the deterministic fallback.
        """
        try:
            prompt = self.prompt_manager.load("knowledge_query_v1")
        except Exception:  # noqa: BLE001
            return None

        active_task_payload: dict[str, Any] | None = None
        if active_task is not None:
            active_task_payload = {
                "task_id": active_task.task_id,
                "goal": active_task.goal,
                "status": str(active_task.status),
                "summary": active_task.summary,
            }

        request_payload: dict[str, Any] = {
            "question": message,
            "workspace_documents": docs,
            "document_snippets": snippets,
            "memory_hits": [
                {
                    "task_id": item.get("task_id"),
                    "goal": item.get("goal"),
                    "message": item.get("message"),
                }
                for item in memory_hits
            ],
            "workflow_hits": [
                {
                    "goal": item.get("goal"),
                    "plan_summary": item.get("plan_summary"),
                }
                for item in workflow_hits
            ],
            "thread_preferences": [{"text": item.get("text", "")} for item in thread_preferences],
            "active_task": active_task_payload,
        }

        try:
            response = self.llm_adapter.generate_structured(
                prompt_name="knowledge_query_v1",
                system_prompt=prompt,
                request_payload=request_payload,
                schema=self._KNOWLEDGE_QUERY_SCHEMA,
            )
        except Exception:  # noqa: BLE001
            return None

        # Best-effort observability: persist the call under
        # workspace/threads/<tid>/llm_calls/ so `omniagent llm stats` picks it up.
        try:
            self.artifact_manager.write_thread_llm_call(
                thread.thread_id,
                response,
                request_payload={
                    "route_type": "knowledge_query",
                    "question": message,
                    "thread_id": thread.thread_id,
                    # Don't log full bootstrap docs — just the keys we sent.
                    "workspace_document_keys": list(docs.keys()),
                    "snippet_count": len(snippets),
                    "memory_hit_count": len(memory_hits),
                    "workflow_hit_count": len(workflow_hits),
                },
            )
        except Exception:  # noqa: BLE001
            pass

        structured = response.structured_output or {}
        reply = structured.get("reply", "")
        if not isinstance(reply, str) or not reply.strip():
            return None
        return reply.strip()

    def _knowledge_query_fallback(
        self,
        *,
        message: str,
        active_task: Any,
        snippets: list[dict[str, Any]],
        memory_hits: list[dict[str, Any]],
        workflow_hits: list[dict[str, Any]],
        thread_context: dict[str, Any],
    ) -> str:
        """Original keyword-matching structured summary — LLM-less fallback."""
        lines = [f"question: {message}"]
        if active_task is not None:
            lines.append(f"active_task: {active_task.task_id}")
        if snippets:
            lines.append("workspace_knowledge:")
            for item in snippets:
                lines.append(f"- {item['document']}:{item['line']}: {item['text']}")
        if thread_context.get("preferences") and any(
            token in message for token in ["偏好", "preference", "用户"]
        ):
            lines.append("user_preferences:")
            for item in thread_context["preferences"][:3]:
                lines.append(f"- {item['text']}")
        if memory_hits:
            lines.append("memory_hits:")
            for item in memory_hits:
                lines.append(
                    f"- {item.get('goal', 'unknown')}: {item.get('message', 'no message')}"
                )
        if workflow_hits:
            lines.append("workflow_hits:")
            for item in workflow_hits:
                lines.append(
                    f"- {item.get('goal', 'unknown')}: {item.get('plan_summary', 'no summary')}"
                )
        if len(lines) == 1:
            lines.append("未找到直接匹配的工作区知识，可以换一种问法或先补充相关文档。")
        return "\n".join(lines)

    def _handle_system_request(
        self,
        *,
        thread: ConversationThread,
        active_task: Any,
        pending_checkpoint: PendingCheckpoint | None,
    ) -> str:
        return "\n".join(
            [
                f"thread_id: {thread.thread_id}",
                f"active_task: {active_task.task_id if active_task else 'none'}",
                f"pending_checkpoint: {pending_checkpoint.checkpoint_id if pending_checkpoint else 'none'}",
                "supported_routes: new_task, task_update, approval_response, status_query, failure_query, replan_request, knowledge_query, system_request",
                "tips: 直接描述任务可创建执行流；输入“当前状态如何”可查进度；输入“有哪些工作区规则？”可查文档知识。",
            ]
        )

    def _match_document_snippets(
        self, query: str, documents: dict[str, str], *, limit: int = 4
    ) -> list[dict[str, Any]]:
        # META-006: this is a double-nested loop — for every document, for
        # every non-empty line. Previously each call rebuilt the query
        # embedding / token counts / concept set, so on a typical workspace
        # (4 bootstrap docs, ~50 lines each) we rebuilt the query features
        # ~200 times per knowledge query. SemanticScorer does that work
        # once up front.
        scorer = SemanticScorer(query)
        matches: list[dict[str, Any]] = []
        for filename, content in documents.items():
            for line_no, raw_line in enumerate(content.splitlines(), start=1):
                text = raw_line.strip()
                if not text or text.startswith("#"):
                    continue
                score = scorer.score(text)
                if score <= 0:
                    continue
                matches.append(
                    {
                        "document": filename,
                        "line": line_no,
                        "text": text.lstrip("- ").strip(),
                        "score": round(score, 4),
                    }
                )
        matches.sort(key=lambda item: item["score"], reverse=True)
        return matches[:limit]

    def _summarize_task_report(
        self,
        task_report: TaskExecutionReport,
        checkpoint: PendingCheckpoint | None,
    ) -> str:
        message = (
            f"task_id: {task_report.task.task_id}\n"
            f"status: {task_report.task.status}\n"
            f"message: {task_report.result.message}"
        )
        evaluation = task_report.completion_evaluation
        if evaluation is not None and not evaluation.task_satisfied:
            message += (
                "\ncompletion: not_satisfied"
                f"\ncompletion_summary: {evaluation.summary}"
            )
            if evaluation.missing_requirements:
                message += "\nmissing_requirements: " + "；".join(
                    evaluation.missing_requirements[:5]
                )
        if checkpoint is not None:
            message += f"\ncheckpoint_id: {checkpoint.checkpoint_id}\ncheckpoint_prompt: {checkpoint.prompt}"
        return message

    def _record_thread_binding(
        self,
        thread: ConversationThread,
        task_id: str | None,
        checkpoint: PendingCheckpoint | None,
    ) -> None:
        self.store.append_conversation_event(
            thread.thread_id,
            "thread.bound",
            {
                "active_task_id": task_id,
                "pending_checkpoint_id": checkpoint.checkpoint_id if checkpoint else None,
            },
        )

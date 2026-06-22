from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def generate_task_id() -> str:
    return f"task_{datetime.now(tz=timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def generate_graph_id() -> str:
    return f"graph_{uuid.uuid4().hex[:10]}"


def generate_thread_id() -> str:
    return f"thread_{uuid.uuid4().hex[:10]}"


def generate_turn_id() -> str:
    return f"turn_{uuid.uuid4().hex[:12]}"


def generate_checkpoint_id() -> str:
    return f"ckpt_{uuid.uuid4().hex[:10]}"


def generate_resume_attempt_id() -> str:
    return f"resume_{uuid.uuid4().hex[:10]}"


def generate_validation_report_id() -> str:
    return f"validation_{uuid.uuid4().hex[:10]}"


def generate_approval_record_id() -> str:
    return f"approval_{uuid.uuid4().hex[:10]}"


def generate_registration_record_id() -> str:
    return f"registration_{uuid.uuid4().hex[:10]}"


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUSPENDED = "suspended"
    RETRYING = "retrying"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskSource(StrEnum):
    CLI = "cli"
    API = "api"
    SCHEDULER = "scheduler"
    EVENT = "event"
    CONVERSATION = "conversation"


class AutonomyMode(StrEnum):
    ASSIST = "assist"
    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"


class PermissionLevel(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"

    @property
    def rank(self) -> int:
        return int(self.value[1:])


class UserInputType(StrEnum):
    NEW_TASK = "new_task"
    TASK_UPDATE = "task_update"
    APPROVAL_RESPONSE = "approval_response"
    STATUS_QUERY = "status_query"
    FAILURE_QUERY = "failure_query"
    REPLAN_REQUEST = "replan_request"
    KNOWLEDGE_QUERY = "knowledge_query"
    SYSTEM_REQUEST = "system_request"


class ConversationThreadStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class ConversationTurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class PendingCheckpointType(StrEnum):
    APPROVAL = "approval"
    PERMISSION = "permission"
    DECISION = "decision"
    TIMER = "timer"
    EXTERNAL_EVENT = "external_event"


class PendingCheckpointStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class SkillLifecycleStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class TaskNodeType(StrEnum):
    ACTION = "action"
    APPROVAL = "approval"
    DECISION = "decision"
    WAIT = "wait"
    REPLAN = "replan"
    FINALIZE = "finalize"


class TaskNodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRYING = "retrying"
    FAILED = "failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class CriticAction(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"
    REPLAN = "replan"
    DEGRADE = "degrade"
    FAIL = "fail"


def _coerce_critic_action(value: Any) -> CriticAction:
    if isinstance(value, CriticAction):
        return value
    if value is None:
        return CriticAction.ACCEPT

    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return CriticAction.ACCEPT

    direct_map = {
        "accept": CriticAction.ACCEPT,
        "retry": CriticAction.RETRY,
        "replan": CriticAction.REPLAN,
        "degrade": CriticAction.DEGRADE,
        "fail": CriticAction.FAIL,
    }
    if normalized in direct_map:
        return direct_map[normalized]

    alias_map = {
        "pass": CriticAction.ACCEPT,
        "passed": CriticAction.ACCEPT,
        "approve": CriticAction.ACCEPT,
        "approved": CriticAction.ACCEPT,
        "ok": CriticAction.ACCEPT,
        "okay": CriticAction.ACCEPT,
        "success": CriticAction.ACCEPT,
        "succeeded": CriticAction.ACCEPT,
        "complete": CriticAction.ACCEPT,
        "completed": CriticAction.ACCEPT,
        "continue": CriticAction.ACCEPT,
        "try_again": CriticAction.RETRY,
        "rerun": CriticAction.RETRY,
        "retrying": CriticAction.RETRY,
        "plan_again": CriticAction.REPLAN,
        "re_plan": CriticAction.REPLAN,
        "fallback": CriticAction.DEGRADE,
        "fall_back": CriticAction.DEGRADE,
        "downgrade": CriticAction.DEGRADE,
        "use_fallback": CriticAction.DEGRADE,
        "failed": CriticAction.FAIL,
        "reject": CriticAction.FAIL,
        "abort": CriticAction.FAIL,
        "stop": CriticAction.FAIL,
        "error": CriticAction.FAIL,
    }
    return alias_map.get(normalized, CriticAction.FAIL)


@dataclass(slots=True)
class TaskConstraints:
    allow_network: bool = False
    allow_delete: bool = False
    allow_memory_read: bool = True
    timeout_seconds: int = 30
    max_permission: PermissionLevel = PermissionLevel.L2


@dataclass(slots=True)
class RetryPolicy:
    max_retries: int = 0
    backoff_seconds: int = 0
    degrade_to: list[str] = field(default_factory=list)
    replan_on_failure: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RetryPolicy":
        payload = payload or {}
        return cls(
            max_retries=int(payload.get("max_retries", 0)),
            backoff_seconds=int(payload.get("backoff_seconds", 0)),
            degrade_to=list(payload.get("degrade_to", [])),
            replan_on_failure=bool(payload.get("replan_on_failure", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "backoff_seconds": self.backoff_seconds,
            "degrade_to": list(self.degrade_to),
            "replan_on_failure": self.replan_on_failure,
        }


@dataclass(slots=True)
class TaskDependency:
    source_node_id: str
    target_node_id: str
    condition: str = "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskDependency | None":
        """兼容 LLM 产出的字段别名，字段缺失时返回 None 由上层过滤。"""
        if not isinstance(payload, dict):
            return None
        source = (
            payload.get("source_node_id")
            or payload.get("source")
            or payload.get("from")
            or payload.get("from_node_id")
            or payload.get("source_id")
        )
        target = (
            payload.get("target_node_id")
            or payload.get("target")
            or payload.get("to")
            or payload.get("to_node_id")
            or payload.get("target_id")
        )
        if not source or not target:
            return None
        return cls(
            source_node_id=str(source),
            target_node_id=str(target),
            condition=str(payload.get("condition", "success")),
        )


@dataclass(slots=True)
class TaskRequest:
    goal: str
    expected_output: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    executor: str = "auto"
    skill_name: str | None = None
    task_id: str = field(default_factory=generate_task_id)
    mode: AutonomyMode = AutonomyMode.SEMI_AUTO
    source: TaskSource = TaskSource.CLI
    context_refs: list[str] = field(default_factory=list)
    constraints: TaskConstraints = field(default_factory=TaskConstraints)
    preferred_skills: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "plan_and_execute"


@dataclass(slots=True)
class Task:
    task_id: str
    goal: str
    mode: str
    source: str
    status: TaskStatus
    executor: str
    skill_name: str | None
    payload: dict[str, Any]
    workspace_path: str
    expected_output: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    graph_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    summary: str | None = None
    plan_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class ArtifactRecord:
    kind: str
    path: str
    description: str


@dataclass(slots=True)
class Recommendation:
    """Multi-dimensional recommendation score (ISSUE-011 main task).

    Instead of folding ``similarity`` and ``success_rate`` into one opaque
    ``final_score`` scalar, every recommendation now carries its constituent
    dimensions so that callers can inspect / threshold / explain each one
    independently.

    Sorting order (``sort_key``): lexicographic by *similarity bucket* first
    (bucket width 0.05 — items within 5 pp of each other are "equivalent"),
    then by ``success_rate`` descending within the same bucket.  The bucket
    prevents a tiny similarity edge from masking a large success_rate gap.
    """

    similarity: float
    success_rate: float
    source: str = "graph"
    explanation: str = ""

    # -- derived helpers ----------------------------------------------------

    _BUCKET_WIDTH: float = field(default=0.05, init=False, repr=False)

    @property
    def similarity_bucket(self) -> int:
        """Quantise similarity into fixed-width buckets for lexicographic sort."""
        return int(self.similarity / self._BUCKET_WIDTH)

    @property
    def sort_key(self) -> tuple[int, float]:
        """Primary: similarity bucket (desc). Secondary: success_rate (desc)."""
        return (self.similarity_bucket, self.success_rate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "similarity": round(self.similarity, 4),
            "success_rate": round(self.success_rate, 4),
            "source": self.source,
            "explanation": self.explanation,
        }


@dataclass(slots=True)
class ExecutionResult:
    success: bool
    status: str
    message: str
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    executor: str = "unknown"
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)
    error_code: str | None = None


@dataclass(slots=True)
class TaskEvent:
    event_id: str
    task_id: str
    event_type: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class PermissionDecision:
    task_id: str
    permission: str
    level: PermissionLevel
    status: str
    rationale: str
    scope: str = "task"
    node_id: str | None = None
    skill_name: str | None = None
    resource_kind: str | None = None
    resource_ref: str | None = None
    sandbox_level: str = "workspace"
    source: str = "runtime"
    policy: str = "task_constraints"
    requires_approval: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class BootstrapContext:
    documents: dict[str, str]
    memory_summaries: list[dict[str, Any]]
    workflow_summaries: list[dict[str, Any]]
    agent_state_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UserInputEnvelope:
    message: str
    thread_id: str | None = None
    user_id: str = "local_user"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConversationThread:
    title: str
    thread_id: str = field(default_factory=generate_thread_id)
    status: ConversationThreadStatus = ConversationThreadStatus.ACTIVE
    active_task_id: str | None = None
    pending_checkpoint_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ConversationTurn:
    thread_id: str
    role: ConversationTurnRole
    message: str
    turn_id: str = field(default_factory=generate_turn_id)
    input_type: UserInputType | None = None
    route: str | None = None
    task_id: str | None = None
    checkpoint_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class RouteDecision:
    thread_id: str
    input_type: UserInputType
    reasoning: str
    confidence: float
    task_action: str | None = None
    target_task_id: str | None = None
    checkpoint_id: str | None = None
    extracted_goal: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PendingCheckpoint:
    thread_id: str
    task_id: str
    checkpoint_type: PendingCheckpointType
    title: str
    prompt: str
    checkpoint_id: str = field(default_factory=generate_checkpoint_id)
    node_id: str | None = None
    status: PendingCheckpointStatus = PendingCheckpointStatus.PENDING
    required_permission: str | None = None
    required_level: PermissionLevel | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    resolved_at: str | None = None


@dataclass(slots=True)
class TaskResumeAttempt:
    task_id: str
    checkpoint_id: str
    trigger: str
    status: str
    message: str
    attempt_id: str = field(default_factory=generate_resume_attempt_id)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "checkpoint_id": self.checkpoint_id,
            "trigger": self.trigger,
            "status": self.status,
            "message": self.message,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskResumeAttempt":
        return cls(
            attempt_id=str(payload.get("attempt_id", generate_resume_attempt_id())),
            task_id=str(payload["task_id"]),
            checkpoint_id=str(payload["checkpoint_id"]),
            trigger=str(payload.get("trigger", "manual")),
            status=str(payload.get("status", "unknown")),
            message=str(payload.get("message", "")),
            payload=dict(payload.get("payload", {})),
            created_at=str(payload.get("created_at", utc_now())),
        )


@dataclass(slots=True)
class LLMResponse:
    provider: str
    model: str
    prompt_name: str
    request_id: str
    raw_output: str
    structured_output: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    # ISSUE-001: observability fields — captured in the adapter so every
    # layer (workspace log, gateway accumulator, CLI stats command) sees
    # the same numbers without having to time the call itself.
    latency_ms: float | None = None
    tier: str | None = None
    attempts: int = 1
    started_at: str | None = None


@dataclass(slots=True)
class TaskNode:
    node_id: str
    type: TaskNodeType
    goal: str
    dependencies: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    executor_hint: str | None = None
    skill_name: str | None = None
    risk_level: str = "low"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    status: TaskNodeStatus = TaskNodeStatus.PENDING
    input_payload: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    error_message: str | None = None
    attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "type": self.type,
            "goal": self.goal,
            "dependencies": list(self.dependencies),
            "required_skills": list(self.required_skills),
            "executor_hint": self.executor_hint,
            "skill_name": self.skill_name,
            "risk_level": self.risk_level,
            "retry_policy": self.retry_policy.to_dict(),
            "status": self.status,
            "input_payload": dict(self.input_payload),
            "output": dict(self.output),
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "path": artifact.path,
                    "description": artifact.description,
                }
                for artifact in self.artifacts
            ],
            "error_message": self.error_message,
            "attempts": self.attempts,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskNode":
        return cls(
            node_id=str(payload["node_id"]),
            type=TaskNodeType(payload.get("type", TaskNodeType.ACTION)),
            goal=str(payload["goal"]),
            dependencies=list(payload.get("dependencies", [])),
            required_skills=list(payload.get("required_skills", [])),
            executor_hint=payload.get("executor_hint"),
            skill_name=payload.get("skill_name"),
            risk_level=str(payload.get("risk_level", "low")),
            retry_policy=RetryPolicy.from_dict(payload.get("retry_policy")),
            status=TaskNodeStatus(payload.get("status", TaskNodeStatus.PENDING)),
            input_payload=dict(payload.get("input_payload", {})),
            output=dict(payload.get("output", {})),
            artifacts=[
                ArtifactRecord(
                    kind=str(item.get("kind", "data")),
                    path=str(item.get("path", "")),
                    description=str(item.get("description", "")),
                )
                for item in payload.get("artifacts", [])
            ],
            error_message=payload.get("error_message"),
            attempts=int(payload.get("attempts", 0)),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class TaskGraph:
    goal: str
    nodes: list[TaskNode]
    graph_id: str = field(default_factory=generate_graph_id)
    dependencies: list[TaskDependency] = field(default_factory=list)
    summary: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("TaskGraph node_id 重复")
        for node in self.nodes:
            for dep in node.dependencies:
                if dep not in node_ids:
                    raise ValueError(f"TaskGraph 依赖节点不存在: {dep}")
        for dep in self.dependencies:
            if dep.source_node_id not in node_ids or dep.target_node_id not in node_ids:
                raise ValueError("TaskGraph dependency 引用非法节点")

    # ISSUE-006: code-level enforcement of the planner_v1.md rule
    # "高风险操作（删除、覆盖）前应插入 approval 节点". The Planner prompt
    # already asks for this, but an LLM can drop the approval step and
    # the graph would still pass structural validate(). This pass walks
    # every action node, identifies destructive file operations (delete /
    # remove), and injects a preceding approval node whenever no approval
    # ancestor already gates it. Idempotent: a second call is a no-op.
    _DESTRUCTIVE_FILE_ACTIONS = ("delete", "remove")

    def _node_is_destructive(self, node: TaskNode) -> bool:
        if node.type != TaskNodeType.ACTION:
            return False
        executor_hint = (node.executor_hint or "").lower()
        skill_name = (node.skill_name or "").lower()
        looks_like_file = (
            executor_hint == "file" or skill_name.startswith("builtin.file") or skill_name == "file"
        )
        if not looks_like_file:
            return False
        action = str(node.input_payload.get("action", "")).lower()
        return action in self._DESTRUCTIVE_FILE_ACTIONS

    def _has_approval_ancestor(self, node_id: str) -> bool:
        index: dict[str, TaskNode] = {n.node_id: n for n in self.nodes}
        visited: set[str] = set()
        stack: list[str] = list(index[node_id].dependencies)
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            dep_node = index.get(current)
            if dep_node is None:
                continue
            if dep_node.type == TaskNodeType.APPROVAL:
                return True
            stack.extend(dep_node.dependencies)
        return False

    def enforce_destructive_approvals(self) -> int:
        """Inject approval nodes in front of destructive action nodes.

        Returns the number of approval nodes that were injected. Callers
        should treat 0 as "nothing to do" and any positive value as
        evidence that the LLM produced a plan that skipped the required
        approval gate (worth logging).
        """
        injected = 0
        # Snapshot up front — we mutate self.nodes inside the loop.
        for node in list(self.nodes):
            if not self._node_is_destructive(node):
                continue
            if self._has_approval_ancestor(node.node_id):
                continue
            approval_id = f"node_approval_{node.node_id}"
            # Keep node_ids unique even if the planner already used this name.
            existing_ids = {n.node_id for n in self.nodes}
            suffix = 1
            unique_id = approval_id
            while unique_id in existing_ids:
                suffix += 1
                unique_id = f"{approval_id}_{suffix}"

            path = str(node.input_payload.get("path", "")) or "目标文件"
            prompt = f"即将对 {path} 执行 {node.input_payload.get('action', 'delete')}，是否继续？"
            approval_node = TaskNode(
                node_id=unique_id,
                type=TaskNodeType.APPROVAL,
                goal=f"审批删除操作: {path}",
                dependencies=list(node.dependencies),
                required_skills=[],
                executor_hint=None,
                skill_name=None,
                risk_level="high",
                retry_policy=RetryPolicy(),
                input_payload={
                    "prompt": prompt,
                    "confirm_message": f"用户已确认删除 {path}",
                    "injected_for_node": node.node_id,
                },
                metadata={"auto_injected": True, "reason": "ISSUE-006 approval-before-delete"},
            )
            # Insert the approval immediately before the destructive node
            # so the plan reads top-to-bottom in the expected order.
            insert_at = self.nodes.index(node)
            self.nodes.insert(insert_at, approval_node)
            node.dependencies = [unique_id]
            self.dependencies.append(
                TaskDependency(
                    source_node_id=unique_id,
                    target_node_id=node.node_id,
                    condition="on_success",
                )
            )
            injected += 1
        if injected:
            self.updated_at = utc_now()
            self.validate()
        return injected

    def get_node(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def ready_nodes(self) -> list[TaskNode]:
        completed = {
            node.node_id
            for node in self.nodes
            if node.status in {TaskNodeStatus.COMPLETED, TaskNodeStatus.SKIPPED}
        }
        ready: list[TaskNode] = []
        for node in self.nodes:
            if node.status not in {TaskNodeStatus.PENDING, TaskNodeStatus.READY}:
                continue
            if set(node.dependencies).issubset(completed):
                ready.append(node)
        return ready

    def is_finished(self) -> bool:
        return all(
            node.status in {TaskNodeStatus.COMPLETED, TaskNodeStatus.SKIPPED, TaskNodeStatus.FAILED}
            for node in self.nodes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "goal": self.goal,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": [node.to_dict() for node in self.nodes],
            "dependencies": [dep.to_dict() for dep in self.dependencies],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskGraph":
        nodes: list[TaskNode] = []
        for index, raw_node in enumerate(payload.get("nodes", []), start=1):
            if not isinstance(raw_node, dict):
                continue
            node_payload = dict(raw_node)
            if not node_payload.get("node_id"):
                node_payload["node_id"] = f"node_{index}"
            if not node_payload.get("goal"):
                node_payload["goal"] = str(node_payload["node_id"])
            nodes.append(TaskNode.from_dict(node_payload))
        node_ids = {node.node_id for node in nodes}
        # 过滤掉节点内部引用的非法依赖，避免 LLM 产生悬空引用时整个计划崩溃
        for node in nodes:
            node.dependencies = [dep for dep in node.dependencies if dep in node_ids]
        raw_deps = payload.get("dependencies") or []
        dependencies: list[TaskDependency] = []
        for item in raw_deps:
            dep = TaskDependency.from_dict(item)
            if dep is None:
                continue
            if dep.source_node_id not in node_ids or dep.target_node_id not in node_ids:
                # LLM 有时会引用不存在的节点，静默丢弃避免中断整个计划
                continue
            dependencies.append(dep)
        # META-007: backfill node.dependencies from graph-level dependencies.
        # LLMs consistently populate graph.dependencies but leave node.dependencies
        # empty.  The execution engine (ready_nodes, _finalize_node, _apply_replan)
        # only reads node.dependencies, so we must reconcile here at parse time.
        node_map = {n.node_id: n for n in nodes}
        for dep in dependencies:
            target = node_map.get(dep.target_node_id)
            if target is not None and dep.source_node_id not in target.dependencies:
                target.dependencies.append(dep.source_node_id)

        graph = cls(
            graph_id=str(payload.get("graph_id", generate_graph_id())),
            goal=str(payload.get("goal", "")),
            summary=payload.get("summary"),
            created_at=str(payload.get("created_at", utc_now())),
            updated_at=str(payload.get("updated_at", utc_now())),
            nodes=nodes,
            dependencies=dependencies,
        )
        graph.validate()
        return graph


@dataclass(slots=True)
class PlanResult:
    summary: str
    graph: TaskGraph
    reasoning: str = ""
    risk_level: str = "medium"
    required_capabilities: list[str] = field(default_factory=list)
    llm_trace_id: str | None = None
    # META-003: decision gives the Planner a legal way to refuse planning.
    # "plan"                 — a graph was produced and should be executed
    # "decline_not_a_task"   — input is conversational; return decline_reason
    # "clarify"              — input is ambiguous; ask clarify_question
    # "missing_capability"   — intent is clear but no executor/skill matches;
    #                          surface missing_capability so runtime can
    #                          trigger auto-generation or report the gap
    decision: str = "plan"
    decline_reason: str | None = None
    clarify_question: str | None = None
    missing_capability: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "reasoning": self.reasoning,
            "risk_level": self.risk_level,
            "required_capabilities": list(self.required_capabilities),
            "llm_trace_id": self.llm_trace_id,
            "graph": self.graph.to_dict(),
            "decision": self.decision,
            "decline_reason": self.decline_reason,
            "clarify_question": self.clarify_question,
            "missing_capability": dict(self.missing_capability)
            if self.missing_capability
            else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanResult":
        decision = str(payload.get("decision") or "plan").strip() or "plan"
        # Non-plan decisions may omit ``graph`` entirely. TaskGraph.from_dict
        # with an empty dict yields a valid empty graph, so runtime code that
        # inspects ``plan.graph`` never sees ``None``.
        graph = TaskGraph.from_dict(payload.get("graph") or {})
        missing_raw = payload.get("missing_capability")
        missing_capability = dict(missing_raw) if isinstance(missing_raw, dict) else None
        return cls(
            summary=str(payload.get("summary", "")),
            reasoning=str(payload.get("reasoning", "")),
            risk_level=str(payload.get("risk_level", "medium")),
            required_capabilities=list(payload.get("required_capabilities", [])),
            llm_trace_id=payload.get("llm_trace_id"),
            graph=graph,
            decision=decision,
            decline_reason=payload.get("decline_reason"),
            clarify_question=payload.get("clarify_question"),
            missing_capability=missing_capability,
        )


@dataclass(slots=True)
class CriticVerdict:
    target_type: str
    target_id: str
    action: CriticAction
    summary: str
    rationale: str
    confidence: float = 0.5
    suggested_retry_policy: RetryPolicy | None = None
    downgrade_target: str | None = None
    replan_goal: str | None = None
    llm_trace_id: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "action": self.action,
            "summary": self.summary,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "suggested_retry_policy": self.suggested_retry_policy.to_dict()
            if self.suggested_retry_policy
            else None,
            "downgrade_target": self.downgrade_target,
            "replan_goal": self.replan_goal,
            "llm_trace_id": self.llm_trace_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CriticVerdict":
        return cls(
            target_type=str(payload.get("target_type", "node")),
            target_id=str(payload.get("target_id", "")),
            action=_coerce_critic_action(payload.get("action", CriticAction.ACCEPT)),
            summary=str(payload.get("summary", "")),
            rationale=str(payload.get("rationale", "")),
            confidence=float(payload.get("confidence", 0.5)),
            suggested_retry_policy=RetryPolicy.from_dict(payload.get("suggested_retry_policy"))
            if payload.get("suggested_retry_policy")
            else None,
            downgrade_target=payload.get("downgrade_target"),
            replan_goal=payload.get("replan_goal"),
            llm_trace_id=payload.get("llm_trace_id"),
            created_at=str(payload.get("created_at", utc_now())),
        )


@dataclass(slots=True)
class CompletionEvaluation:
    task_id: str
    task_satisfied: bool
    completion_score: float
    summary: str
    missing_requirements: list[str] = field(default_factory=list)
    unexpected_side_effects: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    primary_failure_layer: str = "none"
    model_capability_suspected: bool = False
    recommended_next_action: str = "none"
    confidence: float = 0.5
    llm_trace_id: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_satisfied": self.task_satisfied,
            "completion_score": self.completion_score,
            "summary": self.summary,
            "missing_requirements": list(self.missing_requirements),
            "unexpected_side_effects": list(self.unexpected_side_effects),
            "evidence": list(self.evidence),
            "primary_failure_layer": self.primary_failure_layer,
            "model_capability_suspected": self.model_capability_suspected,
            "recommended_next_action": self.recommended_next_action,
            "confidence": self.confidence,
            "llm_trace_id": self.llm_trace_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CompletionEvaluation":
        def _list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item) for item in value]

        score = float(payload.get("completion_score", 0.0))
        confidence = float(payload.get("confidence", 0.5))
        primary_failure_layer = str(payload.get("primary_failure_layer", "none") or "none")
        model_capability_suspected = bool(payload.get("model_capability_suspected", False))
        if primary_failure_layer != "model_capability":
            model_capability_suspected = False
        return cls(
            task_id=str(payload.get("task_id", "")),
            task_satisfied=bool(payload.get("task_satisfied", False)),
            completion_score=max(0.0, min(score, 1.0)),
            summary=str(payload.get("summary", "")),
            missing_requirements=_list(payload.get("missing_requirements")),
            unexpected_side_effects=_list(payload.get("unexpected_side_effects")),
            evidence=_list(payload.get("evidence")),
            primary_failure_layer=primary_failure_layer,
            model_capability_suspected=model_capability_suspected,
            recommended_next_action=str(payload.get("recommended_next_action", "none") or "none"),
            confidence=max(0.0, min(confidence, 1.0)),
            llm_trace_id=payload.get("llm_trace_id"),
            created_at=str(payload.get("created_at", utc_now())),
        )


# ----------------------------------------------------------------------------
# META-001: Structured skill contracts
# ----------------------------------------------------------------------------
# A SkillContract declares, per-skill, the canonical actions it can handle
# and the shape of each action's input/output payload. Used by SkillRegistry
# to do hard filtering (dispatcher picks only skills whose contracts cover
# the requested action) and by SkillDispatcher to proactively validate
# node.input_payload before calling the executor, instead of letting the
# executor discover the mismatch at runtime (the old "reactive validation"
# pattern that ISSUE-008 creep-patched around).


@dataclass(slots=True)
class SkillContractField:
    """One field in a SkillContract's input or output schema.

    Intentionally minimal for Phase 3a — just name/type/required/description.
    Can be upgraded to full JSON Schema later without breaking callers.
    """

    name: str
    type: str = "any"  # "string" | "integer" | "number" | "boolean" | "object" | "array" | "any"
    required: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "SkillContractField":
        # Accept either a plain string (shorthand for `{name: str, type: "any"}`)
        # or a full dict. The yaml shorthand is useful so manifests stay readable.
        if isinstance(payload, str):
            return cls(name=payload)
        data = dict(payload or {})
        return cls(
            name=str(data.get("name", "")),
            type=str(data.get("type", "any")),
            required=bool(data.get("required", False)),
            description=str(data.get("description", "")),
        )


@dataclass(slots=True)
class SkillContract:
    """One action a skill can handle + its declared input/output schema.

    A skill manifest's `contracts:` section is a list of these. Example:

        contracts:
          - action: read
            description: "Read a text file from workspace"
            input_fields:
              - {name: path, type: string, required: true}
            output_fields:
              - {name: content, type: string}
              - {name: path, type: string}
            side_effects: [filesystem.read]

    If a skill has zero contracts, the dispatcher treats it as "legacy /
    unknown contract" and falls back to tag-similarity only. New skills
    and all builtin skills should declare full contracts.
    """

    action: str
    description: str = ""
    input_fields: list[SkillContractField] = field(default_factory=list)
    output_fields: list[SkillContractField] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)

    def input_field(self, name: str) -> SkillContractField | None:
        return next((f for f in self.input_fields if f.name == name), None)

    def required_input_fields(self) -> list[str]:
        return [f.name for f in self.input_fields if f.required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "description": self.description,
            "input_fields": [f.to_dict() for f in self.input_fields],
            "output_fields": [f.to_dict() for f in self.output_fields],
            "side_effects": list(self.side_effects),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SkillContract":
        data = dict(payload or {})
        return cls(
            action=str(data.get("action", "")),
            description=str(data.get("description", "")),
            input_fields=[
                SkillContractField.from_dict(item) for item in data.get("input_fields", [])
            ],
            output_fields=[
                SkillContractField.from_dict(item) for item in data.get("output_fields", [])
            ],
            side_effects=list(data.get("side_effects", [])),
        )


@dataclass(slots=True)
class SkillRegistryEntry:
    skill_name: str
    source: str
    version: str
    description: str
    permission_requirements: list[str]
    tags: list[str]
    location: str
    scope: str
    sandbox_level: str
    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    lifecycle_status: SkillLifecycleStatus = SkillLifecycleStatus.ACTIVE
    activation_source: str | None = None
    activation_target: str | None = None
    last_validated_at: str | None = None
    last_approved_at: str | None = None
    last_seen_at: str = field(default_factory=utc_now)
    # META-001: Structured action contracts declared by the skill. Empty list
    # means "legacy / unknown", in which case SkillRegistry falls back to
    # tag-similarity scoring only. See SkillContract for the per-action shape.
    contracts: list[SkillContract] = field(default_factory=list)

    def supports_action(self, action: str | None) -> bool:
        """Contract-aware filter for SkillRegistry.select().

        Semantics:
        - If the skill has no declared contracts, return True (legacy pass-through).
        - If no action is requested (None or empty), return True (non-action executors
          like python / browser / llm_native do not dispatch on `action`).
        - Otherwise, return True iff at least one contract declares that action.
        """
        if not self.contracts:
            return True
        if not action:
            return True
        return any(c.action == action for c in self.contracts)

    def contract_for_action(self, action: str | None) -> SkillContract | None:
        if not action:
            return None
        return next((c for c in self.contracts if c.action == action), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "permission_requirements": list(self.permission_requirements),
            "tags": list(self.tags),
            "location": self.location,
            "scope": self.scope,
            "sandbox_level": self.sandbox_level,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": self.success_rate,
            "lifecycle_status": self.lifecycle_status,
            "activation_source": self.activation_source,
            "activation_target": self.activation_target,
            "last_validated_at": self.last_validated_at,
            "last_approved_at": self.last_approved_at,
            "last_seen_at": self.last_seen_at,
            "contracts": [c.to_dict() for c in self.contracts],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SkillRegistryEntry":
        return cls(
            skill_name=str(payload["skill_name"]),
            source=str(payload.get("source", "unknown")),
            version=str(payload.get("version", "0.1.0")),
            description=str(payload.get("description", "")),
            permission_requirements=list(payload.get("permission_requirements", [])),
            tags=list(payload.get("tags", [])),
            location=str(payload.get("location", "")),
            scope=str(payload.get("scope", "workspace")),
            sandbox_level=str(payload.get("sandbox_level", "workspace")),
            success_count=int(payload.get("success_count", 0)),
            failure_count=int(payload.get("failure_count", 0)),
            success_rate=float(payload.get("success_rate", 0.0)),
            lifecycle_status=SkillLifecycleStatus(
                payload.get("lifecycle_status", SkillLifecycleStatus.ACTIVE)
            ),
            activation_source=payload.get("activation_source"),
            activation_target=payload.get("activation_target"),
            last_validated_at=payload.get("last_validated_at"),
            last_approved_at=payload.get("last_approved_at"),
            last_seen_at=str(payload.get("last_seen_at", utc_now())),
            contracts=[SkillContract.from_dict(item) for item in payload.get("contracts", [])],
        )


@dataclass(slots=True)
class SkillDraft:
    skill_name: str
    version: str
    source_kind: str
    source_ref: str
    source_dir: str
    manifest_path: str
    main_path: str
    permission_requirements: list[str]
    lifecycle_status: SkillLifecycleStatus = SkillLifecycleStatus.DRAFT
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "version": self.version,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "source_dir": self.source_dir,
            "manifest_path": self.manifest_path,
            "main_path": self.main_path,
            "permission_requirements": list(self.permission_requirements),
            "lifecycle_status": self.lifecycle_status,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SkillDraft":
        return cls(
            skill_name=str(payload["skill_name"]),
            version=str(payload.get("version", "0.1.0")),
            source_kind=str(payload.get("source_kind", "unknown")),
            source_ref=str(payload.get("source_ref", "")),
            source_dir=str(payload.get("source_dir", "")),
            manifest_path=str(payload.get("manifest_path", "")),
            main_path=str(payload.get("main_path", "")),
            permission_requirements=list(payload.get("permission_requirements", [])),
            lifecycle_status=SkillLifecycleStatus(
                payload.get("lifecycle_status", SkillLifecycleStatus.DRAFT)
            ),
            metadata=dict(payload.get("metadata", {})),
            created_at=str(payload.get("created_at", utc_now())),
            updated_at=str(payload.get("updated_at", utc_now())),
        )


@dataclass(slots=True)
class ValidationReport:
    skill_name: str
    version: str
    passed: bool
    summary: str
    checks: dict[str, Any]
    findings: list[dict[str, Any]] = field(default_factory=list)
    report_id: str = field(default_factory=generate_validation_report_id)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "skill_name": self.skill_name,
            "version": self.version,
            "passed": self.passed,
            "summary": self.summary,
            "checks": dict(self.checks),
            "findings": list(self.findings),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ValidationReport":
        return cls(
            report_id=str(payload.get("report_id", generate_validation_report_id())),
            skill_name=str(payload["skill_name"]),
            version=str(payload.get("version", "0.1.0")),
            passed=bool(payload.get("passed", False)),
            summary=str(payload.get("summary", "")),
            checks=dict(payload.get("checks", {})),
            findings=[dict(item) for item in payload.get("findings", [])],
            created_at=str(payload.get("created_at", utc_now())),
        )


@dataclass(slots=True)
class ApprovalRecord:
    skill_name: str
    version: str
    status: str
    approver: str
    rationale: str
    policy: dict[str, Any]
    approval_id: str = field(default_factory=generate_approval_record_id)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "skill_name": self.skill_name,
            "version": self.version,
            "status": self.status,
            "approver": self.approver,
            "rationale": self.rationale,
            "policy": dict(self.policy),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ApprovalRecord":
        return cls(
            approval_id=str(payload.get("approval_id", generate_approval_record_id())),
            skill_name=str(payload["skill_name"]),
            version=str(payload.get("version", "0.1.0")),
            status=str(payload.get("status", "pending")),
            approver=str(payload.get("approver", "unknown")),
            rationale=str(payload.get("rationale", "")),
            policy=dict(payload.get("policy", {})),
            created_at=str(payload.get("created_at", utc_now())),
        )


@dataclass(slots=True)
class RegistrationRecord:
    skill_name: str
    version: str
    action: str
    status: str
    source_path: str
    target_path: str | None = None
    previous_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    registration_id: str = field(default_factory=generate_registration_record_id)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "skill_name": self.skill_name,
            "version": self.version,
            "action": self.action,
            "status": self.status,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "previous_version": self.previous_version,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegistrationRecord":
        return cls(
            registration_id=str(payload.get("registration_id", generate_registration_record_id())),
            skill_name=str(payload["skill_name"]),
            version=str(payload.get("version", "0.1.0")),
            action=str(payload.get("action", "activate")),
            status=str(payload.get("status", "active")),
            source_path=str(payload.get("source_path", "")),
            target_path=payload.get("target_path"),
            previous_version=payload.get("previous_version"),
            metadata=dict(payload.get("metadata", {})),
            created_at=str(payload.get("created_at", utc_now())),
        )


@dataclass(slots=True)
class SkillSelection:
    node_id: str
    selected_skill: str | None
    executor_hint: str | None
    gap_reason: str | None = None
    registry_match: SkillRegistryEntry | None = None
    explanation: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskRunContext:
    task: Task
    task_request: TaskRequest
    workspace_root: Path
    task_dir: Path
    bootstrap: BootstrapContext
    loaded_skills: dict[str, Any]
    sandbox: Any
    task_graph: TaskGraph | None = None
    workflow_matches: list[dict[str, Any]] = field(default_factory=list)
    vector_matches: list[dict[str, Any]] = field(default_factory=list)
    failure_patterns: list[dict[str, Any]] = field(default_factory=list)
    graph_workflow_matches: list[dict[str, Any]] = field(default_factory=list)
    graph_skill_matches: list[dict[str, Any]] = field(default_factory=list)
    graph_failure_patterns: list[dict[str, Any]] = field(default_factory=list)
    hybrid_recall: dict[str, Any] = field(default_factory=dict)
    agent_state_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskExecutionReport:
    task: Task
    result: ExecutionResult
    events: list[TaskEvent]
    task_graph: TaskGraph | None = None
    critic_verdicts: list[CriticVerdict] = field(default_factory=list)
    completion_evaluation: CompletionEvaluation | None = None
    intake_assessment: dict[str, Any] = field(default_factory=dict)
    learning_feedback: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConversationResponse:
    thread: ConversationThread
    route: RouteDecision
    assistant_message: str
    task_report: TaskExecutionReport | None = None
    checkpoint: PendingCheckpoint | None = None

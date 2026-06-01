from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from omniagent.models import (
    ApprovalRecord,
    ConversationThread,
    ConversationThreadStatus,
    ConversationTurn,
    ConversationTurnRole,
    CriticVerdict,
    ExecutionResult,
    PendingCheckpoint,
    PendingCheckpointStatus,
    PendingCheckpointType,
    PermissionLevel,
    PermissionDecision,
    RegistrationRecord,
    SkillContract,
    SkillDraft,
    SkillLifecycleStatus,
    SkillRegistryEntry,
    Task,
    TaskEvent,
    TaskGraph,
    TaskNode,
    TaskResumeAttempt,
    TaskStatus,
    UserInputType,
    ValidationReport,
    utc_now,
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any) -> Any:
    if value in {None, ""}:
        return default
    return json.loads(value)


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    executor TEXT NOT NULL,
                    skill_name TEXT,
                    payload_json TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    expected_output TEXT,
                    metadata_json TEXT,
                    graph_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    summary TEXT,
                    plan_summary TEXT,
                    error_code TEXT,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    executor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS permission_audits (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    level TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'task',
                    node_id TEXT,
                    skill_name TEXT,
                    resource_kind TEXT,
                    resource_ref TEXT,
                    sandbox_level TEXT NOT NULL DEFAULT 'workspace',
                    source TEXT NOT NULL DEFAULT 'runtime',
                    policy TEXT NOT NULL DEFAULT 'task_constraints',
                    requires_approval INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_graphs (
                    task_id TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    graph_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_nodes (
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    required_skills_json TEXT NOT NULL,
                    executor_hint TEXT,
                    skill_name TEXT,
                    risk_level TEXT NOT NULL,
                    retry_policy_json TEXT NOT NULL,
                    input_payload_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    artifacts_json TEXT NOT NULL,
                    error_message TEXT,
                    attempts INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, node_id)
                );

                CREATE TABLE IF NOT EXISTS critic_verdicts (
                    verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    verdict_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skill_registry (
                    skill_name TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    version TEXT NOT NULL,
                    description TEXT NOT NULL,
                    permission_requirements_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    location TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    sandbox_level TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    success_rate REAL NOT NULL DEFAULT 0.0,
                    lifecycle_status TEXT NOT NULL DEFAULT 'active',
                    activation_source TEXT,
                    activation_target TEXT,
                    last_validated_at TEXT,
                    last_approved_at TEXT,
                    last_seen_at TEXT NOT NULL,
                    contracts_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS skill_drafts (
                    skill_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_dir TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    main_path TEXT NOT NULL,
                    permission_requirements_json TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (skill_name, version)
                );

                CREATE TABLE IF NOT EXISTS skill_validation_reports (
                    report_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    checks_json TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skill_approval_records (
                    approval_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skill_registration_records (
                    registration_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_path TEXT,
                    previous_version TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_threads (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_task_id TEXT,
                    pending_checkpoint_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    message TEXT NOT NULL,
                    input_type TEXT,
                    route TEXT,
                    task_id TEXT,
                    checkpoint_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    node_id TEXT,
                    checkpoint_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    required_permission TEXT,
                    required_level TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS task_resume_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS graph_nodes (
                    entity_key TEXT PRIMARY KEY,
                    node_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    properties_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    score REAL NOT NULL,
                    access_scope TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS graph_node_versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS graph_edges (
                    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_entity_key TEXT NOT NULL,
                    target_entity_key TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    properties_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    score REAL NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_entity_key, target_entity_key, edge_type)
                );

                CREATE TABLE IF NOT EXISTS graph_edge_versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_entity_key TEXT NOT NULL,
                    target_entity_key TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_permission_audit_columns(conn)
            self._ensure_skill_registry_columns(conn)

    def _ensure_skill_registry_columns(self, conn: sqlite3.Connection) -> None:
        # META-001: structured action contracts. For existing databases we
        # need to ALTER the table to add the column (fresh databases already
        # have it via CREATE TABLE above). Idempotent via _ensure_column.
        self._ensure_column(
            conn,
            "skill_registry",
            "contracts_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )

    def _ensure_permission_audit_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(permission_audits)").fetchall()
        }
        required = {
            "scope": "TEXT NOT NULL DEFAULT 'task'",
            "node_id": "TEXT",
            "skill_name": "TEXT",
            "resource_kind": "TEXT",
            "resource_ref": "TEXT",
            "sandbox_level": "TEXT NOT NULL DEFAULT 'workspace'",
            "source": "TEXT NOT NULL DEFAULT 'runtime'",
            "policy": "TEXT NOT NULL DEFAULT 'task_constraints'",
            "requires_approval": "INTEGER NOT NULL DEFAULT 0",
            "details_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, ddl in required.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE permission_audits ADD COLUMN {name} {ddl}")
            self._ensure_column(conn, "tasks", "expected_output", "TEXT")
            self._ensure_column(conn, "tasks", "metadata_json", "TEXT")
            self._ensure_column(conn, "tasks", "graph_id", "TEXT")
            self._ensure_column(conn, "tasks", "plan_summary", "TEXT")
            self._ensure_column(
                conn, "skill_registry", "lifecycle_status", "TEXT NOT NULL DEFAULT 'active'"
            )
            self._ensure_column(conn, "skill_registry", "activation_source", "TEXT")
            self._ensure_column(conn, "skill_registry", "activation_target", "TEXT")
            self._ensure_column(conn, "skill_registry", "last_validated_at", "TEXT")
            self._ensure_column(conn, "skill_registry", "last_approved_at", "TEXT")

    def _ensure_column(
        self, conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_task(self, task: Task) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, goal, mode, source, status, executor, skill_name, payload_json,
                    workspace_path, expected_output, metadata_json, graph_id, created_at, updated_at,
                    summary, plan_summary, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.goal,
                    task.mode,
                    task.source,
                    task.status,
                    task.executor,
                    task.skill_name,
                    _json_dumps(task.payload),
                    task.workspace_path,
                    task.expected_output,
                    _json_dumps(task.metadata),
                    task.graph_id,
                    task.created_at,
                    task.updated_at,
                    task.summary,
                    task.plan_summary,
                    task.error_code,
                    task.error_message,
                ),
            )

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        updated_at: str,
        summary: str | None = None,
        plan_summary: str | None = None,
        graph_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, summary = COALESCE(?, summary),
                    plan_summary = COALESCE(?, plan_summary), graph_id = COALESCE(?, graph_id),
                    error_code = ?, error_message = ?
                WHERE task_id = ?
                """,
                (
                    status,
                    updated_at,
                    summary,
                    plan_summary,
                    graph_id,
                    error_code,
                    error_message,
                    task_id,
                ),
            )

    def get_task(self, task_id: str) -> Task | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, *, limit: int = 20, status: str | None = None) -> list[Task]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_tasks_for_thread(self, thread_id: str, *, limit: int = 50) -> list[Task]:
        """Return recent tasks whose metadata binds them to a conversation thread."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE metadata_json LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{thread_id}%", limit),
            ).fetchall()
        tasks: list[Task] = []
        for row in rows:
            task = self._row_to_task(row)
            if str(task.metadata.get("thread_id", "")) == thread_id:
                tasks.append(task)
        return tasks

    def append_event(self, event: TaskEvent) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_events (event_id, task_id, event_type, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.event_type,
                    event.status,
                    _json_dumps(event.payload),
                    event.created_at,
                ),
            )

    def list_events(self, task_id: str) -> list[TaskEvent]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [
            TaskEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                status=row["status"],
                payload=_json_loads(row["payload_json"], {}),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def log_execution(
        self, task_id: str, executor: str, payload: dict[str, Any], result: ExecutionResult
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO execution_logs (task_id, executor, payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    executor,
                    _json_dumps(payload),
                    _json_dumps(
                        {
                            "success": result.success,
                            "status": result.status,
                            "message": result.message,
                            "output": result.output,
                            "artifacts": [asdict(item) for item in result.artifacts],
                            "logs": result.logs,
                            "error_code": result.error_code,
                        }
                    ),
                    result.finished_at,
                ),
            )

    def list_execution_logs(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_logs WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [
            {
                "log_id": row["log_id"],
                "task_id": row["task_id"],
                "executor": row["executor"],
                "payload": _json_loads(row["payload_json"], {}),
                "result": _json_loads(row["result_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_permission(self, decision: PermissionDecision) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO permission_audits (
                    task_id, permission, level, status, rationale, scope, node_id, skill_name,
                    resource_kind, resource_ref, sandbox_level, source, policy,
                    requires_approval, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.task_id,
                    decision.permission,
                    decision.level,
                    decision.status,
                    decision.rationale,
                    decision.scope,
                    decision.node_id,
                    decision.skill_name,
                    decision.resource_kind,
                    decision.resource_ref,
                    decision.sandbox_level,
                    decision.source,
                    decision.policy,
                    int(decision.requires_approval),
                    _json_dumps(decision.details),
                    decision.created_at,
                ),
            )

    def list_permissions(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM permission_audits WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["requires_approval"] = bool(item.get("requires_approval", 0))
            item["details"] = _json_loads(item.pop("details_json", "{}"), {})
            payloads.append(item)
        return payloads

    def save_task_graph(self, task_id: str, graph: TaskGraph) -> None:
        graph.updated_at = graph.updated_at or graph.created_at
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_graphs (task_id, graph_id, graph_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    graph_id = excluded.graph_id,
                    graph_json = excluded.graph_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    graph.graph_id,
                    _json_dumps(graph.to_dict()),
                    graph.created_at,
                    graph.updated_at,
                ),
            )
        for node in graph.nodes:
            self.upsert_task_node(task_id, node)

    def get_task_graph(self, task_id: str) -> TaskGraph | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT graph_json FROM task_graphs WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return TaskGraph.from_dict(_json_loads(row["graph_json"], {}))

    def upsert_task_node(self, task_id: str, node: TaskNode) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_nodes (
                    task_id, node_id, node_type, status, goal, dependencies_json, required_skills_json,
                    executor_hint, skill_name, risk_level, retry_policy_json, input_payload_json,
                    output_json, artifacts_json, error_message, attempts, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, node_id) DO UPDATE SET
                    node_type = excluded.node_type,
                    status = excluded.status,
                    goal = excluded.goal,
                    dependencies_json = excluded.dependencies_json,
                    required_skills_json = excluded.required_skills_json,
                    executor_hint = excluded.executor_hint,
                    skill_name = excluded.skill_name,
                    risk_level = excluded.risk_level,
                    retry_policy_json = excluded.retry_policy_json,
                    input_payload_json = excluded.input_payload_json,
                    output_json = excluded.output_json,
                    artifacts_json = excluded.artifacts_json,
                    error_message = excluded.error_message,
                    attempts = excluded.attempts,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    node.node_id,
                    node.type,
                    node.status,
                    node.goal,
                    _json_dumps(node.dependencies),
                    _json_dumps(node.required_skills),
                    node.executor_hint,
                    node.skill_name,
                    node.risk_level,
                    _json_dumps(node.retry_policy.to_dict()),
                    _json_dumps(node.input_payload),
                    _json_dumps(node.output),
                    _json_dumps([asdict(item) for item in node.artifacts]),
                    node.error_message,
                    node.attempts,
                    _json_dumps(node.metadata),
                    utc_now(),
                ),
            )

    def list_task_nodes(self, task_id: str) -> list[TaskNode]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_nodes WHERE task_id = ? ORDER BY rowid ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_task_node(row) for row in rows]

    def record_critic_verdict(self, task_id: str, verdict: CriticVerdict) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO critic_verdicts (task_id, target_type, target_id, verdict_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    verdict.target_type,
                    verdict.target_id,
                    _json_dumps(verdict.to_dict()),
                    verdict.created_at,
                ),
            )

    def list_critic_verdicts(self, task_id: str) -> list[CriticVerdict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT verdict_json FROM critic_verdicts WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [CriticVerdict.from_dict(_json_loads(row["verdict_json"], {})) for row in rows]

    def upsert_skill_registry_entry(self, entry: SkillRegistryEntry) -> None:
        contracts_payload = [c.to_dict() for c in entry.contracts]
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO skill_registry (
                    skill_name, source, version, description, permission_requirements_json, tags_json,
                    location, scope, sandbox_level, success_count, failure_count, success_rate,
                    lifecycle_status, activation_source, activation_target, last_validated_at,
                    last_approved_at, last_seen_at, contracts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    source = excluded.source,
                    version = excluded.version,
                    description = excluded.description,
                    permission_requirements_json = excluded.permission_requirements_json,
                    tags_json = excluded.tags_json,
                    location = excluded.location,
                    scope = excluded.scope,
                    sandbox_level = excluded.sandbox_level,
                    lifecycle_status = excluded.lifecycle_status,
                    activation_source = excluded.activation_source,
                    activation_target = excluded.activation_target,
                    last_validated_at = excluded.last_validated_at,
                    last_approved_at = excluded.last_approved_at,
                    last_seen_at = excluded.last_seen_at,
                    contracts_json = excluded.contracts_json
                """,
                (
                    entry.skill_name,
                    entry.source,
                    entry.version,
                    entry.description,
                    _json_dumps(entry.permission_requirements),
                    _json_dumps(entry.tags),
                    entry.location,
                    entry.scope,
                    entry.sandbox_level,
                    entry.success_count,
                    entry.failure_count,
                    entry.success_rate,
                    entry.lifecycle_status,
                    entry.activation_source,
                    entry.activation_target,
                    entry.last_validated_at,
                    entry.last_approved_at,
                    entry.last_seen_at,
                    _json_dumps(contracts_payload),
                ),
            )

    def update_skill_registry_stats(self, skill_name: str, *, success: bool) -> None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT success_count, failure_count FROM skill_registry WHERE skill_name = ?
                """,
                (skill_name,),
            ).fetchone()
            if row is None:
                return
            success_count = int(row["success_count"]) + (1 if success else 0)
            failure_count = int(row["failure_count"]) + (0 if success else 1)
            total = success_count + failure_count
            success_rate = round(success_count / total, 3) if total else 0.0
            conn.execute(
                """
                UPDATE skill_registry
                SET success_count = ?, failure_count = ?, success_rate = ?, last_seen_at = CURRENT_TIMESTAMP
                WHERE skill_name = ?
                """,
                (success_count, failure_count, success_rate, skill_name),
            )

    def get_skill_registry_entry(self, skill_name: str) -> SkillRegistryEntry | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM skill_registry WHERE skill_name = ?", (skill_name,)
            ).fetchone()
        return self._row_to_skill_registry_entry(row) if row else None

    def list_skill_registry_entries(self, *, active_only: bool = False) -> list[SkillRegistryEntry]:
        query = "SELECT * FROM skill_registry"
        params: list[Any] = []
        if active_only:
            query += " WHERE lifecycle_status = ?"
            params.append(SkillLifecycleStatus.ACTIVE)
        query += " ORDER BY success_rate DESC, skill_name ASC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_skill_registry_entry(row) for row in rows]

    def upsert_skill_draft(self, draft: SkillDraft) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO skill_drafts (
                    skill_name, version, source_kind, source_ref, source_dir, manifest_path, main_path,
                    permission_requirements_json, lifecycle_status, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_name, version) DO UPDATE SET
                    source_kind = excluded.source_kind,
                    source_ref = excluded.source_ref,
                    source_dir = excluded.source_dir,
                    manifest_path = excluded.manifest_path,
                    main_path = excluded.main_path,
                    permission_requirements_json = excluded.permission_requirements_json,
                    lifecycle_status = excluded.lifecycle_status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    draft.skill_name,
                    draft.version,
                    draft.source_kind,
                    draft.source_ref,
                    draft.source_dir,
                    draft.manifest_path,
                    draft.main_path,
                    _json_dumps(draft.permission_requirements),
                    draft.lifecycle_status,
                    _json_dumps(draft.metadata),
                    draft.created_at,
                    draft.updated_at,
                ),
            )

    def get_skill_draft(self, skill_name: str, *, version: str | None = None) -> SkillDraft | None:
        query = "SELECT * FROM skill_drafts WHERE skill_name = ?"
        params: list[Any] = [skill_name]
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        query += " ORDER BY created_at DESC, version DESC LIMIT 1"
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row_to_skill_draft(row) if row else None

    def list_skill_drafts(self, skill_name: str | None = None) -> list[SkillDraft]:
        query = "SELECT * FROM skill_drafts"
        params: list[Any] = []
        if skill_name is not None:
            query += " WHERE skill_name = ?"
            params.append(skill_name)
        query += " ORDER BY created_at DESC, version DESC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_skill_draft(row) for row in rows]

    def append_validation_report(self, report: ValidationReport) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO skill_validation_reports (
                    report_id, skill_name, version, passed, summary, checks_json, findings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.skill_name,
                    report.version,
                    1 if report.passed else 0,
                    report.summary,
                    _json_dumps(report.checks),
                    _json_dumps(report.findings),
                    report.created_at,
                ),
            )

    def list_validation_reports(
        self, skill_name: str, *, version: str | None = None
    ) -> list[ValidationReport]:
        query = "SELECT * FROM skill_validation_reports WHERE skill_name = ?"
        params: list[Any] = [skill_name]
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        query += " ORDER BY created_at DESC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_validation_report(row) for row in rows]

    def latest_validation_report(
        self, skill_name: str, *, version: str | None = None
    ) -> ValidationReport | None:
        reports = self.list_validation_reports(skill_name, version=version)
        return reports[0] if reports else None

    def append_approval_record(self, record: ApprovalRecord) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO skill_approval_records (
                    approval_id, skill_name, version, status, approver, rationale, policy_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.approval_id,
                    record.skill_name,
                    record.version,
                    record.status,
                    record.approver,
                    record.rationale,
                    _json_dumps(record.policy),
                    record.created_at,
                ),
            )

    def list_approval_records(
        self, skill_name: str, *, version: str | None = None
    ) -> list[ApprovalRecord]:
        query = "SELECT * FROM skill_approval_records WHERE skill_name = ?"
        params: list[Any] = [skill_name]
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        query += " ORDER BY created_at DESC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_approval_record(row) for row in rows]

    def latest_approval_record(
        self, skill_name: str, *, version: str | None = None
    ) -> ApprovalRecord | None:
        records = self.list_approval_records(skill_name, version=version)
        return records[0] if records else None

    def append_registration_record(self, record: RegistrationRecord) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO skill_registration_records (
                    registration_id, skill_name, version, action, status, source_path, target_path,
                    previous_version, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.registration_id,
                    record.skill_name,
                    record.version,
                    record.action,
                    record.status,
                    record.source_path,
                    record.target_path,
                    record.previous_version,
                    _json_dumps(record.metadata),
                    record.created_at,
                ),
            )

    def list_registration_records(self, skill_name: str) -> list[RegistrationRecord]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_registration_records WHERE skill_name = ? ORDER BY created_at DESC",
                (skill_name,),
            ).fetchall()
        return [self._row_to_registration_record(row) for row in rows]

    def save_conversation_thread(self, thread: ConversationThread) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO conversation_threads (
                    thread_id, title, status, active_task_id, pending_checkpoint_id, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    active_task_id = excluded.active_task_id,
                    pending_checkpoint_id = excluded.pending_checkpoint_id,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    thread.thread_id,
                    thread.title,
                    thread.status,
                    thread.active_task_id,
                    thread.pending_checkpoint_id,
                    _json_dumps(thread.metadata),
                    thread.created_at,
                    thread.updated_at,
                ),
            )

    def get_conversation_thread(self, thread_id: str) -> ConversationThread | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return self._row_to_conversation_thread(row) if row else None

    def list_conversation_threads(
        self, *, limit: int = 20, status: str | None = None
    ) -> list[ConversationThread]:
        query = "SELECT * FROM conversation_threads"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_conversation_thread(row) for row in rows]

    def append_conversation_turn(self, turn: ConversationTurn) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns (
                    turn_id, thread_id, role, message, input_type, route, task_id, checkpoint_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    turn.thread_id,
                    turn.role,
                    turn.message,
                    turn.input_type,
                    turn.route,
                    turn.task_id,
                    turn.checkpoint_id,
                    _json_dumps(turn.payload),
                    turn.created_at,
                ),
            )

    def list_conversation_turns(self, thread_id: str) -> list[ConversationTurn]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_turns WHERE thread_id = ? ORDER BY created_at ASC",
                (thread_id,),
            ).fetchall()
        return [self._row_to_conversation_turn(row) for row in rows]

    def save_pending_checkpoint(self, checkpoint: PendingCheckpoint) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO pending_checkpoints (
                    checkpoint_id, thread_id, task_id, node_id, checkpoint_type, status, title, prompt,
                    required_permission, required_level, payload_json, created_at, updated_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    status = excluded.status,
                    title = excluded.title,
                    prompt = excluded.prompt,
                    required_permission = excluded.required_permission,
                    required_level = excluded.required_level,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    resolved_at = excluded.resolved_at
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.thread_id,
                    checkpoint.task_id,
                    checkpoint.node_id,
                    checkpoint.checkpoint_type,
                    checkpoint.status,
                    checkpoint.title,
                    checkpoint.prompt,
                    checkpoint.required_permission,
                    checkpoint.required_level,
                    _json_dumps(checkpoint.payload),
                    checkpoint.created_at,
                    checkpoint.updated_at,
                    checkpoint.resolved_at,
                ),
            )

    def get_pending_checkpoint(self, checkpoint_id: str) -> PendingCheckpoint | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM pending_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self._row_to_pending_checkpoint(row) if row else None

    def get_latest_pending_checkpoint_for_thread(self, thread_id: str) -> PendingCheckpoint | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM pending_checkpoints
                WHERE thread_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (thread_id, PendingCheckpointStatus.PENDING),
            ).fetchone()
        return self._row_to_pending_checkpoint(row) if row else None

    def list_pending_checkpoints(
        self,
        *,
        status: PendingCheckpointStatus | None = PendingCheckpointStatus.PENDING,
    ) -> list[PendingCheckpoint]:
        query = "SELECT * FROM pending_checkpoints"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_pending_checkpoint(row) for row in rows]

    def append_task_resume_attempt(self, attempt: TaskResumeAttempt) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_resume_attempts (
                    attempt_id, task_id, checkpoint_id, trigger, status, message, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.checkpoint_id,
                    attempt.trigger,
                    attempt.status,
                    attempt.message,
                    _json_dumps(attempt.payload),
                    attempt.created_at,
                ),
            )

    def list_task_resume_attempts(self, task_id: str) -> list[TaskResumeAttempt]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_resume_attempts WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_task_resume_attempt(row) for row in rows]

    def append_conversation_event(
        self, thread_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO conversation_events (thread_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, event_type, _json_dumps(payload), utc_now()),
            )

    def list_conversation_events(self, thread_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_events WHERE thread_id = ? ORDER BY created_at ASC, event_id ASC",
                (thread_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "thread_id": row["thread_id"],
                "event_type": row["event_type"],
                "payload": _json_loads(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def upsert_graph_node(
        self,
        *,
        entity_key: str,
        node_type: str,
        name: str,
        properties: dict[str, Any],
        source: str = "system",
        confidence: float = 1.0,
        score: float = 1.0,
        access_scope: str = "workspace",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        tags = tags or []
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM graph_nodes WHERE entity_key = ?",
                (entity_key,),
            ).fetchone()
            created_at = utc_now()
            version = 1
            if row is not None:
                current_snapshot = {
                    "node_type": row["node_type"],
                    "name": row["name"],
                    "properties_json": row["properties_json"],
                    "source": row["source"],
                    "confidence": float(row["confidence"]),
                    "score": float(row["score"]),
                    "access_scope": row["access_scope"],
                    "tags_json": row["tags_json"],
                }
                next_snapshot = {
                    "node_type": node_type,
                    "name": name,
                    "properties_json": _json_dumps(properties),
                    "source": source,
                    "confidence": float(confidence),
                    "score": float(score),
                    "access_scope": access_scope,
                    "tags_json": _json_dumps(tags),
                }
                version = int(row["version"])
                if current_snapshot != next_snapshot:
                    version += 1
                created_at = row["created_at"]
            conn.execute(
                """
                INSERT INTO graph_nodes (
                    entity_key, node_type, name, properties_json, source, confidence, score, access_scope,
                    tags_json, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_key) DO UPDATE SET
                    node_type = excluded.node_type,
                    name = excluded.name,
                    properties_json = excluded.properties_json,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    score = excluded.score,
                    access_scope = excluded.access_scope,
                    tags_json = excluded.tags_json,
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (
                    entity_key,
                    node_type,
                    name,
                    _json_dumps(properties),
                    source,
                    confidence,
                    score,
                    access_scope,
                    _json_dumps(tags),
                    version,
                    created_at,
                    utc_now(),
                ),
            )
            conn.execute(
                """
                INSERT INTO graph_node_versions (entity_key, version, snapshot_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    entity_key,
                    version,
                    _json_dumps(
                        {
                            "entity_key": entity_key,
                            "node_type": node_type,
                            "name": name,
                            "properties": properties,
                            "source": source,
                            "confidence": confidence,
                            "score": score,
                            "access_scope": access_scope,
                            "tags": tags,
                        }
                    ),
                    utc_now(),
                ),
            )
        return self.get_graph_node(entity_key) or {}

    def get_graph_node(self, entity_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM graph_nodes WHERE entity_key = ?",
                (entity_key,),
            ).fetchone()
        return self._row_to_graph_node(row) if row else None

    def list_graph_nodes(
        self, *, node_type: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM graph_nodes"
        params: list[Any] = []
        if node_type:
            query += " WHERE node_type = ?"
            params.append(node_type)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_graph_node(row) for row in rows]

    def upsert_graph_edge(
        self,
        *,
        source_entity_key: str,
        target_entity_key: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
        source: str = "system",
        confidence: float = 1.0,
        score: float = 1.0,
    ) -> dict[str, Any]:
        properties = properties or {}
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM graph_edges
                WHERE source_entity_key = ? AND target_entity_key = ? AND edge_type = ?
                """,
                (source_entity_key, target_entity_key, edge_type),
            ).fetchone()
            created_at = utc_now()
            version = 1
            if row is not None:
                current_snapshot = {
                    "properties_json": row["properties_json"],
                    "source": row["source"],
                    "confidence": float(row["confidence"]),
                    "score": float(row["score"]),
                }
                next_snapshot = {
                    "properties_json": _json_dumps(properties),
                    "source": source,
                    "confidence": float(confidence),
                    "score": float(score),
                }
                version = int(row["version"])
                if current_snapshot != next_snapshot:
                    version += 1
                created_at = row["created_at"]
            conn.execute(
                """
                INSERT INTO graph_edges (
                    source_entity_key, target_entity_key, edge_type, properties_json, source,
                    confidence, score, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_entity_key, target_entity_key, edge_type) DO UPDATE SET
                    properties_json = excluded.properties_json,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    score = excluded.score,
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (
                    source_entity_key,
                    target_entity_key,
                    edge_type,
                    _json_dumps(properties),
                    source,
                    confidence,
                    score,
                    version,
                    created_at,
                    utc_now(),
                ),
            )
            conn.execute(
                """
                INSERT INTO graph_edge_versions (
                    source_entity_key, target_entity_key, edge_type, version, snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_entity_key,
                    target_entity_key,
                    edge_type,
                    version,
                    _json_dumps(
                        {
                            "source_entity_key": source_entity_key,
                            "target_entity_key": target_entity_key,
                            "edge_type": edge_type,
                            "properties": properties,
                            "source": source,
                            "confidence": confidence,
                            "score": score,
                        }
                    ),
                    utc_now(),
                ),
            )
        return self.get_graph_edge(source_entity_key, target_entity_key, edge_type) or {}

    def get_graph_edge(
        self,
        source_entity_key: str,
        target_entity_key: str,
        edge_type: str,
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM graph_edges
                WHERE source_entity_key = ? AND target_entity_key = ? AND edge_type = ?
                """,
                (source_entity_key, target_entity_key, edge_type),
            ).fetchone()
        return self._row_to_graph_edge(row) if row else None

    def list_graph_edges(
        self,
        entity_key: str,
        *,
        direction: str = "both",
        edge_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if direction == "out":
            clauses.append("source_entity_key = ?")
            params.append(entity_key)
        elif direction == "in":
            clauses.append("target_entity_key = ?")
            params.append(entity_key)
        else:
            clauses.append("(source_entity_key = ? OR target_entity_key = ?)")
            params.extend([entity_key, entity_key])
        if edge_type:
            clauses.append("edge_type = ?")
            params.append(edge_type)
        query = (
            "SELECT * FROM graph_edges WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_graph_edge(row) for row in rows]

    def list_graph_neighbors(
        self,
        entity_key: str,
        *,
        edge_type: str | None = None,
        node_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        # META-006 fix: the previous implementation was classic N+1 —
        # 1 edge query + N per-neighbor get_graph_node calls + Python-side
        # node_type filter. In `recommend_skills_for_goal`, which invokes
        # this per TaskNode (up to 800), that compounded to thousands of
        # SQLite round-trips per turn.
        #
        # The rewrite: still one edge query, but then ONE batched node
        # fetch using `WHERE entity_key IN (...) AND node_type = ?`, and
        # join edges→nodes in Python. 2 queries total, SQL-side filter.
        edges = self.list_graph_edges(
            entity_key, direction="both", edge_type=edge_type, limit=limit
        )
        if not edges:
            return []
        neighbor_keys: list[str] = []
        seen_keys: set[str] = set()
        for edge in edges:
            neighbor_key = (
                edge["target_entity_key"]
                if edge["source_entity_key"] == entity_key
                else edge["source_entity_key"]
            )
            if neighbor_key in seen_keys:
                continue
            seen_keys.add(neighbor_key)
            neighbor_keys.append(neighbor_key)

        placeholders = ",".join("?" * len(neighbor_keys))
        query = f"SELECT * FROM graph_nodes WHERE entity_key IN ({placeholders})"
        params: list[Any] = list(neighbor_keys)
        if node_type:
            query += " AND node_type = ?"
            params.append(node_type)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        nodes_by_key: dict[str, dict[str, Any]] = {
            row["entity_key"]: self._row_to_graph_node(row) for row in rows
        }

        neighbors: list[dict[str, Any]] = []
        for edge in edges:
            neighbor_key = (
                edge["target_entity_key"]
                if edge["source_entity_key"] == entity_key
                else edge["source_entity_key"]
            )
            node = nodes_by_key.get(neighbor_key)
            if node is None:
                continue
            neighbors.append({"edge": edge, "node": node})
        return neighbors

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            goal=row["goal"],
            mode=row["mode"],
            source=row["source"],
            status=TaskStatus(row["status"]),
            executor=row["executor"],
            skill_name=row["skill_name"],
            payload=_json_loads(row["payload_json"], {}),
            workspace_path=row["workspace_path"],
            expected_output=row["expected_output"],
            metadata=_json_loads(row["metadata_json"], {}),
            graph_id=row["graph_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            summary=row["summary"],
            plan_summary=row["plan_summary"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def _row_to_conversation_thread(self, row: sqlite3.Row) -> ConversationThread:
        return ConversationThread(
            thread_id=row["thread_id"],
            title=row["title"],
            status=ConversationThreadStatus(row["status"]),
            active_task_id=row["active_task_id"],
            pending_checkpoint_id=row["pending_checkpoint_id"],
            metadata=_json_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_conversation_turn(self, row: sqlite3.Row) -> ConversationTurn:
        input_type = row["input_type"]
        return ConversationTurn(
            turn_id=row["turn_id"],
            thread_id=row["thread_id"],
            role=ConversationTurnRole(row["role"]),
            message=row["message"],
            input_type=UserInputType(input_type) if input_type else None,
            route=row["route"],
            task_id=row["task_id"],
            checkpoint_id=row["checkpoint_id"],
            payload=_json_loads(row["payload_json"], {}),
            created_at=row["created_at"],
        )

    def _row_to_pending_checkpoint(self, row: sqlite3.Row) -> PendingCheckpoint:
        required_level = row["required_level"]
        return PendingCheckpoint(
            checkpoint_id=row["checkpoint_id"],
            thread_id=row["thread_id"],
            task_id=row["task_id"],
            node_id=row["node_id"],
            checkpoint_type=PendingCheckpointType(row["checkpoint_type"]),
            status=PendingCheckpointStatus(row["status"]),
            title=row["title"],
            prompt=row["prompt"],
            required_permission=row["required_permission"],
            required_level=PermissionLevel(required_level) if required_level else None,
            payload=_json_loads(row["payload_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
        )

    def _row_to_task_resume_attempt(self, row: sqlite3.Row) -> TaskResumeAttempt:
        return TaskResumeAttempt(
            attempt_id=row["attempt_id"],
            task_id=row["task_id"],
            checkpoint_id=row["checkpoint_id"],
            trigger=row["trigger"],
            status=row["status"],
            message=row["message"],
            payload=_json_loads(row["payload_json"], {}),
            created_at=row["created_at"],
        )

    def _row_to_graph_node(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entity_key": row["entity_key"],
            "node_type": row["node_type"],
            "name": row["name"],
            "properties": _json_loads(row["properties_json"], {}),
            "source": row["source"],
            "confidence": float(row["confidence"]),
            "score": float(row["score"]),
            "access_scope": row["access_scope"],
            "tags": _json_loads(row["tags_json"], []),
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_graph_edge(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "edge_id": row["edge_id"],
            "source_entity_key": row["source_entity_key"],
            "target_entity_key": row["target_entity_key"],
            "edge_type": row["edge_type"],
            "properties": _json_loads(row["properties_json"], {}),
            "source": row["source"],
            "confidence": float(row["confidence"]),
            "score": float(row["score"]),
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_task_node(self, row: sqlite3.Row) -> TaskNode:
        payload = {
            "node_id": row["node_id"],
            "type": row["node_type"],
            "status": row["status"],
            "goal": row["goal"],
            "dependencies": _json_loads(row["dependencies_json"], []),
            "required_skills": _json_loads(row["required_skills_json"], []),
            "executor_hint": row["executor_hint"],
            "skill_name": row["skill_name"],
            "risk_level": row["risk_level"],
            "retry_policy": _json_loads(row["retry_policy_json"], {}),
            "input_payload": _json_loads(row["input_payload_json"], {}),
            "output": _json_loads(row["output_json"], {}),
            "artifacts": _json_loads(row["artifacts_json"], []),
            "error_message": row["error_message"],
            "attempts": row["attempts"],
            "metadata": _json_loads(row["metadata_json"], {}),
        }
        return TaskNode.from_dict(payload)

    def _row_to_skill_registry_entry(self, row: sqlite3.Row) -> SkillRegistryEntry:
        # META-001: contracts_json may be absent on rows inserted before the
        # column was added to very old databases; _json_loads returns [] then.
        contracts_raw = _json_loads(self._row_column(row, "contracts_json"), [])
        contracts = [
            SkillContract.from_dict(item) for item in contracts_raw if isinstance(item, dict)
        ]
        return SkillRegistryEntry(
            skill_name=row["skill_name"],
            source=row["source"],
            version=row["version"],
            description=row["description"],
            permission_requirements=_json_loads(row["permission_requirements_json"], []),
            tags=_json_loads(row["tags_json"], []),
            location=row["location"],
            scope=row["scope"],
            sandbox_level=row["sandbox_level"],
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            success_rate=float(row["success_rate"]),
            lifecycle_status=SkillLifecycleStatus(row["lifecycle_status"]),
            activation_source=row["activation_source"],
            activation_target=row["activation_target"],
            last_validated_at=row["last_validated_at"],
            last_approved_at=row["last_approved_at"],
            last_seen_at=row["last_seen_at"],
            contracts=contracts,
        )

    @staticmethod
    def _row_column(row: sqlite3.Row, column: str) -> Any:
        # sqlite3.Row raises IndexError on unknown columns; we want a graceful
        # None for schemas missing a migration. Keys() is the stable check.
        try:
            keys = row.keys()
        except Exception:
            return None
        return row[column] if column in keys else None

    def _row_to_skill_draft(self, row: sqlite3.Row) -> SkillDraft:
        return SkillDraft(
            skill_name=row["skill_name"],
            version=row["version"],
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            source_dir=row["source_dir"],
            manifest_path=row["manifest_path"],
            main_path=row["main_path"],
            permission_requirements=_json_loads(row["permission_requirements_json"], []),
            lifecycle_status=SkillLifecycleStatus(row["lifecycle_status"]),
            metadata=_json_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_validation_report(self, row: sqlite3.Row) -> ValidationReport:
        return ValidationReport(
            report_id=row["report_id"],
            skill_name=row["skill_name"],
            version=row["version"],
            passed=bool(row["passed"]),
            summary=row["summary"],
            checks=_json_loads(row["checks_json"], {}),
            findings=_json_loads(row["findings_json"], []),
            created_at=row["created_at"],
        )

    def _row_to_approval_record(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            skill_name=row["skill_name"],
            version=row["version"],
            status=row["status"],
            approver=row["approver"],
            rationale=row["rationale"],
            policy=_json_loads(row["policy_json"], {}),
            created_at=row["created_at"],
        )

    def _row_to_registration_record(self, row: sqlite3.Row) -> RegistrationRecord:
        return RegistrationRecord(
            registration_id=row["registration_id"],
            skill_name=row["skill_name"],
            version=row["version"],
            action=row["action"],
            status=row["status"],
            source_path=row["source_path"],
            target_path=row["target_path"],
            previous_version=row["previous_version"],
            metadata=_json_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
        )

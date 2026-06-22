from __future__ import annotations

from typing import Any

from omniagent.models import PermissionLevel, TaskRequest


TASK_INTAKE_SCHEMA_VERSION = "task-intake/v1"

_TASK_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "file_organization",
        (
            "整理",
            "归档",
            "移动",
            "复制",
            "rename",
            "move",
            "copy",
            "organize",
            "archive file",
        ),
    ),
    (
        "information_synthesis",
        (
            "归纳",
            "总结",
            "摘要",
            "提炼",
            "summarize",
            "synthesis",
            "brief",
            "digest",
        ),
    ),
    (
        "document_generation",
        (
            "文档",
            "报告",
            "草稿",
            "生成",
            "写一份",
            "write report",
            "draft",
            "document",
            "markdown",
        ),
    ),
    (
        "data_processing",
        (
            "数据",
            "csv",
            "json",
            "jsonl",
            "sqlite",
            "统计",
            "汇总",
            "aggregate",
            "report_lines",
        ),
    ),
    (
        "web_information",
        (
            "网页",
            "网站",
            "链接",
            "html",
            "http",
            "browser",
            "web",
            "fetch",
            "url",
        ),
    ),
    (
        "task_planning",
        (
            "计划",
            "规划",
            "路线",
            "拆解",
            "roadmap",
            "plan",
            "schedule",
            "next step",
        ),
    ),
    (
        "light_automation",
        (
            "自动化",
            "批量",
            "脚本",
            "定时",
            "automation",
            "batch",
            "repeat",
            "scheduler",
        ),
    ),
    (
        "development_maintenance",
        (
            "代码",
            "测试",
            "git",
            "bug",
            "fix",
            "pytest",
            "ruff",
            "commit",
            "开发",
        ),
    ),
)

_EXECUTOR_TOOL_MAP = {
    "file": "builtin.file.basic",
    "python": "builtin.python.basic",
    "browser": "builtin.browser.basic",
    "csv": "builtin.data.csv",
    "json": "builtin.data.json",
    "sqlite": "builtin.data.sqlite",
    "markdown": "builtin.text.markdown",
    "html": "builtin.web.html",
    "http": "builtin.http.client",
    "shell": "builtin.shell.safe",
    "git": "builtin.vcs.git",
    "archive": "builtin.archive.basic",
    "config": "builtin.data.config",
    "datetime": "builtin.datetime.format",
}

_TOOL_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("builtin.data.csv", ("csv",)),
    ("builtin.data.json", ("json", "jsonl", "ndjson")),
    ("builtin.data.sqlite", ("sqlite", "sql")),
    ("builtin.text.markdown", ("markdown", ".md")),
    ("builtin.web.html", ("html", "网页", "web page")),
    ("builtin.http.client", ("http", "https://", "http://", "url", "fetch")),
    ("builtin.browser.basic", ("browser", "浏览器")),
    ("builtin.archive.basic", ("zip", "archive", "tar")),
    ("builtin.vcs.git", ("git", "commit", "diff")),
    ("builtin.shell.safe", ("shell", "wc ", "grep", "命令")),
    ("builtin.python.basic", ("python", "脚本", "script")),
    ("builtin.file.basic", ("file", "文件", "reports/", "notes/", ".txt")),
)


def assess_task_intake(
    request: TaskRequest,
    *,
    skill_registry_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic pre-execution task assessment.

    This is intentionally heuristic and auditable. It does not replace the
    Planner; it records what the runtime can safely infer before planning or
    execution so evals and users can inspect the agent's initial task framing.
    """

    text = _task_text(request)
    payload = request.payload if isinstance(request.payload, dict) else {}
    task_type, task_signals = _classify_task_type(text, request, payload)
    tools = _infer_tools(text, request, payload)
    risk_level, risk_reasons = _infer_risk_level(request, payload, tools)
    validation = _infer_validation(request, payload, task_type)
    confirmation_required = _confirmation_required(request, risk_level, risk_reasons, tools)
    matched_tool_entries = _matched_tool_entries(tools, skill_registry_entries or [])

    return {
        "schema_version": TASK_INTAKE_SCHEMA_VERSION,
        "task_id": request.task_id,
        "task_type": task_type,
        "task_type_signals": task_signals,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "tools": tools,
        "matched_tool_entries": matched_tool_entries,
        "validation_strategy": validation["strategy"],
        "validation_targets": validation["targets"],
        "confirmation_required": confirmation_required["required"],
        "confirmation_reasons": confirmation_required["reasons"],
        "source": str(request.source),
        "executor": request.executor,
        "skill_name": request.skill_name,
    }


def _task_text(request: TaskRequest) -> str:
    payload_text = _flatten_text(request.payload)
    return "\n".join(
        item
        for item in (
            request.goal,
            request.expected_output or "",
            request.executor,
            request.skill_name or "",
            " ".join(request.preferred_skills),
            payload_text,
        )
        if item
    ).lower()


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join([str(key) for key in value] + [_flatten_text(item) for item in value.values()])
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "")


def _classify_task_type(
    text: str, request: TaskRequest, payload: dict[str, Any]
) -> tuple[str, list[str]]:
    explicit = request.metadata.get("task_type") if isinstance(request.metadata, dict) else None
    if explicit:
        return str(explicit), ["metadata.task_type"]

    executor = request.executor.strip().lower()
    skill = str(request.skill_name or "").strip().lower()
    if executor in {"csv", "json", "sqlite"} or any(
        item in skill for item in ("data.csv", "data.json", "data.sqlite")
    ):
        return "data_processing", ["executor_or_skill:data"]
    if executor in {"http", "browser"} or any(item in skill for item in ("http", "browser", "web.html")):
        return "web_information", ["executor_or_skill:web"]
    if executor in {"markdown"} or "text.markdown" in skill:
        return "document_generation", ["executor_or_skill:document"]
    if executor in {"git"} or "vcs.git" in skill:
        return "development_maintenance", ["executor_or_skill:development"]
    if any(item in text for item in (".csv", ".tsv", ".jsonl", ".json", ".sqlite", ".db")):
        return "data_processing", ["content:data_file"]
    if any(item in text for item in (".html", ".htm", " html ", "webpage", "web page")):
        return "web_information", ["content:web_file"]

    action = str(payload.get("action") or "").lower()
    if action in {"move", "rename", "copy", "list"}:
        return "file_organization", [f"payload.action:{action}"]
    if action in {"report", "aggregate", "select", "filter", "query", "report_lines"}:
        return "data_processing", [f"payload.action:{action}"]
    if action in {"append_section", "replace_section", "headings", "extract_section"}:
        return "document_generation", [f"payload.action:{action}"]

    best_type = "general_personal_task"
    best_signals: list[str] = []
    for task_type, terms in _TASK_TYPE_RULES:
        signals = [term for term in terms if term.lower() in text]
        if len(signals) > len(best_signals):
            best_type = task_type
            best_signals = signals
    return best_type, best_signals or ["fallback:general"]


def _infer_tools(text: str, request: TaskRequest, payload: dict[str, Any]) -> list[str]:
    tools: list[str] = []

    def add(tool: str | None) -> None:
        if tool and tool not in tools:
            tools.append(tool)

    add(request.skill_name)
    add(_EXECUTOR_TOOL_MAP.get(request.executor.strip().lower()))
    for skill in request.preferred_skills:
        add(str(skill))
    for tool, hints in _TOOL_TYPE_HINTS:
        if any(hint.lower() in text for hint in hints):
            add(tool)

    action = str(payload.get("action") or "").lower()
    if action in {"write", "append", "read", "copy", "move", "rename", "replace_line"}:
        add("builtin.file.basic")
    if action in {"report", "aggregate", "select", "filter"} and "csv" in text:
        add("builtin.data.csv")
    if action in {"report", "query", "validate", "transform", "report_lines"} and (
        "json" in text or "jsonl" in text
    ):
        add("builtin.data.json")
    if action in {"extract", "links", "report"} and ("html" in text or "网页" in text):
        add("builtin.web.html")

    return tools or ["planner"]


def _infer_risk_level(
    request: TaskRequest,
    payload: dict[str, Any],
    tools: list[str],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    action = str(payload.get("action") or "").strip().lower()
    max_permission = request.constraints.max_permission

    if max_permission.rank >= PermissionLevel.L4.rank:
        reasons.append(f"max_permission:{max_permission.value}")
    if request.constraints.allow_delete or action in {"delete", "remove", "unlink", "rmdir"}:
        reasons.append("delete_or_destructive_action")
    if any(tool in tools for tool in ("builtin.shell.safe", "builtin.python.basic")):
        reasons.append("process_or_code_execution_tool")
    if request.constraints.allow_network or any(
        tool in tools for tool in ("builtin.http.client", "builtin.browser.basic")
    ):
        reasons.append("network_or_browser_tool")
    if _has_external_path(payload):
        reasons.append("external_path")

    if any(item in reasons for item in ("delete_or_destructive_action", "external_path")):
        return "high", reasons
    if max_permission.rank >= PermissionLevel.L4.rank:
        return "high", reasons
    if reasons:
        return "medium", reasons
    if max_permission.rank >= PermissionLevel.L3.rank:
        return "medium", [f"max_permission:{max_permission.value}"]
    return "low", ["workspace_local_low_risk"]


def _has_external_path(value: Any) -> bool:
    path_keys = {"path", "source", "target", "output_path", "archive_path", "extract_dir"}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in path_keys and isinstance(item, str):
                stripped = item.strip()
                if stripped.startswith("/") or stripped == ".." or stripped.startswith("../"):
                    return True
            if _has_external_path(item):
                return True
    if isinstance(value, list):
        return any(_has_external_path(item) for item in value)
    return False


def _infer_validation(
    request: TaskRequest,
    payload: dict[str, Any],
    task_type: str,
) -> dict[str, Any]:
    targets: list[str] = []
    for key in ("path", "target", "destination", "dest", "output_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and value not in targets:
            targets.append(value)
    if request.expected_output:
        targets.append("expected_output")

    strategy = "completion_evaluation"
    action = str(payload.get("action") or "").lower()
    if task_type in {"file_organization", "document_generation"} or action in {
        "write",
        "append",
        "copy",
        "move",
        "rename",
        "replace_line",
        "append_section",
        "replace_section",
    }:
        strategy = "artifact_file_check"
    elif task_type == "data_processing":
        strategy = "data_result_and_artifact_check"
    elif task_type == "web_information":
        strategy = "retrieved_content_check"
    elif task_type == "task_planning":
        strategy = "plan_quality_review"

    return {"strategy": strategy, "targets": targets or ["task_result"]}


def _confirmation_required(
    request: TaskRequest,
    risk_level: str,
    risk_reasons: list[str],
    tools: list[str],
) -> dict[str, Any]:
    reasons: list[str] = []
    if risk_level in {"medium", "high"}:
        reasons.append(f"risk_level:{risk_level}")
    if request.constraints.max_permission.rank >= PermissionLevel.L3.rank:
        reasons.append(f"max_permission:{request.constraints.max_permission.value}")
    if any(tool in tools for tool in ("builtin.http.client", "builtin.browser.basic")) and not (
        request.constraints.allow_network
    ):
        reasons.append("network_capable_tool_without_explicit_network")
    for reason in risk_reasons:
        if reason not in reasons and reason != "workspace_local_low_risk":
            reasons.append(reason)
    return {"required": bool(reasons), "reasons": reasons}


def _matched_tool_entries(
    tools: list[str], skill_registry_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_name = {str(item.get("skill_name")): item for item in skill_registry_entries}
    matched: list[dict[str, Any]] = []
    for tool in tools:
        entry = by_name.get(tool)
        if not entry:
            continue
        matched.append(
            {
                "skill_name": tool,
                "permission_requirements": list(entry.get("permission_requirements") or []),
                "sandbox_level": entry.get("sandbox_level"),
                "lifecycle_status": entry.get("lifecycle_status"),
            }
        )
    return matched

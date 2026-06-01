"""LLM-native executor — lets the LLM itself be the executor for text-only tasks.

META-004: before this executor existed, the only way Planner could produce a
"generate a poem / write an itinerary / list three project names" task was to
wrap the LLM's own output as a ``python print(literal_string)`` node. That
wrapper:

1. Triggered an L3 code_execution permission checkpoint for a pure text task.
2. Re-ran the LLM through the sandbox to echo a string the Planner had
   already written verbatim.
3. Made genuine code-execution tasks indistinguishable from "fake python"
   text-generation tasks in the permission model.

An ``llm_native`` executor removes the pretense: Planner declares
``executor_hint="llm_native"`` and this executor calls the LLM directly with
the node goal, optional prompt hints, and workspace context. The response is
returned as an ExecutionResult and saved as a text artifact — no sandbox, no
code_execution permission, no Python.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.base import BaseExecutor
from omniagent.llm.base import LLMAdapter
from omniagent.models import ArtifactRecord, ExecutionResult, TaskRunContext, utc_now

if TYPE_CHECKING:
    from omniagent.workspace import TaskArtifactManager


_LLM_NATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "response": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["response"],
}


_LLM_NATIVE_SYSTEM_PROMPT = """你是 OmniAgent 的 llm_native 执行器。任务目标是一段纯文本生成（回答问题、写作、创意、推理、对比分析等）。

要求：
1. 用中文作答（除非用户明确指定其他语言）
2. 直接给出完整答案，不要添加"好的"、"以下是"这类过渡句
3. 如果用户需要 Markdown 结构化格式（列表、表格、标题），请使用 Markdown
4. `reasoning` 字段写一两句话解释你如何理解这个任务（可选）
5. 不要调用任何外部工具或文件系统，你就是执行者
6. 如果任务需要实时、最新、当前或本周/今天等时效性事实，必须只基于调用方提供的可用外部证据整理；证据缺失、为空、被验证码/安全验证/访问限制拦截时，不要用模型经验知识补齐。
7. 仅输出严格 JSON 对象：{"response": "...", "reasoning": "..."}
"""


_DIRECT_FRESH_DATA_TOKENS = (
    "实时",
    "最新",
    "最近",
    "此刻",
    "搜索",
    "搜一下",
    "网上",
    "联网",
    "查一下",
    "latest",
    "real-time",
    "realtime",
)
_TEMPORAL_TOKENS = (
    "当前",
    "今天",
    "明天",
    "本周",
    "这周",
    "本月",
    "周末",
    "现在",
    "today",
    "tomorrow",
    "this week",
    "weekend",
    "current",
)
_EXTERNAL_FACT_TOKENS = (
    "天气",
    "预报",
    "新闻",
    "价格",
    "股价",
    "汇率",
    "日程",
    "开放时间",
    "营业",
    "攻略",
    "门票",
    "票价",
    "评价",
    "评测",
    "评论",
    "口碑",
    "航班",
    "火车",
    "搜索",
    "联网",
    "查",
    "weather",
    "forecast",
    "news",
    "price",
    "stock",
    "exchange rate",
    "schedule",
    "opening",
    "hours",
    "travel guide",
    "review",
    "reviews",
    "feedback",
)
_MODEL_PRIOR_ALLOWED_TOKENS = (
    "不用联网",
    "不需要联网",
    "无需联网",
    "不要联网",
    "不查实时",
    "不需要实时",
    "不用实时",
    "基于常识",
    "基于通用知识",
    "凭经验",
    "典型",
    "大概",
    "rough",
    "general knowledge",
    "no web",
    "without browsing",
)
_BLOCKED_CONTENT_TOKENS = (
    "百度安全验证",
    "安全验证",
    "人机验证",
    "验证码",
    "网络不给力，请稍后重试",
    "verify you are human",
    "captcha",
    "access denied",
    "forbidden",
    "just a moment",
    "enable javascript",
    "unusual traffic",
    "location.replace(location.href.replace",
    "http-equiv=\"refresh\"",
    "url=http://www.baidu.com/",
)


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def _requires_fresh_external_data(text: str) -> bool:
    if _contains_any(text, _DIRECT_FRESH_DATA_TOKENS):
        return True
    if re.search(r"(?:19|20)\d{2}\s*年?|\b(?:19|20)\d{2}\b", text) and _contains_any(
        text, _EXTERNAL_FACT_TOKENS
    ):
        return True
    return _contains_any(text, _TEMPORAL_TOKENS) and _contains_any(
        text, _EXTERNAL_FACT_TOKENS
    )


def _allows_model_prior_knowledge(text: str) -> bool:
    return _contains_any(text, _MODEL_PRIOR_ALLOWED_TOKENS)


def _iter_context_strings(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_context_strings(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_context_strings(item)


def _fresh_data_context_issue(context_payload: dict[str, Any]) -> str | None:
    strings = [item.strip() for item in _iter_context_strings(context_payload) if item.strip()]
    if not strings:
        return "missing_external_context"
    if any(_contains_any(item, _BLOCKED_CONTENT_TOKENS) for item in strings):
        return "blocked_external_context"
    return None


class LLMNativeExecutor(BaseExecutor):
    name = "llm_native"

    def __init__(
        self,
        llm_adapter: LLMAdapter,
        artifact_manager: "TaskArtifactManager | None" = None,
    ) -> None:
        self.llm_adapter = llm_adapter
        # ISSUE-001: when the executor runs inside a task it should persist
        # the LLM response to workspace/tasks/<task_id>/llm_calls/ so that
        # `omniagent llm stats` can include llm_native text-generation
        # calls in its aggregate. The manager is optional so unit tests
        # that construct the executor in isolation still work.
        self.artifact_manager = artifact_manager

    def execute(self, context: TaskRunContext) -> ExecutionResult:
        payload = context.task_request.payload or {}
        goal = str(payload.get("prompt") or context.task_request.goal or "").strip()
        if not goal:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "llm_native executor 需要 goal 或 payload.prompt",
            )

        started_at = utc_now()
        request_payload: dict[str, Any] = {
            "goal": goal,
            "task_id": context.task.task_id,
            "node_id": context.task_request.metadata.get("node_id"),
            # Surface any extra caller-provided context so the LLM has
            # enough grounding (e.g. workflow_compiler might pass recall
            # snippets, critic feedback, or format hints as payload fields).
            "context": {key: value for key, value in payload.items() if key != "prompt"},
        }
        original_goal = context.task.goal or context.task_request.goal or goal
        context_payload = dict(request_payload["context"])
        fresh_context_issue = None
        if _requires_fresh_external_data(original_goal) and not _allows_model_prior_knowledge(
            original_goal
        ):
            fresh_context_issue = _fresh_data_context_issue(context_payload)
        if fresh_context_issue is not None:
            if fresh_context_issue == "blocked_external_context":
                message = (
                    "任务需要实时/最新外部信息，但提供给 llm_native 的外部内容被安全验证、"
                    "验证码或访问限制拦截；已停止，避免基于模型过期知识生成。"
                )
            else:
                message = (
                    "任务需要实时/最新外部信息，但 llm_native 没有可用外部证据；已停止，"
                    "避免基于模型过期知识生成。"
                )
            return ExecutionResult(
                success=False,
                status="failed",
                message=message,
                output={
                    "fresh_data_required": True,
                    "issue": fresh_context_issue,
                    "original_goal": original_goal,
                    "context_keys": sorted(context_payload.keys()),
                },
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
                error_code=ErrorCode.EXECUTION_ERROR,
            )
        # Include a small slice of bootstrap context so the LLM sees the
        # workspace rules — but don't dump the entire AGENT.md / POLICY.md
        # into every text-generation call. Just the first ~400 chars per doc
        # is enough for the LLM to recall the workspace's tone/language
        # conventions.
        bootstrap_snippets = {
            filename: (content or "")[:400]
            for filename, content in (context.bootstrap.documents or {}).items()
        }
        if bootstrap_snippets:
            request_payload["bootstrap_snippets"] = bootstrap_snippets

        try:
            response = self.llm_adapter.generate_structured(
                prompt_name="llm_native_v1",
                system_prompt=_LLM_NATIVE_SYSTEM_PROMPT,
                request_payload=request_payload,
                schema=_LLM_NATIVE_SCHEMA,
            )
        except OmniAgentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OmniAgentError(
                ErrorCode.EXECUTION_ERROR,
                f"llm_native 调用 LLM 失败: {exc}",
                details={"exception_type": type(exc).__name__},
            ) from exc

        # ISSUE-001: capture the response for `llm stats`. Wrapped in a
        # best-effort try/except because a disk write failure must not
        # fail an otherwise-successful text-generation task.
        if self.artifact_manager is not None:
            try:
                self.artifact_manager.write_llm_call(
                    context.task_dir,
                    response,
                    request_payload=request_payload,
                )
            except Exception:  # noqa: BLE001
                pass

        structured = response.structured_output or {}
        text = str(structured.get("response") or "").strip()
        if not text:
            return ExecutionResult(
                success=False,
                status="failed",
                message="llm_native 执行器未返回可用文本",
                output={"raw": response.raw_output, "structured": structured},
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
                error_code=ErrorCode.EXECUTION_ERROR,
            )

        # Persist the response as a plain-text artifact so downstream
        # tooling (responder, critic, user) can re-read it without parsing
        # ExecutionResult.output.
        artifact_dir = context.task_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        node_id = context.task_request.metadata.get("node_id") or "llm_native"
        artifact_path = artifact_dir / f"{node_id}_response.md"
        artifact_path.write_text(text, encoding="utf-8")

        return ExecutionResult(
            success=True,
            status="completed",
            message=text,
            output={
                "content": text,
                "reasoning": structured.get("reasoning"),
                "llm_trace_id": response.request_id,
            },
            artifacts=[
                ArtifactRecord(
                    kind="text",
                    path=str(artifact_path),
                    description="llm_native 生成的纯文本响应",
                ),
            ],
            executor=self.name,
            started_at=started_at,
            finished_at=utc_now(),
        )

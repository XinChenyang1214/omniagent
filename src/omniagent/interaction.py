from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from omniagent.llm.base import LLMAdapter
from omniagent.models import (
    ConversationThread,
    PendingCheckpoint,
    PendingCheckpointType,
    RouteDecision,
    Task,
    UserInputEnvelope,
    UserInputType,
)
from omniagent.prompting import PromptManager

if TYPE_CHECKING:
    from omniagent.workspace import TaskArtifactManager

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "input_type": {"type": "string"},
        "task_action": {"type": ["string", "null"]},
        "reasoning": {"type": "string"},
        "confidence": {"type": "number"},
        "extracted_goal": {"type": ["string", "null"]},
        "metadata": {"type": "object", "additionalProperties": True},
    },
    "required": [
        "input_type",
        "task_action",
        "reasoning",
        "confidence",
        "extracted_goal",
        "metadata",
    ],
}

_AFFIRMATIVE_TOKENS = (
    "同意",
    "批准",
    "允许",
    "通过",
    "继续",
    "确认",
    "可以",
    "好",
    "yes",
    "approve",
)
_NEGATIVE_TOKENS = (
    "拒绝",
    "不允许",
    "不要",
    "取消审批",
    "停止",
    "算了",
    "reject",
    "deny",
    "no",
)
_STATUS_TOKENS = ("状态", "进度", "做到哪", "当前情况", "还在", "status", "progress")
_FAILURE_TOKENS = ("为什么失败", "失败原因", "怎么失败", "报错", "error", "failed")
_REPLAN_TOKENS = ("重新规划", "重规划", "换个方案", "replan", "plan again")
_RETRY_TOKENS = ("重试", "再试", "retry", "retry it")
_CONTINUE_TOKENS = (
    "继续",
    "恢复",
    "接着",
    "刚才",
    "刚才那个",
    "那个",
    "继续那个",
    "上一个",
    "resume",
    "continue",
    "that one",
)
_CANCEL_TOKENS = ("取消", "终止", "停止任务", "abort", "cancel")
_SUPPLEMENT_TOKENS = ("补充", "约束", "要求", "改成", "改为", "追加", "另外")
_KNOWLEDGE_TOKENS = (
    "什么是",
    "解释",
    "说明",
    "有哪些规则",
    "工作区规则",
    "用户偏好",
    "根据文档",
    "policy",
    "agent.md",
    "user.md",
    "tools.md",
)
_TASK_HINT_TOKENS = (
    "写",
    "创建",
    "修改",
    "更新",
    "追加",
    "生成",
    "读取",
    "执行",
    "抓取",
    "打开",
    "分析",
    "整理",
    "总结",
    "汇总",
    "报告",
    "搜索",
    "测试",
    "append",
    "change",
    "create",
    "edit",
    "modify",
    "report",
    "run",
    "summarise",
    "summarize",
    "summary",
    "update",
    "write",
    "read",
    "fetch",
)
_SYSTEM_REQUEST_MESSAGES = {
    "help",
    "/help",
    "帮助",
    "你能做什么",
    "支持哪些命令",
    "系统信息",
    "system",
}

# META-004 cleanup: the previous `_GREETING_TOKENS / _SELF_INTRO_TOKENS /
# _THANKS_TOKENS / _CHITCHAT_TOKENS` keyword lists and the SMALL_TALK
# short-circuit were a band-aid for the missing `llm_native` executor —
# they kept "讲笑话 / 写首诗 / 你觉得 ..." out of Planner because Planner
# had no way to handle pure text-generation. With META-004 (LLMNativeExecutor)
# and META-003 (Planner decision = decline_not_a_task / plan) both in place,
# chitchat and greetings are now handled uniformly:
#   - 闲聊 / 创意 / 陪聊  → Planner decision="plan" + executor_hint="llm_native"
#   - 纯问候 (你好 / 晚安) → Planner decision="decline_not_a_task"
# so the router no longer needs to classify them — fall through to NEW_TASK
# and let Planner decide.


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def _is_system_request(text: str) -> bool:
    return text.strip().lower() in {item.lower() for item in _SYSTEM_REQUEST_MESSAGES}


def _looks_like_status_query(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in {"status", "/status", "progress", "/progress"}:
        return True
    if re.search(r"\bstatus\s*:", lowered):
        return False
    if _contains_any(stripped, _TASK_HINT_TOKENS + _SUPPLEMENT_TOKENS):
        return False
    return _contains_any(stripped, _STATUS_TOKENS)


def _looks_like_knowledge_query(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _contains_any(stripped, _TASK_HINT_TOKENS):
        return False
    if _contains_any(stripped, _STATUS_TOKENS + _FAILURE_TOKENS):
        return False
    return (
        stripped.endswith("?")
        or stripped.endswith("？")
        or _contains_any(stripped, _KNOWLEDGE_TOKENS)
    )


class InteractionRouter:
    def __init__(
        self,
        llm: LLMAdapter,
        prompts: PromptManager,
        *,
        artifact_manager: "TaskArtifactManager | None" = None,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        # ISSUE-001: optional hook so router LLM calls (which are
        # thread-scoped, not task-scoped) get persisted under
        # workspace/threads/<thread_id>/llm_calls/. Kept optional so
        # narrow unit tests can construct a router without wiring the
        # whole artifact/workspace stack.
        self.artifact_manager = artifact_manager

    def decide(
        self,
        envelope: UserInputEnvelope,
        *,
        thread: ConversationThread,
        active_task: Task | None,
        pending_checkpoint: PendingCheckpoint | None,
        thread_context: dict[str, Any] | None = None,
    ) -> tuple[RouteDecision, Any | None]:
        decision = self._rule_based(
            envelope, thread=thread, active_task=active_task, pending_checkpoint=pending_checkpoint
        )
        if decision is not None:
            return decision, None
        decision, response = self._llm_based(
            envelope,
            thread=thread,
            active_task=active_task,
            pending_checkpoint=pending_checkpoint,
            thread_context=thread_context or {},
        )
        if self.artifact_manager is not None and response is not None:
            try:
                self.artifact_manager.write_thread_llm_call(
                    thread.thread_id,
                    response,
                    request_payload={
                        "message": envelope.message,
                        "thread_id": thread.thread_id,
                        "active_task_id": thread.active_task_id,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return decision, response

    def _rule_based(
        self,
        envelope: UserInputEnvelope,
        *,
        thread: ConversationThread,
        active_task: Task | None,
        pending_checkpoint: PendingCheckpoint | None,
    ) -> RouteDecision | None:
        message = envelope.message.strip()
        if not message:
            return RouteDecision(
                thread_id=thread.thread_id,
                input_type=UserInputType.SYSTEM_REQUEST,
                task_action="noop",
                reasoning="空消息按系统请求处理",
                confidence=1.0,
            )

        if pending_checkpoint is not None:
            if _contains_any(message, _AFFIRMATIVE_TOKENS):
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.APPROVAL_RESPONSE,
                    task_action="approve",
                    target_task_id=pending_checkpoint.task_id,
                    checkpoint_id=pending_checkpoint.checkpoint_id,
                    reasoning="检测到待审批 checkpoint，且用户给出肯定回复",
                    confidence=0.99,
                    metadata={
                        "route_source": "rule",
                        "checkpoint_type": str(pending_checkpoint.checkpoint_type),
                    },
                )
            if _contains_any(message, _NEGATIVE_TOKENS):
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.APPROVAL_RESPONSE,
                    task_action="reject",
                    target_task_id=pending_checkpoint.task_id,
                    checkpoint_id=pending_checkpoint.checkpoint_id,
                    reasoning="检测到待审批 checkpoint，且用户给出拒绝回复",
                    confidence=0.99,
                    metadata={
                        "route_source": "rule",
                        "checkpoint_type": str(pending_checkpoint.checkpoint_type),
                    },
                )
            if pending_checkpoint.checkpoint_type == PendingCheckpointType.DECISION:
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.APPROVAL_RESPONSE,
                    task_action="respond",
                    target_task_id=pending_checkpoint.task_id,
                    checkpoint_id=pending_checkpoint.checkpoint_id,
                    extracted_goal=message,
                    reasoning="检测到待决策 checkpoint，当前消息作为用户决策输入",
                    confidence=0.97,
                    metadata={
                        "route_source": "rule",
                        "checkpoint_type": str(pending_checkpoint.checkpoint_type),
                    },
                )

        if _looks_like_status_query(message):
            return RouteDecision(
                thread_id=thread.thread_id,
                input_type=UserInputType.STATUS_QUERY,
                task_action="status",
                target_task_id=active_task.task_id if active_task else None,
                reasoning="命中状态查询关键词",
                confidence=0.97,
                metadata={"route_source": "rule"},
            )

        if _contains_any(message, _FAILURE_TOKENS):
            return RouteDecision(
                thread_id=thread.thread_id,
                input_type=UserInputType.FAILURE_QUERY,
                task_action="failure",
                target_task_id=active_task.task_id if active_task else None,
                reasoning="命中失败追问关键词",
                confidence=0.97,
                metadata={"route_source": "rule"},
            )

        if _contains_any(message, _REPLAN_TOKENS):
            return RouteDecision(
                thread_id=thread.thread_id,
                input_type=UserInputType.REPLAN_REQUEST,
                task_action="replan",
                target_task_id=active_task.task_id if active_task else None,
                extracted_goal=message,
                reasoning="命中重规划关键词",
                confidence=0.95,
                metadata={"route_source": "rule"},
            )

        if _is_system_request(message):
            return RouteDecision(
                thread_id=thread.thread_id,
                input_type=UserInputType.SYSTEM_REQUEST,
                task_action="help",
                reasoning="命中系统帮助/能力查询请求",
                confidence=0.97,
                metadata={"route_source": "rule"},
            )

        if _looks_like_knowledge_query(message):
            return RouteDecision(
                thread_id=thread.thread_id,
                input_type=UserInputType.KNOWLEDGE_QUERY,
                task_action="answer",
                reasoning="消息更像在查询规则、文档或工作区知识，而不是发起执行任务",
                confidence=0.9,
                metadata={"route_source": "rule"},
            )

        if active_task is not None:
            if _contains_any(message, _RETRY_TOKENS):
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.TASK_UPDATE,
                    task_action="retry",
                    target_task_id=active_task.task_id,
                    reasoning="命中重试关键词",
                    confidence=0.95,
                    metadata={"route_source": "rule"},
                )
            if _contains_any(message, _CONTINUE_TOKENS):
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.TASK_UPDATE,
                    task_action="continue",
                    target_task_id=active_task.task_id,
                    reasoning="命中继续执行关键词",
                    confidence=0.93,
                    metadata={"route_source": "rule"},
                )
            if _contains_any(message, _CANCEL_TOKENS):
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.TASK_UPDATE,
                    task_action="cancel",
                    target_task_id=active_task.task_id,
                    reasoning="命中取消任务关键词",
                    confidence=0.95,
                    metadata={"route_source": "rule"},
                )
            if _contains_any(message, _SUPPLEMENT_TOKENS):
                return RouteDecision(
                    thread_id=thread.thread_id,
                    input_type=UserInputType.TASK_UPDATE,
                    task_action="supplement",
                    target_task_id=active_task.task_id,
                    extracted_goal=message,
                    reasoning="命中补充约束关键词",
                    confidence=0.9,
                    metadata={"route_source": "rule"},
                )

        # 没有命中任何特殊 route 规则 → 直接判定为 NEW_TASK，由 Planner 决定是否规划。
        # META-004 之前这里 fall-through 到 _llm_based() 会触发一次 5-15s 的 Qwen 调用
        # （每条消息都跑一次 router_v1 + planner_v1 + responder_v1，三连 LLM）。现在有了
        # Planner decision 字段（META-003）+ LLMNativeExecutor（META-004），Planner 可以
        # 自己处理 decline_not_a_task / clarify / plan 三种出口，router 无需再做分类。
        task_hint_matched = _contains_any(message, _TASK_HINT_TOKENS)
        return RouteDecision(
            thread_id=thread.thread_id,
            input_type=UserInputType.NEW_TASK,
            task_action="create",
            extracted_goal=message,
            reasoning=(
                "消息命中任务动作动词（写/读/执行 ...），规则层直接判定为新任务"
                if task_hint_matched
                else "未命中任何特殊 route 规则，rule 层 fall-through 到 NEW_TASK，由 Planner 判定"
            ),
            confidence=0.88 if task_hint_matched else 0.7,
            metadata={
                "route_source": "rule",
                "rule_path": "task_hint" if task_hint_matched else "fall_through",
            },
        )

    def _llm_based(
        self,
        envelope: UserInputEnvelope,
        *,
        thread: ConversationThread,
        active_task: Task | None,
        pending_checkpoint: PendingCheckpoint | None,
        thread_context: dict[str, Any],
    ) -> tuple[RouteDecision, Any]:
        prompt = self.prompts.load("router_v1")
        response = self.llm.generate_structured(
            prompt_name="router_v1",
            system_prompt=prompt,
            request_payload={
                "message": envelope.message,
                "thread": {
                    "thread_id": thread.thread_id,
                    "status": str(thread.status),
                    "active_task_id": thread.active_task_id,
                    "pending_checkpoint_id": thread.pending_checkpoint_id,
                },
                "active_task": {
                    "task_id": active_task.task_id,
                    "goal": active_task.goal,
                    "status": str(active_task.status),
                    "summary": active_task.summary,
                    "plan_summary": active_task.plan_summary,
                }
                if active_task
                else None,
                "pending_checkpoint": {
                    "checkpoint_id": pending_checkpoint.checkpoint_id,
                    "task_id": pending_checkpoint.task_id,
                    "checkpoint_type": str(pending_checkpoint.checkpoint_type),
                    "prompt": pending_checkpoint.prompt,
                }
                if pending_checkpoint
                else None,
                "thread_context": thread_context,
            },
            schema=ROUTER_SCHEMA,
        )
        structured = response.structured_output
        input_type = UserInputType(str(structured.get("input_type", UserInputType.NEW_TASK)))
        decision = RouteDecision(
            thread_id=thread.thread_id,
            input_type=input_type,
            task_action=structured.get("task_action"),
            target_task_id=active_task.task_id if active_task else None,
            checkpoint_id=pending_checkpoint.checkpoint_id if pending_checkpoint else None,
            extracted_goal=structured.get("extracted_goal"),
            reasoning=str(structured.get("reasoning", "")),
            confidence=float(structured.get("confidence", 0.5)),
            metadata=dict(structured.get("metadata", {})),
        )
        decision.metadata.setdefault("route_source", "llm")
        return decision, response

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omniagent.llm.base import LLMAdapter
from omniagent.models import (
    ConversationTurn,
    PendingCheckpoint,
    RouteDecision,
    Task,
    TaskExecutionReport,
)
from omniagent.prompting import PromptManager
from omniagent.storage import SQLiteStore

if TYPE_CHECKING:
    from omniagent.workspace import TaskArtifactManager

logger = logging.getLogger(__name__)

RESPONDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "suggested_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["reply", "suggested_actions"],
}

# ---------------------------------------------------------------------------
# ISSUE-005: intent-mismatch detection + post-validation
# ---------------------------------------------------------------------------

# Verb → canonical action mapping for both Chinese and English.
_ACTION_VERB_MAP: dict[str, str] = {
    # Chinese
    "改名": "rename",
    "重命名": "rename",
    "删除": "delete",
    "移除": "delete",
    "清除": "delete",
    "读取": "read",
    "查看": "read",
    "打开": "read",
    "写入": "write",
    "创建": "write",
    "生成": "write",
    "写": "write",
    "列出": "list",
    "列举": "list",
    "显示": "list",
    "移动": "move",
    "搬": "move",
    "复制": "copy",
    "拷贝": "copy",
    "执行": "execute",
    "运行": "execute",
    "追加": "append",
    # English
    "rename": "rename",
    "delete": "delete",
    "remove": "delete",
    "read": "read",
    "write": "write",
    "create": "write",
    "list": "list",
    "move": "move",
    "copy": "copy",
    "execute": "execute",
    "run": "execute",
    "append": "append",
}

# Phrases that indicate the reply is claiming success.
_SUCCESS_CLAIM_PHRASES: tuple[str, ...] = (
    "已完成",
    "已成功",
    "已写入",
    "已生成",
    "已创建",
    "已删除",
    "已移动",
    "已复制",
    "已改名",
    "已重命名",
    "已执行",
    "已运行",
    "已追加",
    "已读取",
    "成功",
)


# ---------------------------------------------------------------------------
# ISSUE-016: smart output preview for Responder context
# ---------------------------------------------------------------------------

# Per-field character limit after content extraction.  200 was far too
# aggressive — the first 200 chars of a web page are typically CSS/meta
# tags, not useful content.  1500 chars post-extraction gives the
# Responder LLM enough signal to compose a data-rich reply while still
# fitting within typical context budgets.
_OUTPUT_FIELD_LIMIT: int = 1500

# Total character budget across *all* fields of a single node's output.
# Prevents one node with many large fields from monopolising the context.
_OUTPUT_NODE_BUDGET: int = 3000

_HTML_PATTERN = re.compile(r"<[a-zA-Z!/]")


def _extract_text_content(value: str) -> str:
    """If *value* looks like HTML, extract visible text; otherwise return as-is.

    Uses lightweight regex stripping — no external dependency required.
    Strips ``<style>``/``<script>`` blocks, all tags, HTML entities, and
    collapses whitespace.
    """
    if not _HTML_PATTERN.search(value[:200]):
        return value
    # Remove style/script blocks entirely
    text = re.sub(
        r"<(style|script)[^>]*>.*?</\1>",
        "",
        value,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    for entity, char in (
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
    ):
        text = text.replace(entity, char)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _prepare_output_preview(output: dict[str, Any]) -> dict[str, Any]:
    """Build a size-limited preview of a node's output dict.

    1. For each string value, extract text content (HTML → plaintext).
    2. Truncate individual fields to ``_OUTPUT_FIELD_LIMIT``.
    3. Enforce a total budget of ``_OUTPUT_NODE_BUDGET`` across all fields.
    """
    preview: dict[str, Any] = {}
    budget_remaining = _OUTPUT_NODE_BUDGET
    for key, val in output.items():
        if budget_remaining <= 0:
            preview[key] = "(truncated — budget exhausted)"
            continue
        if isinstance(val, str):
            extracted = _extract_text_content(val)
            limit = min(_OUTPUT_FIELD_LIMIT, budget_remaining)
            if len(extracted) > limit:
                extracted = extracted[:limit] + "..."
            budget_remaining -= len(extracted)
            preview[key] = extracted
        else:
            # Non-string values (int, bool, list, dict) — keep as-is
            preview[key] = val
    return preview


def _extract_intent_verbs(text: str) -> set[str]:
    """Extract canonical action verbs from *text*.

    Scans for known verb keywords (Chinese and English) and returns the
    set of canonical action names (e.g. ``{"rename", "read"}``).
    """
    found: set[str] = set()
    lower = text.lower()
    for keyword, canonical in _ACTION_VERB_MAP.items():
        if keyword in lower:
            found.add(canonical)
    return found


def _detect_intent_mismatch(
    user_message: str,
    node_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Compare user's intended actions with what was actually executed.

    Returns a mismatch descriptor dict if the user's message implies actions
    that none of the executed nodes performed, or ``None`` if there is no
    detectable mismatch.
    """
    user_intents = _extract_intent_verbs(user_message)
    if not user_intents:
        return None
    actual_actions: set[str] = set()
    for node in node_results:
        actual_actions.update(_extract_intent_verbs(node.get("goal", "")))
    if not actual_actions:
        return None
    unmatched = user_intents - actual_actions
    if not unmatched:
        return None
    return {
        "intent_mismatch": True,
        "user_intents": sorted(user_intents),
        "actual_actions": sorted(actual_actions),
        "unmatched_intents": sorted(unmatched),
        "warning": (
            "用户意图中包含以下动作但实际节点未执行: "
            + ", ".join(sorted(unmatched))
            + "。回复中绝对不能声称这些动作已完成。"
        ),
    }


def _validate_reply(reply: str, context: "ResponseContext") -> str | None:
    """Post-LLM validation: reject replies that contradict execution facts.

    Returns the *reply* unchanged if valid, or ``None`` to signal that the
    caller should fall back to ``context.fallback_message``.
    """
    if context.task_report is None:
        return reply

    # Guard 1: task failed → reply must not claim success.
    if not context.task_report.result.success:
        for phrase in _SUCCESS_CLAIM_PHRASES:
            if phrase in reply:
                logger.warning(
                    "ISSUE-005: reply claims success ('%s') but task failed — "
                    "falling back to factual message",
                    phrase,
                )
                return None

    # Guard 1b: completion evaluation can mark a technically successful
    # execution as not satisfying the original user goal. In that case the
    # reply must not present the task as completed.
    evaluation = context.task_report.completion_evaluation
    if evaluation is not None and not evaluation.task_satisfied:
        for phrase in _SUCCESS_CLAIM_PHRASES:
            if phrase in reply:
                logger.warning(
                    "completion evaluation says task is unsatisfied but reply "
                    "claims success ('%s') — falling back to factual message",
                    phrase,
                )
                return None

    # Guard 2: specific node actions not in results but claimed in reply.
    # E.g. user asked to rename, system only read, but reply says "已改名".
    if context.task_report.task_graph:
        actual_actions: set[str] = set()
        for node in context.task_report.task_graph.nodes:
            actual_actions.update(_extract_intent_verbs(node.goal))
        reply_claims = _extract_intent_verbs(reply)
        fabricated = reply_claims - actual_actions
        # Only flag if the fabricated action overlaps with the user's intent
        # (to avoid false positives on generic phrasing).
        if fabricated:
            user_intents = _extract_intent_verbs(context.user_message)
            dangerous_fabrication = fabricated & user_intents
            if dangerous_fabrication:
                logger.warning(
                    "ISSUE-005: reply claims actions %s that were not "
                    "executed — falling back to factual message",
                    dangerous_fabrication,
                )
                return None

    return reply


@dataclass
class ResponseContext:
    route: RouteDecision
    user_message: str
    task_report: TaskExecutionReport | None = None
    checkpoint: PendingCheckpoint | None = None
    active_task: Task | None = None
    conversation_history: list[ConversationTurn] | None = None
    fallback_message: str = ""


class ResponseGenerator:
    def __init__(
        self,
        llm: LLMAdapter,
        prompts: PromptManager,
        store: SQLiteStore,
        *,
        artifact_manager: "TaskArtifactManager | None" = None,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.store = store
        # ISSUE-001: responder calls happen outside a task (they run
        # after routing to produce the user-visible reply). When we
        # have a thread_id on the route decision, we log the response
        # under workspace/threads/<thread_id>/llm_calls/ so
        # `omniagent llm stats --thread <id>` can include them.
        self.artifact_manager = artifact_manager

    def generate(self, *, context: ResponseContext) -> str:
        try:
            prompt = self.prompts.load("responder_v1")
            request_payload = self._build_payload(context)
            response = self.llm.generate_structured(
                prompt_name="responder_v1",
                system_prompt=prompt,
                request_payload=request_payload,
                schema=RESPONDER_SCHEMA,
            )
            if self.artifact_manager is not None and context.route.thread_id:
                try:
                    self.artifact_manager.write_thread_llm_call(
                        context.route.thread_id,
                        response,
                        request_payload={
                            "route_type": str(context.route.input_type),
                            "user_message": context.user_message,
                            "thread_id": context.route.thread_id,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            reply = response.structured_output.get("reply", "")
            actions = response.structured_output.get("suggested_actions", [])
            if not reply:
                return context.fallback_message
            # ISSUE-005: post-LLM validation — reject hallucinated replies
            validated = _validate_reply(reply, context)
            if validated is None:
                return context.fallback_message
            reply = validated
            if actions:
                reply += "\n\n" + "\n".join(f"- {a}" for a in actions)
            return reply
        except Exception:
            return context.fallback_message

    def _build_payload(self, context: ResponseContext) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "route_type": str(context.route.input_type),
            "task_action": context.route.task_action,
            "user_message": context.user_message,
            "fallback_message": context.fallback_message,
        }
        if context.task_report is not None:
            task_summary = self._summarize_report(context.task_report)
            # ISSUE-005: inject intent-mismatch warning so the LLM sees an
            # explicit signal when user intent diverges from actual execution.
            mismatch = _detect_intent_mismatch(
                context.user_message, task_summary.get("node_results", [])
            )
            if mismatch:
                task_summary["intent_mismatch"] = mismatch
            payload["task_summary"] = task_summary
        if context.checkpoint is not None:
            payload["checkpoint_summary"] = self._summarize_checkpoint(context.checkpoint)
        if context.active_task is not None and context.task_report is None:
            payload["active_task"] = {
                "task_id": context.active_task.task_id,
                "goal": context.active_task.goal,
                "status": str(context.active_task.status),
                "summary": context.active_task.summary,
                "error_message": context.active_task.error_message,
            }
        if context.conversation_history:
            payload["conversation_history"] = [
                {"role": str(t.role), "message": t.message}
                for t in context.conversation_history[-10:]
            ]
        return payload

    def _summarize_report(self, report: TaskExecutionReport) -> dict[str, Any]:
        node_results = []
        if report.task_graph:
            for node in report.task_graph.nodes:
                entry: dict[str, Any] = {
                    "node_id": node.node_id,
                    "type": str(node.type),
                    "goal": node.goal,
                    "status": str(node.status),
                }
                if node.output:
                    # ISSUE-016: extract text from HTML, apply per-field +
                    # per-node budget instead of the old 200-char blind cut.
                    entry["output"] = _prepare_output_preview(node.output)
                if node.error_message:
                    entry["error"] = node.error_message
                node_results.append(entry)

        artifacts = []
        if report.result.artifacts:
            artifacts = [
                {"path": a.path, "type": a.artifact_type} for a in report.result.artifacts[:5]
            ]

        return {
            "goal": report.task.goal,
            "status": str(report.task.status),
            "success": report.result.success,
            "message": report.result.message,
            "node_results": node_results,
            "artifacts": artifacts,
            "completion_evaluation": report.completion_evaluation.to_dict()
            if report.completion_evaluation is not None
            else None,
        }

    def _summarize_checkpoint(self, checkpoint: PendingCheckpoint) -> dict[str, Any]:
        return {
            "type": str(checkpoint.checkpoint_type),
            "prompt": checkpoint.prompt,
            "options": checkpoint.options,
            "status": str(checkpoint.status),
        }

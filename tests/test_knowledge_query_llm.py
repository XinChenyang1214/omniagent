"""ROADMAP P1 #4 — knowledge query routed through LLM.

Replaces the old keyword-matching `_handle_knowledge_query` with an LLM
call against the new ``knowledge_query_v1`` prompt, keeping the previous
structured-text output as a deterministic fallback for offline use.

Coverage map:
- LLM reply is preferred when adapter succeeds  → KnowledgeQueryLLMPathTest
- Fallback to deterministic summary on error    → KnowledgeQueryFallbackTest
- Schema + prompt wiring                         → KnowledgeQueryWiringTest
- End-to-end via send_message                    → KnowledgeQueryEndToEndTest
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omniagent.config import AppConfig
from omniagent.gateway import RuntimeGateway
from omniagent.models import (
    ConversationThread,
    LLMResponse,
    utc_now,
)


# ===========================================================================
# fixture: a gateway with a stub LLM backend
# ===========================================================================


class _GatewayFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_root = Path(self.tempdir.name)
        self.workspace = self.project_root / "workspace"
        self.db_path = self.project_root / "data" / "omniagent.sqlite3"
        self.log_path = self.project_root / "artifacts" / "omniagent.log"
        self.env = {
            "OMNIAGENT_SQLITE_PATH": str(self.db_path),
            "OMNIAGENT_LOG_PATH": str(self.log_path),
            "OMNIAGENT_JSON_LOGS": "false",
            "OMNIAGENT_LLM_PROVIDER": "test",
        }

    def _build_gateway(self) -> RuntimeGateway:
        with patch.dict(os.environ, self.env, clear=False):
            config = AppConfig.from_env(self.project_root)
        return RuntimeGateway(config, self.workspace)


# ===========================================================================
# 1. LLM path — returns stub reply directly
# ===========================================================================


class KnowledgeQueryLLMPathTest(_GatewayFixture):
    def test_llm_reply_wins_over_fallback(self) -> None:
        gateway = self._build_gateway()
        thread = ConversationThread(title="t")
        reply = gateway._handle_knowledge_query(
            "工作区有什么规则？",
            thread=thread,
            active_task=None,
        )
        # Stub reply starts with "根据工作区文档" or "目前工作区没有" — both
        # are natural-language, neither matches the old structured wrapper.
        self.assertNotIn("workspace_knowledge:", reply)
        self.assertNotIn("memory_hits:", reply)
        self.assertNotIn("question:", reply)
        self.assertTrue(reply.strip())

    def test_llm_reply_contains_question_context(self) -> None:
        """Stub echoes the question; prove the payload reached the LLM."""
        gateway = self._build_gateway()
        thread = ConversationThread(title="t")
        reply = gateway._handle_knowledge_query(
            "POLICY 是什么？",
            thread=thread,
            active_task=None,
        )
        self.assertIn("POLICY", reply)

    def test_llm_reply_persists_to_thread_llm_calls(self) -> None:
        """Observability: the call should land in workspace/threads/<id>/llm_calls/."""
        gateway = self._build_gateway()
        thread = ConversationThread(title="obs")
        gateway.store.save_conversation_thread(thread)
        gateway._handle_knowledge_query(
            "工作区里有什么工具？",
            thread=thread,
            active_task=None,
        )
        thread_dir = gateway.workspace_manager.root / "threads" / thread.thread_id
        calls_dir = thread_dir / "llm_calls"
        self.assertTrue(calls_dir.exists())
        files = list(calls_dir.glob("knowledge_query_v1_*.json"))
        self.assertEqual(len(files), 1, f"expected exactly one call record, got: {files}")


# ===========================================================================
# 2. Fallback path — LLM fails, keyword summary returned
# ===========================================================================


class KnowledgeQueryFallbackTest(_GatewayFixture):
    def test_fallback_on_llm_exception(self) -> None:
        gateway = self._build_gateway()
        thread = ConversationThread(title="fb")
        # Swap the adapter with one that always raises.
        mock = MagicMock()
        mock.generate_structured.side_effect = RuntimeError("LLM down")
        gateway.llm_adapter = mock
        reply = gateway._handle_knowledge_query(
            "工作区有哪些规则？",
            thread=thread,
            active_task=None,
        )
        # Fallback structured summary should kick in.
        self.assertIn("question:", reply)

    def test_fallback_on_prompt_not_found(self) -> None:
        gateway = self._build_gateway()
        thread = ConversationThread(title="no_prompt")
        # Swap prompt manager so loading knowledge_query_v1 fails.
        mock_prompts = MagicMock()
        mock_prompts.load.side_effect = FileNotFoundError("no prompt")
        gateway.prompt_manager = mock_prompts
        reply = gateway._handle_knowledge_query(
            "工作区规则？",
            thread=thread,
            active_task=None,
        )
        self.assertIn("question:", reply)  # keyword fallback path

    def test_fallback_on_empty_llm_reply(self) -> None:
        """LLM succeeds but returns an empty `reply` string → fallback kicks in."""
        gateway = self._build_gateway()
        thread = ConversationThread(title="empty")
        mock = MagicMock()
        mock.generate_structured.return_value = LLMResponse(
            provider="test",
            model="m",
            prompt_name="knowledge_query_v1",
            request_id="r1",
            raw_output="{}",
            structured_output={"reply": "   ", "sources": []},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            started_at=utc_now(),
        )
        gateway.llm_adapter = mock
        reply = gateway._handle_knowledge_query(
            "工作区规则？",
            thread=thread,
            active_task=None,
        )
        self.assertIn("question:", reply)


# ===========================================================================
# 3. Wiring — payload structure and schema
# ===========================================================================


class KnowledgeQueryWiringTest(_GatewayFixture):
    def test_payload_contains_expected_keys(self) -> None:
        gateway = self._build_gateway()
        thread = ConversationThread(title="wiring")
        captured: dict = {}
        original = gateway.llm_adapter.generate_structured

        def _spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        gateway.llm_adapter.generate_structured = _spy
        gateway._handle_knowledge_query(
            "POLICY 文件说了什么？",
            thread=thread,
            active_task=None,
        )
        self.assertEqual(captured["prompt_name"], "knowledge_query_v1")
        payload = captured["request_payload"]
        for key in (
            "question",
            "workspace_documents",
            "document_snippets",
            "memory_hits",
            "workflow_hits",
            "thread_preferences",
            "active_task",
        ):
            self.assertIn(key, payload, f"missing {key} in request_payload")
        self.assertEqual(payload["question"], "POLICY 文件说了什么？")
        self.assertIn("POLICY.md", payload["workspace_documents"])

    def test_schema_requires_reply_and_sources(self) -> None:
        schema = RuntimeGateway._KNOWLEDGE_QUERY_SCHEMA
        self.assertEqual(set(schema["required"]), {"reply", "sources"})
        self.assertEqual(schema["properties"]["reply"]["type"], "string")
        self.assertEqual(schema["properties"]["sources"]["type"], "array")

    def test_active_task_payload_surfaces_task_metadata(self) -> None:
        gateway = self._build_gateway()
        thread = ConversationThread(title="at")
        captured: dict = {}
        original = gateway.llm_adapter.generate_structured

        def _spy(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        gateway.llm_adapter.generate_structured = _spy

        class _FakeTask:
            task_id = "tsk_active"
            goal = "do the thing"
            status = "running"
            summary = None

        gateway._handle_knowledge_query(
            "当前任务在干啥？",
            thread=thread,
            active_task=_FakeTask(),
        )
        active = captured["request_payload"]["active_task"]
        self.assertEqual(active["task_id"], "tsk_active")
        self.assertEqual(active["goal"], "do the thing")


# ===========================================================================
# 4. End-to-end via send_message
# ===========================================================================


class KnowledgeQueryEndToEndTest(_GatewayFixture):
    def test_knowledge_query_routed_and_replied(self) -> None:
        gateway = self._build_gateway()
        response = gateway.send_message("工作区里有哪些工具可以用？")
        self.assertEqual(str(response.route.input_type), "knowledge_query")
        self.assertTrue(response.assistant_message)
        # The old structured wrapper prefixes are gone.
        self.assertNotIn("workspace_knowledge:", response.assistant_message)


if __name__ == "__main__":
    unittest.main()

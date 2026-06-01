from __future__ import annotations

import json
import unittest
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

from omniagent.config import LLMConfig
from omniagent.errors import OmniAgentError
from omniagent.llm.openai_compatible_adapter import OpenAICompatibleAdapter


def _fake_response(body: dict[str, Any], status: int = 200) -> MagicMock:
    data = json.dumps(body).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = data
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    resp.status = status
    return resp


def _chat_response(content: str, model: str = "test-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


class OpenAICompatibleAdapterTest(unittest.TestCase):
    def _config(self, **overrides: Any) -> LLMConfig:
        defaults = {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-test",
            "api_base": "https://api.openai.com/v1",
        }
        defaults.update(overrides)
        return LLMConfig(**defaults)

    def test_endpoint_resolution_openai(self) -> None:
        adapter = OpenAICompatibleAdapter(self._config(api_base="https://api.openai.com/v1"))
        self.assertEqual(adapter.endpoint, "https://api.openai.com/v1/chat/completions")

    def test_endpoint_resolution_already_has_chat_completions(self) -> None:
        adapter = OpenAICompatibleAdapter(
            self._config(api_base="https://example.com/v1/chat/completions")
        )
        self.assertEqual(adapter.endpoint, "https://example.com/v1/chat/completions")

    def test_endpoint_resolution_default(self) -> None:
        adapter = OpenAICompatibleAdapter(self._config(api_base=None))
        self.assertEqual(adapter.endpoint, "https://api.openai.com/v1/chat/completions")

    def test_endpoint_resolution_ark(self) -> None:
        adapter = OpenAICompatibleAdapter(
            self._config(api_base="https://ark.cn-beijing.volces.com/api/v3")
        )
        self.assertEqual(
            adapter.endpoint, "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        )

    def test_local_endpoint_no_api_key_required(self) -> None:
        adapter = OpenAICompatibleAdapter(
            self._config(api_key=None, api_base="http://localhost:11434/v1")
        )
        self.assertEqual(adapter.endpoint, "http://localhost:11434/v1/chat/completions")

    @patch("urllib.request.urlopen")
    def test_tier1_structured_output(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _fake_response(
            _chat_response('{"ok": true}'),
        )
        adapter = OpenAICompatibleAdapter(self._config())
        response = adapter.generate_structured(
            prompt_name="health_check",
            system_prompt="test",
            request_payload={"ping": "test"},
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        self.assertEqual(response.structured_output, {"ok": True})
        self.assertEqual(response.provider, "openai")
        call_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertIn("response_format", call_body)
        self.assertEqual(call_body["response_format"]["type"], "json_schema")

    @patch("urllib.request.urlopen")
    def test_tier2_fallback_on_400(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        error_400 = urllib.error.HTTPError(
            url="test",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"error":"bad"}'),
        )
        mock_urlopen.side_effect = [
            error_400,
            _fake_response(_chat_response('{"ok": true}')),
        ]
        adapter = OpenAICompatibleAdapter(self._config())
        response = adapter.generate_structured(
            prompt_name="test",
            system_prompt="test",
            request_payload={},
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        self.assertEqual(response.structured_output, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        second_body = json.loads(mock_urlopen.call_args_list[1][0][0].data.decode("utf-8"))
        self.assertEqual(second_body["response_format"]["type"], "json_object")

    @patch("urllib.request.urlopen")
    def test_tier3_fallback_on_double_400(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        error_400 = urllib.error.HTTPError(
            url="test",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"error":"bad"}'),
        )
        mock_urlopen.side_effect = [
            error_400,
            urllib.error.HTTPError(url="test", code=400, msg="Bad", hdrs=None, fp=BytesIO(b"{}")),
            _fake_response(_chat_response('{"ok": true}')),
        ]
        adapter = OpenAICompatibleAdapter(self._config())
        response = adapter.generate_structured(
            prompt_name="test",
            system_prompt="test",
            request_payload={},
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        self.assertEqual(response.structured_output, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 3)
        third_body = json.loads(mock_urlopen.call_args_list[2][0][0].data.decode("utf-8"))
        self.assertNotIn("response_format", third_body)

    @patch("urllib.request.urlopen")
    def test_parses_json_wrapped_in_markdown(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _fake_response(
            _chat_response('```json\n{"ok": true}\n```'),
        )
        adapter = OpenAICompatibleAdapter(self._config())
        response = adapter.generate_structured(
            prompt_name="test",
            system_prompt="test",
            request_payload={},
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        self.assertEqual(response.structured_output, {"ok": True})

    @patch("urllib.request.urlopen")
    def test_parses_json_embedded_in_text(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _fake_response(
            _chat_response('Here is the result: {"ok": true} done.'),
        )
        adapter = OpenAICompatibleAdapter(self._config())
        response = adapter.generate_structured(
            prompt_name="test",
            system_prompt="test",
            request_payload={},
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )
        self.assertEqual(response.structured_output, {"ok": True})

    @patch("urllib.request.urlopen")
    def test_health_check(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _fake_response(
            _chat_response('{"ok": true}'),
        )
        adapter = OpenAICompatibleAdapter(self._config())
        health = adapter.check_health()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["provider"], "openai")

    @patch("urllib.request.urlopen")
    def test_all_retries_exhausted_raises(self, mock_urlopen: MagicMock) -> None:
        import socket

        mock_urlopen.side_effect = socket.timeout("timed out")
        adapter = OpenAICompatibleAdapter(self._config(max_retries=0))
        with self.assertRaises(OmniAgentError):
            adapter.generate_structured(
                prompt_name="test",
                system_prompt="test",
                request_payload={},
                schema={"type": "object"},
            )

    @patch("urllib.request.urlopen")
    def test_task_constraints_bound_llm_attempt_timeout(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _fake_response(_chat_response('{"ok": true}'))
        adapter = OpenAICompatibleAdapter(self._config(timeout_seconds=120, max_retries=2))

        adapter.generate_structured(
            prompt_name="test",
            system_prompt="test",
            request_payload={
                "task_request": {
                    "constraints": {
                        "timeout_seconds": 90,
                    }
                }
            },
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )

        self.assertLessEqual(mock_urlopen.call_args.kwargs["timeout"], 90.0)
        self.assertGreater(mock_urlopen.call_args.kwargs["timeout"], 89.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.error as urllib_error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from omniagent.models import TaskNode, TaskNodeType
from omniagent.sandbox import build_sandbox
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore


class FakeHTTPResponse:
    def __init__(
        self,
        body: str,
        *,
        status: int = 200,
        reason: str = "OK",
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self._body = body.encode("utf-8")
        self.status = status
        self.reason = reason
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        return None


class HttpSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.loader = SkillLoader(self.workspace)
        self.definitions = self.loader.load_all()

    def _run(self, payload: dict, *, allow_network: bool = True):
        sandbox = build_sandbox(self.workspace, level="workspace", allow_network=allow_network)
        self.addCleanup(sandbox.close)
        runner = self.loader.load_runner(self.definitions["builtin.http.client"])
        return runner(payload, {"sandbox": sandbox})

    def test_http_contracts_load_expected_actions(self) -> None:
        contracts = self.definitions["builtin.http.client"].metadata.contracts

        self.assertEqual({contract.action for contract in contracts}, {"get", "post", "put", "delete"})
        for contract in contracts:
            self.assertIn("url", contract.required_input_fields())

    def test_registry_selects_http_skill_by_tag_and_action(self) -> None:
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(self.definitions)
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="call API",
            required_skills=["http"],
            input_payload={"action": "get", "url": "https://example.test/items"},
        )

        selection = registry.select(node)

        self.assertEqual(selection.selected_skill, "builtin.http.client")

    def test_network_disabled_returns_permission_error_without_request(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            result = self._run(
                {"action": "get", "url": "https://example.test/items"},
                allow_network=False,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "permission_denied")
        urlopen.assert_not_called()

    def test_get_adds_query_params_and_headers(self) -> None:
        captured = {}

        def _fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHTTPResponse("ok")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = self._run(
                {
                    "action": "get",
                    "url": "https://example.test/items?existing=1",
                    "params": {"q": "中文", "tag": ["a", "b"]},
                    "headers": {"X-Test": "yes"},
                    "timeout": 3,
                }
            )

        request = captured["request"]
        self.assertTrue(result["success"])
        self.assertEqual(captured["timeout"], 3.0)
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("existing=1", request.full_url)
        self.assertIn("q=%E4%B8%AD%E6%96%87", request.full_url)
        self.assertIn("tag=a", request.full_url)
        self.assertIn("tag=b", request.full_url)
        self.assertEqual(request.get_header("X-test"), "yes")

    def test_post_json_body_encodes_request_and_decodes_json_response(self) -> None:
        captured = {}

        def _fake_urlopen(request, timeout):
            del timeout
            captured["request"] = request
            return FakeHTTPResponse('{"ok": true}', content_type="application/json")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = self._run(
                {
                    "action": "post",
                    "url": "https://example.test/items",
                    "json": {"name": "测试"},
                }
            )

        request = captured["request"]
        self.assertTrue(result["success"])
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"name": "测试"})
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(result["output"]["json"], {"ok": True})

    def test_post_form_body_and_basic_auth(self) -> None:
        captured = {}

        def _fake_urlopen(request, timeout):
            del timeout
            captured["request"] = request
            return FakeHTTPResponse("created", status=201, reason="Created")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = self._run(
                {
                    "action": "post",
                    "url": "https://example.test/login",
                    "form": {"name": "Ann", "role": ["admin", "user"]},
                    "auth": {"type": "basic", "username": "u", "password": "p"},
                }
            )

        request = captured["request"]
        expected_auth = "Basic " + base64.b64encode(b"u:p").decode("ascii")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["status"], 201)
        self.assertEqual(request.get_header("Authorization"), expected_auth)
        self.assertEqual(
            request.get_header("Content-type"),
            "application/x-www-form-urlencoded",
        )
        body = request.data.decode("utf-8")
        self.assertIn("name=Ann", body)
        self.assertIn("role=admin", body)
        self.assertIn("role=user", body)

    def test_put_raw_body_and_bearer_auth(self) -> None:
        captured = {}

        def _fake_urlopen(request, timeout):
            del timeout
            captured["request"] = request
            return FakeHTTPResponse("updated")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = self._run(
                {
                    "action": "put",
                    "url": "https://example.test/items/1",
                    "raw_body": "raw payload",
                    "auth": {"type": "bearer", "token": "tok"},
                }
            )

        request = captured["request"]
        self.assertTrue(result["success"])
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.data, b"raw payload")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")

    def test_delete_uses_delete_method(self) -> None:
        captured = {}

        def _fake_urlopen(request, timeout):
            del timeout
            captured["request"] = request
            return FakeHTTPResponse("", status=204, reason="No Content")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = self._run({"action": "delete", "url": "https://example.test/items/1"})

        self.assertTrue(result["success"])
        self.assertEqual(captured["request"].get_method(), "DELETE")
        self.assertEqual(result["output"]["status"], 204)

    def test_http_error_keeps_body_preview(self) -> None:
        headers = Message()
        headers["Content-Type"] = "application/json"
        error = urllib_error.HTTPError(
            "https://example.test/items/1",
            404,
            "Not Found",
            headers,
            FakeHTTPResponse('{"error": "missing"}'),
        )

        with patch("urllib.request.urlopen", side_effect=error):
            result = self._run({"action": "get", "url": "https://example.test/items/1"})

        self.assertFalse(result["success"])
        self.assertEqual(result["output"]["status"], 404)
        self.assertEqual(result["output"]["body_preview"], '{"error": "missing"}')
        self.assertEqual(result["output"]["json"], {"error": "missing"})

    def test_response_body_is_truncated_stably(self) -> None:
        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse("abcdef")):
            result = self._run(
                {
                    "action": "get",
                    "url": "https://example.test/large",
                    "max_response_chars": 3,
                }
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["body"], "abc")
        self.assertTrue(result["output"]["body_truncated"])
        self.assertEqual(result["output"]["body_size"], 6)

    def test_timeout_returns_execution_failure(self) -> None:
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = self._run({"action": "get", "url": "https://example.test/slow"})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "execution_error")
        self.assertIn("timeout", result["message"])

    def test_invalid_params_type_returns_validation_failure(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            result = self._run(
                {"action": "get", "url": "https://example.test/items", "params": ["bad"]}
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "validation_error")
        urlopen.assert_not_called()

    def test_unsupported_scheme_returns_validation_failure(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            result = self._run({"action": "get", "url": "file:///tmp/a"})

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "validation_error")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.sandbox import (
    ContainerSandbox,
    ProcessSandbox,
    WorkspaceSandbox,
    _encode_http_url,
    build_sandbox,
)


class _FakeHTTPResponse:
    status = 203

    def __init__(self, body: str = "ok") -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


class SandboxBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "seed.txt").write_text("seed", encoding="utf-8")

    def test_workspace_backend_uses_host_workspace_root(self) -> None:
        sandbox = build_sandbox(self.workspace, level="workspace")
        self.addCleanup(sandbox.close)

        self.assertIsInstance(sandbox, WorkspaceSandbox)
        self.assertEqual(sandbox.runtime_root, self.workspace.resolve())
        description = sandbox.describe()
        self.assertEqual(description["backend_name"], "workspace")
        self.assertEqual(description["runtime_root"], str(self.workspace.resolve()))

    def test_process_backend_uses_isolated_runtime_root_and_syncs_on_close(self) -> None:
        sandbox = build_sandbox(self.workspace, level="process")
        self.assertIsInstance(sandbox, ProcessSandbox)
        try:
            self.assertNotEqual(sandbox.runtime_root, self.workspace.resolve())
            self.assertEqual(
                (sandbox.runtime_root / "seed.txt").read_text(encoding="utf-8"), "seed"
            )
            isolated_file = sandbox.runtime_root / "generated" / "from-process.txt"
            isolated_file.parent.mkdir(parents=True, exist_ok=True)
            isolated_file.write_text("process", encoding="utf-8")
            self.assertFalse((self.workspace / "generated" / "from-process.txt").exists())
        finally:
            sandbox.close()

        self.assertEqual(
            (self.workspace / "generated" / "from-process.txt").read_text(encoding="utf-8"),
            "process",
        )

    def test_container_backend_uses_dedicated_virtualenv_python(self) -> None:
        sandbox = build_sandbox(self.workspace, level="container")
        self.assertIsInstance(sandbox, ContainerSandbox)
        try:
            description = sandbox.describe()
            self.assertEqual(description["backend_name"], "container")
            self.assertNotEqual(description["runtime_root"], str(self.workspace.resolve()))
            self.assertNotEqual(
                Path(description["python_executable"]).resolve(), Path(sys.executable).resolve()
            )
            response = sandbox.run_python(
                "import sys\n"
                "result = {\n"
                "    'executable': sys.executable,\n"
                "    'prefix': sys.prefix,\n"
                "    'base_prefix': sys.base_prefix,\n"
                "}\n"
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["executable"], description["python_executable"])
            self.assertNotEqual(response["result"]["prefix"], response["result"]["base_prefix"])
            self.assertIn(".omniagent_container_venv", response["result"]["executable"])
        finally:
            sandbox.close()

    def test_read_text_returns_validation_error_when_path_is_directory(self) -> None:
        sandbox = build_sandbox(self.workspace, level="workspace")
        self.addCleanup(sandbox.close)

        with self.assertRaises(OmniAgentError) as context:
            sandbox.read_text(".")

        self.assertEqual(context.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertIn("路径是目录", context.exception.message)

    def test_intent_path_requires_authorized_external_roots(self) -> None:
        home = Path(self.tempdir.name) / "home"
        downloads = home / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "photo.jpg").write_text("image", encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(home)}):
            sandbox = build_sandbox(self.workspace, level="workspace")
            self.addCleanup(sandbox.close)

            with self.assertRaises(OmniAgentError) as context:
                sandbox.list_dir("下载")

            self.assertEqual(context.exception.code, ErrorCode.PERMISSION_DENIED)
            self.assertIn("OMNIAGENT_SANDBOX_AUTHORIZED_PATHS", context.exception.message)

    def test_intent_path_and_tilde_path_are_resolved_after_authorization(self) -> None:
        home = Path(self.tempdir.name) / "home"
        downloads = home / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "photo.jpg").write_text("image", encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(home)}):
            sandbox = build_sandbox(
                self.workspace,
                level="workspace",
                authorized_roots=[downloads],
            )
            self.addCleanup(sandbox.close)

            self.assertEqual(sandbox.list_dir("下载"), ["photo.jpg"])
            self.assertEqual(sandbox.read_text("~/Downloads/photo.jpg"), "image")

    def test_custom_intent_mapping_works_for_external_paths(self) -> None:
        home = Path(self.tempdir.name) / "home"
        assets = home / "Assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "note.txt").write_text("hello", encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(home)}):
            sandbox = build_sandbox(
                self.workspace,
                level="workspace",
                authorized_roots=[assets],
                path_intents={"素材": "~/Assets"},
            )
            self.addCleanup(sandbox.close)

            self.assertEqual(sandbox.read_text("素材/note.txt"), "hello")

    def test_encode_http_url_preserves_ascii_url(self) -> None:
        url = "https://example.com/path/to?q=abc&n=1#frag"

        self.assertEqual(_encode_http_url(url), url)

    def test_encode_http_url_quotes_chinese_path_and_query(self) -> None:
        url = "https://example.com/目录/文件?q=中文&city=北京"

        self.assertEqual(
            _encode_http_url(url),
            "https://example.com/%E7%9B%AE%E5%BD%95/%E6%96%87%E4%BB%B6"
            "?q=%E4%B8%AD%E6%96%87&city=%E5%8C%97%E4%BA%AC",
        )

    def test_fetch_url_uses_encoded_request_url_for_non_ascii_http_url(self) -> None:
        sandbox = build_sandbox(self.workspace, level="workspace", allow_network=True)
        self.addCleanup(sandbox.close)

        with patch("omniagent.sandbox.urlopen", return_value=_FakeHTTPResponse("done")) as mock:
            result = sandbox.fetch_url("https://example.com/路径?q=中文")

        request_url = "https://example.com/%E8%B7%AF%E5%BE%84?q=%E4%B8%AD%E6%96%87"
        mock.assert_called_once_with(request_url, timeout=sandbox.timeout_seconds)
        self.assertEqual(result["url"], "https://example.com/路径?q=中文")
        self.assertEqual(result["request_url"], request_url)
        self.assertEqual(result["body"], "done")

    def test_fetch_url_keeps_ascii_request_url_unchanged(self) -> None:
        sandbox = build_sandbox(self.workspace, level="workspace", allow_network=True)
        self.addCleanup(sandbox.close)

        with patch("omniagent.sandbox.urlopen", return_value=_FakeHTTPResponse("ascii")) as mock:
            result = sandbox.fetch_url("https://example.com/a/b?q=abc")

        mock.assert_called_once_with(
            "https://example.com/a/b?q=abc",
            timeout=sandbox.timeout_seconds,
        )
        self.assertEqual(result["request_url"], "https://example.com/a/b?q=abc")


if __name__ == "__main__":
    unittest.main()

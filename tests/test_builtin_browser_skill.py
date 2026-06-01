from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omniagent.skills.loader import SkillLoader


class FakeSandbox:
    def __init__(self, body: str) -> None:
        self.body = body

    def fetch_url(self, url: str) -> dict[str, object]:
        return {"url": url, "status": 200, "body": self.body, "source": "https"}


class BrowserSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.loader = SkillLoader(self.workspace)
        self.definitions = self.loader.load_all()

    def _run(self, body: str):
        runner = self.loader.load_runner(self.definitions["builtin.browser.basic"])
        return runner(
            {"url": "https://www.baidu.com/s?wd=test"},
            {"sandbox": FakeSandbox(body)},
        )

    def test_access_verification_page_is_failed_result(self) -> None:
        result = self._run("<html><title>百度安全验证</title></html>")

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["output"]["blocked"])
        self.assertEqual(result["output"]["blocked_reason"], "access_verification_or_denial")

    def test_redirect_shell_page_is_failed_result(self) -> None:
        result = self._run(
            '<html><script>location.replace(location.href.replace("https://","http://"));</script>'
            '<noscript><meta http-equiv="refresh" content="0;url=http://www.baidu.com/">'
            "</noscript></html>"
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["output"]["blocked"])

    def test_regular_page_still_succeeds(self) -> None:
        result = self._run("<html><body>weather: sunny</body></html>")

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "completed")

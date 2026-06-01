from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from omniagent.config import AppConfig


class GatewayConfigTest(unittest.TestCase):
    def test_task_timeout_seconds_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            (project_root / "omniagent.toml").write_text(
                textwrap.dedent(
                    """
                    [gateway]
                    task_timeout_seconds = 1200
                    """
                ).strip(),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertEqual(config.gateway.task_timeout_seconds, 1200)

    def test_task_timeout_env_overrides_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            (project_root / "omniagent.toml").write_text(
                textwrap.dedent(
                    """
                    [gateway]
                    task_timeout_seconds = 1200
                    """
                ).strip(),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"OMNIAGENT_TASK_TIMEOUT": "45"}, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertEqual(config.gateway.task_timeout_seconds, 45)


if __name__ == "__main__":
    unittest.main()

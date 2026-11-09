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

    def test_openrouter_uses_free_defaults_and_dedicated_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            env = {
                "OMNIAGENT_LLM_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "sk-or-test",
                "OPENAI_API_KEY": "sk-openai-should-not-be-used",
                "ARK_BASE_URL": "https://unrelated-ark.example/v1",
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertEqual(config.llm.provider, "openrouter")
        self.assertEqual(config.llm.model, "openrouter/free")
        self.assertEqual(config.llm.api_base, "https://openrouter.ai/api/v1")
        self.assertEqual(config.llm.api_key, "sk-or-test")
        self.assertTrue(config.llm.openrouter_free_only)

    def test_openrouter_generic_key_and_explicit_free_model_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            env = {
                "OMNIAGENT_LLM_PROVIDER": "openrouter",
                "OMNIAGENT_LLM_MODEL": "meta-llama/llama-3.2-3b-instruct:free",
                "OMNIAGENT_LLM_API_KEY": "generic-key",
                "OPENROUTER_API_KEY": "provider-key",
                "OMNIAGENT_OPENROUTER_SITE_URL": "https://example.test/omniagent",
                "OMNIAGENT_OPENROUTER_APP_TITLE": "OmniAgent Test",
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertEqual(config.llm.model, "meta-llama/llama-3.2-3b-instruct:free")
        self.assertEqual(config.llm.api_key, "generic-key")
        self.assertEqual(config.llm.openrouter_site_url, "https://example.test/omniagent")
        self.assertEqual(config.llm.openrouter_app_title, "OmniAgent Test")

    def test_opencode_uses_free_defaults_and_dedicated_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            env = {
                "OMNIAGENT_LLM_PROVIDER": "opencode",
                "OPENCODE_API_KEY": "opencode-test-key",
                "OPENAI_API_KEY": "sk-openai-should-not-be-used",
                "ARK_BASE_URL": "https://unrelated-ark.example/v1",
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertEqual(config.llm.provider, "opencode")
        self.assertEqual(config.llm.model, "deepseek-v4-flash-free")
        self.assertEqual(config.llm.api_base, "https://opencode.ai/zen/v1")
        self.assertEqual(config.llm.api_key, "opencode-test-key")
        self.assertTrue(config.llm.opencode_free_only)
        self.assertFalse(config.llm.opencode_allow_anonymous)

    def test_opencode_generic_key_and_free_model_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            env = {
                "OMNIAGENT_LLM_PROVIDER": "opencode",
                "OMNIAGENT_LLM_MODEL": "north-mini-code-free",
                "OMNIAGENT_LLM_API_KEY": "generic-key",
                "OPENCODE_API_KEY": "provider-key",
                "OMNIAGENT_OPENCODE_FREE_ONLY": "true",
            }
            with patch.dict(os.environ, env, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertEqual(config.llm.model, "north-mini-code-free")
        self.assertEqual(config.llm.api_key, "generic-key")
        self.assertTrue(config.llm.opencode_free_only)

    def test_opencode_anonymous_mode_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            (project_root / "omniagent.toml").write_text(
                textwrap.dedent(
                    """
                    [llm]
                    provider = "opencode"
                    opencode_allow_anonymous = true
                    """
                ).strip(),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = AppConfig.from_env(project_root)

        self.assertIsNone(config.llm.api_key)
        self.assertTrue(config.llm.opencode_free_only)
        self.assertTrue(config.llm.opencode_allow_anonymous)

    def test_opencode_anonymous_env_can_disable_file_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project_root = Path(tempdir)
            (project_root / "omniagent.toml").write_text(
                textwrap.dedent(
                    """
                    [llm]
                    provider = "opencode"
                    opencode_allow_anonymous = true
                    """
                ).strip(),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"OMNIAGENT_OPENCODE_ALLOW_ANONYMOUS": "false"},
                clear=True,
            ):
                config = AppConfig.from_env(project_root)

        self.assertFalse(config.llm.opencode_allow_anonymous)


if __name__ == "__main__":
    unittest.main()

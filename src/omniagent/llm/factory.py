from __future__ import annotations

from dataclasses import replace

from omniagent.config import LLMConfig
from omniagent.llm.base import LLMAdapter
from omniagent.llm.openai_compatible_adapter import OpenAICompatibleAdapter
from omniagent.llm.resilient_adapter import ResilientLLMAdapter
from omniagent.llm.test_adapter import TestLLMAdapter

# External test adapters can register here via register_test_provider().
_custom_providers: dict[str, type[LLMAdapter]] = {}


def register_test_provider(name: str, adapter_cls: type[LLMAdapter]) -> None:
    """Register a custom LLM adapter class for use in tests."""
    _custom_providers[name.lower()] = adapter_cls


def _build_provider(config: LLMConfig) -> LLMAdapter:
    provider = config.provider.lower()
    if provider in _custom_providers:
        return _custom_providers[provider](config)
    if provider == "test":
        return TestLLMAdapter(config)
    return OpenAICompatibleAdapter(config)


def build_llm_adapter(config: LLMConfig) -> LLMAdapter:
    primary = _build_provider(config)
    provider = config.provider.lower()
    fallback_provider = config.fallback_provider.lower() if config.fallback_provider else ""
    if config.enable_runtime_fallback and fallback_provider and fallback_provider != provider:
        fallback_config = replace(
            config,
            provider=fallback_provider,
            model=f"{config.model}-fallback-{fallback_provider}",
        )
        try:
            fallback = _build_provider(fallback_config)
        except Exception:
            return primary
        return ResilientLLMAdapter(primary, fallback)
    return primary

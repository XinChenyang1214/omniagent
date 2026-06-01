"""Shared test configuration.

Registers the StubLLMAdapter so integration tests can use
OMNIAGENT_LLM_PROVIDER=test without depending on the deleted mock adapter.
"""

from tests.llm_test_helper import StubLLMAdapter
from omniagent.llm.factory import register_test_provider

register_test_provider("test", StubLLMAdapter)

from __future__ import annotations

from pathlib import Path

from omniagent.config import LLMConfig
from omniagent.errors import ErrorCode, OmniAgentError


class PromptManager:
    def __init__(self, config: LLMConfig) -> None:
        if config.prompt_dir is None:
            raise OmniAgentError(ErrorCode.CONFIG_INVALID, "未配置 prompt 目录")
        self.prompt_dir = Path(config.prompt_dir).resolve()

    def load(self, prompt_name: str) -> str:
        path = self.prompt_dir / f"{prompt_name}.md"
        if not path.exists():
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"Prompt 模板不存在: {path}")
        return path.read_text(encoding="utf-8")

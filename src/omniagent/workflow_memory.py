from __future__ import annotations

from omniagent.memory import FileMemoryManager


class WorkflowMemory:
    def __init__(self, memory_manager: FileMemoryManager) -> None:
        self.memory_manager = memory_manager

    def recall(self, goal: str, *, limit: int = 5) -> list[dict]:
        return self.memory_manager.recall_workflows(goal, limit=limit)

    def recall_failures(self, goal: str, *, limit: int = 5) -> list[dict]:
        return self.memory_manager.recall_failure_patterns(goal, limit=limit)

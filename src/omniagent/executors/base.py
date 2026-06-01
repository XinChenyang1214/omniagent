from __future__ import annotations

from abc import ABC, abstractmethod

from omniagent.models import ExecutionResult, TaskRunContext


class BaseExecutor(ABC):
    name = "base"
    # META-001: the set of `action` values this executor's code can handle.
    # Must match the `contracts[*].action` set declared in the matching
    # builtin skill.yaml — `RuntimeGateway._check_executor_skill_consistency`
    # cross-checks this at startup so a drift between executor code and
    # skill manifest fails loudly instead of at dispatch time. An empty
    # tuple means "this executor has no action-based branching" (e.g.
    # llm_native) and the consistency check is skipped for it.
    supported_actions: tuple[str, ...] = ()

    @abstractmethod
    def execute(self, context: TaskRunContext) -> ExecutionResult:
        raise NotImplementedError

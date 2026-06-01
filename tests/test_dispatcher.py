from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from omniagent.dispatcher import SkillDispatcher
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.models import BootstrapContext, Task, TaskRequest, TaskRunContext, TaskStatus
from omniagent.sandbox import build_sandbox
from omniagent.skills.loader import SkillDefinition, SkillMetadata


class DispatcherSkillRunnerErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def test_run_skill_wraps_unhandled_exception_as_skill_error(self) -> None:
        skill_loader = MagicMock()

        def _broken_runner(payload, context):
            raise ValueError("bad action")

        skill_loader.load_runner.return_value = _broken_runner
        dispatcher = SkillDispatcher(
            skill_loader=skill_loader,
            skill_registry=MagicMock(),
            auto_skill_generator=MagicMock(),
            executors={},
        )
        skill = SkillDefinition(
            metadata=SkillMetadata(
                name="workspace.broken",
                version="0.1.0",
                description="broken",
                input_schema={},
                output_schema={},
                permission_requirements=[],
                dependency_requirements=[],
                side_effects=[],
                sandbox_level="workspace",
                tags=[],
            ),
            root_path=self.workspace / "skills" / "broken",
            main_path=self.workspace / "skills" / "broken" / "main.py",
            location="workspace",
        )
        task_request = TaskRequest(
            goal="run broken skill", payload={"action": "list_files"}, task_id="task_test"
        )
        task = Task(
            task_id=task_request.task_id,
            goal=task_request.goal,
            mode=str(task_request.mode),
            source=str(task_request.source),
            status=TaskStatus.RUNNING,
            executor="auto",
            skill_name=skill.metadata.name,
            payload=dict(task_request.payload),
            workspace_path=str(self.workspace),
        )
        task_dir = self.workspace / "tasks" / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        sandbox = build_sandbox(self.workspace, level="workspace")
        self.addCleanup(sandbox.close)
        context = TaskRunContext(
            task=task,
            task_request=task_request,
            workspace_root=self.workspace,
            task_dir=task_dir,
            bootstrap=BootstrapContext(documents={}, memory_summaries=[], workflow_summaries=[]),
            loaded_skills={},
            sandbox=sandbox,
        )

        with self.assertRaises(OmniAgentError) as exception_context:
            dispatcher._run_skill(skill, context)

        self.assertEqual(exception_context.exception.code, ErrorCode.SKILL_ERROR)
        self.assertIn("运行失败", exception_context.exception.message)
        self.assertEqual(exception_context.exception.details.get("exception_type"), "ValueError")


if __name__ == "__main__":
    unittest.main()

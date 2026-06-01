from __future__ import annotations

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.base import BaseExecutor
from omniagent.models import ArtifactRecord, ExecutionResult, TaskRunContext, utc_now


class PythonExecutor(BaseExecutor):
    name = "python"
    # META-001: keep this in sync with builtin_skills/python.basic/skill.yaml.
    supported_actions = ("run",)

    def execute(self, context: TaskRunContext) -> ExecutionResult:
        code = str(context.task_request.payload.get("code", ""))
        if not code.strip():
            raise OmniAgentError(ErrorCode.VALIDATION_ERROR, "python executor 需要 code")

        started_at = utc_now()
        payload = context.sandbox.run_python(code)
        if not bool(payload.get("ok")):
            return ExecutionResult(
                success=False,
                status="failed",
                message=str(payload.get("error", "Python 执行失败")),
                output={
                    "stdout": payload.get("stdout", ""),
                    "traceback": payload.get("traceback", ""),
                },
                logs=[str(payload.get("traceback", ""))],
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
                error_code=ErrorCode.EXECUTION_ERROR,
            )

        result_artifact = context.task_dir / "artifacts" / "python_stdout.txt"
        result_artifact.write_text(str(payload.get("stdout", "")), encoding="utf-8")
        return ExecutionResult(
            success=True,
            status="completed",
            message="Python 执行成功",
            output={"result": payload.get("result"), "stdout": payload.get("stdout", "")},
            artifacts=[
                ArtifactRecord(
                    kind="log", path=str(result_artifact), description="Python stdout 快照"
                ),
            ],
            executor=self.name,
            started_at=started_at,
            finished_at=utc_now(),
        )

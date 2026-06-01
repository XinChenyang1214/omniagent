from __future__ import annotations

import shutil

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.base import BaseExecutor
from omniagent.models import ArtifactRecord, ExecutionResult, TaskRunContext, utc_now


def _append_text(existing: str, content: str) -> str:
    if existing and not existing.endswith("\n") and content and not content.startswith("\n"):
        return existing + "\n" + content
    return existing + content


class FileExecutor(BaseExecutor):
    name = "file"
    # META-001: keep this in sync with builtin_skills/file.basic/skill.yaml.
    supported_actions = (
        "list",
        "read",
        "write",
        "append",
        "replace_line",
        "copy",
        "move",
        "rename",
        "delete",
    )

    def execute(self, context: TaskRunContext) -> ExecutionResult:
        payload = context.task_request.payload
        action = str(payload.get("action", "read"))
        started_at = utc_now()

        if action == "list":
            path = str(payload.get("path", "."))
            entries = context.sandbox.list_dir(path)
            return ExecutionResult(
                success=True,
                status="completed",
                message=f"列出目录 {path} 成功",
                output={"entries": entries, "path": path},
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
            )

        if action == "read":
            path = str(payload.get("path", ""))
            if not path:
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, "file.read 需要 path")
            content = context.sandbox.read_text(path)
            artifact_path = str(context.sandbox.resolve_host_path(path))
            return ExecutionResult(
                success=True,
                status="completed",
                message=f"读取文件 {path} 成功",
                output={"content": content, "path": path},
                artifacts=[
                    ArtifactRecord(kind="file", path=artifact_path, description="workspace 文件")
                ],
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
            )

        if action in {"write", "append"}:
            path = str(payload.get("path", ""))
            content = str(payload.get("content", ""))
            if not path:
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, "file.write 需要 path")
            overwrite = bool(payload.get("overwrite", False))
            target = context.sandbox.resolve_path(path)
            if target.exists() and target.is_dir():
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"写入失败，目标是目录: {target}")
            if action == "append" and target.exists():
                existing = target.read_text(encoding="utf-8")
                content = _append_text(existing, content)
                overwrite = True
            written = context.sandbox.write_text(path, content, overwrite=overwrite)
            return ExecutionResult(
                success=True,
                status="completed",
                message=f"写入文件 {path} 成功",
                output={"path": path, "size": len(content)},
                artifacts=[ArtifactRecord(kind="file", path=str(written), description="写入结果")],
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
            )

        if action == "replace_line":
            path = str(payload.get("path", ""))
            line_prefix = str(payload.get("line_prefix", ""))
            content = str(payload.get("content", ""))
            if not path:
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, "file.replace_line 需要 path")
            if not line_prefix:
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    "file.replace_line 需要 line_prefix",
                )
            target = context.sandbox.resolve_path(path)
            if not target.exists():
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"文件不存在: {path}")
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            matched = [index for index, line in enumerate(lines) if line.startswith(line_prefix)]
            if len(matched) != 1:
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"line_prefix 匹配到 {len(matched)} 行，期望 1 行: {line_prefix}",
                    details={"matches": len(matched), "path": path},
                )
            index = matched[0]
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = content + newline
            updated = "".join(lines)
            written = context.sandbox.write_text(path, updated, overwrite=True)
            return ExecutionResult(
                success=True,
                status="completed",
                message=f"替换文件 {path} 中的目标行成功",
                output={"path": path, "line_index": index, "size": len(updated)},
                artifacts=[
                    ArtifactRecord(kind="file", path=str(written), description="行替换结果")
                ],
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
            )

        if action in {"copy", "move", "rename"}:
            source = str(payload.get("source") or payload.get("path") or "")
            target = str(
                payload.get("target") or payload.get("destination") or payload.get("dest") or ""
            )
            if not source or not target:
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"file.{action} 需要 source 和 target（或 path + target）",
                )
            source_path = context.sandbox.resolve_path(source)
            target_path = context.sandbox.resolve_path(target)
            if not source_path.exists():
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"源文件不存在: {source}",
                )
            overwrite = bool(payload.get("overwrite", False))
            if target_path.exists() and not overwrite:
                raise OmniAgentError(
                    ErrorCode.VALIDATION_ERROR,
                    f"目标已存在，如需覆盖请设置 overwrite=true: {target}",
                )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if action == "copy":
                if source_path.is_dir():
                    if target_path.exists():
                        shutil.rmtree(target_path)
                    shutil.copytree(source_path, target_path)
                else:
                    shutil.copy2(source_path, target_path)
                verb = "复制"
            else:  # move / rename
                shutil.move(str(source_path), str(target_path))
                verb = "移动" if action == "move" else "重命名"
            return ExecutionResult(
                success=True,
                status="completed",
                message=f"{verb} {source} → {target} 成功",
                output={"source": source, "target": target, "action": action},
                artifacts=[
                    ArtifactRecord(
                        kind="file",
                        path=str(target_path),
                        description=f"{verb}后的目标",
                    )
                ],
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
            )

        if action == "delete":
            path = str(payload.get("path", ""))
            if not path:
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, "file.delete 需要 path")
            # 删除是破坏性操作，需要 constraints.allow_delete 或者 L3+ 权限兜底
            constraints = context.task_request.constraints
            if not constraints.allow_delete:
                raise OmniAgentError(
                    ErrorCode.PERMISSION_DENIED,
                    f"删除文件需要 allow_delete=true（当前任务未授权）: {path}",
                )
            target_path = context.sandbox.resolve_path(path)
            if not target_path.exists():
                raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"文件不存在: {path}")
            if target_path.is_dir():
                shutil.rmtree(target_path)
            else:
                target_path.unlink()
            return ExecutionResult(
                success=True,
                status="completed",
                message=f"删除 {path} 成功",
                output={"path": path, "action": "delete"},
                executor=self.name,
                started_at=started_at,
                finished_at=utc_now(),
            )

        raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"不支持的 file action: {action}")

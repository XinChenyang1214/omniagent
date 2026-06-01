from __future__ import annotations

import shutil


def _append_text(existing: str, content: str) -> str:
    if existing and not existing.endswith("\n") and content and not content.startswith("\n"):
        return existing + "\n" + content
    return existing + content


def run(payload, context):
    sandbox = context["sandbox"]
    action = str(payload.get("action", "read"))

    if action == "list":
        path = str(payload.get("path", "."))
        return {
            "success": True,
            "status": "completed",
            "message": f"列出目录 {path} 成功",
            "output": {"entries": sandbox.list_dir(path), "path": path},
        }

    if action == "read":
        path = str(payload.get("path", ""))
        if not path:
            return {
                "success": False,
                "status": "failed",
                "message": "file.read 需要 path",
                "output": {},
            }
        return {
            "success": True,
            "status": "completed",
            "message": f"读取文件 {path} 成功",
            "output": {"content": sandbox.read_text(path), "path": path},
        }

    if action in {"write", "append"}:
        path = str(payload.get("path", ""))
        if not path:
            return {
                "success": False,
                "status": "failed",
                "message": f"file.{action} 需要 path",
                "output": {},
            }
        content = str(payload.get("content", ""))
        target_path = sandbox.resolve_path(path)
        if target_path.exists() and target_path.is_dir():
            return {
                "success": False,
                "status": "failed",
                "message": f"写入失败，目标是目录: {target_path}",
                "output": {},
            }
        overwrite = bool(payload.get("overwrite", False))
        if action == "append" and target_path.exists():
            existing = target_path.read_text(encoding="utf-8")
            content = _append_text(existing, content)
            overwrite = True
        written = sandbox.write_text(path, content, overwrite=overwrite)
        return {
            "success": True,
            "status": "completed",
            "message": f"写入文件 {path} 成功",
            "output": {"path": path, "size": len(content)},
            "artifacts": [{"kind": "file", "path": str(written), "description": "skill 写入结果"}],
        }

    if action == "replace_line":
        path = str(payload.get("path", ""))
        line_prefix = str(payload.get("line_prefix", ""))
        content = str(payload.get("content", ""))
        if not path:
            return {
                "success": False,
                "status": "failed",
                "message": "file.replace_line 需要 path",
                "output": {},
            }
        if not line_prefix:
            return {
                "success": False,
                "status": "failed",
                "message": "file.replace_line 需要 line_prefix",
                "output": {},
            }
        target_path = sandbox.resolve_path(path)
        if not target_path.exists():
            return {
                "success": False,
                "status": "failed",
                "message": f"文件不存在: {path}",
                "output": {},
            }
        lines = target_path.read_text(encoding="utf-8").splitlines(keepends=True)
        matched = [index for index, line in enumerate(lines) if line.startswith(line_prefix)]
        if len(matched) != 1:
            return {
                "success": False,
                "status": "failed",
                "message": f"line_prefix 匹配到 {len(matched)} 行，期望 1 行: {line_prefix}",
                "output": {"matches": len(matched), "path": path},
            }
        index = matched[0]
        newline = "\n" if lines[index].endswith("\n") else ""
        lines[index] = content + newline
        updated = "".join(lines)
        written = sandbox.write_text(path, updated, overwrite=True)
        return {
            "success": True,
            "status": "completed",
            "message": f"替换文件 {path} 中的目标行成功",
            "output": {"path": path, "line_index": index, "size": len(updated)},
            "artifacts": [{"kind": "file", "path": str(written), "description": "skill 行替换结果"}],
        }

    if action in {"copy", "move", "rename"}:
        source = str(payload.get("source") or payload.get("path") or "")
        target = str(
            payload.get("target") or payload.get("destination") or payload.get("dest") or ""
        )
        if not source or not target:
            return {
                "success": False,
                "status": "failed",
                "message": f"file.{action} 需要 source 和 target",
                "output": {},
            }
        source_path = sandbox.resolve_path(source)
        target_path = sandbox.resolve_path(target)
        if not source_path.exists():
            return {
                "success": False,
                "status": "failed",
                "message": f"源文件不存在: {source}",
                "output": {},
            }
        overwrite = bool(payload.get("overwrite", False))
        if target_path.exists() and not overwrite:
            return {
                "success": False,
                "status": "failed",
                "message": f"目标已存在，如需覆盖请设置 overwrite=true: {target}",
                "output": {},
            }
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if action == "copy":
            if source_path.is_dir():
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(source_path, target_path)
            else:
                shutil.copy2(source_path, target_path)
            verb = "复制"
        else:
            shutil.move(str(source_path), str(target_path))
            verb = "移动" if action == "move" else "重命名"
        return {
            "success": True,
            "status": "completed",
            "message": f"{verb} {source} → {target} 成功",
            "output": {"source": source, "target": target, "action": action},
            "artifacts": [
                {"kind": "file", "path": str(target_path), "description": f"{verb}后的目标"}
            ],
        }

    if action == "delete":
        # builtin skill 上下文无法访问 task_request.constraints.allow_delete，
        # 为安全起见直接拒绝，让 dispatcher 回退到 FileExecutor（由其做授权校验）。
        return {
            "success": False,
            "status": "failed",
            "message": "builtin.file.basic 不支持 delete，请使用 file 执行器走授权路径",
            "output": {},
        }

    return {
        "success": False,
        "status": "failed",
        "message": f"不支持的 file action: {action}",
        "output": {},
    }

from __future__ import annotations

import subprocess
from typing import Any

from omniagent.errors import OmniAgentError


def _ok(message: str, output: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "status": "completed", "message": message, "output": output}


def _fail(message: str, output: dict[str, Any] | None = None, error_code: str | None = None):
    result = {
        "success": False,
        "status": "failed",
        "message": message,
        "output": output or {},
    }
    if error_code:
        result["error_code"] = error_code
    return result


def _bounded_text(value: str, limit: int) -> tuple[str, bool, int]:
    size = len(value)
    if size <= limit:
        return value, False, size
    return value[:limit], True, size


def _limit(payload: dict[str, Any]) -> int:
    value = payload.get("max_output_chars", 12000)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("git max_output_chars must be a positive integer")
    return min(value, 200000)


def _cwd(payload: dict[str, Any], sandbox):
    raw = str(payload.get("cwd") or ".")
    target = sandbox.resolve_path(raw)
    if not target.exists():
        raise ValueError(f"git cwd does not exist: {raw}")
    if not target.is_dir():
        raise ValueError(f"git cwd is not a directory: {raw}")
    return target, raw


def _paths(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("paths", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("git paths must be an array")
    paths: list[str] = []
    for item in raw:
        value = str(item)
        if not value or value.startswith("-") or "\x00" in value:
            raise ValueError("git paths must be non-option path strings")
        paths.append(value)
    return paths


def _max_count(payload: dict[str, Any]) -> int:
    value = payload.get("max_count", 20)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("git max_count must be a positive integer")
    return min(value, 200)


def _argv(payload: dict[str, Any]) -> list[str]:
    action = str(payload.get("action") or "status").strip().lower()
    paths = _paths(payload)
    if action == "status":
        argv = ["git", "status", "--short"]
        if paths:
            argv.extend(["--", *paths])
        return argv
    if action == "diff":
        argv = ["git", "diff", "--no-ext-diff"]
        if bool(payload.get("stat", False)):
            argv.append("--stat")
        if paths:
            argv.extend(["--", *paths])
        return argv
    if action == "log":
        return ["git", "log", "--oneline", "-n", str(_max_count(payload))]
    if action == "show":
        rev = str(payload.get("rev") or "HEAD").strip()
        if not rev or rev.startswith("-") or "\x00" in rev:
            raise ValueError("git rev must be a non-option revision string")
        return ["git", "show", "--stat", "--oneline", rev]
    raise ValueError(f"unsupported git action: {action}")


def run(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    sandbox = context["sandbox"]
    try:
        if not getattr(sandbox, "allow_external_commands", False):
            return _fail(
                "当前 sandbox 未开启外部命令执行，builtin.vcs.git 拒绝请求",
                error_code="permission_denied",
            )
        cwd, raw_cwd = _cwd(payload, sandbox)
        argv = _argv(payload)
        timeout = min(float(payload.get("timeout", 10)), 60.0)
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        limit = _limit(payload)
        stdout, stdout_truncated, stdout_size = _bounded_text(completed.stdout, limit)
        stderr, stderr_truncated, stderr_size = _bounded_text(completed.stderr, limit)
        output = {
            "argv": argv,
            "cwd": raw_cwd,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_size": stdout_size,
            "stderr_size": stderr_size,
        }
        if completed.returncode == 0:
            return _ok("Git command completed", output)
        return _fail("Git command failed", output, error_code="execution_error")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output if isinstance(exc.output, str) else (exc.output or b"").decode(
            "utf-8", "replace"
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(
            "utf-8", "replace"
        )
        limit = _limit(payload)
        stdout, stdout_truncated, stdout_size = _bounded_text(stdout, limit)
        stderr, stderr_truncated, stderr_size = _bounded_text(stderr, limit)
        return _fail(
            "Git command timed out",
            {
                "argv": _argv(payload),
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "stdout_size": stdout_size,
                "stderr_size": stderr_size,
                "timed_out": True,
            },
            error_code="execution_error",
        )
    except OmniAgentError as exc:
        return _fail(exc.message, error_code=str(exc.code))
    except (OSError, ValueError) as exc:
        return _fail(str(exc), error_code="validation_error")

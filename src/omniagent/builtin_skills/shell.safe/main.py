from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from omniagent.errors import ErrorCode, OmniAgentError

_ALLOWED_COMMANDS = {
    "grep",
    "find",
    "wc",
    "head",
    "tail",
    "sort",
    "uniq",
    "cat",
    "ls",
    "du",
    "df",
}
_DEFAULT_MAX_OUTPUT_CHARS = 4096


def _ok(message: str, output: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "status": "completed", "message": message, "output": output}


def _fail(
    message: str,
    output: dict[str, Any] | None = None,
    error_code: ErrorCode | str = ErrorCode.EXECUTION_ERROR,
) -> dict[str, Any]:
    return {
        "success": False,
        "status": "failed",
        "message": message,
        "output": output or {},
        "error_code": str(error_code),
    }


def _argv(payload: dict[str, Any]) -> list[str]:
    value = payload.get("argv", payload.get("command"))
    if not isinstance(value, list) or not value:
        raise ValueError("shell.safe requires non-empty argv array")
    argv = [str(item) for item in value]
    if any(not item for item in argv):
        raise ValueError("shell.safe argv items must be non-empty strings")
    command = argv[0]
    if "/" in command or "\\" in command:
        raise ValueError("shell.safe command must be a whitelisted command name, not a path")
    if Path(command).name not in _ALLOWED_COMMANDS:
        raise ValueError(f"shell.safe command is not allowed: {command}")
    return argv


def _timeout(payload: dict[str, Any], sandbox) -> float:
    value = payload.get("timeout", sandbox.timeout_seconds)
    timeout = float(value)
    if timeout <= 0:
        raise ValueError("shell.safe timeout must be greater than zero")
    return timeout


def _max_output_chars(payload: dict[str, Any]) -> int:
    value = int(payload.get("max_output_chars", _DEFAULT_MAX_OUTPUT_CHARS))
    if value <= 0:
        raise ValueError("shell.safe max_output_chars must be greater than zero")
    return value


def _cwd(payload: dict[str, Any], sandbox) -> Path:
    raw_cwd = str(payload.get("cwd") or ".")
    cwd = sandbox.resolve_path(raw_cwd)
    if not cwd.exists():
        raise OmniAgentError(ErrorCode.NOT_FOUND, f"cwd does not exist: {raw_cwd}")
    if not cwd.is_dir():
        raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"cwd is not a directory: {raw_cwd}")
    return cwd


def _preview(value: str | bytes | None, max_chars: int) -> tuple[str, bool, int]:
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text[:max_chars], len(text) > max_chars, len(text)


def _output(
    *,
    argv: list[str],
    cwd: Path,
    returncode: int | None,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    max_chars: int,
    timed_out: bool = False,
) -> dict[str, Any]:
    stdout_preview, stdout_truncated, stdout_size = _preview(stdout, max_chars)
    stderr_preview, stderr_truncated, stderr_size = _preview(stderr, max_chars)
    return {
        "argv": list(argv),
        "cwd": str(cwd),
        "returncode": returncode,
        "stdout": stdout_preview,
        "stderr": stderr_preview,
        "stdout_preview": stdout_preview,
        "stderr_preview": stderr_preview,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_size": stdout_size,
        "stderr_size": stderr_size,
        "timed_out": timed_out,
    }


def run(payload, context):
    payload = dict(payload or {})
    sandbox = context["sandbox"]
    if not getattr(sandbox, "allow_external_commands", False):
        return _fail(
            "当前 sandbox 未开启外部命令执行，builtin.shell.safe 拒绝请求",
            error_code=ErrorCode.PERMISSION_DENIED,
        )

    try:
        argv = _argv(payload)
        cwd = _cwd(payload, sandbox)
        timeout = _timeout(payload, sandbox)
        max_chars = _max_output_chars(payload)
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        output = _output(
            argv=argv,
            cwd=cwd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            max_chars=max_chars,
        )
        if completed.returncode == 0:
            return _ok(f"shell.safe completed: {' '.join(argv)}", output)
        return _fail(
            f"shell.safe command failed with returncode {completed.returncode}",
            output,
            ErrorCode.EXECUTION_ERROR,
        )
    except subprocess.TimeoutExpired as exc:
        argv_value = [str(item) for item in exc.cmd] if isinstance(exc.cmd, list) else [str(exc.cmd)]
        try:
            cwd = _cwd(payload, sandbox)
            max_chars = _max_output_chars(payload)
        except (TypeError, ValueError, OmniAgentError):
            cwd = sandbox.runtime_root
            max_chars = _DEFAULT_MAX_OUTPUT_CHARS
        return _fail(
            f"shell.safe command timed out after {exc.timeout} seconds",
            _output(
                argv=argv_value,
                cwd=cwd,
                returncode=None,
                stdout=exc.stdout,
                stderr=exc.stderr,
                max_chars=max_chars,
                timed_out=True,
            ),
            ErrorCode.EXECUTION_ERROR,
        )
    except OmniAgentError as exc:
        return _fail(exc.message, {"details": exc.details}, exc.code)
    except (TypeError, ValueError) as exc:
        return _fail(str(exc), error_code=ErrorCode.VALIDATION_ERROR)

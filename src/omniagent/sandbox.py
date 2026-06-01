from __future__ import annotations

import ast
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import urlopen

from omniagent.errors import ErrorCode, OmniAgentError

SANDBOX_LEVEL_RANK = {
    "workspace": 1,
    "process": 2,
    "container": 3,
}

_DEFAULT_PATH_INTENTS = {
    "download": "~/Downloads",
    "downloads": "~/Downloads",
    "下载": "~/Downloads",
    "下载文件夹": "~/Downloads",
    "desktop": "~/Desktop",
    "桌面": "~/Desktop",
    "document": "~/Documents",
    "documents": "~/Documents",
    "文档": "~/Documents",
    "我的文档": "~/Documents",
    "picture": "~/Pictures",
    "pictures": "~/Pictures",
    "image": "~/Pictures",
    "images": "~/Pictures",
    "图片": "~/Pictures",
}


def _normalize_intent_key(value: str) -> str:
    return str(value).strip().lower().replace("\\", "/").rstrip("/")


_NETWORK_IMPORT_PREFIXES = (
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "requests",
    "socket",
    "smtplib",
    "telnetlib",
    "urllib",
    "websocket",
    "websockets",
)
_SYSTEM_EXEC_IMPORT_PREFIXES = ("asyncio.subprocess", "pty", "shlex", "subprocess")
_DEPENDENCY_INSTALL_IMPORT_PREFIXES = ("ensurepip", "pip", "poetry", "uv", "venv")
_CONTAINER_VENV_DIRNAME = ".omniagent_container_venv"

_URL_PATH_SAFE = "/%:@!$&'()*+,;="
_URL_QUERY_SAFE = "/%?=&:@!$'()*+,;[]"
_URL_FRAGMENT_SAFE = "/%?=&:@!$'()*+,;[]"


def _encode_http_url(url: str) -> str:
    """Percent-encode non-ASCII URL parts before handing them to urlopen."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url

    netloc = parsed.netloc
    if parsed.hostname:
        host = parsed.hostname
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            host = quote(host, safe="")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        userinfo = ""
        if parsed.username is not None:
            userinfo = quote(parsed.username, safe="")
            if parsed.password is not None:
                userinfo = f"{userinfo}:{quote(parsed.password, safe='')}"
            userinfo = f"{userinfo}@"
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{userinfo}{host}{port}"

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            quote(parsed.path, safe=_URL_PATH_SAFE),
            quote(parsed.params, safe=_URL_PATH_SAFE),
            quote(parsed.query, safe=_URL_QUERY_SAFE),
            quote(parsed.fragment, safe=_URL_FRAGMENT_SAFE),
        )
    )


class _PythonRiskDetector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.network = False
        self.external_commands = False
        self.dependency_install = False

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._mark(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module:
            self._mark(module)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        text = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
        normalized = text.lower()
        if any(
            marker in normalized
            for marker in ("subprocess.", "os.system", "os.popen", "asyncio.create_subprocess")
        ):
            self.external_commands = True
        if any(
            marker in normalized for marker in ("pip.", "ensurepip.", "venv.", "poetry.", "uv.")
        ):
            self.dependency_install = True
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            normalized = node.value.lower()
            if "pip install" in normalized or "python -m pip" in normalized:
                self.dependency_install = True
        self.generic_visit(node)

    def _mark(self, module: str) -> None:
        normalized = module.lower()
        if any(
            normalized == item or normalized.startswith(f"{item}.")
            for item in _NETWORK_IMPORT_PREFIXES
        ):
            self.network = True
        if any(
            normalized == item or normalized.startswith(f"{item}.")
            for item in _SYSTEM_EXEC_IMPORT_PREFIXES
        ):
            self.external_commands = True
        if any(
            normalized == item or normalized.startswith(f"{item}.")
            for item in _DEPENDENCY_INSTALL_IMPORT_PREFIXES
        ):
            self.dependency_install = True


class BaseSandbox:
    backend_name = "base"

    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: int = 30,
        allow_network: bool = False,
        level: str = "workspace",
        allow_external_commands: bool | None = None,
        allow_dependency_install: bool | None = None,
        authorized_roots: list[Path] | None = None,
        path_intents: dict[str, str] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.timeout_seconds = timeout_seconds
        self.allow_network = allow_network
        self.level = level if level in SANDBOX_LEVEL_RANK else "workspace"
        self._allow_external_commands_override = allow_external_commands
        self._allow_dependency_install_override = allow_dependency_install
        self.allow_external_commands = (
            SANDBOX_LEVEL_RANK[self.level] >= SANDBOX_LEVEL_RANK["process"]
            if allow_external_commands is None
            else allow_external_commands
        )
        self.allow_dependency_install = (
            SANDBOX_LEVEL_RANK[self.level] >= SANDBOX_LEVEL_RANK["container"]
            if allow_dependency_install is None
            else allow_dependency_install
        )
        self._path_intents = self._build_path_intent_map(path_intents or {})
        self._authorized_roots = [
            Path(item).expanduser().resolve() for item in (authorized_roots or [])
        ]
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime_root = self._init_runtime_root().resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def with_profile(self, *, level: str | None = None) -> "BaseSandbox":
        resolved_level = level or self.level
        return build_sandbox(
            self.root,
            timeout_seconds=self.timeout_seconds,
            allow_network=self.allow_network,
            level=resolved_level,
            allow_external_commands=self._allow_external_commands_override,
            allow_dependency_install=self._allow_dependency_install_override,
            authorized_roots=list(self._authorized_roots),
            path_intents=dict(self._path_intents),
        )

    def resolve_path(self, raw_path: str) -> Path:
        normalized = self._normalize_requested_path(raw_path)
        candidate = Path(normalized)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if self._is_inside(resolved, self.runtime_root):
                runtime_path = resolved
            elif self._is_inside(resolved, self.root):
                runtime_path = (self.runtime_root / resolved.relative_to(self.root)).resolve()
            elif self._is_authorized_external_path(resolved):
                runtime_path = resolved
            else:
                raise self._path_permission_error(raw_path, resolved)
        else:
            runtime_path = (self.runtime_root / candidate).resolve()
        if not self._is_inside(
            runtime_path, self.runtime_root
        ) and not self._is_authorized_external_path(runtime_path):
            raise self._path_permission_error(raw_path, runtime_path)
        return runtime_path

    def resolve_host_path(self, raw_path: str) -> Path:
        normalized = self._normalize_requested_path(raw_path)
        candidate = Path(normalized)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if self._is_inside(resolved, self.root):
                host_path = resolved
            elif self._is_inside(resolved, self.runtime_root):
                host_path = (self.root / resolved.relative_to(self.runtime_root)).resolve()
            elif self._is_authorized_external_path(resolved):
                host_path = resolved
            else:
                raise self._path_permission_error(raw_path, resolved)
        else:
            host_path = (self.root / candidate).resolve()
        if not self._is_inside(host_path, self.root) and not self._is_authorized_external_path(
            host_path
        ):
            raise self._path_permission_error(raw_path, host_path)
        return host_path

    def translate_path(self, raw_path: str | Path) -> Path:
        return self.resolve_path(str(raw_path))

    def list_dir(self, raw_path: str = ".") -> list[str]:
        target = self.resolve_path(raw_path)
        if not target.exists():
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"目录不存在: {target}")
        if not target.is_dir():
            raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"路径不是目录: {target}")
        return sorted(item.name for item in target.iterdir())

    def read_text(self, raw_path: str) -> str:
        target = self.resolve_path(raw_path)
        if not target.exists():
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"文件不存在: {target}")
        if target.is_dir():
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR, f"读取失败，路径是目录，请使用 list action: {target}"
            )
        return target.read_text(encoding="utf-8")

    def write_text(self, raw_path: str, content: str, *, overwrite: bool = False) -> Path:
        target = self.resolve_path(raw_path)
        if target.exists() and target.is_dir():
            raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"写入失败，目标是目录: {target}")
        if target.exists() and not overwrite:
            raise OmniAgentError(
                ErrorCode.PERMISSION_DENIED,
                f"目标文件已存在，未允许覆盖: {target}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._sync_file_to_host(target)
        return self.resolve_host_path(raw_path)

    def _build_path_intent_map(self, custom_mapping: dict[str, str]) -> dict[str, str]:
        intents = {
            _normalize_intent_key(key): value for key, value in _DEFAULT_PATH_INTENTS.items()
        }
        for key, value in custom_mapping.items():
            normalized_key = _normalize_intent_key(str(key))
            normalized_value = str(value).strip()
            if not normalized_key or not normalized_value:
                continue
            intents[normalized_key] = normalized_value
        return intents

    def _normalize_requested_path(self, raw_path: str) -> str:
        candidate = str(raw_path or "").strip()
        if not candidate:
            return candidate

        expanded = os.path.expanduser(candidate)
        if expanded != candidate:
            return expanded

        normalized = candidate.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized in {".", ".."} or normalized.startswith("../") or normalized.startswith("/"):
            return candidate

        direct_key = _normalize_intent_key(normalized)
        direct_target = self._path_intents.get(direct_key)
        if direct_target:
            return str(Path(os.path.expanduser(direct_target)))

        first_segment, separator, remainder = normalized.partition("/")
        alias_target = self._path_intents.get(_normalize_intent_key(first_segment))
        if alias_target:
            mapped = Path(os.path.expanduser(alias_target))
            return str((mapped / remainder) if separator else mapped)
        return candidate

    def _is_inside(self, path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def _is_authorized_external_path(self, path: Path) -> bool:
        return any(self._is_inside(path, root) for root in self._authorized_roots)

    def _path_permission_error(self, requested: str, resolved: Path) -> OmniAgentError:
        suggestion_root = resolved if resolved.is_dir() else resolved.parent
        details = {
            "requested_path": requested,
            "resolved_path": str(resolved),
            "workspace_root": str(self.root),
            "runtime_root": str(self.runtime_root),
            "authorized_roots": [str(item) for item in self._authorized_roots],
            "hint_env": f"OMNIAGENT_SANDBOX_AUTHORIZED_PATHS={suggestion_root}",
        }
        message = (
            f"路径未授权访问: {resolved}。"
            " 当前仅允许 workspace 内路径；如需访问外部目录，请将目标目录加入 OMNIAGENT_SANDBOX_AUTHORIZED_PATHS。"
        )
        return OmniAgentError(ErrorCode.PERMISSION_DENIED, message, details=details)

    def run_python(self, code: str) -> dict[str, object]:
        self._validate_python_code(code)
        wrapper = """
import contextlib
import io
import json
import sys
import traceback

ns = {}
stdout = io.StringIO()
payload = {"ok": True, "result": None, "stdout": ""}
try:
    source = sys.stdin.read()
    with contextlib.redirect_stdout(stdout):
        exec(source, ns, ns)
    payload["result"] = ns.get("result")
    payload["stdout"] = stdout.getvalue()
except Exception as exc:
    payload = {
        "ok": False,
        "error": str(exc),
        "traceback": traceback.format_exc(),
        "stdout": stdout.getvalue(),
    }
print(json.dumps(payload, ensure_ascii=False, default=str))
"""
        completed = subprocess.run(
            [str(self.python_executable), "-I", "-c", wrapper],
            input=code,
            text=True,
            capture_output=True,
            cwd=self.runtime_root,
            timeout=self.timeout_seconds,
            check=False,
            env=self._python_env(),
        )
        self._sync_tree_to_host()
        stdout = completed.stdout.strip()
        if not stdout:
            raise OmniAgentError(ErrorCode.EXECUTION_ERROR, "Python 执行器未返回输出")
        return json.loads(stdout)

    def fetch_url(self, url: str) -> dict[str, object]:
        parsed = urlparse(url)
        if parsed.scheme in {"", "file"}:
            path = parsed.path if parsed.scheme == "file" else url
            body = self.read_text(path)
            return {
                "url": url,
                "status": 200,
                "body": body,
                "source": "file",
            }

        if parsed.scheme in {"http", "https"}:
            if not self.allow_network:
                raise OmniAgentError(ErrorCode.PERMISSION_DENIED, "当前 sandbox 未开启网络访问")
            request_url = _encode_http_url(url)
            with contextlib.closing(urlopen(request_url, timeout=self.timeout_seconds)) as response:
                body = response.read().decode("utf-8", errors="replace")
                return {
                    "url": url,
                    "request_url": request_url,
                    "status": getattr(response, "status", 200),
                    "body": body,
                    "source": parsed.scheme,
                }

        raise OmniAgentError(ErrorCode.VALIDATION_ERROR, f"不支持的 URL 协议: {parsed.scheme}")

    def describe(self) -> dict[str, object]:
        return {
            "backend_name": self.backend_name,
            "level": self.level,
            "workspace_root": str(self.root),
            "runtime_root": str(self.runtime_root),
            "python_executable": str(self.python_executable),
            "allow_network": self.allow_network,
            "allow_external_commands": self.allow_external_commands,
            "allow_dependency_install": self.allow_dependency_install,
            "authorized_roots": [str(item) for item in self._authorized_roots],
            "path_intents": dict(self._path_intents),
        }

    def close(self) -> None:
        self._sync_tree_to_host()

    @property
    def python_executable(self) -> Path:
        return Path(sys.executable)

    def _init_runtime_root(self) -> Path:
        return self.root

    def _python_env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PYTHONIOENCODING": "utf-8",
        }
        return env

    def _sync_file_to_host(self, runtime_path: Path) -> None:
        host_path = self.resolve_host_path(str(runtime_path))
        if runtime_path == host_path:
            return
        host_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime_path, host_path)

    def _sync_tree_to_host(self) -> None:
        for path in self.runtime_root.rglob("*"):
            if not path.is_file():
                continue
            if self._should_exclude_runtime_path(path):
                continue
            host_path = self.resolve_host_path(str(path))
            if path == host_path:
                continue
            host_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, host_path)

    def _should_exclude_runtime_path(self, path: Path) -> bool:
        return False

    def _validate_python_code(self, code: str) -> None:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return
        detector = _PythonRiskDetector()
        detector.visit(tree)
        if detector.network and not self.allow_network:
            raise OmniAgentError(
                ErrorCode.PERMISSION_DENIED,
                "当前 sandbox profile 未开启 Python 网络访问",
                details={"sandbox_level": self.level, "backend": self.backend_name},
            )
        if detector.external_commands and not self.allow_external_commands:
            raise OmniAgentError(
                ErrorCode.PERMISSION_DENIED,
                "当前 sandbox profile 不允许外部命令执行",
                details={"sandbox_level": self.level, "backend": self.backend_name},
            )
        if detector.dependency_install and not self.allow_dependency_install:
            raise OmniAgentError(
                ErrorCode.PERMISSION_DENIED,
                "当前 sandbox profile 不允许依赖安装",
                details={"sandbox_level": self.level, "backend": self.backend_name},
            )


class WorkspaceSandbox(BaseSandbox):
    backend_name = "workspace"

    def close(self) -> None:
        return None


class ProcessSandbox(BaseSandbox):
    backend_name = "process"

    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: int = 30,
        allow_network: bool = False,
        level: str = "process",
        allow_external_commands: bool | None = None,
        allow_dependency_install: bool | None = None,
        authorized_roots: list[Path] | None = None,
        path_intents: dict[str, str] | None = None,
    ) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="omniagent_process_")
        super().__init__(
            root,
            timeout_seconds=timeout_seconds,
            allow_network=allow_network,
            level=level,
            allow_external_commands=allow_external_commands,
            allow_dependency_install=allow_dependency_install,
            authorized_roots=authorized_roots,
            path_intents=path_intents,
        )
        self._mirror_workspace()

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._tempdir.cleanup()

    def _init_runtime_root(self) -> Path:
        return Path(self._tempdir.name) / "workspace"

    def _mirror_workspace(self) -> None:
        if self.runtime_root.exists():
            shutil.rmtree(self.runtime_root)
        shutil.copytree(self.root, self.runtime_root, dirs_exist_ok=True)


class ContainerSandbox(ProcessSandbox):
    backend_name = "container"

    def __init__(
        self,
        root: Path,
        *,
        timeout_seconds: int = 30,
        allow_network: bool = False,
        level: str = "container",
        allow_external_commands: bool | None = None,
        allow_dependency_install: bool | None = None,
        authorized_roots: list[Path] | None = None,
        path_intents: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            root,
            timeout_seconds=timeout_seconds,
            allow_network=allow_network,
            level=level,
            allow_external_commands=allow_external_commands,
            allow_dependency_install=allow_dependency_install,
            authorized_roots=authorized_roots,
            path_intents=path_intents,
        )
        self._venv_dir = self.runtime_root / _CONTAINER_VENV_DIRNAME
        builder = venv.EnvBuilder(with_pip=True, clear=True)
        builder.create(self._venv_dir)

    @property
    def python_executable(self) -> Path:
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        executable = "python.exe" if os.name == "nt" else "python"
        return self._venv_dir / bin_dir / executable

    def _python_env(self) -> dict[str, str]:
        env = super()._python_env()
        bin_dir = self.python_executable.parent
        env.update(
            {
                "VIRTUAL_ENV": str(self._venv_dir),
                "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            }
        )
        return env

    def _should_exclude_runtime_path(self, path: Path) -> bool:
        return self._venv_dir in path.parents or path == self._venv_dir


def build_sandbox(
    root: Path,
    *,
    timeout_seconds: int = 30,
    allow_network: bool = False,
    level: str = "workspace",
    allow_external_commands: bool | None = None,
    allow_dependency_install: bool | None = None,
    authorized_roots: list[Path] | None = None,
    path_intents: dict[str, str] | None = None,
) -> BaseSandbox:
    if level == "container":
        return ContainerSandbox(
            root,
            timeout_seconds=timeout_seconds,
            allow_network=allow_network,
            level=level,
            allow_external_commands=allow_external_commands,
            allow_dependency_install=allow_dependency_install,
            authorized_roots=authorized_roots,
            path_intents=path_intents,
        )
    if level == "process":
        return ProcessSandbox(
            root,
            timeout_seconds=timeout_seconds,
            allow_network=allow_network,
            level=level,
            allow_external_commands=allow_external_commands,
            allow_dependency_install=allow_dependency_install,
            authorized_roots=authorized_roots,
            path_intents=path_intents,
        )
    return WorkspaceSandbox(
        root,
        timeout_seconds=timeout_seconds,
        allow_network=allow_network,
        level="workspace",
        allow_external_commands=allow_external_commands,
        allow_dependency_install=allow_dependency_install,
        authorized_roots=authorized_roots,
        path_intents=path_intents,
    )

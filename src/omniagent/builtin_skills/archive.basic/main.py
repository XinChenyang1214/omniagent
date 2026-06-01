from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Any

from omniagent.errors import OmniAgentError


def _ok(
    message: str, output: dict[str, Any], artifacts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    result = {"success": True, "status": "completed", "message": message, "output": output}
    if artifacts:
        result["artifacts"] = artifacts
    return result


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


def _archive_path(payload: dict[str, Any], sandbox) -> tuple[Path, str]:
    raw = str(payload.get("path") or "").strip()
    if not raw:
        raise ValueError("archive action requires path")
    target = sandbox.resolve_path(raw)
    if not target.exists():
        raise ValueError(f"archive does not exist: {raw}")
    if target.is_dir():
        raise ValueError(f"archive path is a directory: {raw}")
    return target, raw


def _detect_format(path: Path, requested: str | None = None) -> str:
    fmt = (requested or "").strip().lower()
    if fmt in {"zip", "tar", "gztar", "tgz"}:
        return "gztar" if fmt == "tgz" else fmt
    suffixes = "".join(path.suffixes).lower()
    if suffixes.endswith(".zip"):
        return "zip"
    if suffixes.endswith(".tar.gz") or suffixes.endswith(".tgz"):
        return "gztar"
    if suffixes.endswith(".tar"):
        return "tar"
    raise ValueError("archive format must be zip, tar, or gztar")


def _entry_dict(name: str, *, size: int | None = None, is_dir: bool = False) -> dict[str, Any]:
    return {"name": name, "size": size, "is_dir": is_dir}


def _list(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    target, raw = _archive_path(payload, sandbox)
    fmt = _detect_format(target, payload.get("format"))
    entries: list[dict[str, Any]] = []
    if fmt == "zip":
        with zipfile.ZipFile(target) as archive:
            for item in archive.infolist():
                entries.append(
                    _entry_dict(item.filename, size=item.file_size, is_dir=item.is_dir())
                )
    else:
        mode = "r:gz" if fmt == "gztar" else "r:"
        with tarfile.open(target, mode) as archive:
            for item in archive.getmembers():
                entries.append(_entry_dict(item.name, size=item.size, is_dir=item.isdir()))
    return _ok("Archive listed", {"entries": entries, "path": raw, "format": fmt})


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_output(root: Path, name: str) -> Path:
    if name.startswith("/") or "\x00" in name:
        raise ValueError(f"unsafe archive entry path: {name}")
    target = (root / name).resolve()
    if not _is_inside(target, root):
        raise ValueError(f"unsafe archive entry path: {name}")
    return target


def _extract(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    target, raw = _archive_path(payload, sandbox)
    output_dir_raw = str(payload.get("output_dir") or "").strip()
    if not output_dir_raw:
        return _fail("archive.extract requires output_dir", error_code="validation_error")
    output_dir = sandbox.resolve_path(output_dir_raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    overwrite = bool(payload.get("overwrite", False))
    fmt = _detect_format(target, payload.get("format"))
    entries: list[dict[str, Any]] = []
    if fmt == "zip":
        with zipfile.ZipFile(target) as archive:
            for item in archive.infolist():
                destination = _safe_output(output_dir, item.filename)
                entries.append(
                    _entry_dict(item.filename, size=item.file_size, is_dir=item.is_dir())
                )
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if destination.exists() and not overwrite:
                    raise ValueError(f"target already exists: {item.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
    else:
        mode = "r:gz" if fmt == "gztar" else "r:"
        with tarfile.open(target, mode) as archive:
            for item in archive.getmembers():
                destination = _safe_output(output_dir, item.name)
                entries.append(_entry_dict(item.name, size=item.size, is_dir=item.isdir()))
                if item.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not item.isfile():
                    continue
                if destination.exists() and not overwrite:
                    raise ValueError(f"target already exists: {item.name}")
                source = archive.extractfile(item)
                if source is None:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read())
    return _ok(
        "Archive extracted",
        {"entries": entries, "path": raw, "output_dir": output_dir_raw, "format": fmt},
    )


def _walk_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.is_file())
    raise ValueError(f"path does not exist: {path}")


def _create(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    raw_paths = payload.get("paths")
    output_path = str(payload.get("output_path") or "").strip()
    if not isinstance(raw_paths, list) or not raw_paths:
        return _fail("archive.create requires non-empty paths array", error_code="validation_error")
    if not output_path:
        return _fail("archive.create requires output_path", error_code="validation_error")
    output = sandbox.resolve_path(output_path)
    if output.exists() and not bool(payload.get("overwrite", False)):
        raise ValueError(f"archive output already exists: {output_path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fmt = _detect_format(output, payload.get("format"))
    entries: list[dict[str, Any]] = []
    resolved_roots = [(str(item), sandbox.resolve_path(str(item))) for item in raw_paths]
    if fmt == "zip":
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for raw, root in resolved_roots:
                for file_path in _walk_files(root):
                    arcname = file_path.relative_to(sandbox.resolve_path(raw).parent)
                    archive.write(file_path, arcname.as_posix())
                    entries.append(_entry_dict(arcname.as_posix(), size=file_path.stat().st_size))
    else:
        mode = "w:gz" if fmt == "gztar" else "w:"
        with tarfile.open(output, mode) as archive:
            for raw, root in resolved_roots:
                for file_path in _walk_files(root):
                    arcname = file_path.relative_to(sandbox.resolve_path(raw).parent)
                    archive.add(file_path, arcname=arcname.as_posix(), recursive=False)
                    entries.append(_entry_dict(arcname.as_posix(), size=file_path.stat().st_size))
    return _ok(
        "Archive created",
        {"entries": entries, "output_path": output_path, "format": fmt},
        artifacts=[{"kind": "file", "path": str(output), "description": "Archive output"}],
    )


def run(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    sandbox = context["sandbox"]
    try:
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            return _list(payload, sandbox)
        if action == "extract":
            return _extract(payload, sandbox)
        if action == "create":
            return _create(payload, sandbox)
        return _fail(f"unsupported archive action: {action}", error_code="validation_error")
    except OmniAgentError as exc:
        return _fail(exc.message, error_code=str(exc.code))
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
        return _fail(str(exc), error_code="validation_error")

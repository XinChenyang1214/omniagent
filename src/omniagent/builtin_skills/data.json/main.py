from __future__ import annotations

import copy
import json
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


def _read_json_text(payload: dict[str, Any], sandbox) -> tuple[str, str | None]:
    if payload.get("text") is not None:
        if not isinstance(payload["text"], str):
            raise ValueError("json text must be a string")
        return payload["text"], None
    path = payload.get("path")
    if path is None or str(path).strip() == "":
        raise ValueError("builtin.data.json requires text or path")
    return sandbox.read_text(str(path)), str(path)


def _parse(payload: dict[str, Any], sandbox) -> tuple[Any, str | None]:
    text, path = _read_json_text(payload, sandbox)
    return json.loads(text), path


def _split_path(path: str) -> list[str]:
    if path == "":
        return []
    return [part for part in str(path).split(".") if part != ""]


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in _split_path(path):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise KeyError(part) from exc
            try:
                current = current[index]
            except IndexError as exc:
                raise KeyError(part) from exc
            continue
        raise KeyError(part)
    return current


def _set_path(value: Any, path: str, new_value: Any) -> None:
    parts = _split_path(path)
    if not parts:
        raise ValueError("set path must not be empty")
    current = value
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current or not isinstance(current[part], (dict, list)):
                current[part] = {}
            current = current[part]
            continue
        if isinstance(current, list):
            current = current[int(part)]
            continue
        raise KeyError(part)
    last = parts[-1]
    if isinstance(current, dict):
        current[last] = new_value
        return
    if isinstance(current, list):
        current[int(last)] = new_value
        return
    raise KeyError(last)


def _delete_path(value: Any, path: str) -> None:
    parts = _split_path(path)
    if not parts:
        raise ValueError("delete path must not be empty")
    parent = _get_path(value, ".".join(parts[:-1])) if len(parts) > 1 else value
    last = parts[-1]
    if isinstance(parent, dict):
        parent.pop(last, None)
        return
    if isinstance(parent, list):
        del parent[int(last)]
        return
    raise KeyError(last)


def _validate(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    path: str | None = None
    try:
        _, path = _parse(payload, sandbox)
    except json.JSONDecodeError as exc:
        return _ok(
            "JSON validation completed: invalid",
            {
                "valid": False,
                "value": None,
                "formatted": None,
                "errors": [str(exc)],
                "path": path or payload.get("path"),
            },
        )
    return _ok(
        "JSON validation completed: valid",
        {"valid": True, "value": None, "formatted": None, "errors": [], "path": path},
    )


def _query(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    data, path = _parse(payload, sandbox)
    query = payload.get("query")
    if query is None:
        return _fail("json.query requires query", {"errors": ["missing query"], "path": path})
    try:
        value = _get_path(data, str(query))
    except (KeyError, ValueError) as exc:
        return _fail(
            f"json.query path not found: {query}",
            {"valid": True, "value": None, "errors": [str(exc)], "path": path},
        )
    return _ok(
        "JSON query completed",
        {"valid": True, "value": value, "formatted": None, "errors": [], "path": path},
    )


def _format(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    data, path = _parse(payload, sandbox)
    options = payload.get("format_options") or {}
    if not isinstance(options, dict):
        return _fail("json.format format_options must be an object")
    compact = bool(options.get("compact", False))
    indent = None if compact else int(options.get("indent", 2))
    sort_keys = bool(options.get("sort_keys", False))
    ensure_ascii = bool(options.get("ensure_ascii", False))
    separators = (",", ":") if compact else None
    formatted = json.dumps(
        data,
        ensure_ascii=ensure_ascii,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
    )
    return _ok(
        "JSON format completed",
        {"valid": True, "value": data, "formatted": formatted, "errors": [], "path": path},
    )


def _transform(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    data, path = _parse(payload, sandbox)
    transform = payload.get("transform")
    if not isinstance(transform, dict):
        return _fail("json.transform requires transform object", {"path": path})

    if "pick" in transform:
        picked: dict[str, Any] = {}
        pick_paths = transform.get("pick")
        if not isinstance(pick_paths, list):
            return _fail("json.transform pick must be an array", {"path": path})
        for item in pick_paths:
            item_path = str(item)
            _set_path(picked, item_path, copy.deepcopy(_get_path(data, item_path)))
        result: Any = picked
    else:
        result = copy.deepcopy(data)

    rename = transform.get("rename", {})
    if rename:
        if not isinstance(rename, dict):
            return _fail("json.transform rename must be an object", {"path": path})
        for source, target in rename.items():
            value = copy.deepcopy(_get_path(result, str(source)))
            _delete_path(result, str(source))
            _set_path(result, str(target), value)

    set_values = transform.get("set", {})
    if set_values:
        if not isinstance(set_values, dict):
            return _fail("json.transform set must be an object", {"path": path})
        for target, value in set_values.items():
            _set_path(result, str(target), value)

    delete_paths = transform.get("delete", [])
    if delete_paths:
        if not isinstance(delete_paths, list):
            return _fail("json.transform delete must be an array", {"path": path})
        for item in delete_paths:
            _delete_path(result, str(item))

    return _ok(
        "JSON transform completed",
        {"valid": True, "value": result, "formatted": None, "errors": [], "path": path},
    )


def _format_report_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize_report_fields(fields: Any) -> list[dict[str, str]]:
    if isinstance(fields, dict):
        return [{"label": str(label), "query": str(query)} for label, query in fields.items()]
    if not isinstance(fields, list) or not fields:
        raise ValueError("json.report requires non-empty fields")

    normalized: list[dict[str, str]] = []
    for item in fields:
        if isinstance(item, str):
            normalized.append({"label": item, "query": item})
            continue
        if not isinstance(item, dict):
            raise ValueError("json.report fields must be strings or objects")
        query = str(item.get("query") or item.get("path") or "").strip()
        if not query:
            raise ValueError("json.report field requires query")
        label = str(item.get("label") or item.get("name") or query).strip()
        normalized.append({"label": label, "query": query})
    return normalized


def _report(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    data, path = _parse(payload, sandbox)
    output_path = str(payload.get("output_path") or payload.get("path_out") or "").strip()
    if not output_path:
        return _fail("json.report requires output_path", {"path": path})

    values: dict[str, Any] = {}
    lines: list[str] = []
    for field in _normalize_report_fields(payload.get("fields")):
        value = _get_path(data, field["query"])
        values[field["label"]] = value
        lines.append(f"{field['label']}: {_format_report_value(value)}")

    content = "\n".join(lines)
    if payload.get("trailing_newline", True):
        content += "\n"
    written = sandbox.write_text(
        output_path,
        content,
        overwrite=bool(payload.get("overwrite", True)),
    )
    return _ok(
        "JSON report completed",
        {"valid": True, "value": values, "formatted": content, "errors": [], "path": path},
        artifacts=[{"kind": "file", "path": str(written), "description": "JSON report output"}],
    )


def run(payload, context):
    payload = dict(payload or {})
    sandbox = context["sandbox"]
    action = str(payload.get("action", "validate")).strip().lower()
    try:
        if action == "validate":
            return _validate(payload, sandbox)
        if action == "query":
            return _query(payload, sandbox)
        if action == "format":
            return _format(payload, sandbox)
        if action == "transform":
            return _transform(payload, sandbox)
        if action == "report":
            return _report(payload, sandbox)
        return _fail(f"Unsupported JSON action: {action}")
    except OmniAgentError as exc:
        return _fail(exc.message, {"errors": [exc.message]}, str(exc.code))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        return _fail(str(exc), {"errors": [str(exc)]})

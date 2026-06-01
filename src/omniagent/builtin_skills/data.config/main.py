from __future__ import annotations

import json
import tomllib
import xml.etree.ElementTree as ET
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


def _read_config(payload: dict[str, Any], sandbox) -> tuple[str, str | None, str]:
    if payload.get("text") is not None:
        if not isinstance(payload["text"], str):
            raise ValueError("config text must be a string")
        fmt = str(payload.get("format") or "").strip().lower()
        if not fmt:
            raise ValueError("config text input requires format")
        return payload["text"], None, fmt
    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("config action requires text or path")
    fmt = str(payload.get("format") or "").strip().lower()
    if not fmt:
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        fmt = {"yml": "yaml"}.get(suffix, suffix)
    if fmt not in {"toml", "yaml", "xml", "json"}:
        raise ValueError("config format must be toml, yaml, xml, or json")
    return sandbox.read_text(path), path, fmt


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if ":" not in stripped:
            raise ValueError("simple YAML parser only supports mapping lines")
        key, value = stripped.strip().split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _xml_to_data(text: str) -> ET.Element:
    return ET.fromstring(text)


def _parse(payload: dict[str, Any], sandbox) -> tuple[Any, str | None, str]:
    text, path, fmt = _read_config(payload, sandbox)
    if fmt == "toml":
        return tomllib.loads(text), path, fmt
    if fmt == "yaml":
        return _parse_simple_yaml(text), path, fmt
    if fmt == "json":
        return json.loads(text), path, fmt
    if fmt == "xml":
        return _xml_to_data(text), path, fmt
    raise ValueError(f"unsupported config format: {fmt}")


def _split_path(path: str) -> list[str]:
    return [part for part in str(path).split(".") if part != ""]


def _get_mapping_path(value: Any, path: str) -> Any:
    current = value
    for part in _split_path(path):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
            continue
        if isinstance(current, list):
            current = current[int(part)]
            continue
        raise KeyError(part)
    return current


def _get_xml_path(root: ET.Element, path: str) -> Any:
    parts = _split_path(path)
    current = root
    if parts and parts[0] == root.tag:
        parts = parts[1:]
    for part in parts:
        if part == "text":
            return (current.text or "").strip()
        if part.startswith("@"):
            return current.attrib.get(part[1:])
        matches = current.findall(part)
        if not matches:
            raise KeyError(part)
        current = matches[0]
    return {
        "tag": current.tag,
        "text": (current.text or "").strip(),
        "attributes": dict(current.attrib),
    }


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _query_value(data: Any, fmt: str, query: str) -> Any:
    if fmt == "xml":
        return _get_xml_path(data, query)
    try:
        return _get_mapping_path(data, query)
    except (KeyError, IndexError, ValueError) as exc:
        for alias in _mapping_query_aliases(data, query):
            try:
                return _get_mapping_path(data, alias)
            except (KeyError, IndexError, ValueError):
                continue
        raise exc


def _mapping_query_aliases(data: Any, query: str) -> list[str]:
    if not isinstance(data, dict):
        return []
    aliases: list[str] = []
    query = str(query or "").strip()
    if not query:
        return aliases

    if "." not in query and "_" in query:
        parts = [part for part in query.split("_") if part]
        if parts and parts[0] in data:
            aliases.append(".".join(parts))

    if "." not in query and len(data) == 1:
        root_key = next(iter(data))
        root_value = data.get(root_key)
        if isinstance(root_value, dict):
            aliases.append(f"{root_key}.{query}")
            prefix = f"{root_key}_"
            if query.startswith(prefix):
                suffix = query[len(prefix) :].replace("_", ".")
                if suffix:
                    aliases.append(f"{root_key}.{suffix}")

    if "." not in query:
        leaf_paths = _find_unique_leaf_paths(data, query)
        aliases.extend(leaf_paths)

    deduped: list[str] = []
    for alias in aliases:
        if alias != query and alias not in deduped:
            deduped.append(alias)
    return deduped


def _find_unique_leaf_paths(data: Any, leaf_key: str) -> list[str]:
    matches: list[str] = []

    def visit(value: Any, parts: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                next_parts = [*parts, str(key)]
                if str(key) == leaf_key:
                    matches.append(".".join(next_parts))
                visit(child, next_parts)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*parts, str(index)])

    visit(data, [])
    deduped = list(dict.fromkeys(matches))
    return deduped if len(deduped) == 1 else []


def _query(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    if not query:
        return _fail("config.query requires query", error_code="validation_error")
    data, path, fmt = _parse(payload, sandbox)
    value = _query_value(data, fmt, query)
    return _ok("Config query completed", {"format": fmt, "value": value, "path": path})


def _normalize_fields(fields: Any) -> list[dict[str, str]]:
    if not isinstance(fields, list) or not fields:
        raise ValueError("config.report requires non-empty fields array")
    result: list[dict[str, str]] = []
    for item in fields:
        if isinstance(item, str):
            result.append({"label": item, "query": item})
            continue
        if not isinstance(item, dict):
            raise ValueError("config.report fields must be strings or objects")
        query = str(item.get("query") or "").strip()
        if not query:
            raise ValueError("config.report field requires query")
        result.append({"label": str(item.get("label") or query), "query": query})
    return result


def _report(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output_path = str(payload.get("output_path") or "").strip()
    if not output_path:
        return _fail("config.report requires output_path", error_code="validation_error")
    data, path, fmt = _parse(payload, sandbox)
    fields = _normalize_fields(payload.get("fields"))
    values: dict[str, Any] = {}
    lines: list[str] = []
    for field in fields:
        value = _query_value(data, fmt, field["query"])
        values[field["label"]] = value
        lines.append(f"{field['label']}: {_format_value(value)}")
    written = sandbox.write_text(
        output_path,
        "\n".join(lines) + "\n",
        overwrite=bool(payload.get("overwrite", True)),
    )
    return _ok(
        "Config report completed",
        {"format": fmt, "result": values, "path": path, "output_path": output_path},
        artifacts=[{"kind": "file", "path": str(written), "description": "Config report"}],
    )


def run(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    sandbox = context["sandbox"]
    try:
        action = str(payload.get("action") or "query").strip().lower()
        if action == "query":
            return _query(payload, sandbox)
        if action == "report":
            return _report(payload, sandbox)
        return _fail(f"unsupported config action: {action}", error_code="validation_error")
    except OmniAgentError as exc:
        return _fail(exc.message, error_code=str(exc.code))
    except (ET.ParseError, KeyError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        return _fail(str(exc), error_code="validation_error")

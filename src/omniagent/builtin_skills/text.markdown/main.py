from __future__ import annotations

import re
from typing import Any

from omniagent.errors import OmniAgentError

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


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


def _read_text(payload: dict[str, Any], sandbox) -> tuple[str, str | None]:
    if payload.get("text") is not None:
        if not isinstance(payload["text"], str):
            raise ValueError("markdown text must be a string")
        return payload["text"], None
    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("markdown action requires text or path")
    return sandbox.read_text(path), path


def _normalize_heading(value: str) -> str:
    match = _HEADING_RE.match(value.strip())
    if match:
        return match.group(2).strip().lower()
    return value.strip().strip("#").strip().lower()


def _heading_line(heading: str) -> str:
    heading = heading.strip()
    if heading.startswith("#"):
        return heading
    return f"## {heading}"


def _heading_positions(lines: list[str]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.rstrip("\n"))
        if not match:
            continue
        positions.append(
            {
                "level": len(match.group(1)),
                "title": match.group(2).strip(),
                "line": index,
            }
        )
    return positions


def _find_section(lines: list[str], heading: str) -> tuple[int, int, dict[str, Any]]:
    target = _normalize_heading(heading)
    positions = _heading_positions(lines)
    for idx, item in enumerate(positions):
        if _normalize_heading(str(item["title"])) != target:
            continue
        start = int(item["line"])
        level = int(item["level"])
        end = len(lines)
        for later in positions[idx + 1 :]:
            if int(later["level"]) <= level:
                end = int(later["line"])
                break
        return start, end, item
    raise ValueError(f"markdown heading not found: {heading}")


def _headings(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    text, path = _read_text(payload, sandbox)
    headings = _heading_positions(text.splitlines())
    return _ok("Markdown headings extracted", {"headings": headings, "path": path})


def _extract_section(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    text, path = _read_text(payload, sandbox)
    heading = str(payload.get("heading") or "").strip()
    if not heading:
        return _fail("markdown.extract_section requires heading", error_code="validation_error")
    lines = text.splitlines(keepends=True)
    start, end, item = _find_section(lines, heading)
    section = "".join(lines[start + 1 : end]).strip("\n")
    return _ok(
        "Markdown section extracted",
        {"section": section, "heading": item, "path": path},
    )


def _replace_section(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    path = str(payload.get("path") or "").strip()
    heading = str(payload.get("heading") or "").strip()
    content = payload.get("content")
    if not path or not heading or not isinstance(content, str):
        return _fail(
            "markdown.replace_section requires path, heading, and string content",
            error_code="validation_error",
        )
    text = sandbox.read_text(path)
    lines = text.splitlines(keepends=True)
    start, end, _ = _find_section(lines, heading)
    replacement = [lines[start]]
    if content:
        replacement.extend([line + "\n" for line in content.rstrip("\n").split("\n")])
    updated = "".join(lines[: start + 1] + replacement[1:] + lines[end:])
    output_path = str(payload.get("output_path") or path)
    written = sandbox.write_text(
        output_path,
        updated,
        overwrite=True if output_path == path else bool(payload.get("overwrite", False)),
    )
    return _ok(
        "Markdown section replaced",
        {"path": path, "output_path": output_path},
        artifacts=[{"kind": "file", "path": str(written), "description": "Markdown output"}],
    )


def _append_section(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    path = str(payload.get("path") or "").strip()
    heading = str(payload.get("heading") or "").strip()
    content = payload.get("content")
    if not path or not heading or not isinstance(content, str):
        return _fail(
            "markdown.append_section requires path, heading, and string content",
            error_code="validation_error",
        )
    original = sandbox.read_text(path)
    separator = "\n\n" if original and not original.endswith("\n\n") else ""
    updated = (
        original
        + separator
        + _heading_line(heading)
        + "\n"
        + content.rstrip("\n")
        + "\n"
    )
    written = sandbox.write_text(path, updated, overwrite=True)
    return _ok(
        "Markdown section appended",
        {"path": path},
        artifacts=[{"kind": "file", "path": str(written), "description": "Markdown output"}],
    )


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _parse_table_block(lines: list[str]) -> dict[str, Any]:
    headers = [cell.strip() for cell in lines[0].strip().strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[2:]:
        values = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(values) != len(headers):
            continue
        rows.append({header: value for header, value in zip(headers, values, strict=False)})
    return {"columns": headers, "rows": rows}


def _tables_from_text(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    tables: list[dict[str, Any]] = []
    index = 0
    while index + 1 < len(lines):
        if "|" not in lines[index] or not _is_table_separator(lines[index + 1]):
            index += 1
            continue
        block = [lines[index], lines[index + 1]]
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            block.append(lines[index])
            index += 1
        tables.append(_parse_table_block(block))
    return tables


def _table(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    text, path = _read_text(payload, sandbox)
    tables = _tables_from_text(text)
    table_index = payload.get("table_index", 0)
    if not isinstance(table_index, int) or isinstance(table_index, bool):
        return _fail("markdown.table table_index must be an integer", error_code="validation_error")
    rows = tables[table_index]["rows"] if 0 <= table_index < len(tables) else []
    return _ok("Markdown tables extracted", {"tables": tables, "rows": rows, "path": path})


def run(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    sandbox = context["sandbox"]
    try:
        action = str(payload.get("action") or "headings").strip().lower()
        if action == "headings":
            return _headings(payload, sandbox)
        if action == "extract_section":
            return _extract_section(payload, sandbox)
        if action == "replace_section":
            return _replace_section(payload, sandbox)
        if action == "append_section":
            return _append_section(payload, sandbox)
        if action == "table":
            return _table(payload, sandbox)
        return _fail(f"unsupported markdown action: {action}", error_code="validation_error")
    except OmniAgentError as exc:
        return _fail(exc.message, error_code=str(exc.code))
    except ValueError as exc:
        return _fail(str(exc), error_code="validation_error")

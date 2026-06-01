from __future__ import annotations

import csv
import io
from typing import Any

from omniagent.errors import OmniAgentError


def _ok(message: str, output: dict[str, Any], artifacts: list[dict[str, Any]] | None = None):
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


def _delimiter(payload: dict[str, Any]) -> str:
    delimiter = str(payload.get("delimiter") or ",")
    if len(delimiter) != 1:
        raise ValueError("csv delimiter must be one character")
    return delimiter


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    return columns


def _normalize_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("csv rows must be an array")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each csv row must be an object")
        rows.append(dict(item))
    return rows


def _load_rows(payload: dict[str, Any], sandbox) -> tuple[list[dict[str, Any]], list[str], str | None]:
    if payload.get("rows") is not None:
        rows = _normalize_rows(payload["rows"])
        columns = [str(item) for item in payload.get("columns", [])] or _columns_from_rows(rows)
        return rows, columns, None

    path = payload.get("path")
    if path is None or str(path).strip() == "":
        raise ValueError("builtin.data.csv requires path or rows")
    text = sandbox.read_text(str(path))
    reader = csv.DictReader(io.StringIO(text), delimiter=_delimiter(payload))
    rows = [dict(row) for row in reader]
    columns = list(reader.fieldnames or [])
    return rows, columns, str(path)


def _numeric(value: Any) -> float:
    if value is None or str(value).strip() == "":
        raise ValueError("empty numeric value")
    return float(value)


def _matches(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    column = str(condition.get("column") or "")
    op = str(condition.get("op") or condition.get("operator") or "equals").lower()
    expected = condition.get("value")
    actual = row.get(column)
    if op == "equals":
        return str(actual) == str(expected)
    if op == "contains":
        return str(expected) in str(actual)
    if op == "gt":
        return _numeric(actual) > _numeric(expected)
    if op == "gte":
        return _numeric(actual) >= _numeric(expected)
    if op == "lt":
        return _numeric(actual) < _numeric(expected)
    if op == "lte":
        return _numeric(actual) <= _numeric(expected)
    raise ValueError(f"unsupported csv filter operator: {op}")


def _filtered_rows(rows: list[dict[str, Any]], filters: Any) -> list[dict[str, Any]]:
    if filters is None:
        return rows
    if isinstance(filters, dict):
        filters = [filters]
    if not isinstance(filters, list):
        raise ValueError("csv filters must be an array")
    conditions = [dict(item) for item in filters if isinstance(item, dict)]
    return [row for row in rows if all(_matches(row, condition) for condition in conditions)]


def _aggregate_value(rows: list[dict[str, Any]], aggregate: dict[str, Any]) -> float | int:
    op = str(aggregate.get("op") or aggregate.get("function") or "count").lower()
    column = aggregate.get("column")
    if op == "count":
        return len(rows)
    if column is None:
        raise ValueError("csv aggregate requires column for numeric aggregate")
    numbers = [_numeric(row.get(str(column))) for row in rows]
    if not numbers:
        raise ValueError("csv aggregate has no numeric values")
    if op == "sum":
        return sum(numbers)
    if op == "avg":
        return sum(numbers) / len(numbers)
    if op == "min":
        return min(numbers)
    if op == "max":
        return max(numbers)
    raise ValueError(f"unsupported csv aggregate op: {op}")


def _read(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    rows, columns, path = _load_rows(payload, sandbox)
    return _ok(
        "CSV read completed",
        {"rows": rows, "columns": columns, "row_count": len(rows), "result": None, "path": path},
    )


def _select(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    rows, _, path = _load_rows(payload, sandbox)
    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        return _fail("csv.select requires non-empty columns array", {"path": path})
    selected_columns = [str(item) for item in columns]
    selected = [{column: row.get(column) for column in selected_columns} for row in rows]
    return _ok(
        "CSV select completed",
        {
            "rows": selected,
            "columns": selected_columns,
            "row_count": len(selected),
            "result": None,
            "path": path,
        },
    )


def _filter(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    rows, columns, path = _load_rows(payload, sandbox)
    filtered = _filtered_rows(rows, payload.get("filters", []))
    return _ok(
        "CSV filter completed",
        {
            "rows": filtered,
            "columns": columns,
            "row_count": len(filtered),
            "result": None,
            "path": path,
        },
    )


def _aggregate(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    rows, columns, path = _load_rows(payload, sandbox)
    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, dict):
        return _fail("csv.aggregate requires aggregate object", {"path": path})
    filtered = _filtered_rows(rows, aggregate.get("filters"))
    value = _aggregate_value(filtered, aggregate)
    op = str(aggregate.get("op") or aggregate.get("function") or "count").lower()
    column = aggregate.get("column")
    return _ok(
        "CSV aggregate completed",
        {
            "rows": rows,
            "columns": columns,
            "row_count": len(rows),
            "result": {"op": op, "column": column, "value": value},
            "path": path,
        },
    )


def _report(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output_path = str(payload.get("output_path") or "").strip()
    if not output_path:
        return _fail("csv.report requires output_path")
    metrics = payload.get("metrics") or payload.get("aggregates")
    if not isinstance(metrics, list) or not metrics:
        return _fail("csv.report requires non-empty metrics array")

    rows, columns, path = _load_rows(payload, sandbox)
    result: dict[str, float | int] = {}
    lines: list[str] = []
    for item in metrics:
        if not isinstance(item, dict):
            return _fail("csv.report metrics must be objects", {"path": path})
        name = str(item.get("name") or item.get("label") or item.get("column") or "value")
        filtered = _filtered_rows(rows, item.get("filters"))
        value = _aggregate_value(filtered, item)
        result[name] = value
        lines.append(f"{name}: {value}")

    content = "\n".join(lines)
    if payload.get("trailing_newline", True):
        content += "\n"
    written = sandbox.write_text(
        output_path,
        content,
        overwrite=bool(payload.get("overwrite", True)),
    )
    return _ok(
        "CSV report completed",
        {
            "rows": rows,
            "columns": columns,
            "row_count": len(rows),
            "result": result,
            "path": path,
            "output_path": str(output_path),
        },
        artifacts=[{"kind": "file", "path": str(written), "description": "CSV report output"}],
    )


def _write(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output_path = payload.get("output_path")
    if output_path is None or str(output_path).strip() == "":
        return _fail("csv.write requires output_path")
    rows, columns, _ = _load_rows(payload, sandbox)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, delimiter=_delimiter(payload))
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    written = sandbox.write_text(
        str(output_path),
        buffer.getvalue(),
        overwrite=bool(payload.get("overwrite", False)),
    )
    return _ok(
        "CSV write completed",
        {
            "rows": rows,
            "columns": columns,
            "row_count": len(rows),
            "result": {"bytes": len(buffer.getvalue().encode("utf-8"))},
            "path": str(output_path),
        },
        artifacts=[{"kind": "file", "path": str(written), "description": "CSV output"}],
    )


def run(payload, context):
    payload = dict(payload or {})
    sandbox = context["sandbox"]
    action = str(payload.get("action", "read")).strip().lower()
    try:
        if action == "read":
            return _read(payload, sandbox)
        if action == "select":
            return _select(payload, sandbox)
        if action == "filter":
            return _filter(payload, sandbox)
        if action == "aggregate":
            return _aggregate(payload, sandbox)
        if action == "report":
            return _report(payload, sandbox)
        if action == "write":
            return _write(payload, sandbox)
        return _fail(f"Unsupported CSV action: {action}")
    except OmniAgentError as exc:
        return _fail(exc.message, {"errors": [exc.message]}, str(exc.code))
    except (TypeError, ValueError, csv.Error) as exc:
        return _fail(str(exc), {"errors": [str(exc)]})

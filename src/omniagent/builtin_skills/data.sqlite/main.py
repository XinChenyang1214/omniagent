from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any
from urllib.parse import quote

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


def _db_path(payload: dict[str, Any], sandbox):
    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("sqlite action requires path")
    target = sandbox.resolve_path(path)
    if not target.exists():
        raise ValueError(f"sqlite database does not exist: {path}")
    if target.is_dir():
        raise ValueError(f"sqlite path is a directory: {path}")
    return target, path


def _connect(payload: dict[str, Any], sandbox) -> tuple[sqlite3.Connection, str]:
    target, raw_path = _db_path(payload, sandbox)
    uri_path = quote(target.as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn, raw_path


def _validate_readonly_sql(sql: Any) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sqlite query requires sql")
    cleaned = sql.strip().rstrip(";").strip()
    lowered = cleaned.lower()
    first = lowered.split(None, 1)[0] if lowered.split(None, 1) else ""
    if first not in {"select", "with", "pragma"}:
        raise ValueError("sqlite skill only allows read-only SELECT, WITH, or PRAGMA queries")
    blocked = (
        " insert ",
        " update ",
        " delete ",
        " create ",
        " drop ",
        " alter ",
        " attach ",
        " detach ",
        " replace ",
        " vacuum ",
    )
    padded = f" {lowered} "
    if any(token in padded for token in blocked):
        raise ValueError("sqlite query contains a write or schema-changing statement")
    return cleaned


def _params(payload: dict[str, Any]) -> list[Any]:
    params = payload.get("params", [])
    if params is None:
        return []
    if not isinstance(params, list):
        raise ValueError("sqlite params must be an array")
    return params


def _max_rows(payload: dict[str, Any]) -> int:
    value = payload.get("max_rows", 100)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("sqlite max_rows must be a positive integer")
    return min(value, 1000)


def _run_query(
    conn: sqlite3.Connection, sql: str, params: list[Any], max_rows: int
) -> tuple[list[str], list[dict[str, Any]], bool]:
    cursor = conn.execute(sql, params)
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    columns = [item[0] for item in (cursor.description or [])]
    return columns, [dict(row) for row in rows], truncated


def _list_tables(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    conn, raw_path = _connect(payload, sandbox)
    try:
        rows = conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    finally:
        conn.close()
    return _ok(
        "SQLite tables listed",
        {"tables": [dict(row) for row in rows], "path": raw_path},
    )


def _schema(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    conn, raw_path = _connect(payload, sandbox)
    table = str(payload.get("table") or "").strip()
    try:
        if table:
            rows = conn.execute(
                """
                SELECT name, type, sql
                FROM sqlite_master
                WHERE type IN ('table', 'view') AND name = ?
                ORDER BY type, name
                """,
                [table],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT name, type, sql
                FROM sqlite_master
                WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
    finally:
        conn.close()
    return _ok(
        "SQLite schema loaded",
        {"result": {"objects": [dict(row) for row in rows]}, "path": raw_path},
    )


def _query(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    sql = _validate_readonly_sql(payload.get("sql"))
    conn, raw_path = _connect(payload, sandbox)
    try:
        columns, rows, truncated = _run_query(conn, sql, _params(payload), _max_rows(payload))
    finally:
        conn.close()
    return _ok(
        "SQLite query completed",
        {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "path": raw_path,
        },
    )


def _export_csv(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output_path = str(payload.get("output_path") or "").strip()
    if not output_path:
        return _fail("sqlite.export_csv requires output_path", error_code="validation_error")
    sql = _validate_readonly_sql(payload.get("sql"))
    conn, raw_path = _connect(payload, sandbox)
    try:
        columns, rows, truncated = _run_query(conn, sql, _params(payload), _max_rows(payload))
    finally:
        conn.close()

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    written = sandbox.write_text(
        output_path,
        buffer.getvalue(),
        overwrite=bool(payload.get("overwrite", True)),
    )
    return _ok(
        "SQLite CSV export completed",
        {
            "columns": columns,
            "row_count": len(rows),
            "truncated": truncated,
            "path": raw_path,
            "output_path": output_path,
        },
        artifacts=[{"kind": "file", "path": str(written), "description": "SQLite CSV export"}],
    )


def _report(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output_path = str(payload.get("output_path") or "").strip()
    queries = payload.get("queries")
    if not output_path:
        return _fail("sqlite.report requires output_path", error_code="validation_error")
    if not isinstance(queries, list) or not queries:
        return _fail("sqlite.report requires non-empty queries array", error_code="validation_error")

    conn, raw_path = _connect(payload, sandbox)
    result: dict[str, Any] = {}
    lines: list[str] = []
    try:
        for item in queries:
            if not isinstance(item, dict):
                raise ValueError("sqlite.report queries must be objects")
            label = str(item.get("label") or item.get("name") or "").strip()
            if not label:
                raise ValueError("sqlite.report query requires label")
            sql = _validate_readonly_sql(item.get("sql"))
            columns, rows, _ = _run_query(
                conn,
                sql,
                item.get("params") if isinstance(item.get("params"), list) else [],
                1,
            )
            value = None
            if rows:
                column = str(item.get("column") or (columns[0] if columns else ""))
                value = rows[0].get(column)
            result[label] = value
            lines.append(f"{label}: {value}")
    finally:
        conn.close()

    written = sandbox.write_text(
        output_path,
        "\n".join(lines) + "\n",
        overwrite=bool(payload.get("overwrite", True)),
    )
    return _ok(
        "SQLite report completed",
        {"result": result, "path": raw_path, "output_path": output_path},
        artifacts=[{"kind": "file", "path": str(written), "description": "SQLite report"}],
    )


def run(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    sandbox = context["sandbox"]
    try:
        action = str(payload.get("action") or "query").strip().lower()
        if action == "list_tables":
            return _list_tables(payload, sandbox)
        if action == "schema":
            return _schema(payload, sandbox)
        if action == "query":
            return _query(payload, sandbox)
        if action == "export_csv":
            return _export_csv(payload, sandbox)
        if action == "report":
            return _report(payload, sandbox)
        return _fail(f"unsupported sqlite action: {action}", error_code="validation_error")
    except OmniAgentError as exc:
        return _fail(exc.message, error_code=str(exc.code))
    except (sqlite3.Error, ValueError) as exc:
        return _fail(str(exc), error_code="validation_error")

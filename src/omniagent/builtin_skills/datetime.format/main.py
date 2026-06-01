from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _ok(message: str, output: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "status": "completed", "message": message, "output": output}


def _fail(message: str, output: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"success": False, "status": "failed", "message": message, "output": output or {}}


def _timezone(name: str | None):
    if not name:
        return UTC
    normalized = str(name).strip()
    if normalized.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {normalized}") from exc


def _parse_datetime(value: Any, *, tz_name: str | None = None, input_format: str | None = None):
    tz = _timezone(tz_name)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=tz)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("datetime value must be a non-empty string or timestamp")
    raw = value.strip()
    if input_format:
        parsed = datetime.strptime(raw, input_format)
    else:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _serialize(dt: datetime, tz_name: str | None = None, *, value: str | None = None):
    timezone_name = tz_name or (dt.tzinfo.tzname(dt) if dt.tzinfo else "naive")
    return {
        "value": value if value is not None else dt.isoformat(),
        "iso": dt.isoformat(),
        "timestamp": dt.timestamp() if dt.tzinfo else None,
        "timezone": timezone_name,
        "diff": None,
    }


def _timedelta(amount: Any, unit: str) -> timedelta:
    number = float(amount)
    normalized = str(unit).strip().lower()
    aliases = {
        "second": "seconds",
        "seconds": "seconds",
        "minute": "minutes",
        "minutes": "minutes",
        "hour": "hours",
        "hours": "hours",
        "day": "days",
        "days": "days",
        "week": "weeks",
        "weeks": "weeks",
    }
    key = aliases.get(normalized)
    if key is None:
        raise ValueError(f"unsupported datetime unit: {unit}")
    return timedelta(**{key: number})


def _now(payload: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(payload.get("timezone") or "UTC")
    dt = datetime.now(tz=_timezone(tz_name))
    return _ok("datetime.now completed", _serialize(dt, tz_name))


def _parse(payload: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(payload.get("timezone") or "UTC")
    dt = _parse_datetime(
        payload.get("value"),
        tz_name=tz_name,
        input_format=payload.get("input_format"),
    )
    return _ok("datetime.parse completed", _serialize(dt, tz_name))


def _format(payload: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(payload.get("timezone") or "UTC")
    dt = _parse_datetime(
        payload.get("value"),
        tz_name=tz_name,
        input_format=payload.get("input_format"),
    )
    output_format = str(payload.get("output_format") or "%Y-%m-%dT%H:%M:%S%z")
    formatted = dt.strftime(output_format)
    return _ok("datetime.format completed", _serialize(dt, tz_name, value=formatted))


def _diff(payload: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(payload.get("timezone") or "UTC")
    start = _parse_datetime(
        payload.get("start"),
        tz_name=tz_name,
        input_format=payload.get("input_format"),
    )
    end = _parse_datetime(
        payload.get("end"),
        tz_name=tz_name,
        input_format=payload.get("input_format"),
    )
    delta = end - start
    output = _serialize(end, tz_name)
    output["diff"] = {
        "seconds": delta.total_seconds(),
        "days": delta.days,
        "microseconds": delta.microseconds,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    return _ok("datetime.diff completed", output)


def _add(payload: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(payload.get("timezone") or "UTC")
    base_value = payload.get("value") or datetime.now(tz=_timezone(tz_name)).isoformat()
    dt = _parse_datetime(
        base_value,
        tz_name=tz_name,
        input_format=payload.get("input_format"),
    )
    if payload.get("amount") is None or payload.get("unit") is None:
        return _fail("datetime.add requires amount and unit")
    result = dt + _timedelta(payload.get("amount"), str(payload.get("unit")))
    return _ok("datetime.add completed", _serialize(result, tz_name))


def run(payload, context):
    del context
    payload = dict(payload or {})
    action = str(payload.get("action", "now")).strip().lower()
    try:
        if action == "now":
            return _now(payload)
        if action == "parse":
            return _parse(payload)
        if action == "format":
            return _format(payload)
        if action == "diff":
            return _diff(payload)
        if action == "add":
            return _add(payload)
        return _fail(f"Unsupported datetime action: {action}")
    except (TypeError, ValueError) as exc:
        return _fail(str(exc), {"errors": [str(exc)]})

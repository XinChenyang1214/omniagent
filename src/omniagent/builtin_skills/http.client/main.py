from __future__ import annotations

import base64
import contextlib
import json
import socket
import urllib.error as urllib_error
import urllib.parse as urllib_parse
import urllib.request as urllib_request
from typing import Any

from omniagent.errors import ErrorCode
from omniagent.sandbox import _encode_http_url

_ACTIONS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE"}
_DEFAULT_MAX_RESPONSE_CHARS = 4096


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


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("http headers must be an object")
    return {str(key): str(item) for key, item in value.items()}


def _append_params(url: str, params: Any) -> str:
    if params in (None, {}):
        return url
    if not isinstance(params, dict):
        raise ValueError("http params must be an object")
    encoded = urllib_parse.urlencode(params, doseq=True)
    if not encoded:
        return url
    parts = urllib_parse.urlsplit(url)
    query = f"{parts.query}&{encoded}" if parts.query else encoded
    return urllib_parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    lowered = {key.lower() for key in headers}
    if name.lower() not in lowered:
        headers[name] = value


def _apply_auth(headers: dict[str, str], auth: Any) -> None:
    if auth in (None, {}):
        return
    if not isinstance(auth, dict):
        raise ValueError("http auth must be an object")
    auth_type = str(auth.get("type") or "").strip().lower()
    if auth_type == "bearer":
        token = str(auth.get("token") or "").strip()
        if not token:
            raise ValueError("bearer auth requires token")
        headers["Authorization"] = f"Bearer {token}"
        return
    if auth_type == "basic":
        username = str(auth.get("username") or "")
        password = str(auth.get("password") or "")
        raw = f"{username}:{password}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return
    raise ValueError(f"unsupported http auth type: {auth_type}")


def _request_body(payload: dict[str, Any], headers: dict[str, str]) -> bytes | None:
    if "json" in payload or "json_body" in payload:
        value = payload.get("json") if "json" in payload else payload.get("json_body")
        _set_default_header(headers, "Content-Type", "application/json")
        return json.dumps(value, ensure_ascii=False).encode("utf-8")

    if "form" in payload:
        form = payload.get("form")
        if not isinstance(form, dict):
            raise ValueError("http form body must be an object")
        _set_default_header(headers, "Content-Type", "application/x-www-form-urlencoded")
        return urllib_parse.urlencode(form, doseq=True).encode("utf-8")

    if "raw_body" in payload or "body" in payload:
        value = payload.get("raw_body") if "raw_body" in payload else payload.get("body")
        if isinstance(value, bytes):
            return value
        if value is None:
            return None
        return str(value).encode("utf-8")

    return None


def _timeout(payload: dict[str, Any], sandbox) -> float:
    value = payload.get("timeout", sandbox.timeout_seconds)
    timeout = float(value)
    if timeout <= 0:
        raise ValueError("http timeout must be greater than zero")
    return timeout


def _max_response_chars(payload: dict[str, Any]) -> int:
    value = int(payload.get("max_response_chars", _DEFAULT_MAX_RESPONSE_CHARS))
    if value <= 0:
        raise ValueError("http max_response_chars must be greater than zero")
    return value


def _headers_map(headers_obj: Any) -> dict[str, str]:
    if headers_obj is None:
        return {}
    if hasattr(headers_obj, "items"):
        return {str(key): str(value) for key, value in headers_obj.items()}
    return {}


def _charset(headers_obj: Any, headers_map: dict[str, str]) -> str:
    if hasattr(headers_obj, "get_content_charset"):
        charset = headers_obj.get_content_charset()
        if charset:
            return str(charset)
    content_type = next(
        (value for key, value in headers_map.items() if key.lower() == "content-type"),
        "",
    )
    for part in content_type.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.lower() == "charset" and value.strip():
            return value.strip()
    return "utf-8"


def _response_output(
    *,
    method: str,
    url: str,
    request_url: str,
    status: int,
    reason: str,
    headers_obj: Any,
    body_bytes: bytes,
    max_chars: int,
) -> dict[str, Any]:
    response_headers = _headers_map(headers_obj)
    body = body_bytes.decode(_charset(headers_obj, response_headers), errors="replace")
    preview = body[:max_chars]
    content_type = next(
        (value for key, value in response_headers.items() if key.lower() == "content-type"),
        "",
    )
    parsed_json: Any = None
    json_error: str | None = None
    if "json" in content_type.lower() and body:
        try:
            parsed_json = json.loads(body)
        except json.JSONDecodeError as exc:
            json_error = str(exc)

    output = {
        "method": method,
        "url": url,
        "request_url": request_url,
        "status": status,
        "reason": reason,
        "headers": response_headers,
        "body": preview,
        "body_preview": preview,
        "body_truncated": len(body) > max_chars,
        "body_size": len(body),
        "json": parsed_json,
    }
    if json_error:
        output["json_error"] = json_error
    return output


def run(payload, context):
    payload = dict(payload or {})
    sandbox = context["sandbox"]
    action = str(payload.get("action", "get")).strip().lower()
    method = _ACTIONS.get(action)
    if method is None:
        return _fail(f"Unsupported HTTP action: {action}", error_code=ErrorCode.VALIDATION_ERROR)

    if not getattr(sandbox, "allow_network", False):
        return _fail(
            "当前 sandbox 未开启网络访问，builtin.http.client 拒绝请求",
            error_code=ErrorCode.PERMISSION_DENIED,
        )

    try:
        url = str(payload.get("url") or "").strip()
        if not url:
            return _fail("http request requires url", error_code=ErrorCode.VALIDATION_ERROR)
        parsed = urllib_parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return _fail(
                f"unsupported HTTP URL scheme: {parsed.scheme}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )

        request_url = _encode_http_url(_append_params(url, payload.get("params")))
        headers = _headers(payload.get("headers"))
        _apply_auth(headers, payload.get("auth"))
        body = _request_body(payload, headers)
        timeout = _timeout(payload, sandbox)
        max_chars = _max_response_chars(payload)

        request = urllib_request.Request(request_url, data=body, headers=headers, method=method)
        with contextlib.closing(urllib_request.urlopen(request, timeout=timeout)) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 200)
            reason = str(getattr(response, "reason", ""))
            output = _response_output(
                method=method,
                url=url,
                request_url=request_url,
                status=status,
                reason=reason,
                headers_obj=getattr(response, "headers", None),
                body_bytes=response.read(),
                max_chars=max_chars,
            )
            return _ok(f"HTTP {method} completed: {status}", output)
    except urllib_error.HTTPError as exc:
        output = _response_output(
            method=method,
            url=str(payload.get("url") or ""),
            request_url=getattr(exc, "url", str(payload.get("url") or "")),
            status=int(exc.code),
            reason=str(exc.reason),
            headers_obj=exc.headers,
            body_bytes=exc.read(),
            max_chars=_max_response_chars(payload),
        )
        return _fail(f"HTTP {method} failed: {exc.code}", output, ErrorCode.EXECUTION_ERROR)
    except (TimeoutError, socket.timeout) as exc:
        return _fail(f"HTTP {method} timeout: {exc}", error_code=ErrorCode.EXECUTION_ERROR)
    except urllib_error.URLError as exc:
        return _fail(f"HTTP {method} failed: {exc.reason}", error_code=ErrorCode.EXECUTION_ERROR)
    except (TypeError, ValueError) as exc:
        return _fail(str(exc), error_code=ErrorCode.VALIDATION_ERROR)

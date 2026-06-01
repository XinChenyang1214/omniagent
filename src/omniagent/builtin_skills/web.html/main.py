from __future__ import annotations

import re
from html.parser import HTMLParser
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


class _HTMLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.meta: dict[str, str] = {}
        self.tables: list[dict[str, Any]] = []
        self._skip_depth = 0
        self._tag_stack: list[str] = []
        self._current_link: dict[str, str] | None = None
        self._current_link_text: list[str] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        self._tag_stack.append(tag)
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "a":
            self._current_link = {"href": attrs_dict.get("href", ""), "text": ""}
            self._current_link_text = []
        if tag == "meta":
            key = attrs_dict.get("name") or attrs_dict.get("property")
            content = attrs_dict.get("content")
            if key and content:
                self.meta[key] = content
        if tag == "table":
            self._current_table = []
        if tag == "tr" and self._current_table is not None:
            self._current_row = []
        if tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "a" and self._current_link is not None:
            self._current_link["text"] = _collapse_ws(" ".join(self._current_link_text))
            self.links.append(self._current_link)
            self._current_link = None
            self._current_link_text = []
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_row.append(_collapse_ws(" ".join(self._current_cell)))
            self._current_cell = None
        if tag == "tr" and self._current_row is not None and self._current_table is not None:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        if tag == "table" and self._current_table is not None:
            if self._current_table:
                headers = self._current_table[0]
                rows = [
                    {header: value for header, value in zip(headers, row, strict=False)}
                    for row in self._current_table[1:]
                    if len(row) == len(headers)
                ]
                self.tables.append({"columns": headers, "rows": rows})
            self._current_table = None
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._tag_stack and self._tag_stack[-1] == "title":
            self.title_parts.append(text)
        if self._current_link is not None:
            self._current_link_text.append(text)
        if self._current_cell is not None:
            self._current_cell.append(text)
        if not (self._tag_stack and self._tag_stack[-1] == "title"):
            self.text_parts.append(text)


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _read_html(payload: dict[str, Any], sandbox) -> tuple[str, str | None]:
    if payload.get("text") is not None:
        if not isinstance(payload["text"], str):
            raise ValueError("html text must be a string")
        return payload["text"], None
    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("html action requires text or path")
    return sandbox.read_text(path), path


def _parse(payload: dict[str, Any], sandbox) -> tuple[dict[str, Any], str | None]:
    html, path = _read_html(payload, sandbox)
    parser = _HTMLExtractor()
    parser.feed(html)
    max_chars = payload.get("max_text_chars", 4000)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("html max_text_chars must be a positive integer")
    text = _collapse_ws(" ".join(parser.text_parts))
    output = {
        "title": _collapse_ws(" ".join(parser.title_parts)),
        "text": text[:max_chars],
        "text_truncated": len(text) > max_chars,
        "links": parser.links,
        "meta": parser.meta,
        "tables": parser.tables,
        "path": path,
    }
    return output, path


def _extract(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output, _ = _parse(payload, sandbox)
    return _ok("HTML extracted", output)


def _links(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output, path = _parse(payload, sandbox)
    return _ok("HTML links extracted", {"links": output["links"], "path": path})


def _report(payload: dict[str, Any], sandbox) -> dict[str, Any]:
    output_path = str(payload.get("output_path") or "").strip()
    if not output_path:
        return _fail("html.report requires output_path", error_code="validation_error")
    output, _ = _parse(payload, sandbox)
    lines = [
        f"title: {output['title']}",
        f"link_count: {len(output['links'])}",
        f"text: {output['text']}",
    ]
    written = sandbox.write_text(
        output_path,
        "\n".join(lines) + "\n",
        overwrite=bool(payload.get("overwrite", True)),
    )
    output["output_path"] = output_path
    return _ok(
        "HTML report written",
        output,
        artifacts=[{"kind": "file", "path": str(written), "description": "HTML report"}],
    )


def run(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    sandbox = context["sandbox"]
    try:
        action = str(payload.get("action") or "extract").strip().lower()
        if action == "extract":
            return _extract(payload, sandbox)
        if action == "links":
            return _links(payload, sandbox)
        if action == "report":
            return _report(payload, sandbox)
        return _fail(f"unsupported html action: {action}", error_code="validation_error")
    except OmniAgentError as exc:
        return _fail(exc.message, error_code=str(exc.code))
    except ValueError as exc:
        return _fail(str(exc), error_code="validation_error")

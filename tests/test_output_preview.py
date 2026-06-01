"""ISSUE-016 — Smart output preview for Responder context.

Ensures that _prepare_output_preview extracts meaningful text from HTML,
applies per-field and per-node budget limits, and passes non-HTML content
through with a higher truncation limit.

Coverage map:
- _extract_text_content     → HTMLExtractionTest (5)
- _prepare_output_preview   → OutputPreviewTest (5)
- _summarize_report path    → SummarizeReportIntegrationTest (2)
"""

from __future__ import annotations

import unittest

from omniagent.response_generator import (
    _extract_text_content,
    _prepare_output_preview,
    _OUTPUT_FIELD_LIMIT,
    _OUTPUT_NODE_BUDGET,
)


# ---------------------------------------------------------------------------
# HTMLExtractionTest
# ---------------------------------------------------------------------------


class HTMLExtractionTest(unittest.TestCase):
    """Verifies HTML → plaintext extraction."""

    def test_plain_text_passthrough(self) -> None:
        """Non-HTML text is returned unchanged."""
        text = "这是一段普通的中文文本，不含任何 HTML。"
        self.assertEqual(_extract_text_content(text), text)

    def test_simple_html_tags_stripped(self) -> None:
        result = _extract_text_content("<p>hello <b>world</b></p>")
        self.assertIn("hello", result)
        self.assertIn("world", result)
        self.assertNotIn("<p>", result)
        self.assertNotIn("<b>", result)

    def test_style_and_script_blocks_removed(self) -> None:
        html = (
            "<html><head><style>.a { color: red; }</style>"
            "<script>alert('hi')</script></head>"
            "<body><p>visible content</p></body></html>"
        )
        result = _extract_text_content(html)
        self.assertIn("visible content", result)
        self.assertNotIn("color: red", result)
        self.assertNotIn("alert", result)

    def test_html_entities_decoded(self) -> None:
        html = "<p>A &amp; B &lt; C &gt; D</p>"
        result = _extract_text_content(html)
        self.assertIn("A & B", result)
        self.assertIn("< C >", result)

    def test_whitespace_collapsed(self) -> None:
        html = "<div>  hello   \n\n   world  </div>"
        result = _extract_text_content(html)
        # Multiple spaces/newlines should collapse to single space
        self.assertNotIn("  ", result)
        self.assertIn("hello", result)
        self.assertIn("world", result)


# ---------------------------------------------------------------------------
# OutputPreviewTest
# ---------------------------------------------------------------------------


class OutputPreviewTest(unittest.TestCase):
    """Verifies the per-field + per-node budget enforcement."""

    def test_short_output_unchanged(self) -> None:
        """Small output dicts pass through without truncation."""
        output = {"url": "https://example.com", "status": 200, "body": "OK"}
        preview = _prepare_output_preview(output)
        self.assertEqual(preview["url"], "https://example.com")
        self.assertEqual(preview["status"], 200)
        self.assertEqual(preview["body"], "OK")

    def test_html_body_extracted_and_truncated(self) -> None:
        """Large HTML body → text extraction → truncation to field limit."""
        # Build a large HTML page where content is buried after CSS
        css = "<style>" + ".x { color: red; }\n" * 500 + "</style>"
        content = "天气数据：晴天 25度 " * 200  # ~2000 chars
        html = f"<html><head>{css}</head><body><p>{content}</p></body></html>"
        output = {"body": html}
        preview = _prepare_output_preview(output)
        # CSS should be gone
        self.assertNotIn("color: red", preview["body"])
        # Weather data should be present
        self.assertIn("天气数据", preview["body"])
        # Should be truncated
        self.assertLessEqual(len(preview["body"]), _OUTPUT_FIELD_LIMIT + 10)

    def test_non_string_values_preserved(self) -> None:
        """int, bool, dict values are kept as-is."""
        output = {"status": 200, "success": True, "meta": {"k": "v"}}
        preview = _prepare_output_preview(output)
        self.assertEqual(preview["status"], 200)
        self.assertIs(preview["success"], True)
        self.assertEqual(preview["meta"], {"k": "v"})

    def test_node_budget_enforced(self) -> None:
        """When total chars exceed _OUTPUT_NODE_BUDGET, later fields truncated."""
        # First field takes most of the budget
        output = {
            "field_a": "A" * (_OUTPUT_NODE_BUDGET - 100),
            "field_b": "B" * 1000,
        }
        preview = _prepare_output_preview(output)
        # field_b should be heavily truncated or budget-exhausted
        total = sum(len(str(v)) for v in preview.values())
        self.assertLessEqual(total, _OUTPUT_NODE_BUDGET + 200)

    def test_plain_text_truncated_at_field_limit(self) -> None:
        """Non-HTML long text truncated at _OUTPUT_FIELD_LIMIT, not 200."""
        long_text = "测试内容" * 500  # 2000 chars
        output = {"content": long_text}
        preview = _prepare_output_preview(output)
        # Should be truncated at _OUTPUT_FIELD_LIMIT, not 200
        self.assertGreater(len(preview["content"]), 200)
        self.assertLessEqual(len(preview["content"]), _OUTPUT_FIELD_LIMIT + 10)


# ---------------------------------------------------------------------------
# SummarizeReportIntegrationTest
# ---------------------------------------------------------------------------


class SummarizeReportIntegrationTest(unittest.TestCase):
    """Verifies that _summarize_report uses the new preview function."""

    def test_summarize_report_uses_prepare_output_preview(self) -> None:
        """_summarize_report should call _prepare_output_preview, not inline 200-char cut."""
        import inspect
        from omniagent.response_generator import ResponseGenerator

        source = inspect.getsource(ResponseGenerator._summarize_report)
        # Old pattern should be gone
        self.assertNotIn("val[:200]", source)
        # New function should be referenced
        self.assertIn("_prepare_output_preview", source)

    def test_old_200_char_pattern_absent(self) -> None:
        """Ensure the hardcoded 200-char truncation is fully removed."""
        import inspect
        from omniagent.response_generator import ResponseGenerator

        source = inspect.getsource(ResponseGenerator._summarize_report)
        self.assertNotIn("> 200", source)
        self.assertNotIn("[:200]", source)


if __name__ == "__main__":
    unittest.main()

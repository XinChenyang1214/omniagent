# builtin.text.markdown

Structured Markdown extraction and section editing for workspace files.

Examples:

```json
{"action": "headings", "path": "README.md"}
```

```json
{"action": "extract_section", "path": "ROADMAP.md", "heading": "P1"}
```

```json
{"action": "replace_section", "path": "notes/status.md", "heading": "Summary", "content": "status: ready"}
```

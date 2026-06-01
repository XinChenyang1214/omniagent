# builtin.data.config

Query TOML, simple YAML mappings, XML, and JSON config files.

Examples:

```json
{"action": "query", "path": "omniagent.toml", "query": "llm.provider"}
```

For TOML sections, query the parsed dot path. Use `label` only for the output name:

```json
{"action": "report", "path": "service.toml", "fields": [{"label": "service_name", "query": "service.name"}], "output_path": "reports/service.md"}
```

Unambiguous aliases such as `service_name` -> `service.name` and `owner` -> `service.owner` are accepted when the file has a single root section.

```json
{"action": "query", "path": "service.yaml", "query": "service.owner"}
```

```json
{"action": "report", "path": "service.xml", "format": "xml", "fields": [{"label": "name", "query": "service.name.text"}], "output_path": "reports/service.md"}
```

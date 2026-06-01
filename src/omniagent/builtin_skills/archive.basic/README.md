# builtin.archive.basic

List, extract, and create zip/tar archives inside the workspace.

Examples:

```json
{"action": "list", "path": "inputs/bundle.zip"}
```

```json
{"action": "extract", "path": "inputs/bundle.zip", "output_dir": "unpacked"}
```

```json
{"action": "create", "paths": ["reports"], "output_path": "reports.zip", "format": "zip"}
```

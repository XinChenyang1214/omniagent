# builtin.vcs.git

Read-only git commands through the git CLI. This skill requires process execution permission.

Examples:

```json
{"action": "status", "cwd": "."}
```

```json
{"action": "diff", "cwd": ".", "stat": true}
```

```json
{"action": "log", "cwd": ".", "max_count": 5}
```

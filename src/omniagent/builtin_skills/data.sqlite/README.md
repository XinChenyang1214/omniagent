# builtin.data.sqlite

Read-only SQLite inspection and reporting for workspace databases.

Examples:

```json
{"action": "list_tables", "path": "data/app.sqlite3"}
```

```json
{"action": "query", "path": "data/app.sqlite3", "sql": "select name, count(*) as total from users group by name"}
```

```json
{"action": "report", "path": "data/app.sqlite3", "queries": [{"label": "user_count", "sql": "select count(*) as value from users"}], "output_path": "reports/db.md"}
```

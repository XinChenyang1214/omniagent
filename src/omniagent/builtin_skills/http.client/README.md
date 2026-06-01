# builtin.http.client

Stdlib HTTP client skill for controlled API calls.

Actions:

- `get`
- `post`
- `put`
- `delete`

Capabilities:

- request headers
- query params
- JSON, form, and raw request body
- Bearer and Basic auth helper
- per-request timeout
- response body preview with stable truncation

Network is denied unless the sandbox is created with `allow_network=true`.

Example payloads:

```json
{"action": "get", "url": "https://api.example.test/items", "params": {"limit": 10}}
```

```json
{"action": "post", "url": "https://api.example.test/items", "json": {"name": "alpha"}}
```

```json
{"action": "get", "url": "https://api.example.test/me", "auth": {"type": "bearer", "token": "<token>"}}
```

# builtin.data.json

Stdlib JSON capability for validation, dot-path lookup, formatting, and small deterministic transforms.

Actions:

- `validate`: parse JSON from `text` or `path`
- `query`: return a dot-path value such as `items.0.name`
- `format`: pretty-print or compact JSON
- `transform`: apply `pick`, `rename`, `set`, and `delete`

This skill only reads files through the configured sandbox and introduces no runtime dependency.

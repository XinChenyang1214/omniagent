# builtin.shell.safe

Safe shell capability for a narrow set of read-oriented commands.

Constraints:

- `shell=True` is never used
- command must be an argv array
- command name must be in the whitelist
- cwd is resolved through the sandbox and must stay inside the workspace/runtime
- timeout is always passed to `subprocess.run`
- stdout and stderr are returned as stable previews

Allowed commands:

`grep`, `find`, `wc`, `head`, `tail`, `sort`, `uniq`, `cat`, `ls`, `du`, `df`.

Example payload:

```json
{"action": "run", "argv": ["wc", "-l", "README.md"], "cwd": ".", "timeout": 5}
```

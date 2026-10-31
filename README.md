# siril-mcp (prototype)

Single-file MCP server that drives a persistent `siril-cli -p` process over named pipes,
so the loaded image survives between tool calls.

Requires Siril (`brew install siril`) and `uv`. Dependencies are declared inline (PEP 723);
`mcp` is pinned `<2` because 2.x renamed `FastMCP`.

## Register with Claude Code

```bash
claude mcp add siril -- /Volumes/Files/claude/siril-mcp/siril_mcp.py
```

Env: `SIRIL_BIN` (path to `siril-cli`), `SIRIL_MCP_TIMEOUT` (per-command seconds, default 300).

## Tools

| tool | does |
|---|---|
| `siril_command` | any raw Siril command (the escape hatch for everything else) |
| `siril_help` | list commands / syntax of one |
| `siril_set_directory`, `siril_load`, `siril_save`, `siril_stats` | basics |
| `siril_preview` | autostretched JPEG of the current image, returned as an image |

## Pipe protocol (observed on Siril 1.4.5)

`ready`, then per command `status: starting <cmd>` / `log: ...` / `status: success|error <cmd>`.
Success/failure and completion come only from the `status:` lines; `log:` text is returned unparsed.
`exit` ends the process.

## Known gaps

- No tests; smoke-tested by hand against a synthetic FITS only.
- Siril's python-module bootstrap logs a harmless warning on startup.
- One global session; a timed-out command resets Siril (loaded image is lost).
- No sequence/stacking helpers yet (`siril_command "convert ..."`, `stack ...` work via the raw tool).

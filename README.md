# siril-mcp (prototype)

Single-file MCP server that drives a persistent `siril-cli -p` process over named pipes,
so the loaded image survives between tool calls.

Requires Siril (`brew install siril`) and `uv`. Dependencies are declared inline (PEP 723);
`mcp` is pinned `>=1.27,<2`: 2.x renamed `FastMCP`, and releases before 1.23 no longer import against current pydantic, and before 1.27 an in-flight tool call is not cancelled when the client disconnects, so shutdown waits for it.

## Register with Claude Code

```bash
claude mcp add siril -- /Volumes/Files/claude/siril-mcp/siril_mcp.py
```

Env: `SIRIL_BIN` (path to `siril-cli`; falls back to `siril-cli` on `PATH`, then `/opt/homebrew/bin/siril-cli`),
`SIRIL_MCP_TIMEOUT` (seconds, positive and finite, default 300; see *Timeouts*),
`SIRIL_MCP_LOG_LIMIT` (characters, an integer of at least 2000, default 20000), `SIRIL_MCP_LOG_DIR` (default
`~/.cache/siril-mcp/logs`; `~` is expanded and a relative path is taken from the server's working directory); for both
see *Long logs are summarised* below. A bad value stops the server at start, with a message naming the setting.

## Tools

| tool | does |
|---|---|
| `siril_command` | any raw Siril command (the escape hatch for everything else) |
| `siril_help` | list commands / syntax of one |
| `siril_set_directory`, `siril_load`, `siril_save`, `siril_stats` | basics |
| `siril_preview` | autostretched JPEG of the current image (long side 2048 px by default), returned as an image |

Tool calls are serialised: one runs at a time.

## Behaviour worth knowing

- **Timeouts are idle timeouts.** `timeout_s` / `SIRIL_MCP_TIMEOUT` is how long Siril may stay silent; any output,
  including progress, restarts the clock, so a long stack that keeps reporting is not cut off. It must be positive
  and finite (a bad value is rejected before anything is sent). Every tool except `siril_command` (which takes `timeout_s`) uses the default.
- **A timeout or crash resets the session and loses the loaded image.** The error says `(session reset)`. If Siril
  dies by itself, the next command starts a fresh one and the result begins with a notice saying so.
- **Bad input never resets the session.** Empty or multi-line commands, `@script` invocations, over-long commands
  and paths containing `"` or newlines are rejected up front (Siril has no way to escape them).
- **Long logs are summarised.** A command that logs more than `SIRIL_MCP_LOG_LIMIT` characters (a 400-frame
  `calibrate` logs about 54,000) comes back as a summary of at most that size, beginning `[log shortened: N lines, M
  characters; log saved to <path>]` (on a failed command that is the second line, after `Siril command failed: ...`).
  Lines whose shape (the line with its numbers masked) occurs at most 3 times are shown in order, each cut at 600
  characters if longer (`...[line cut, N characters]`); commoner shapes are listed as `count x first example`, the most
  repeated first, examples cut at 160 characters. Because only numbers are masked, lines that differ only in a number
  (30 `Error: could not open frame_00017.fit` lines) are grouped and only the first is shown: the saved log has them all.
  When the rare lines don't all fit, the summary keeps those from the start and from the end of the log (an error or
  the closing totals come last) and says how many it left out of the middle; the same goes for repeated kinds that
  don't fit. Nothing is judged by keyword or meaning. The saved file is the log text exactly as the server received it
  (up to 1,000,000 characters; beyond that an `[output truncated: ...]` line says so), so it is the whole log unless
  that cap fired. If it can't be written, the header says `log could NOT be saved` and why. The same summary appears in
  the error text of a failed command. `siril_help` is never summarised (its output is a fixed ~11,000 characters). Each
  saved log is a file named `<time>-<verb>-<random>.log`; the newest 50 of those are kept and nothing else in the
  directory is touched (if a deletion fails the header says so). A log is not saved when the command times out or
  Siril dies; the error then carries only Siril's console tail.
- **`exit` / `quit`** end the Siril process cleanly; the next command starts a new one.
- **`@script.ssf` is not supported:** a script emits no completion status over the pipe, so its end cannot be
  detected. Send its commands one at a time.
- **Working directory** is tracked from successful `cd` commands (including ones sent through `siril_command`) and
  is used if the session has to restart.
- **`siril_preview`** applies the stretch/resize to the loaded image and restores it by reloading a temporary
  snapshot, so pixel data is preserved but Siril's current-file name, undo history and any selection are not. With
  `autostretch=false` and `max_dim=0` nothing is modified and nothing is snapshotted.
- **Unrecognised Siril version:** the first start runs `siril-cli --version`; anything other than 1.4.x adds a notice,
  because the pipe protocol below was only observed on 1.4.5.
- **Pipe dirs** live in the temp dir as `siril-mcp-<server pid>-*`. SIGTERM/SIGHUP and normal exit clean up; after a
  SIGKILL, the next server start removes the dead server's dir and kills its orphaned Siril.

## Pipe protocol (observed on Siril 1.4.5)

`ready`, then per command `status: starting <verb>` / `log: ...` / `status: success|error <verb>`, where `<verb>` is
the command's first word as typed (not its arguments).

- Success/failure and completion come only from `status:` lines that echo the command we sent; a status for any other
  command is ignored and counted in the result (`[ignored N stale status line(s)...]`), so a late reply from a timed-out
  command cannot answer the next one. A status word outside `starting|success|error` that echoes our command is treated
  as a failed completion rather than waited on.
- `log:` text is returned unparsed, unless it exceeds `SIRIL_MCP_LOG_LIMIT`, when it is summarised (see above).
  Unprefixed lines are continuations of a multi-line log message and are kept.
  `progress:` lines are dropped on purpose. Output before the command's own `starting` marker (Siril's asynchronous
  python-module bootstrap logs) belongs to nothing we sent and is dropped. The log text collected for one command is
  capped at 1,000,000 characters with a visible marker; what the caller gets back is then the summary above if it is
  still over the limit.
- `exit`/`quit` emit `starting` and then the process ends, with no terminal status; that is detected from the process.
- Siril's stdout/stderr are kept in the pipe dir and the tail is included in startup and crash errors.

## Known gaps

- Tests run against a fake Siril (`tests/fake_siril.py`); the same behaviours were checked by hand against the real
  `siril-cli` 1.4.5 during review, but there is no automated real-Siril test.
- `siril_preview` uses Siril's `resample -maxdim=N`, which also enlarges images smaller than N (real astro frames are
  larger than the 2048 default, but a small test image is scaled up). The server cannot tell the image size without
  scraping log text.
- One global session. Two server processes sharing a machine each get their own Siril, but nothing coordinates them.
- Status echoes carry only the command verb, so a stale status for the *same* verb as the next command cannot be
  told apart (a timed-out command resets the session, which makes this unlikely in practice).
- Siril's python-module bootstrap logs can still appear inside a command's output if they arrive between its
  `starting` and terminal lines.
- No sequence/stacking helpers yet (`siril_command "convert ..."`, `stack ...` work via the raw tool).

## Tests

```bash
uv run --with 'mcp>=1.27,<2' --with pytest --with pytest-cov pytest --cov
```

(The `mcp` pin repeats the one in the script header; keep them in step.) `tests/fake_siril.py` stands in for
`siril-cli -p` and speaks the pipe protocol, with commands that provoke each case (multi-line logs, late status,
abrupt exit, invalid UTF-8, hung exit...). Test comments cite the review finding (`.claude-review/`) each one pins; the log-summary tests (`tests/test_logs.py`) instead say what behaviour they pin.
Coverage is a floor-check only (`fail_under` in `.coveragerc`): it says the code ran, not that the tests assert
anything meaningful.

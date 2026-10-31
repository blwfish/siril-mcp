# Full Review — siril-mcp (whole repo: `siril_mcp.py`, 264 lines; single commit 94ab153)

Pass-id: `siril-mcp-20261031-d1bf`. Date: 2026-10-31. No prior ledger entry exists for this repo.

## Summary
| Pass        | Critical | High | Med | Low | Status |
|-------------|----------|------|-----|-----|--------|
| Code review | 0 | 4 | 13 | 12 | ✓ ran (7 of 9 angles; angles 3, 4 skipped, see below) |
| Interface   | 0 | 0 | 0 | 0 | ✓ ran (2 passes; new findings: none, all corroborated Phase 1 findings) |
| Inventory   | 0 | 0 | 0 | 0 | ✓ ran (1 module × 3 passes; new findings: none, all corroborated Phase 1 findings) |
| Test review | — | — | — | — | SKIPPED (no tests exist) |
| Coverage    | — | — | — | — | SKIPPED (no coverage tool configured) |

Totals across the pass: **0 Critical, 4 High, 13 Medium, 12 Low (29 findings).**

Pass notes:
- **Angles 3 and 4 (git history) skipped:** no scope file has history before the reviewed change (one commit). Unmet precondition, not a cost reduction.
- **Mechanical pre-passes:** `unchecked_result_sweep.py` 0 hits; `duplication_sweep.py` 0 hits; `missing_test_sweep.py` 1 hit (F29). The `os.write` unchecked-result issue (F16) was found by reading, not by the sweep, which only covers `subprocess`/`os.system`/`requests`.
- **Interface and inventory added no unique findings.** Their value was reproduction: interface passes and angle 1 ran real `siril-cli` 1.4.5 and `mcp` 1.30 and reproduced F04, F05, F06, F07, F09 and F17's pixel-preservation claim; inventory pass 3 reproduced the F01 FIFO behavior.
- **Process incident:** every subagent inherited one shared scratchpad directory; two interface passes overwrote each other's probe scripts and one ran `pkill -f siril-cli` machine-wide. This was an orchestration mistake (no per-agent scratch dirs in the briefs), not a code defect. Findings affected are corroborated by multiple independent passes, so none are believed to depend on a disturbed run. One leaked test pipe dir was cleaned up.
- Finding ids are `siril-mcp-20261031-d1bf#NN` where NN is the F number below. `found_by` lists Phase 1 angle numbers; "also" lists corroborating interface (I1, I2) / inventory (V1-V3) passes.

## Critical
None.

## High
- [code] siril_mcp.py:235-255 — `siril_preview` calls `session.run` directly and skips `_run`'s catch-`SirilError` → `session.stop()` reset. After a mid-preview timeout the session stays alive with the command still running, the `finally` sends `load snap` into it, and the restore error replaces the original one. The lock/run/raise handling is hand-copied from `_run` and has already diverged with nothing to catch it (this is what lifts it to High). angles [2,5,6,7,8,9]; also I1,I2,V1-V3. [siril-mcp-20261031-d1bf#03]
- [code] siril_mcp.py:143-156 — The terminal `status: success|error <cmd>` line's command echo is never compared to the issued command, so a late status from an earlier command is returned as the next command's result. Silent wrong data with a success status. Reproduced: after a timed-out `stat`, `load "nonexistent"` returned `(True, <stat output>)`. Today reachable via F03 and F09. angles [5,9]; also I1,I2,V1-V3. [siril-mcp-20261031-d1bf#04]
- [code] siril_mcp.py:133-134,168-170 — Input validation (empty command, newline/CR in a command or any file/path argument) raises `SirilError`, which `_run` treats as fatal: `session.stop()` and "(session reset)". One bad tool argument destroys the loaded/processed image though nothing was sent to Siril. Reproduced (`siril_command "stat\nstat"`). angles [2,5]; also I2,V3. [siril-mcp-20261031-d1bf#05]
- [code] siril_mcp.py:147-156 — Multi-line Siril log messages lose every line after the first (continuation lines have no `log: ` prefix and fall into the silent "other kinds" branch, no counter). `siril_help()` returned 203 of 240 lines; `siril_help("stack")` loses its syntax lines (also register, platesolve, seqapplyreg, makepsf, rgbcomp, stackall, update_key); tool reports success. A continuation line beginning `status: success` would be read as a terminal marker. Bare `log:` and unknown prefixes are dropped too. Violates the Data-Capture rule (dropped with no reason or counter). angles [1,2,5,9]; also V1-V3. [siril-mcp-20261031-d1bf#06]

## Medium
- [code] siril_mcp.py:72-75,100-126 — Out FIFO opened `O_RDWR`, so the process is its own writer and the reader never sees EOF; the "Siril process exited" branch and `None` sentinel are dead code. A Siril crash/kill/`exit` waits the full timeout (default 300s) and is reported as "timed out". Verified. angles [1,2,5,7,8,9]; also I1,I2,V1-V3. [siril-mcp-20261031-d1bf#01]
- [code] siril_mcp.py:147-155 — `exit`, `quit` and script (`@script.ssf`) invocations never emit a terminal status: the call hangs to timeout then resets the session. Binary also contains `status: exit`/`status: busy`; an unknown status word is treated as non-terminal and hangs. README documents the protocol as complete. Verified. angles [1,5]; also I1,I2,V2,V3. [siril-mcp-20261031-d1bf#07]
- [code] siril_mcp.py:122,146,179 — `timeout_s` is unvalidated: negative → `ValueError` after the command was written (no reset, output mis-paired with the next call, the F04 path); 0 → always times out and resets; `inf` → `OverflowError`. Verified for -1 and 0. angles [1,2]; also I1,I2. [siril-mcp-20261031-d1bf#09]
- [code] siril_mcp.py:192,202,208,217,223,235-239 — Per-tool hardcoded timeouts (30/60/120s) ignore `SIRIL_MCP_TIMEOUT`, which the README presents as the per-command default; an overrun triggers the destructive reset. 6 sites. angles [5]. [siril-mcp-20261031-d1bf#10]
- [code] siril_mcp.py:60,76-78,137-143 — Siril's python-venv bootstrap `log:` lines arrive after `ready` and after the first command's terminal status, contaminating whichever command is in flight; the pre-command drain only removes already-queued lines. Intermittent. angles [1]; also I1,I2. [siril-mcp-20261031-d1bf#11]
- [code] siril_mcp.py:196-202 — `siril_set_directory` validates against the server's cwd but Siril resolves `cd` against its own cwd; `session.workdir` is set before the `cd` succeeds and outside the lock; a raw `siril_command("cd ...")` never updates it, so a reset silently reverts the directory; Siril started with a missing `-d` silently runs with cwd `(null)`. angles [1,2,5,9]; also I1,I2,V1. [siril-mcp-20261031-d1bf#13]
- [code] siril_mcp.py:67-70,76-78,111-112,125-126 — Siril stdout/stderr go to DEVNULL, exit code/signal never read, pre-`ready` lines discarded, reader swallows OSError. Every startup failure or crash shows only "timed out"/"Siril process exited". angles [9]; also V1-V3. [siril-mcp-20261031-d1bf#14]
- [code] siril_mcp.py:226-255 — `siril_preview` docstring says the stretch applies to "a temporary copy"; it actually mutates the live image then restores from a temp FITS snapshot. Afterwards Siril's current filename points at the deleted temp `snap.fit`; undo history and boxselect are lost. Pixel data/bit depth verified preserved at 16/32-bit. angles [1,5,9]; also I1,I2. [siril-mcp-20261031-d1bf#17]
- [code] siril_mcp.py:259-264,80-97 — Cleanup only in `finally`: if the Python process dies (SIGKILL/SIGTERM) an orphan `siril-cli` keeps running and the pipe dir is left behind. No atexit/signal handler. A leftover pipe dir was found on disk during this review. angles [1,2]. [siril-mcp-20261031-d1bf#20]
- [code] siril_mcp.py:241-255 — Default `max_dim=0` returns a full-resolution JPEG, which for tens-of-megapixel frames may exceed MCP/API image limits (untested at scale). The snapshot/reload also runs when nothing mutates the image (`autostretch=False`, `max_dim=0`), and the stretch runs before the shrink. angles [1,8]. [siril-mcp-20261031-d1bf#23]
- [code] siril_mcp.py:34,38,60-78,150-156 — Siril flags (`-p -r -w -d`), `ready`, `exit`, prefixes, `savejpg` appending `.jpg`, `resample -maxdim=` are assumed with no version gate or smoke check ("observed on 1.4.5" but never checked). mcp `>=1.2` lower bound never exercised (only 1.30 tested). angles [9]; also I1,I2,V3. [siril-mcp-20261031-d1bf#25]
- [code] siril_mcp.py:211-217,246,252-255 — The save-format→command map in `siril_save` is hand-typed; `siril_preview` re-hardcodes `save`/`savejpg … 90` and `.jpg` instead of consuming it; preview trusts `status: success` then does a raw `read_bytes()`. Not yet diverged. angles [6]; also I1,V2. [siril-mcp-20261031-d1bf#26]
- [code] siril_mcp.py — No tests for the only source file (`missing_test_sweep.py`); modified today; README admits it. Most defects above are what a fake-pipe-peer test would pin. mechanical sweep. [siril-mcp-20261031-d1bf#29]

## Low / Nit
- [code] siril_mcp.py:73,75,80-97 — `out_fd` never closed; each `start()` leaks 1 fd, 1 blocked daemon thread, 1 queue. Low on exposure: `start()` runs only on reset/crash restart, ~250 restarts to hit the macOS fd limit; re-rate if resets become frequent. angles [1,2,6,7,8,9]. [siril-mcp-20261031-d1bf#02]
- [code] siril_mcp.py:76-78,120-127,146 — Timeout is per received line, not per command; README says "per-command". Not reproduced. angles [1,2,5,9]; also V3. [siril-mcp-20261031-d1bf#08]
- [code] siril_mcp.py:202,208,217,246,252,254 — Paths interpolated as `"{x}"` with no escaping at 6 sites; Siril has no escape for `"`; names with `"` are saved mangled or inject an extra argument. Newlines rejected; `siril_command` already allows arbitrary Siril commands. Low on exposure (names containing `"` are rare). angles [2,5,6,9]; also I1,I2. [siril-mcp-20261031-d1bf#12]
- [code] siril_mcp.py:34,60-75,168 — Missing/non-executable `SIRIL_BIN` raises a raw `FileNotFoundError` that `_run` does not catch; `mkdtemp` dir leaks until the next `stop()`; silent fallback to `/opt/homebrew/bin/siril-cli`. One start path. angles [2,9]; also I1,V2,V3. [siril-mcp-20261031-d1bf#15]
- [code] siril_mcp.py:118 — `os.write` return value unchecked (Unchecked-Result rule). One helper, 2 call sites. Low on exposure. angles [2,5]; also V1-V3. [siril-mcp-20261031-d1bf#16]
- [code] siril_mcp.py:242-243 — "no image loaded" inferred from process liveness; mislabels a dead/never-started process. 1 site. angles [7,9]; also V1-V3. [siril-mcp-20261031-d1bf#18]
- [code] siril_mcp.py:171-173,239 — `status: error` with no `log:` line gives an empty failure reason ("Siril command failed: <cmd>\n"). angles [1]; also I2,V2. [siril-mcp-20261031-d1bf#19]
- [code] siril_mcp.py:84-86 — `proc.kill()` not followed by `wait()` (zombie); the 5s wait holds `session.lock`. Rare path, 1 site. angles [2,8,9]. [siril-mcp-20261031-d1bf#21]
- [code] siril_mcp.py:51,100-114,144-153 — Unbounded queue / log list / line buffer (no truncation flag) and O(n²) `buf.split(b"\n",1)`. Not measured; largest observed output ~240 lines. Low on exposure. angles [8]; also V3. [siril-mcp-20261031-d1bf#22]
- [code] siril_mcp.py:163-175,199 — Synchronous `is_dir()` on the event loop; queued calls park executor threads on `session.lock`. Single user, 1 stat call. angles [8]. [siril-mcp-20261031-d1bf#24]
- [code] siril_mcp.py:100-114 — `_reader` hand-rolls line splitting where `os.fdopen(fd,"rb").readline()` would do; `decode("utf-8","replace")` silently substitutes. Single copy, 1 site. angles [6]; also V1-V3. [siril-mcp-20261031-d1bf#27]
- [code] siril_mcp.py:88,76-78,138-142,116-118,38 — Minor simplification: one-element `for fd in (self.in_fd,)`, duplicated priming call, inline drain loop, `assert` that vanishes under `-O`, status vocabulary split across `TERMINAL_STATUSES` and an inline `"starting"`, unused `workdir` param, thin wrapper tools. angles [7]. [siril-mcp-20261031-d1bf#28]

## Root-cause clusters (for fix planning)
- **F01 + F02 + F14:** one change (open the out FIFO read-only/nonblocking or poll `proc.poll()`, store and close `out_fd`, capture stderr) fixes the dead exit path, the leak and most of the diagnosability gap.
- **F03 + F04 + F09 + F05:** a single locked `run` helper that owns reset-on-error, validates `timeout_s`, separates validation errors from fatal session errors, and correlates the terminal status's `<cmd>` echo; `siril_preview` and `_run` both call it.
- **F06 + F07 + F11:** make the line classifier honest (continuation lines, unknown prefixes counted/surfaced, terminal vocabulary including exit/script).
- **F12 + F13 + F26:** one quoting/command-table helper and one place that applies the working directory.

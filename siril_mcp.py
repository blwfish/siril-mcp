#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.27,<2"]
# ///
"""Minimal MCP server for Siril.

Drives one persistent `siril-cli -p` process over a pair of named pipes, so
the loaded image / sequence survives between tool calls.

Pipe protocol (Siril 1.4, observed; see README):
  out pipe:  "ready" once, then per command
             "status: starting <verb>"        (typed progress marker)
             "log: <text>"                    (free text, returned to the caller)
             <unprefixed text>                (continuation of a multi-line log message)
             "progress: <text>"               (dropped on purpose; nothing branches on it)
             "status: success|error <verb>"   (terminal; closed vocabulary)
  in pipe:   one command per line.
`<verb>` is the command's first word as typed (not its arguments): it is what ties a
status line to the command we sent, so a late status from an earlier command is ignored.
`exit`/`quit` emit "starting" and then the process ends, with no terminal status.

Completion and success/failure come only from the `status:` lines (or process exit);
`log:` text is passed back to the model unparsed, unless it is longer than RETURN_LOG_CHARS: then it is summarised
(see condense_log) and the whole log is saved to a file.
"""
from __future__ import annotations

import asyncio
import atexit
import enum
import math
import os
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image


def _env_number(name: str, default, cast, valid, expected: str):
    """A numeric setting from the environment: `default` if unset or empty, else the value, which must satisfy `valid`.
    A bad value stops the server at start with a message that names it, instead of a bare traceback or a surprise later."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        value = None
    if value is None or not valid(value):
        raise SystemExit(f"{name} must be {expected} (got {raw!r})")
    return value


def _log_dir_from_env() -> Path:
    """SIRIL_MCP_LOG_DIR with `~` expanded and made absolute (an env block in an MCP config is not run through a shell)."""
    return Path(os.environ.get("SIRIL_MCP_LOG_DIR") or "~/.cache/siril-mcp/logs").expanduser().absolute()


SIRIL_BIN = os.environ.get("SIRIL_BIN") or shutil.which("siril-cli") or "/opt/homebrew/bin/siril-cli"
DEFAULT_TIMEOUT = _env_number(
    "SIRIL_MCP_TIMEOUT", 300.0, float, lambda v: math.isfinite(v) and v > 0, "a positive, finite number of seconds"
)
STARTUP_TIMEOUT = 60.0
TESTED_SIRIL = "1.4."  # the pipe protocol above was observed on 1.4.x; other versions get a notice

POLL_S = 0.2  # how often a waiter checks that the Siril process is still alive
MAX_COMMAND_BYTES = 4096  # a command line must fit the pipe buffer so the write cannot block
MAX_LINE_BYTES = 1 << 20  # a "line" longer than this without a newline is split and flagged
MAX_LOG_CHARS = 1_000_000  # returned log text is capped, with a visible marker
MIN_RETURN_LOG_CHARS = 2000  # below this a summary's own headings and notes would not fit inside the limit
RETURN_LOG_CHARS = _env_number(  # a longer log is summarised, not returned whole
    "SIRIL_MCP_LOG_LIMIT", 20000, int, lambda v: v >= MIN_RETURN_LOG_CHARS, f"an integer of at least {MIN_RETURN_LOG_CHARS}"
)
LOG_DIR = _log_dir_from_env()
MAX_LOG_FILES = 50  # saved logs kept in LOG_DIR (this server's own); the oldest are pruned
RARE_LINE_MAX = 3  # a line shape seen at most this often is shown verbatim in a summary
MAX_SHAPES_SHOWN = 15  # most-repeated line shapes listed in a summary
RARE_LINE_CUT = 600  # a verbatim line longer than this is cut, visibly
EXAMPLE_CUT = 160  # likewise the example shown for a repeated kind
TAIL_SHARE = 3  # 1/TAIL_SHARE of the verbatim-line budget goes to the END of the log, where errors and totals are
LOG_VERB_CHARS = 30  # characters of the command verb used in a saved log's file name
BLANK_LINE = "(blank line)"
EXIT_COMMANDS = frozenset({"exit", "quit"})
DEFAULT_PREVIEW_DIM = 2048  # long side of a preview JPEG; keeps it within typical image-size limits


class SirilError(RuntimeError):
    """The session is broken or unresponsive (timeout, process exit, startup/IO failure).

    Callers reset the session on this; the loaded image is lost."""


class InvalidCommand(ValueError):
    """Bad caller input. Nothing was sent to Siril and the session is untouched."""


class Kind(enum.Enum):
    """Kinds of line on Siril's out pipe."""

    LOG = "log"
    STATUS = "status"
    PROGRESS = "progress"
    TEXT = "text"  # no recognised prefix: continuation of a multi-line log message


class Status(enum.Enum):
    STARTING = "starting"
    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True)
class SaveFormat:
    command: str
    extension: str  # what the command appends to the name it is given


# Single source of truth for Siril's save commands; siril_save and siril_preview both consume it.
SAVE_FORMATS = {
    "fits": SaveFormat("save", ".fit"),
    "tiff": SaveFormat("savetif", ".tif"),
    "png": SaveFormat("savepng", ".png"),
    "jpg": SaveFormat("savejpg", ".jpg"),
}


def classify(line: str) -> tuple[Kind, str]:
    for kind in (Kind.LOG, Kind.STATUS, Kind.PROGRESS):
        prefix = kind.value + ":"
        if line == prefix:
            return kind, ""
        if line.startswith(prefix + " "):
            return kind, line[len(prefix) + 1:]
    return Kind.TEXT, line


def _status(word: str) -> Status | None:
    try:
        return Status(word)
    except ValueError:
        return None


def _has_control_chars(text: str) -> bool:
    return any(ord(c) < 32 and c != "\t" for c in text)  # newlines, CR, NUL...: Siril reads C strings, lines


def _validate_command(command: str) -> str:
    command = command.strip()
    if not command or _has_control_chars(command):
        raise InvalidCommand("exactly one single-line Siril command (without control characters) is required")
    if len(command.encode()) > MAX_COMMAND_BYTES:
        raise InvalidCommand(f"command longer than {MAX_COMMAND_BYTES} bytes")
    if command.startswith("@"):
        raise InvalidCommand(
            "script invocation (@file) emits no completion status over the pipe, so the end of the script "
            "cannot be detected; send its commands one at a time"
        )
    return command


def _validate_timeout(timeout: float) -> float:
    if not (math.isfinite(timeout) and timeout > 0):
        raise InvalidCommand(f"timeout must be a positive, finite number of seconds (got {timeout!r})")
    return timeout


def _quote(arg: str, what: str) -> str:
    """Quote a path for a Siril command line. Siril has no escape for `"`, so refuse rather than mangle."""
    if '"' in arg or _has_control_chars(arg):
        raise InvalidCommand(
            f"{what} may not contain a double quote or control character (Siril cannot represent them): {arg!r}"
        )
    return f'"{arg}"'


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _shape(line: str) -> str:
    """A line with its numbers masked, so 'Reading FITS: file a_00031.fit' and '...a_00032.fit' count as one kind."""
    return _NUMBER.sub("N", line)


_OWN_LOG = re.compile(r"\d+-\d{8}-\d{6}-.*\.log")  # what _save_full_log names its files; pruning touches only these
MAX_ERR_CHARS = 200  # an OS error text quoted in a summary header


def _marker(text: str) -> str:
    """A note this server adds to text it returns, as opposed to text that came from Siril."""
    return f"[{text}]"


def _save_full_log(command: str, log: str) -> tuple[Path | None, str | None]:
    """Write the log, exactly as received, under LOG_DIR. Returns (path, None), or (None, reason) when it can't be saved."""
    words = command.split()
    verb = re.sub(r"[^A-Za-z0-9_]", "", words[0] if words else "")[:LOG_VERB_CHARS] or "cmd"
    stamp = f"{time.time_ns()}-{time.strftime('%Y%m%d-%H%M%S')}"  # the nanosecond prefix keeps names in time order
    try:
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=LOG_DIR, prefix=f"{stamp}-{verb}-", suffix=".log")
    except OSError as e:
        return None, str(e)[:MAX_ERR_CHARS]
    try:
        with open(fd, "w", encoding="utf-8", errors="replace") as fh:  # takes over (and closes) fd
            fh.write(log)
    except OSError as e:  # e.g. disk full: do not leave a partial file that would pass for a log
        try:
            os.unlink(name)
        except OSError:
            pass  # nothing more to do; the failure is reported either way
        return None, str(e)[:MAX_ERR_CHARS]
    return Path(name), None


def _prune_logs(keep: Path) -> int:
    """Delete this server's oldest saved logs beyond MAX_LOG_FILES, counting `keep` (the one just written, which is
    never deleted). Files in LOG_DIR that _save_full_log did not name are left alone. Returns how many deletions failed."""
    try:
        old = sorted(p for p in LOG_DIR.glob("*.log") if p != keep and _OWN_LOG.fullmatch(p.name))
    except OSError:
        return 1
    failed = 0
    for path in old[: max(0, len(old) - (MAX_LOG_FILES - 1))]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # another server pruned it first
        except OSError:
            failed += 1
    return failed


def _cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else f"{line[:limit]} ...{_marker(f'line cut, {len(line)} characters')}"


def _pick_rare(items: list[str], budget: int) -> tuple[list[str], list[str]]:
    """The verbatim lines to show, as (start, end) of `items`: all of them if they fit in `budget` characters (an
    item costs its length plus 3 for the indent and newline), otherwise a run from each end, 1/TAIL_SHARE of the
    budget for the end. The end is where Siril's error and closing totals, and this server's own end-of-log notes, are."""
    cost = [len(item) + 3 for item in items]
    if sum(cost) <= budget:
        return items, []
    tail_budget = max(0, budget) // TAIL_SHARE
    head_budget = max(0, budget) - tail_budget
    head = tail = used = 0
    while head < len(items) and used + cost[head] <= head_budget:
        used += cost[head]
        head += 1
    used = 0
    while tail < len(items) - head and used + cost[-1 - tail] <= tail_budget:
        used += cost[-1 - tail]
        tail += 1
    return items[:head], items[len(items) - tail:]


def condense_log(command: str, log: str) -> str:
    """`log` unchanged if it is short; otherwise a summary, at most RETURN_LOG_CHARS long, that says it is one.

    The summary shows every line shape seen at most RARE_LINE_MAX times, verbatim and in order (a warning, an error or
    the closing totals are rare by nature), and collapses everything else into 'N x first example'. When the rare lines
    don't fit it keeps those from both ends of the log and says how many it left out of the middle. It never decides what
    a line means. The log is saved to a file whose path is in the first line."""
    if len(log) <= RETURN_LOG_CHARS:
        return log
    limit = RETURN_LOG_CHARS
    lines = log.split("\n")
    shapes = [_shape(ln) for ln in lines]
    counts = Counter(shapes)
    first: dict[str, str] = {}
    for ln, sh in zip(lines, shapes):
        first.setdefault(sh, ln)
    path, err = _save_full_log(command, log)
    if path:
        pruned_failed = _prune_logs(path)
        where = f"log saved to {path}" + (f"; could not delete {pruned_failed} old log(s) in {LOG_DIR}" if pruned_failed else "")
    else:
        where = f"log could NOT be saved ({err})"
    header = _marker(f"log shortened: {len(lines)} lines, {len(log)} characters; {where}")
    rare_heading = f"Lines seen at most {RARE_LINE_MAX} times (verbatim, in order):"
    repeated_heading = "Repeated lines (count x first example; lines that differ only in their numbers are grouped):"

    frequent = [(sh, n) for sh, n in counts.most_common() if n > RARE_LINE_MAX]
    repeated: list[str] = []
    repeated_chars = 0
    for sh, n in frequent[:MAX_SHAPES_SHOWN]:
        entry = f"  {n:>5}x  {_cut(first[sh] or BLANK_LINE, EXAMPLE_CUT)}"
        if repeated_chars + len(entry) + 1 > limit // 3:  # the repeated kinds may not crowd out the rare lines
            break
        repeated.append(entry)
        repeated_chars += len(entry) + 1
    more_kinds = len(frequent) - len(repeated)
    kinds_note = _marker(f"... {more_kinds} more repeated kind(s) not listed; see the saved log") if more_kinds else ""

    rare = [_cut(ln or BLANK_LINE, RARE_LINE_CUT) for ln, sh in zip(lines, shapes) if counts[sh] <= RARE_LINE_MAX]
    gap_note = _marker(f"... {len(rare)} more such line(s) omitted from the middle to keep this short; see the saved log")
    fixed = [header, rare_heading if rare else "", gap_note if rare else "", repeated_heading if repeated else "", kinds_note]
    budget = limit - sum(len(s) + 1 for s in fixed) - repeated_chars
    head, tail = _pick_rare(rare, budget)
    omitted = len(rare) - len(head) - len(tail)

    out = [header]
    if rare:
        out.append(rare_heading)
        out.extend(f"  {item}" for item in head)
        if omitted:
            out.append(_marker(f"... {omitted} more such line(s) omitted from the middle to keep this short; see the saved log"))
        out.extend(f"  {item}" for item in tail)
    if repeated:
        out.append(repeated_heading)
        out.extend(repeated)
        if more_kinds:
            out.append(kinds_note)
    return "\n".join(out)


def _condensed(command: str, log: str) -> str:
    """condense_log(), but a bug in it must not discard the result of a command that has already run: fall back to the
    start and end of the log, with a note saying why."""
    try:
        return condense_log(command, log)
    except Exception as e:  # noqa: BLE001 - deliberately broad: whatever went wrong, the caller still needs the log
        half = max(0, RETURN_LOG_CHARS - 300) // 2
        return "\n".join([
            _marker(f"log summary failed ({type(e).__name__}: {e}); showing only the first and last {half} characters"),
            log[:half],
            _marker(f"... {max(0, len(log) - 2 * half)} characters omitted ..."),
            log[len(log) - half:],
        ])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):  # not a real pid: leave that directory alone
        return True
    return True


def _reap_stale_dirs() -> None:
    """Remove pipe dirs, and kill their Siril, left by server processes that died without cleaning up.

    Directory names are siril-mcp-<server pid>-<random>; a dir whose server pid is gone is stale. The
    recorded Siril pid is only killed if `ps` shows a command line that mentions that dir (pid reuse guard)."""
    tmp = Path(tempfile.gettempdir())
    for d in tmp.glob("siril-mcp-*"):
        parts = d.name.split("-", 3)
        if len(parts) != 4 or not (parts[2].isascii() and parts[2].isdigit()):
            continue
        server_pid = int(parts[2])
        if server_pid == os.getpid() or _pid_alive(server_pid):
            continue
        try:
            siril_pid = int((d / "siril.pid").read_text().strip())
        except (OSError, ValueError, OverflowError):
            siril_pid = None
        if siril_pid is not None:
            try:
                ps = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(siril_pid)], capture_output=True, text=True, timeout=5, check=False
                )
                if ps.returncode == 0 and str(d) in ps.stdout:
                    os.killpg(siril_pid, signal.SIGKILL)
            except (OSError, OverflowError, subprocess.TimeoutExpired):
                pass  # best effort: the dir is still removed below
        shutil.rmtree(d, ignore_errors=True)


class _ReaderFailed:
    """Queued by the reader thread when the out pipe breaks, so waiters fail fast with the cause."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class SirilSession:
    """One siril-cli process plus its command/log pipes. Not thread-safe; callers hold `lock`."""

    def __init__(self) -> None:
        self.workdir = os.getcwd()  # tracked from `cd`; used as -d when the session (re)starts
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str | _ReaderFailed] = queue.Queue()
        self.pipe_dir: str | None = None
        self.in_fd: int | None = None
        self.notices: list[str] = []  # one-shot messages prepended to the next command's result
        self.startup_lines: list[str] = []
        self._reader_stop: threading.Event | None = None
        self._reader_thread: threading.Thread | None = None
        self._version_checked = False
        self.lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.stop()
        _reap_stale_dirs()
        if not os.path.isdir(self.workdir):
            fallback = os.getcwd()
            self.notices.append(f"[saved working directory {self.workdir} no longer exists; Siril started in {fallback}]")
            self.workdir = fallback
        if not self._version_checked:
            self._version_checked = True
            self._check_version()
        try:
            self._spawn()
            self._await_ready()
        except OSError as e:
            self.stop()
            raise SirilError(f"could not start {SIRIL_BIN}: {e}") from e
        except SirilError:
            self.stop()
            raise

    def _check_version(self) -> None:
        try:
            r = subprocess.run([SIRIL_BIN, "--version"], capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.notices.append(f"[could not determine the Siril version: {e}]")
            return
        version = r.stdout.strip()
        if r.returncode != 0 or not version.startswith(f"siril {TESTED_SIRIL}"):
            self.notices.append(
                f"[Siril reports {version or 'no version'!r} (exit {r.returncode}); "
                f"this server's pipe protocol was observed on {TESTED_SIRIL}x]"
            )

    def _spawn(self) -> None:
        self.pipe_dir = tempfile.mkdtemp(prefix=f"siril-mcp-{os.getpid()}-")
        in_path = os.path.join(self.pipe_dir, "in")
        out_path = os.path.join(self.pipe_dir, "out")
        os.mkfifo(in_path)
        os.mkfifo(out_path)
        with open(os.path.join(self.pipe_dir, "siril.out"), "wb") as console:  # stdout+stderr, for diagnostics
            self.proc = subprocess.Popen(
                [SIRIL_BIN, "-p", "-r", in_path, "-w", out_path, "-d", self.workdir],
                stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group, so a hung Siril and its children can be killed together
            )
        Path(self.pipe_dir, "siril.pid").write_text(str(self.proc.pid))
        # O_RDWR never blocks on a FIFO; siril opens the other ends itself. Because we hold a write end of the
        # out pipe ourselves the reader never sees EOF, so process death is detected via proc.poll() instead.
        self.in_fd = os.open(in_path, os.O_RDWR)
        out_fd = os.open(out_path, os.O_RDWR)
        self.lines = queue.Queue()
        self._reader_stop = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader, args=(out_fd, self.lines, self._reader_stop), daemon=True
        )
        self._reader_thread.start()

    def _await_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        self.startup_lines = []
        while True:
            try:
                line = self._next_line(max(0.0, deadline - time.monotonic()))
            except SirilError as e:
                shown = "".join(f"\n  {s}" for s in self.startup_lines[-10:])
                raise SirilError(f"{e} during startup{(' (output before failure:' + shown + ')') if shown else ''}") from e
            if line == "ready":
                return
            self.startup_lines.append(line)

    def stop(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self._write("exit")
                    self.proc.wait(timeout=5)
                except (SirilError, subprocess.TimeoutExpired):
                    self._kill()
            self.proc = None
        if self._reader_stop is not None:
            self._reader_stop.set()
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2 * POLL_S + 1)
            self._reader_stop = self._reader_thread = None
        if self.in_fd is not None:
            try:
                os.close(self.in_fd)
            except OSError:
                pass
            self.in_fd = None
        if self.pipe_dir:
            shutil.rmtree(self.pipe_dir, ignore_errors=True)
            self.pipe_dir = None

    def abort(self) -> None:
        """Kill Siril now, from any thread, without touching session state.

        The thread waiting on it sees the process die, fails its command and resets the session itself."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def _kill(self) -> None:
        assert self.proc is not None
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)  # Siril and anything it spawned (starnet...)
        except OSError:
            self.proc.kill()
        self.proc.wait()  # reap it

    # -- pipes -------------------------------------------------------------
    @staticmethod
    def _reader(fd: int, q: queue.Queue[str | _ReaderFailed], stop: threading.Event) -> None:
        buf = b""
        try:
            while not stop.is_set():
                ready, _, _ = select.select([fd], [], [], POLL_S)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise OSError("out pipe closed")
                *complete, buf = (buf + chunk).split(b"\n")
                for raw in complete:
                    q.put(raw.decode("utf-8", "replace").rstrip("\r"))
                if len(buf) > MAX_LINE_BYTES:
                    q.put(buf.decode("utf-8", "replace") + " " + _marker(f"line split: exceeded {MAX_LINE_BYTES} bytes"))
                    buf = b""
        except (OSError, ValueError) as e:  # ValueError: select on a closed fd
            if not stop.is_set():
                q.put(_ReaderFailed(e))
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _write(self, text: str) -> None:
        if self.in_fd is None:
            raise SirilError("Siril is not running")
        view = memoryview((text + "\n").encode())
        try:
            while view:
                view = view[os.write(self.in_fd, view):]
        except OSError as e:
            raise SirilError(f"could not send the command to Siril: {e}") from e

    def _console_tail(self) -> str:
        if not self.pipe_dir:
            return ""
        try:
            with open(os.path.join(self.pipe_dir, "siril.out"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 2000))
                tail = f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""
        return f"\n[Siril console output: {tail}]" if tail else ""

    def _next_line(self, timeout: float) -> str:
        """Next line from Siril, waiting up to `timeout` seconds. Raises SirilError on timeout or process exit."""
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            try:
                item = self.lines.get(timeout=max(0.0, min(POLL_S, remaining)))
            except queue.Empty:
                item = None
            if item is not None:
                if isinstance(item, _ReaderFailed):
                    raise SirilError(f"lost the Siril output pipe: {item.exc}")
                return item
            if self.proc is None or self.proc.poll() is not None:
                code = None if self.proc is None else self.proc.returncode
                try:  # whatever Siril wrote just before exiting is already queued
                    return self.lines.get(timeout=POLL_S)  # type: ignore[return-value]
                except queue.Empty:
                    raise SirilError(f"Siril process exited (code {code}){self._console_tail()}") from None
            if remaining <= 0:
                raise SirilError(f"no output from Siril for {timeout:g}s{self._console_tail()}")

    def _drain(self) -> None:
        """Drop stale output queued before this command. A reader failure is not stale: raise it."""
        while True:
            try:
                item = self.lines.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _ReaderFailed):
                raise SirilError(f"lost the Siril output pipe: {item.exc}")

    # -- commands ----------------------------------------------------------
    def run(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
        """Run one Siril command. Returns (ok, log_text).

        `timeout` is the longest Siril may stay silent (any line of output, including progress, resets it),
        not a limit on total run time. Raises InvalidCommand for bad input (nothing is sent) and SirilError
        when the session is broken; run_checked() turns the latter into a session reset."""
        command = _validate_command(command)
        timeout = _validate_timeout(timeout)
        verb = command.split(None, 1)[0]
        if verb in EXIT_COMMANDS and not self.alive():
            self.stop()  # release a dead process's pipes and reader
            return True, "Siril is not running"
        if not self.alive():
            if self.proc is not None:  # it died on us (crash, kill, OOM), as opposed to a deliberate stop()
                self.notices.append(
                    f"[Siril had exited (code {self.proc.returncode}); started a new session, so the previous image is gone]"
                )
            self.start()
        self._drain()  # anything still queued belongs to an earlier command
        self._write(command)
        if verb in EXIT_COMMANDS:
            return self._finish_exit(timeout)
        ok, log = self._collect(verb, timeout)
        if ok and verb == "cd":
            self._track_cd(command)
        return ok, log

    def _finish_exit(self, timeout: float) -> tuple[bool, str]:
        assert self.proc is not None
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SirilError(f"Siril did not exit within {timeout:g}s") from None
        code = self.proc.returncode
        self.stop()
        return code == 0, f"Siril exited (code {code})"

    def _collect(self, verb: str, timeout: float) -> tuple[bool, str]:
        logs: list[str] = []
        size = truncated = stale = 0
        started = False
        while True:
            kind, text = classify(self._next_line(timeout))
            if kind is Kind.PROGRESS:
                continue  # dropped on purpose; nothing branches on it
            if kind is Kind.STATUS:
                word, _, echoed = text.partition(" ")
                if echoed != verb:  # a status for some other command: a late reply from an earlier one
                    stale += 1
                    continue
                status = _status(word)
                if status is Status.STARTING:
                    started = True
                    continue
                if status is None:  # outside the closed vocabulary: fail fast rather than wait out the timeout
                    logs.append(f"[unrecognised terminal status {word!r}]")
                return status is Status.SUCCESS, self._result(logs, stale, truncated)
            if not started:
                continue  # output of an earlier command, or Siril's asynchronous python bootstrap
            if truncated or size + len(text) + 1 > MAX_LOG_CHARS:  # once capped, drop everything after (no gaps)
                truncated += 1
                continue
            size += len(text) + 1
            logs.append(text)

    def _result(self, logs: list[str], stale: int, truncated: int) -> str:
        out = self.notices + logs
        self.notices = []
        if stale:
            out.append(f"[ignored {stale} stale status line(s) from earlier commands]")
        if truncated:
            out.append(_marker(f"output truncated: {truncated} further line(s) dropped after {MAX_LOG_CHARS} characters"))
        return "\n".join(out)

    def _track_cd(self, command: str) -> None:
        """Mirror a successful `cd` into workdir, resolving the argument as Siril does (no Siril output is parsed)."""
        try:
            args = shlex.split(command)[1:]
        except ValueError:
            return
        if len(args) == 1:
            self.workdir = os.path.normpath(os.path.join(self.workdir, os.path.expanduser(args[0])))

    def run_checked(
        self, command: str, timeout: float | None = None, label: str = "Siril command", condense: bool = True
    ) -> str:
        """run(), but a failure raises RuntimeError. Caller holds `lock`.

        A log longer than RETURN_LOG_CHARS is summarised (and saved to a file), in the result and in the error text
        alike, unless `condense` is False (only for output whose size does not depend on the caller's data).

        A SirilError (timeout, process exit, IO failure) also resets the session: Siril may still be running the
        command, so nothing sent next could be trusted. InvalidCommand propagates unchanged, session intact."""
        timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        try:
            ok, log = self.run(command, timeout)
        except SirilError as e:
            self.stop()
            raise RuntimeError(f"{label}: {command!r}: {e} (session reset)") from e
        if condense:
            log = _condensed(command, log)
        if not ok:
            raise RuntimeError(f"{label} failed: {command}\n{log or '(Siril gave no reason)'}")
        return log


session = SirilSession()
LOG_NOTE = (
    f"A log longer than {RETURN_LOG_CHARS:,} characters comes back as a summary, not whole: it begins "
    "'[log shortened: ...; log saved to <path>]' (on a failed command that is the second line, after the failure line), "
    "lists the lines that occur only a few times verbatim, counts the commonly repeated ones, and says what it left out. "
    "The path is a text file with the whole log."
)
mcp = FastMCP("siril", instructions=LOG_NOTE)
_gate = asyncio.Lock()  # one tool call at a time, queued on the event loop rather than parked on threads


async def _in_session(fn):
    async with _gate:
        try:
            return await asyncio.to_thread(fn)
        except asyncio.CancelledError:
            # The client went away or cancelled the call. The worker thread cannot be interrupted and the event
            # loop will not finish until it does, so end the Siril it is waiting on.
            session.abort()
            raise


async def _run(command: str, timeout: float | None = None, condense: bool = True) -> str:
    def work() -> str:
        with session.lock:
            return session.run_checked(command, timeout, condense=condense)

    return await _in_session(work)


def _documented(fn):
    """Fill the `{log_note}` placeholder in a tool's docstring, so the limit it quotes is the one in force."""
    fn.__doc__ = fn.__doc__.format(log_note=LOG_NOTE)
    return fn


@mcp.tool()
@_documented
async def siril_command(command: str, timeout_s: float = DEFAULT_TIMEOUT) -> str:
    """Run one raw Siril command (e.g. "stat", "autostretch", "rmgreen", "starnet").

    State persists across calls: `load` an image, process it, then `save`.
    Use siril_help to list commands or see one command's syntax.
    Paths are relative to the working directory (see siril_set_directory).
    timeout_s is how long Siril may stay silent before the call is abandoned (and the session reset,
    losing the loaded image); any output, including progress, restarts the clock.
    `exit`/`quit` end the Siril process; the next command starts a fresh one. `@script` is not supported.
    {log_note}
    """
    return await _run(command, timeout_s)


@mcp.tool()
async def siril_help(command: str = "") -> str:
    """List all Siril commands, or show the syntax of one (e.g. command="autostretch")."""
    return await _run(f"help {command}".strip(), condense=False)  # a fixed ~11,000-character catalogue: show it whole


@mcp.tool()
async def siril_set_directory(path: str) -> str:
    """Set Siril's working directory (where load/save/convert resolve relative paths).

    A relative `path` is resolved against the current working directory."""

    def work() -> str:
        with session.lock:
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = Path(session.workdir) / p
            p = Path(os.path.normpath(p))
            if not p.is_dir():
                raise InvalidCommand(f"not a directory: {p}")
            return session.run_checked(f"cd {_quote(str(p), 'path')}")  # run() tracks the new workdir

    return await _in_session(work)


@mcp.tool()
async def siril_load(file: str) -> str:
    """Load an image (FITS, TIFF, PNG, JPEG, RAW...) as the current image."""
    return await _run(f"load {_quote(file, 'file')}")


@mcp.tool()
async def siril_save(file: str, format: str = "fits") -> str:
    """Save the current image. format: fits | tiff | png | jpg. `file` has no extension."""
    fmt = SAVE_FORMATS.get(format)
    if fmt is None:
        raise InvalidCommand(f"format must be one of: {', '.join(SAVE_FORMATS)}")
    return await _run(f"{fmt.command} {_quote(file, 'file')}")


@mcp.tool()
async def siril_stats() -> str:
    """Per-channel statistics (mean, median, sigma, min, max...) of the current image."""
    return await _run("stat")


@mcp.tool()
async def siril_preview(autostretch: bool = True, max_dim: int = DEFAULT_PREVIEW_DIM) -> Image:
    """Render the current image as a JPEG you can look at.

    Linear astro data is nearly black unless stretched, so autostretch defaults on.
    max_dim > 0 resamples the longest side to that many pixels (default 2048; this also ENLARGES smaller
    images; 0 = full size, which can be too large to return). The stretch/resize is applied to the loaded image, which is then restored by
    reloading a temporary snapshot of it: pixel data is preserved, but afterwards Siril's current-file
    name points at that snapshot and its undo history and any selection are gone. With autostretch off
    and max_dim 0 nothing is modified, so nothing is snapshotted or restored.
    """

    def work() -> bytes:
        fits, jpg = SAVE_FORMATS["fits"], SAVE_FORMATS["jpg"]
        mutates = autostretch or max_dim > 0
        with tempfile.TemporaryDirectory(prefix="siril-preview-") as td, session.lock:
            if not session.alive():
                raise RuntimeError("no Siril session is running: call siril_load first")
            snap = _quote(os.path.join(td, "snap"), "path")
            out_path = os.path.join(td, "preview")

            def step(cmd: str, what: str) -> None:
                session.run_checked(cmd, label=f"preview {what}")

            if mutates:
                step(f"{fits.command} {snap}", "snapshot (is an image loaded? try siril_load)")
            try:
                if autostretch:
                    step("autostretch", "stretch")
                if max_dim > 0:
                    step(f"resample -maxdim={max_dim}", "resize")
                step(f"{jpg.command} {_quote(out_path, 'path')} 90", "export")
            except RuntimeError as err:
                if mutates and session.alive():  # a timeout resets the session; there is nothing to restore into
                    try:
                        step(f"load {snap}", "restore")
                    except RuntimeError as restore_err:
                        raise RuntimeError(f"{err}\nand restoring the original image also failed: {restore_err}") from err
                raise
            if mutates:
                step(f"load {snap}", "restore")
            produced = Path(out_path + jpg.extension)
            if not produced.is_file():
                raise RuntimeError(f"Siril reported success but wrote no file at {produced}")
            return produced.read_bytes()

    return Image(data=await _in_session(work), format="jpeg")


def _shutdown(signum: int, _frame) -> None:
    session.abort()  # so stop() does not wait out a busy Siril
    session.stop()
    # Not SystemExit: that would wait for non-daemon threads (anyio's stdin reader, an in-flight command).
    os._exit(128 + signum)


if __name__ == "__main__":
    atexit.register(session.stop)
    for _sig in (signal.SIGTERM, signal.SIGHUP):  # SIGKILL cannot be caught; _reap_stale_dirs cleans up after it
        signal.signal(_sig, _shutdown)
    try:
        mcp.run()
    finally:
        session.stop()  # stdin closed: end Siril now, not after an in-flight command finishes

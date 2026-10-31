#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""Minimal MCP server for Siril.

Drives one persistent `siril-cli -p` process over a pair of named pipes, so
the loaded image / sequence survives between tool calls.

Pipe protocol (Siril 1.4, observed):
  out pipe:  "ready" once, then per command
             "status: starting <cmd>"   (typed progress marker)
             "log: <text>"              (free text, returned to the caller whole)
             "status: success|error <cmd>"  (terminal; closed vocabulary)
  in pipe:   one command per line.

Completion and success/failure come only from the `status:` lines; `log:` text
is passed back to the model unparsed.
"""
from __future__ import annotations

import asyncio
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

SIRIL_BIN = os.environ.get("SIRIL_BIN") or shutil.which("siril-cli") or "/opt/homebrew/bin/siril-cli"
DEFAULT_TIMEOUT = float(os.environ.get("SIRIL_MCP_TIMEOUT", "300"))
STARTUP_TIMEOUT = 60.0

TERMINAL_STATUSES = {"success", "error"}


class SirilError(RuntimeError):
    pass


class SirilSession:
    """One siril-cli process plus its command/log pipes. Not thread-safe; callers hold `lock`."""

    def __init__(self, workdir: str | None = None) -> None:
        self.workdir = workdir or os.getcwd()
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.pipe_dir: str | None = None
        self.in_fd: int | None = None
        self.lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.stop()
        self.pipe_dir = tempfile.mkdtemp(prefix="siril-mcp-")
        in_path = os.path.join(self.pipe_dir, "in")
        out_path = os.path.join(self.pipe_dir, "out")
        os.mkfifo(in_path)
        os.mkfifo(out_path)
        self.proc = subprocess.Popen(
            [SIRIL_BIN, "-p", "-r", in_path, "-w", out_path, "-d", self.workdir],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # O_RDWR never blocks on a FIFO; siril opens the other ends itself.
        self.in_fd = os.open(in_path, os.O_RDWR)
        out_fd = os.open(out_path, os.O_RDWR)
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, args=(out_fd, self.lines), daemon=True).start()
        first = self._next_line(STARTUP_TIMEOUT)
        while first != "ready":
            first = self._next_line(STARTUP_TIMEOUT)

    def stop(self) -> None:
        if self.alive():
            try:
                self._write("exit")
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.proc = None
        for fd in (self.in_fd,):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.in_fd = None
        if self.pipe_dir:
            shutil.rmtree(self.pipe_dir, ignore_errors=True)
            self.pipe_dir = None

    @staticmethod
    def _reader(fd: int, q: queue.Queue[str | None]) -> None:
        buf = b""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    q.put(line.decode("utf-8", "replace").rstrip("\r"))
        except OSError:
            pass
        finally:
            q.put(None)

    def _write(self, text: str) -> None:
        assert self.in_fd is not None
        os.write(self.in_fd, (text + "\n").encode())

    def _next_line(self, timeout: float) -> str:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise SirilError(f"timed out after {timeout:.0f}s waiting for Siril")
        if line is None:
            raise SirilError("Siril process exited")
        return line

    # -- commands ----------------------------------------------------------
    def run(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
        """Run one Siril command. Returns (ok, log_text)."""
        command = command.strip()
        if not command or "\n" in command or "\r" in command:
            raise SirilError("exactly one single-line Siril command is required")
        if not self.alive():
            self.start()
        # Drop anything stale (e.g. output of a previous command that timed out).
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                break
        self._write(command)
        logs: list[str] = []
        while True:
            line = self._next_line(timeout)
            kind, _, rest = line.partition(": ")
            if kind == "log":
                logs.append(rest)
            elif kind == "status":
                status = rest.split(" ", 1)[0]
                if status in TERMINAL_STATUSES:
                    return status == "success", "\n".join(logs)
                if status != "starting":
                    logs.append(f"[unrecognised status line] {line}")
            # other kinds (e.g. "progress") are dropped on purpose; nothing branches on them


session = SirilSession()
mcp = FastMCP("siril")


async def _run(command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    def work() -> str:
        with session.lock:
            try:
                ok, log = session.run(command, timeout)
            except SirilError as e:
                session.stop()  # a timed-out command leaves Siril in an unknown state
                raise RuntimeError(f"{command!r}: {e} (session reset)") from e
        if not ok:
            raise RuntimeError(f"Siril command failed: {command}\n{log}")
        return log

    return await asyncio.to_thread(work)


@mcp.tool()
async def siril_command(command: str, timeout_s: float = DEFAULT_TIMEOUT) -> str:
    """Run one raw Siril command (e.g. "stat", "autostretch", "rmgreen", "starnet").

    State persists across calls: `load` an image, process it, then `save`.
    Use siril_help to list commands or see one command's syntax.
    Paths are relative to the working directory (see siril_set_directory).
    """
    return await _run(command, timeout_s)


@mcp.tool()
async def siril_help(command: str = "") -> str:
    """List all Siril commands, or show the syntax of one (e.g. command="autostretch")."""
    return await _run(f"help {command}".strip(), 30)


@mcp.tool()
async def siril_set_directory(path: str) -> str:
    """Set Siril's working directory (where load/save/convert resolve relative paths)."""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise RuntimeError(f"not a directory: {p}")
    session.workdir = str(p)  # used if the session has to be restarted
    return await _run(f'cd "{p}"', 30)


@mcp.tool()
async def siril_load(file: str) -> str:
    """Load an image (FITS, TIFF, PNG, JPEG, RAW...) as the current image."""
    return await _run(f'load "{file}"', 120)


@mcp.tool()
async def siril_save(file: str, format: str = "fits") -> str:
    """Save the current image. format: fits | tiff | png | jpg. `file` has no extension."""
    cmd = {"fits": "save", "tiff": "savetif", "png": "savepng", "jpg": "savejpg"}.get(format)
    if cmd is None:
        raise RuntimeError("format must be one of: fits, tiff, png, jpg")
    return await _run(f'{cmd} "{file}"', 120)


@mcp.tool()
async def siril_stats() -> str:
    """Per-channel statistics (mean, median, sigma, min, max...) of the current image."""
    return await _run("stat", 60)


@mcp.tool()
async def siril_preview(autostretch: bool = True, max_dim: int = 0) -> Image:
    """Render the current image as a JPEG you can look at.

    Linear astro data is nearly black unless stretched, so autostretch defaults on.
    max_dim > 0 resamples the longest side to that many pixels (0 = full size).
    The stretch/resize is applied to a temporary copy, then the original is reloaded.
    """

    def work() -> bytes:
        def step(cmd: str, what: str) -> None:
            ok, log = session.run(cmd, 120)
            if not ok:
                raise RuntimeError(f"preview {what} failed ({cmd}):\n{log}")

        with tempfile.TemporaryDirectory(prefix="siril-preview-") as td, session.lock:
            if not session.alive():
                raise RuntimeError("no image loaded: call siril_load first")
            snap = os.path.join(td, "snap")
            out = os.path.join(td, "preview")
            step(f'save "{snap}"', "snapshot (is an image loaded? try siril_load)")
            try:
                if autostretch:
                    step("autostretch", "stretch")
                if max_dim > 0:
                    step(f"resample -maxdim={max_dim}", "resize")
                step(f'savejpg "{out}" 90', "export")
            finally:
                step(f'load "{snap}"', "restore")
            return Path(out + ".jpg").read_bytes()

    return Image(data=await asyncio.to_thread(work), format="jpeg")


if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        session.stop()

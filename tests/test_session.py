"""SirilSession / _run against the fake Siril (tests/fake_siril.py).

Each test group pins a behaviour that was found wrong or ambiguous in review siril-mcp-20261031-d1bf;
the finding id is in the comment so a failing test can be traced back to why it exists.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import FAKE_SIRIL, logged

import siril_mcp
from siril_mcp import MAX_COMMAND_BYTES, InvalidCommand, SirilError, _ReaderFailed, _run

# -- protocol parsing ------------------------------------------------------


def test_success_returns_log_text(session):
    assert session.run("echo hi there", 5) == (True, "hi there")


def test_error_status_returns_false_with_empty_reason(session):
    assert session.run("fail", 5) == (False, "")  # Siril gives no reason here (F19)


def test_error_status_keeps_log_text(session):
    assert session.run("failwithlog", 5) == (False, "boom")


def test_progress_prefix_is_dropped_on_purpose(session):
    assert session.run("progress", 5) == (True, "done")


def test_bare_log_line_is_kept_as_an_empty_line(session):
    # F06: "log:" with no trailing space used to be dropped silently.
    assert session.run("bare", 5) == (True, "\nafter")


def test_continuation_lines_are_kept(session):
    # F06: multi-line log messages used to lose every line after the first.
    assert session.run("multi", 5) == (True, "line1\ncontinuation line 2\nline3")


def test_line_split_across_reads_is_reassembled(session):
    assert session.run("split", 5) == (True, "hello")


def test_invalid_utf8_is_replaced_not_fatal(session):
    assert session.run("badbytes", 5) == (True, "caf�")


def test_status_for_another_command_is_ignored_and_reported(session):
    # F04: only a status that echoes the command we sent can complete it.
    ok, log = session.run("weird", 5)
    assert ok is True
    assert log == "after\n[ignored 1 stale status line(s) from earlier commands]"


def test_unknown_terminal_status_fails_fast_instead_of_hanging(session):
    # F07: a status word outside the vocabulary that echoes our command is terminal (and not a success).
    start = time.monotonic()
    assert session.run("weirdterm", 30) == (False, "[unrecognised terminal status 'cancelled']")
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("cap, expected", [
    (100, "line1\ncontinuation line 2\nline3"),  # above: everything fits
    (6, "line1\n[output truncated: 2 further line(s) dropped after 6 characters]"),  # at: 'line1\n' exactly fits
    (5, "[output truncated: 3 further line(s) dropped after 5 characters]"),  # below: not even the first line
    # Monotonic: line3 (6 chars) would fit in what is left of 20, but must not follow a dropped line (no gaps).
    (20, "line1\n[output truncated: 2 further line(s) dropped after 20 characters]"),
])
def test_output_cap_boundary(session, monkeypatch, cap, expected):
    # F22: the returned text is bounded and says so.
    monkeypatch.setattr(siril_mcp, "MAX_LOG_CHARS", cap)
    assert session.run("multi", 5) == (True, expected)


@pytest.mark.parametrize("cap, split", [(51, False), (50, False), (49, True)])
def test_line_length_boundary(session, monkeypatch, cap, split):
    # 'partial' leaves exactly 50 bytes ("log: " + 45 x) unterminated for a moment.
    monkeypatch.setattr(siril_mcp, "MAX_LINE_BYTES", cap)
    ok, log = session.run("partial", 5)
    assert ok is True
    assert (f"[line split: exceeded {cap} bytes]" in log) is split


def test_reader_failure_is_reported_with_its_cause(session):
    session.run("echo x", 5)
    # Feed the failure straight into the queue: the run() below drains first, so write it after the command.
    real_write = session._write

    def write_then_fail(text):
        real_write(text)
        session.lines.put(_ReaderFailed(OSError("pipe broke")))

    session._write = write_then_fail
    with pytest.raises(SirilError, match="lost the Siril output pipe: pipe broke"):
        session.run("late", 5)


def test_stale_terminal_status_queued_before_the_command_is_discarded(session):
    # Pins _drain: without it, a leftover "status: success echo" would complete the next `echo` with no output.
    session.run("echo x", 5)
    session.lines.put("status: success echo")
    assert session.run("echo y", 5) == (True, "y")


def test_reader_failure_while_idle_is_not_swallowed_by_the_drain(session):
    session.run("echo x", 5)
    session.lines.put(_ReaderFailed(OSError("died while idle")))
    with pytest.raises(SirilError, match="died while idle"):
        session.run("echo y", 5)


# -- input validation (F05, F09, F12) ----------------------------------------


@pytest.mark.parametrize("command", ["", " ", "\n", "a\nb", "a\rb", "a\x00b", "a\x01b", "@script.ssf", " @script.ssf"])
def test_invalid_commands_raise_invalidcommand_without_starting_siril(session, command, command_log):
    with pytest.raises(InvalidCommand):
        session.run(command, 5)
    assert not session.alive()
    assert logged(command_log) == []


def test_command_length_boundary(session):
    at_limit = "echo " + "a" * (MAX_COMMAND_BYTES - len("echo "))
    assert session.run(at_limit, 5)[0] is True
    with pytest.raises(InvalidCommand, match="longer than"):
        session.run(at_limit + "a", 5)


def test_surrounding_whitespace_is_stripped_not_rejected(session, command_log):
    assert session.run("echo x\n", 5) == (True, "x")
    assert logged(command_log) == ["echo x"]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_bad_timeouts_are_rejected_before_anything_is_sent(session, timeout, command_log):
    # F09: a negative timeout used to raise ValueError after the command had already been written.
    with pytest.raises(InvalidCommand, match="timeout"):
        session.run("echo x", timeout)
    assert not session.alive()
    assert logged(command_log) == []


def test_bad_argument_does_not_reset_session(session):
    # F05: input validation used to be a SirilError, so _run reset the session and lost the image.
    asyncio.run(_run("echo keepme"))
    assert session.alive()
    pid = session.proc.pid
    with pytest.raises(InvalidCommand):
        asyncio.run(_run("a\nb"))
    with pytest.raises(InvalidCommand):
        asyncio.run(_run("echo x", 0))
    assert session.alive()
    assert session.proc.pid == pid


# -- timeouts and recovery ----------------------------------------------------


def test_silence_longer_than_timeout_raises(session):
    with pytest.raises(SirilError, match="no output from Siril for"):
        session.run("late", 0.3)


def test_timeout_is_idle_time_not_total_time(session):
    # F08: 'chatty' runs ~1.2s but is never silent for 0.5s, so it completes. The timeout is documented as idle time.
    ok, log = session.run("chatty", 0.5)
    assert ok is True
    assert log.splitlines() == [f"tick {i}" for i in range(6)]


def test_run_wrapper_resets_session_on_timeout_and_recovers(session):
    with pytest.raises(RuntimeError, match=r"session reset"):
        asyncio.run(_run("late", 0.3))
    assert not session.alive()
    assert asyncio.run(_run("echo back", 5)) == "back"  # restarts transparently


def test_run_wrapper_failure_includes_command_and_log(session):
    with pytest.raises(RuntimeError, match=r"Siril command failed: failwithlog\nboom"):
        asyncio.run(_run("failwithlog", 5))


def test_run_wrapper_failure_without_log_says_so(session):
    # F19: an empty failure used to produce a message with a blank reason.
    with pytest.raises(RuntimeError, match=r"failed: fail\n\(Siril gave no reason\)"):
        asyncio.run(_run("fail", 5))


def test_late_status_is_not_attributed_to_next_command(session):
    # F04: after a timeout without a reset, the old command's late reply must not answer the next one.
    with pytest.raises(SirilError):
        session.run("late", 0.3)  # the fake sends its terminal status ~1s later
    ok, log = session.run("fail", 5)
    assert ok is False
    assert "late-output" not in log
    assert "[ignored" in log  # the old command's status arrived mid-command and was recognised as stale


def test_output_before_the_command_starts_is_not_returned(session):
    # F11: Siril's asynchronous python-bootstrap logs arrive between commands; only output after our
    # command's own "starting" marker belongs to it.
    with pytest.raises(SirilError):
        session.run("late", 0.3)
    ok, log = session.run("echo mine", 5)
    assert ok is True
    assert log.splitlines()[0] == "mine"
    assert "late-output" not in log


# -- process death and exit commands (F01, F07) --------------------------------


def test_dead_siril_is_reported_as_exited_promptly(session):
    start = time.monotonic()
    with pytest.raises(SirilError, match=r"exited \(code 1\)"):
        session.run("die", 30)
    assert time.monotonic() - start < 5  # not the 30s timeout


def test_unexpected_death_is_reported_when_the_next_command_restarts(session):
    # The restart is transparent, but the loaded image is gone: say so instead of a bare failure.
    session.run("echo x", 5)
    os.killpg(session.proc.pid, signal.SIGKILL)
    session.proc.wait()
    ok, log = session.run("echo again", 5)
    assert ok is True
    assert log == "[Siril had exited (code -9); started a new session, so the previous image is gone]\nagain"
    assert session.run("echo third", 5) == (True, "third")  # one-shot


def test_exit_command_ends_the_process_cleanly(session):
    session.run("echo x", 5)
    assert session.run("exit", 5) == (True, "Siril exited (code 0)")
    assert not session.alive()
    assert session.run("echo again", 5) == (True, "again")  # a fresh process


def test_exit_on_an_already_dead_siril_releases_its_resources(session):
    session.run("echo x", 5)
    os.killpg(session.proc.pid, signal.SIGKILL)
    session.proc.wait()
    assert session.run("exit", 5) == (True, "Siril is not running")
    assert (session.proc, session.in_fd, session.pipe_dir) == (None, None, None)
    assert session.run("echo y", 5) == (True, "y")  # and no 'exited unexpectedly' notice: the user ended it


def test_exit_when_not_running_does_not_start_siril(session, command_log):
    assert session.run("quit", 5) == (True, "Siril is not running")
    assert not session.alive()
    assert logged(command_log) == []


# -- lifecycle and cleanup (F02, F14, F15, F20, F21) ------------------------------


def test_start_stop_does_not_leak_threads(session):
    session.start()
    session.stop()
    baseline = threading.active_count()
    for _ in range(3):
        session.start()
        session.stop()
    assert threading.active_count() == baseline


def test_stop_closes_both_pipe_fds(session):
    session.start()
    session.stop()
    fds_before = len(os.listdir("/dev/fd"))
    for _ in range(3):
        session.start()
        session.stop()
    assert len(os.listdir("/dev/fd")) <= fds_before


def test_run_starts_process_lazily_and_stop_is_idempotent(session):
    assert not session.alive()
    session.run("echo x", 5)
    assert session.alive()
    session.stop()
    session.stop()
    assert not session.alive()
    assert session.pipe_dir is None


def test_stop_kills_a_siril_that_ignores_exit(session, monkeypatch):
    # F21: a hung Siril is killed (process group) and reaped; stop() does not hang or leave a zombie.
    monkeypatch.setenv("FAKE_SIRIL_DELAY", "60")
    session.run("echo x", 5)
    session._write("late")  # the fake sleeps 60s and never reads "exit"
    time.sleep(0.3)
    proc = session.proc
    start = time.monotonic()
    session.stop()
    assert proc.poll() is not None  # reaped, not a zombie
    assert time.monotonic() - start < 12


def test_stop_kills_children_of_a_hung_siril(session, tmp_path, monkeypatch):
    # F21: the whole process group is killed, so things Siril spawned (starnet...) do not outlive it.
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_SIRIL_CHILD_PID", str(pidfile))
    monkeypatch.setenv("FAKE_SIRIL_DELAY", "60")
    session.run("spawnchild", 5)
    child = int(pidfile.read_text())
    try:
        session._write("late")  # hang the fake so it never reads "exit"
        time.sleep(0.3)
        session.stop()
        deadline = time.monotonic() + 3
        while siril_mcp._pid_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not siril_mcp._pid_alive(child)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass


def test_write_without_a_running_siril_raises(session):
    with pytest.raises(SirilError, match="not running"):
        session._write("stat")


def test_write_failure_is_a_sirilerror_not_a_raw_oserror(session):
    # F16: the result of the write is checked.
    session.run("echo x", 5)
    os.close(session.in_fd)  # the next write fails with EBADF
    try:
        with pytest.raises(SirilError, match="could not send the command"):
            session._write("stat")
    finally:
        session.in_fd = None  # already closed; keep stop() from closing the fd number a second time


def test_exit_that_never_finishes_is_reported(session, monkeypatch):
    monkeypatch.setenv("FAKE_SIRIL_IGNORE_EXIT", "1")
    session.run("echo x", 5)
    with pytest.raises(SirilError, match="did not exit within"):
        session.run("exit", 0.5)


def test_output_before_ready_is_kept_for_diagnostics(session, monkeypatch):
    monkeypatch.setenv("FAKE_SIRIL_PREREADY", "log: warming up|log: still warming")
    session.run("echo x", 5)
    assert session.startup_lines == ["log: warming up", "log: still warming"]


def test_sigterm_handler_stops_the_session_then_hard_exits(session, monkeypatch):
    exits = []
    monkeypatch.setattr(os, "_exit", exits.append)
    session.run("echo x", 5)
    siril_mcp._shutdown(15, None)
    assert exits == [128 + 15]
    assert not session.alive()



def test_start_uses_configured_workdir(session, tmp_path):
    session.run("echo x", 5)
    assert session.proc.args[-2:] == ["-d", str(tmp_path)]


def test_missing_siril_binary_is_a_clean_error_and_leaks_nothing(session, monkeypatch, tmp_path):
    monkeypatch.setattr(siril_mcp, "SIRIL_BIN", str(tmp_path / "no-such-siril"))
    with pytest.raises(SirilError, match="could not start"):
        session.run("echo x", 5)
    assert list(tmp_path.glob("siril-mcp-*")) == []
    with pytest.raises(RuntimeError, match="session reset"):  # and through the tool wrapper
        asyncio.run(_run("echo x", 5))


def test_startup_failure_includes_siril_console_output(session, monkeypatch):
    monkeypatch.setenv("FAKE_SIRIL_DIE_AT_START", "1")
    with pytest.raises(SirilError, match=r"(?s)exited \(code 3\).*boom-from-stderr.*during startup"):
        session.run("echo x", 5)


def test_unexpected_siril_version_adds_a_notice_once(session, monkeypatch):
    monkeypatch.setenv("FAKE_SIRIL_VERSION", "siril 2.0.0")
    ok, log = session.run("echo x", 5)
    assert ok is True
    assert log.startswith("[Siril reports 'siril 2.0.0' (exit 0)")
    assert session.run("echo y", 5) == (True, "y")  # one-shot


def test_expected_siril_version_adds_no_notice(session):
    assert session.run("echo x", 5) == (True, "x")


# -- working directory tracking (F13) ---------------------------------------------


def test_successful_cd_updates_workdir_relative_to_the_current_one(session, tmp_path):
    session.run('cd "sub dir"', 5)
    assert session.workdir == str(tmp_path / "sub dir")
    session.run("cd ..", 5)
    assert session.workdir == str(tmp_path)


def test_failed_cd_leaves_workdir_alone(session, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SIRIL_FAIL", "cd")
    assert session.run('cd "elsewhere"', 5)[0] is False
    assert session.workdir == str(tmp_path)


def test_cd_with_unbalanced_quote_or_wrong_arity_is_ignored(session, tmp_path):
    session.run('cd "oops', 5)
    session.run("cd a b", 5)
    session.run("cd", 5)
    assert session.workdir == str(tmp_path)


def test_missing_workdir_falls_back_with_a_notice(session, tmp_path):
    session.workdir = str(tmp_path / "gone")
    ok, log = session.run("echo x", 5)
    assert ok is True
    assert log == f"[saved working directory {tmp_path / 'gone'} no longer exists; Siril started in {os.getcwd()}]\nx"
    assert session.workdir == os.getcwd()


# -- stale pipe dirs left by dead servers (F20) ---------------------------------------


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_start_removes_pipe_dirs_of_dead_servers_but_not_live_ones(session, tmp_path):
    stale = tmp_path / f"siril-mcp-{_dead_pid()}-abcd"
    live = tmp_path / f"siril-mcp-{os.getppid()}-abcd"  # parent process: alive
    foreign = tmp_path / "siril-mcp-notapid-abcd"
    for d in (stale, live, foreign):
        d.mkdir()
    session.run("echo x", 5)
    assert not stale.exists()
    assert live.exists()
    assert foreign.exists()  # not ours to judge: name does not carry a pid


def test_start_kills_an_orphaned_siril_of_a_dead_server(session, tmp_path):
    stale = tmp_path / f"siril-mcp-{_dead_pid()}-abcd"
    stale.mkdir()
    os.mkfifo(stale / "in")
    os.mkfifo(stale / "out")
    orphan = subprocess.Popen(
        [str(FAKE_SIRIL), "-p", "-r", str(stale / "in"), "-w", str(stale / "out")], start_new_session=True
    )
    (stale / "siril.pid").write_text(str(orphan.pid))
    try:
        session.run("echo x", 5)
        assert orphan.wait(timeout=5) == -signal.SIGKILL  # killed by the reaper
        assert not stale.exists()
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()


def test_reaper_does_not_kill_a_process_that_is_not_the_recorded_siril(session, tmp_path):
    # pid-reuse guard: the recorded pid belongs to an unrelated process whose command line lacks the dir.
    stale = tmp_path / f"siril-mcp-{_dead_pid()}-abcd"
    stale.mkdir()
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (stale / "siril.pid").write_text(str(bystander.pid))
    try:
        session.run("echo x", 5)
        assert bystander.poll() is None
        assert not stale.exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_reaper_ignores_unreadable_pid_file(session, tmp_path):
    stale = tmp_path / f"siril-mcp-{_dead_pid()}-abcd"
    stale.mkdir()
    (stale / "siril.pid").write_text("not a number")
    session.run("echo x", 5)
    assert not stale.exists()


def test_reaper_survives_ps_failing_and_still_removes_the_dir(session, tmp_path, monkeypatch):
    stale = tmp_path / f"siril-mcp-{_dead_pid()}-abcd"
    stale.mkdir()
    (stale / "siril.pid").write_text("12345")
    real_run = subprocess.run

    def run(cmd, *a, **kw):
        if cmd[0] == "ps":
            raise subprocess.TimeoutExpired(cmd, 5)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    session.run("echo x", 5)
    assert not stale.exists()


def test_pid_alive_treats_permission_denied_as_alive(monkeypatch):
    def deny(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", deny)
    assert siril_mcp._pid_alive(1) is True


@pytest.mark.parametrize("name", ["siril-mcp-²-abcd", "siril-mcp-99999999999999999999-abcd"])
def test_reaper_leaves_directories_with_implausible_pids_alone(session, tmp_path, name):
    # Non-ASCII digits and out-of-range numbers used to raise out of start() and block every session.
    odd = tmp_path / name
    odd.mkdir()
    assert session.run("echo x", 5) == (True, "x")
    assert odd.exists()


# -- server process: shutdown behaviour (F20) ------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def _server(env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, str(ROOT / "siril_mcp.py")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
    proc.stdin.write(json.dumps(INIT) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["id"] == 1  # the server is up and its handlers are installed
    return proc


def test_sigterm_ends_a_real_server_promptly_with_stdin_still_open():
    proc = _server()
    try:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 128 + signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stdin_eof_ends_siril_without_waiting_for_an_in_flight_command(tmp_path):
    env = {"SIRIL_BIN": str(FAKE_SIRIL), "FAKE_SIRIL_DELAY": "20", "TMPDIR": str(tmp_path)}
    proc = _server(env)
    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "siril_command", "arguments": {"command": "late"}}}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 10
        while not list(tmp_path.glob("siril-mcp-*")) and time.monotonic() < deadline:
            time.sleep(0.05)  # wait until the command is really in flight
        time.sleep(0.5)
        proc.stdin.close()
        start = time.monotonic()
        proc.wait(timeout=10)
        assert time.monotonic() - start < 8  # not the 20s the command would take
        assert list(tmp_path.glob("siril-mcp-*")) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        with pytest.MonkeyPatch.context() as mp:  # a failed run must not leave an orphaned fake Siril behind
            mp.setattr(siril_mcp.tempfile, "tempdir", str(tmp_path))
            siril_mcp._reap_stale_dirs()

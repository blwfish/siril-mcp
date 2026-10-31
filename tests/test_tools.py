"""The MCP tool functions, driven against the fake Siril. Commands are checked via the fake's command log."""
import asyncio
import threading

import pytest
from conftest import logged
from fake_siril import FAKE_JPEG

import siril_mcp
from siril_mcp import InvalidCommand


def call(coro):
    return asyncio.run(coro)


def test_tools_are_registered_with_expected_names():
    names = {t.name for t in call(siril_mcp.mcp.list_tools())}
    assert names == {
        "siril_command", "siril_help", "siril_set_directory",
        "siril_load", "siril_save", "siril_stats", "siril_preview",
    }


def test_siril_command_passes_through(session, command_log):
    assert call(siril_mcp.siril_command("echo hello")) == "hello"
    assert logged(command_log) == ["echo hello"]


@pytest.mark.parametrize("arg, sent", [("", "help"), ("autostretch", "help autostretch")])
def test_siril_help_builds_command(session, command_log, arg, sent):
    call(siril_mcp.siril_help(arg))
    assert logged(command_log) == [sent]


def test_siril_load_and_stats_send_expected_commands(session, command_log):
    call(siril_mcp.siril_load("m31.fit"))
    call(siril_mcp.siril_stats())
    assert logged(command_log) == ['load "m31.fit"', "stat"]


@pytest.mark.parametrize("fmt, verb", [("fits", "save"), ("tiff", "savetif"), ("png", "savepng"), ("jpg", "savejpg")])
def test_siril_save_maps_format_to_command(session, command_log, tmp_path, fmt, verb):
    out = str(tmp_path / "out")  # absolute: the fake writes a file for savejpg
    call(siril_mcp.siril_save(out, fmt))
    assert logged(command_log) == [f'{verb} "{out}"']


@pytest.mark.parametrize("fmt", ["bmp", "", "FITS"])
def test_siril_save_rejects_unknown_format_without_starting_siril(session, fmt):
    # Includes the empty string and wrong case: the lookup is exact and case-sensitive.
    with pytest.raises(InvalidCommand, match="format must be one of"):
        call(siril_mcp.siril_save("out", fmt))
    assert not session.alive()


# -- paths and quoting (F12, F13) ---------------------------------------------------


@pytest.mark.parametrize("bad", ['a"b', 'a"', '"', "a\nb", "a\rb", "a\x00b"])
def test_paths_siril_cannot_represent_are_rejected_not_mangled(session, bad):
    # F12: Siril has no escape for `"`; these used to be interpolated and silently misread.
    for tool in (siril_mcp.siril_load, siril_mcp.siril_save):
        with pytest.raises(InvalidCommand, match="may not contain"):
            call(tool(bad))
    assert not session.alive()


def test_ordinary_awkward_paths_are_quoted_through(session, command_log):
    call(siril_mcp.siril_load("my images/M 31 (2).fit"))
    assert logged(command_log) == ['load "my images/M 31 (2).fit"']


def test_set_directory_resolves_relative_to_siril_workdir_and_tracks_it(session, command_log, tmp_path):
    (tmp_path / "sub dir").mkdir()
    call(siril_mcp.siril_set_directory("sub dir"))
    target = str(tmp_path / "sub dir")
    assert logged(command_log) == [f'cd "{target}"']
    assert session.workdir == target
    (tmp_path / "sub dir" / "deeper").mkdir()
    call(siril_mcp.siril_set_directory("deeper"))  # relative to the NEW directory, as Siril itself would
    assert session.workdir == str(tmp_path / "sub dir" / "deeper")


def test_set_directory_does_not_change_workdir_when_cd_fails(session, tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    monkeypatch.setenv("FAKE_SIRIL_FAIL", "cd")
    with pytest.raises(RuntimeError, match="failed"):
        call(siril_mcp.siril_set_directory(str(tmp_path / "sub")))
    assert session.workdir == str(tmp_path)


def test_set_directory_rejects_non_directory_without_starting_siril(session, tmp_path):
    (tmp_path / "afile").write_text("x")
    for bad in (tmp_path / "missing", tmp_path / "afile"):
        with pytest.raises(InvalidCommand, match="not a directory"):
            call(siril_mcp.siril_set_directory(str(bad)))
    assert not session.alive()


class CountingLock:
    """A threading.Lock that records how many threads were inside `with` (waiting or holding) at once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self.inside = self.peak = 0

    def __enter__(self):
        with self._guard:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        with self._guard:
            self.inside -= 1


def test_queued_tool_calls_wait_on_the_event_loop_not_on_threads(session, monkeypatch):
    # F24: with the gate, a second call never reaches session.lock while the first is running.
    counting = CountingLock()
    monkeypatch.setattr(session, "lock", counting)

    async def both():
        return await asyncio.gather(siril_mcp.siril_command("late"), siril_mcp.siril_command("echo second"))

    first, second = call(both())
    assert (first, second) == ("late-output", "second")
    assert counting.peak == 1


def test_every_tool_uses_the_default_timeout_at_call_time(session, monkeypatch, tmp_path):
    # F10: a sentinel default proves no tool or preview step hardcodes its own limit.
    monkeypatch.setattr(siril_mcp, "DEFAULT_TIMEOUT", 123.5)
    seen = []
    real_run = session.run
    monkeypatch.setattr(session, "run", lambda cmd, timeout=None: (seen.append((cmd.split()[0], timeout)), real_run(cmd, timeout))[1])
    call(siril_mcp.siril_load("a.fit"))
    call(siril_mcp.siril_save("a"))
    call(siril_mcp.siril_stats())
    call(siril_mcp.siril_help())
    call(siril_mcp.siril_set_directory(str(tmp_path)))
    call(siril_mcp.siril_preview(autostretch=True, max_dim=64))
    verbs = [v for v, _ in seen]
    assert verbs == ["load", "save", "stat", "help", "cd", "save", "autostretch", "resample", "savejpg", "load"]
    assert {t for _, t in seen} == {123.5}


# -- siril_preview -------------------------------------------------------------------


def test_preview_requires_a_running_session(session):
    with pytest.raises(RuntimeError, match="no Siril session is running"):
        call(siril_mcp.siril_preview())


def preview_commands(command_log):
    """Received commands reduced to their verb (resample keeps its -maxdim argument)."""
    return [c if c.startswith("resample") else c.split(" ", 1)[0] for c in logged(command_log)]


@pytest.mark.parametrize("autostretch, max_dim, expected", [
    (True, 0, ["save", "autostretch", "savejpg", "load"]),
    (False, 0, ["savejpg"]),  # F23: nothing mutates the image, so no snapshot and no reload
    (False, 1, ["save", "resample -maxdim=1", "savejpg", "load"]),
    (False, -1, ["savejpg"]),  # boundary: only max_dim > 0 resamples
    (True, 512, ["save", "autostretch", "resample -maxdim=512", "savejpg", "load"]),
])
def test_preview_command_sequence(session, command_log, autostretch, max_dim, expected):
    call(siril_mcp.siril_command("echo prime"))  # preview requires a live session
    command_log.write_text("")
    image = call(siril_mcp.siril_preview(autostretch=autostretch, max_dim=max_dim))
    assert preview_commands(command_log) == expected
    assert image.data == FAKE_JPEG
    assert image.to_image_content().mimeType == "image/jpeg"


def test_preview_defaults_to_a_bounded_size(session, command_log):
    # F23: the default used to be full resolution, which can exceed image-size limits.
    call(siril_mcp.siril_command("echo prime"))
    command_log.write_text("")
    call(siril_mcp.siril_preview())
    assert preview_commands(command_log) == [
        "save", "autostretch", f"resample -maxdim={siril_mcp.DEFAULT_PREVIEW_DIM}", "savejpg", "load",
    ]


def test_preview_takes_its_commands_and_extension_from_the_format_table(session, command_log, monkeypatch):
    call(siril_mcp.siril_command("echo prime"))
    monkeypatch.setitem(siril_mcp.SAVE_FORMATS, "jpg", siril_mcp.SaveFormat("savejpgx", ".zz"))
    monkeypatch.setitem(siril_mcp.SAVE_FORMATS, "fits", siril_mcp.SaveFormat("snapx", ".fit"))
    command_log.write_text("")
    with pytest.raises(RuntimeError, match=r"preview\.zz"):  # the fake writes no .zz file
        call(siril_mcp.siril_preview(autostretch=True, max_dim=0))
    assert preview_commands(command_log) == ["snapx", "autostretch", "savejpgx", "load"]


def restarted_with_env(session, monkeypatch, command_log, **env):
    """Start a session whose fake Siril sees the given env vars (it reads them at startup)."""
    session.stop()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    call(siril_mcp.siril_command("echo restart"))
    command_log.write_text("")


def test_preview_restores_image_even_when_export_fails(session, command_log, monkeypatch):
    restarted_with_env(session, monkeypatch, command_log, FAKE_SIRIL_FAIL="savejpg")
    with pytest.raises(RuntimeError, match="preview export failed"):
        call(siril_mcp.siril_preview(autostretch=False, max_dim=64))
    assert preview_commands(command_log) == ["save", "resample -maxdim=64", "savejpg", "load"]  # restore still ran


def test_preview_reports_both_errors_when_export_and_restore_fail(session, command_log, monkeypatch):
    restarted_with_env(session, monkeypatch, command_log, FAKE_SIRIL_FAIL="savejpg,load")
    with pytest.raises(RuntimeError) as exc:
        call(siril_mcp.siril_preview(autostretch=False, max_dim=64))
    assert "preview export failed" in str(exc.value)
    assert "restoring the original image also failed" in str(exc.value)
    assert "preview restore failed" in str(exc.value)  # the original error is not replaced by the restore's


def test_preview_restore_failure_after_good_export_is_reported(session, command_log, monkeypatch):
    restarted_with_env(session, monkeypatch, command_log, FAKE_SIRIL_FAIL="load")
    with pytest.raises(RuntimeError, match="preview restore failed"):
        call(siril_mcp.siril_preview(autostretch=False, max_dim=64))


def test_preview_snapshot_failure_names_the_step_and_stops(session, command_log, monkeypatch):
    restarted_with_env(session, monkeypatch, command_log, FAKE_SIRIL_FAIL="save")
    with pytest.raises(RuntimeError, match="preview snapshot .*failed"):
        call(siril_mcp.siril_preview())
    assert preview_commands(command_log) == ["save"]  # nothing was modified, so nothing to restore


def test_preview_success_without_a_file_is_reported(session, command_log, monkeypatch):
    restarted_with_env(session, monkeypatch, command_log, FAKE_SIRIL_NOFILE="1")
    with pytest.raises(RuntimeError, match="wrote no file"):
        call(siril_mcp.siril_preview(autostretch=False, max_dim=0))


def test_preview_timeout_resets_session_and_skips_the_restore(session, command_log, monkeypatch):
    # F03: siril_preview used to bypass the reset-on-timeout that every other tool gets.
    restarted_with_env(session, monkeypatch, command_log, FAKE_SIRIL_LATE="autostretch", FAKE_SIRIL_DELAY="1.5")
    real_run = session.run
    monkeypatch.setattr(session, "run", lambda cmd, timeout=300: real_run(cmd, min(timeout, 0.4)))
    with pytest.raises(RuntimeError, match="session reset"):
        call(siril_mcp.siril_preview(autostretch=True))
    assert not session.alive()
    assert "load" not in preview_commands(command_log)  # no restore into a session that was just killed

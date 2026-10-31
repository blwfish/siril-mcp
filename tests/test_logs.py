"""Long-log summarising (condense_log / run_checked).

Calibrate, register and the like log one or two lines per frame, which can be 50-100k characters: more than the client
will show. Past RETURN_LOG_CHARS the server returns a summary that says it is one, with the log saved to a file.
Nothing may be dropped silently: rare lines stay verbatim (from both ends of the log when they don't all fit),
repeated kinds are counted, omissions are announced, and the summary never exceeds the limit.
"""
import asyncio
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import siril_mcp
from siril_mcp import (
    BLANK_LINE,
    EXAMPLE_CUT,
    LOG_VERB_CHARS,
    MAX_SHAPES_SHOWN,
    MIN_RETURN_LOG_CHARS,
    RARE_LINE_CUT,
    RARE_LINE_MAX,
    TAIL_SHARE,
    _env_number,
    _log_dir_from_env,
    _pick_rare,
    _prune_logs,
    _shape,
    condense_log,
)

LIMIT = 5000
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def logs(session, monkeypatch):
    """The fake-Siril session, saved logs going to the `session` fixture's temp dir, and a small return limit."""
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", LIMIT)
    return siril_mcp.LOG_DIR


@pytest.fixture
def direct(tmp_path, monkeypatch):
    """For calling condense_log on a hand-made log: saved logs in tmp_path/saved, the smallest allowed limit."""
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path / "saved")
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", MIN_RETURN_LOG_CHARS)
    return tmp_path / "saved"


def saved_path(text: str):
    m = re.match(r"\[log shortened: (\d+) lines, (\d+) characters; log saved to (.+?\.log)(?:;|\])", text)
    assert m, text.splitlines()[0]
    return int(m.group(1)), int(m.group(2)), Path(m.group(3))


def letters(i: int, width: int = 3) -> str:
    """i written with letters only, so a line built from it has no digits and keeps its own shape."""
    return "".join(chr(97 + i // 26 ** k % 26) for k in reversed(range(width)))


def distinct_lines(n: int) -> list[str]:
    return [f"item {letters(i)} lorem ipsum dolor sit amet" for i in range(n)]


# -- what counts as "the same kind of line" -----------------------------------------------------------------------

@pytest.mark.parametrize("line, shape", [
    ("Reading FITS: file lights_00031.fit, 3 layer(s), 6064x4040 pixels", "Reading FITS: file lights_N.fit, N layer(s), NxN pixels"),
    ("background noise 4.389 (6.698e-05)", "background noise N (Ne-N)"),   # only digits are masked; the sign stays
    ("background noise 4.389 (6.698e+05)", "background noise N (Ne+N)"),   # so these two exponents are different kinds
    ("Mean: 1797.580359, Median: 1781.0", "Mean: N, Median: N"),
    ("", ""),
    ("no digits at all", "no digits at all"),
])
def test_shape_masks_numbers_only(line, shape):
    assert _shape(line) == shape


# -- short logs are untouched ----------------------------------------------------------------------------------------

def test_short_log_is_returned_unchanged_and_nothing_is_saved(logs):
    assert siril_mcp.session.run_checked("echo hi there") == "hi there"
    assert not logs.exists()


def test_limit_boundary_exactly_at_the_limit_is_unchanged_one_over_is_summarised(logs, monkeypatch):
    raw = siril_mcp.session.run("spam", 30)[1]
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", len(raw))
    assert siril_mcp.session.run_checked("spam") == raw
    assert not logs.exists()
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", len(raw) - 1)
    assert siril_mcp.session.run_checked("spam").startswith("[log shortened:")
    assert logs.exists()


# -- long logs: the summary ----------------------------------------------------------------------------------------

def test_long_log_is_summarised_and_the_log_is_saved_exactly(logs):
    raw = siril_mcp.session.run("spam", 30)[1]
    out = siril_mcp.session.run_checked("spam")
    n_lines, n_chars, path = saved_path(out)
    assert (n_lines, n_chars) == (len(raw.split("\n")), len(raw))
    assert path.parent == logs and path.read_text() == raw               # exactly what run() returned, nothing added
    assert len(out) <= LIMIT                                              # bounded
    assert "300x  Reading FITS: file frame_00000.fit" in out              # repeated kinds are counted, with an example
    assert "300x  Saving FITS: file out_00000.fit" in out
    assert "Warning: something unusual happened in the middle" in out     # a rare line survives verbatim
    assert "Execution time: 3 min 7 s" in out                             # so do the closing totals
    assert "frame_00150" not in out                                       # repeated lines are not listed one by one
    assert "omitted" not in out and "more repeated kind" not in out       # nothing was left out, so nothing says so


def test_rare_lines_stay_in_their_original_order(logs):
    out = siril_mcp.session.run_checked("spam")
    assert out.index("Running command: spam") < out.index("Warning: something unusual") < out.index("Execution time: 3 min 7 s")


def test_boundary_a_kind_seen_three_times_is_verbatim_four_times_is_collapsed(direct):
    filler = ["filler line"] * 400                          # pushes the log over the limit
    text = "\n".join(["ok 1"] * RARE_LINE_MAX + ["same x 1"] * (RARE_LINE_MAX + 1) + filler)
    out = condense_log("x", text)
    assert out.count("  ok 1") == RARE_LINE_MAX                          # 3 occurrences: shown verbatim
    assert f"{RARE_LINE_MAX + 1}x  same x 1" in out                      # 4 occurrences: collapsed with a count...
    assert out.count("same x 1") == 1                                    # ...and not also listed verbatim


def test_blank_lines_are_counted_and_labelled(direct):
    out = condense_log("x", "\n".join([""] * 2500 + ["the one rare line"]))
    assert f"2500x  {BLANK_LINE}" in out and "the one rare line" in out
    out = condense_log("x", "\n".join([""] + ["same line"] * 400))       # a rare blank line is labelled too, not invisible
    assert f"  {BLANK_LINE}\n" in out


def test_a_log_of_one_repeated_kind_has_no_verbatim_section(direct):
    out = condense_log("x", "\n".join(["same line"] * 3000))
    assert "3000x  same line" in out and "Lines seen at most" not in out and "omitted" not in out


def test_a_log_of_all_different_lines_has_no_repeated_section(direct):
    out = condense_log("x", "\n".join(distinct_lines(400)))
    assert "Lines seen at most" in out and "Repeated lines" not in out and "more repeated kind" not in out


# -- lines are cut visibly, at exactly the documented length -------------------------------------------------------

@pytest.mark.parametrize("n, is_cut", [(RARE_LINE_CUT - 1, False), (RARE_LINE_CUT, False), (RARE_LINE_CUT + 1, True)])
def test_a_verbatim_line_is_cut_only_past_the_cut_length(direct, n, is_cut):
    line = "a" * n
    out = condense_log("x", "\n".join(["filler line"] * 400 + [line]))
    if is_cut:
        assert f"  {'a' * RARE_LINE_CUT} ...[line cut, {n} characters]" in out and "a" * (RARE_LINE_CUT + 1) not in out
    else:
        assert f"  {line}\n" in out + "\n" and "line cut" not in out


@pytest.mark.parametrize("n, is_cut", [(EXAMPLE_CUT - 1, False), (EXAMPLE_CUT, False), (EXAMPLE_CUT + 1, True), (5000, True)])
def test_the_example_of_a_repeated_kind_is_cut_only_past_the_cut_length(direct, n, is_cut):
    out = condense_log("x", "\n".join(["b" * n] * 400))
    assert (f"...[line cut, {n} characters]" in out) is is_cut
    assert f"  400x  {'b' * min(n, EXAMPLE_CUT)}" in out
    assert max(len(ln) for ln in out.splitlines() if "400x" in ln) <= 9 + EXAMPLE_CUT + 40      # bounded, however long


def test_the_documented_cut_lengths():
    assert (RARE_LINE_CUT, EXAMPLE_CUT) == (600, 160)       # README.md quotes these


# -- the kinds listed: the most repeated ones, at most MAX_SHAPES_SHOWN -----------------------------------------------------

def test_repeated_kinds_that_do_not_fit_are_counted_in_the_note(direct):
    out = condense_log("x", "\n".join(f"{letters(k)} " + "z" * 5000 for k in range(30) for _ in range(6)))
    listed = len(re.findall(r"^\s+6x  ", out, re.MULTILINE))
    assert 0 < listed < MAX_SHAPES_SHOWN                                   # the third of the limit ran out first
    assert f"[... {30 - listed} more repeated kind(s) not listed" in out

def test_the_most_repeated_kinds_are_listed_first_and_the_least_repeated_are_counted(direct):
    kinds = MAX_SHAPES_SHOWN + 5
    out = condense_log("x", "\n".join(f"kind {letters(k)}" for k in range(kinds) for _ in range(RARE_LINE_MAX + 1 + k)))
    listed = re.findall(r"^\s+\d+x  kind (\w+)$", out, re.MULTILINE)
    assert listed == [letters(k) for k in range(kinds - 1, kinds - 1 - MAX_SHAPES_SHOWN, -1)]     # most repeated first
    assert "[... 5 more repeated kind(s) not listed; see the saved log]" in out


@pytest.mark.parametrize("kinds, more", [(MAX_SHAPES_SHOWN - 1, 0), (MAX_SHAPES_SHOWN, 0), (MAX_SHAPES_SHOWN + 1, 1)])
def test_the_repeated_kind_limit_boundary(direct, kinds, more):
    out = condense_log("x", "\n".join(f"kind {letters(k)}" for k in range(kinds) for _ in range(RARE_LINE_MAX + 20)))
    assert len(re.findall(r"^\s+\d+x  kind ", out, re.MULTILINE)) == min(kinds, MAX_SHAPES_SHOWN)
    assert (f"[... {more} more repeated kind(s) not listed" in out) is bool(more)


# -- the budget: rare lines from both ends, never over the limit --------------------------------------------------

def test_pick_rare_returns_everything_that_fits():
    items = ["a" * 7] * 3                                   # three items of cost 10
    assert _pick_rare(items, 30) == (items, [])
    assert _pick_rare(items, 100) == (items, [])
    assert _pick_rare([], 0) == ([], [])


def test_pick_rare_boundaries_of_each_end():
    items = [f"{i}" * 7 for i in range(6)]                  # six items of cost 10; the split is head 2/3, tail 1/3
    assert TAIL_SHARE == 3
    assert _pick_rare(items, 28) == (items[:1], [])         # head budget 19
    assert _pick_rare(items, 29) == (items[:2], [])         # head budget 20: exactly two items fit (inclusive)
    assert _pick_rare(items, 30) == (items[:2], items[-1:])  # tail budget 10: exactly one fits (inclusive)
    head, tail = _pick_rare(items, 59)
    assert (len(head), len(tail)) == (4, 1)                 # head budget 40 (four fit, inclusive), tail budget 19 (one)
    assert _pick_rare(items, 60) == (items, [])             # all fit: nothing is split off
    assert _pick_rare(items, -5) == ([], [])


@pytest.mark.parametrize("n", [1, 2, 5, 6, 40])
def test_pick_rare_never_overlaps_never_exceeds_the_budget_and_keeps_order(n):
    items = [f"{i:04d}" * 2 for i in range(n)]
    for budget in range(3 * n * 11 + 5):
        head, tail = _pick_rare(items, budget)
        assert head + tail == [x for x in items if x in head + tail]          # original order
        assert len(head) + len(tail) <= n and head == items[:len(head)] and tail == items[n - len(tail):]
        assert sum(len(x) + 3 for x in head + tail) <= max(budget, 0) or (head + tail) == items


def test_the_end_of_the_log_survives_when_the_rare_lines_do_not_all_fit(direct):
    lines = distinct_lines(1500) + ["Error: the real failure", "[output truncated: 5 further line(s) dropped after 1000000 characters]"]
    out = condense_log("x", "\n".join(lines))
    assert "Error: the real failure" in out and "[output truncated: 5 further line(s)" in out    # the end is kept
    assert "item aaa lorem" in out                                                                # and so is the start
    shown = len([ln for ln in out.splitlines() if ln.startswith(("  item ", "  Error", "  [output"))])
    omitted = int(re.search(r"\[\.\.\. (\d+) more such line\(s\) omitted from the middle", out).group(1))
    assert shown + omitted == len(lines)                                                          # every line shown or counted
    assert out.index("item aaa lorem") < out.index("omitted from the middle") < out.index("Error: the real failure")


@pytest.mark.parametrize("limit", [MIN_RETURN_LOG_CHARS, MIN_RETURN_LOG_CHARS + 1, 3000, 5000, 20000])
def test_the_summary_never_exceeds_the_limit_and_uses_most_of_it(direct, monkeypatch, limit):
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", limit)
    worst = {
        "distinct": distinct_lines(3000),
        "long distinct lines": [f"{letters(i)} " + "y" * 5000 for i in range(60)],
        "many long kinds": [f"{letters(k)} " + "z" * 5000 for k in range(30) for _ in range(6)],
        "blank": [""] * 30000 + ["end"],
        "kinds and rare lines": distinct_lines(500) + [f"kind {letters(k)}" for k in range(30) for _ in range(40)],
    }
    for name, lines in worst.items():
        out = condense_log("x", "\n".join(lines))
        assert len(out) <= limit, name
    out = condense_log("x", "\n".join(distinct_lines(3000)))
    assert limit - len(out) < 100                        # the budget is exact: no large unused reserve


def test_a_save_error_text_is_bounded_in_the_header(direct, monkeypatch):
    def fail(**_):
        raise OSError("x" * 5000)
    monkeypatch.setattr(siril_mcp.tempfile, "mkstemp", fail)
    out = condense_log("x", "\n".join(distinct_lines(3000)))
    assert f"({'x' * 200})" in out and len(out) <= MIN_RETURN_LOG_CHARS


def test_lines_that_are_all_different_are_shown_from_both_ends_and_the_rest_is_counted(logs):
    raw_lines = siril_mcp.session.run("distinct", 30)[1].split("\n")
    out = siril_mcp.session.run_checked("distinct")
    assert LIMIT - 100 < len(out) <= LIMIT
    omitted = int(re.search(r"\[\.\.\. (\d+) more such line\(s\) omitted", out).group(1))
    shown = [ln for ln in out.splitlines() if ln.startswith("  item-")]
    assert len(shown) + omitted == len(raw_lines) == 400                # every line is either shown or counted
    assert shown[0].strip() == raw_lines[0] and shown[-1].strip() == raw_lines[-1]    # first and last are both there


# -- never silent ------------------------------------------------------------------------------------------------

def test_a_failed_save_is_reported_in_the_summary(session, tmp_path, monkeypatch):
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    monkeypatch.setattr(siril_mcp, "LOG_DIR", blocker / "logs")          # cannot be created under a file
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", LIMIT)
    out = siril_mcp.session.run_checked("spam")
    assert out.startswith("[log shortened:") and "log could NOT be saved (" in out
    assert "Execution time: 3 min 7 s" in out                             # the summary itself is still useful


def test_a_failed_write_is_reported_and_leaves_no_partial_file(direct, monkeypatch):
    def readonly_fd(dir, prefix, suffix):
        name = Path(dir) / f"{prefix}x{suffix}"
        name.write_text("")
        return os.open(name, os.O_RDONLY), str(name)                     # writing to it fails
    monkeypatch.setattr(siril_mcp.tempfile, "mkstemp", readonly_fd)
    out = condense_log("x", "\n".join(distinct_lines(3000)))
    assert "log could NOT be saved (" in out
    assert list(direct.glob("*")) == []                                  # the half-made file was removed


def test_a_failing_command_with_a_long_log_reports_the_summary_and_the_cause_in_the_error(logs):
    with pytest.raises(RuntimeError) as exc:
        siril_mcp.session.run_checked("spamfail")
    msg = str(exc.value)
    first, rest = msg.split("\n", 1)
    assert first == "Siril command failed: spamfail"
    saved_path(rest)                                                      # the header is the second line; the log exists
    assert "Error: registration aborted, 3 frames could not be read" in msg and "Execution time: 3 min 7 s" in msg
    assert len(rest) <= LIMIT


def test_the_failure_reason_survives_even_when_a_long_log_of_different_lines_precedes_it(logs):
    with pytest.raises(RuntimeError) as exc:
        siril_mcp.session.run_checked("distinctfail")
    assert "Error: the real failure" in str(exc.value)


def test_the_tool_boundary_returns_the_summary(logs):
    out = asyncio.run(siril_mcp.siril_command("spam"))
    assert out.startswith("[log shortened:") and len(out) <= LIMIT


def test_siril_help_is_returned_whole_even_when_it_is_longer_than_the_limit(logs, monkeypatch):
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", MIN_RETURN_LOG_CHARS)
    raw = siril_mcp.session.run("help", 30)[1]
    assert len(raw) > MIN_RETURN_LOG_CHARS
    assert asyncio.run(siril_mcp.siril_help()) == raw


def test_a_bug_in_the_summary_does_not_discard_the_result(logs, monkeypatch):
    def broken(command, log):
        raise ValueError("boom")
    monkeypatch.setattr(siril_mcp, "condense_log", broken)
    out = siril_mcp.session.run_checked("spam")
    assert out.startswith("[log summary failed (ValueError: boom)") and "Execution time: 3 min 7 s" in out
    assert "Running command: spam" in out and len(out) <= LIMIT           # the start and the end are both there
    with pytest.raises(RuntimeError) as exc:
        siril_mcp.session.run_checked("spamfail")
    assert "Error: registration aborted" in str(exc.value)


def test_the_fallback_summary_keeps_exactly_the_start_and_the_end(direct, monkeypatch):
    def broken(command, log):
        raise ValueError("boom")
    monkeypatch.setattr(siril_mcp, "condense_log", broken)
    half = (siril_mcp.RETURN_LOG_CHARS - 300) // 2
    log = "a" * 3000 + "b" * 5000 + "c" * 3000
    note, head, gap, tail = siril_mcp._condensed("x", log).split("\n")
    assert head == "a" * half and tail == "c" * half
    assert gap == f"[... {len(log) - 2 * half} characters omitted ...]" and "ValueError: boom" in note


def test_the_limit_in_the_tool_description_is_the_one_in_force():
    note = f"{siril_mcp.RETURN_LOG_CHARS:,} characters"
    tools = {t.name: t for t in asyncio.run(siril_mcp.mcp.list_tools())}
    assert note in tools["siril_command"].description
    assert note in siril_mcp.mcp.instructions and "{log_note}" not in tools["siril_command"].description


# -- housekeeping: what is saved, where, and what is pruned ------------------------------------------------------

def save(n: int = 1) -> list[Path]:
    return [saved_path(condense_log("calibrate", "\n".join(distinct_lines(3000))))[2] for _ in range(n)]


@pytest.mark.parametrize("n", [2, 3, 4, 7])
def test_only_the_newest_saved_logs_are_kept(direct, monkeypatch, n):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 3)
    paths = save(n)
    assert sorted(p.name for p in direct.glob("*.log")) == sorted(p.name for p in paths[-3:])


def test_pruning_never_touches_files_it_did_not_write(direct, monkeypatch):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 3)
    direct.mkdir()
    foreign = [direct / f"app{i}.log" for i in range(60)] + [direct / "20261031-notes.log", direct / "keep.txt"]
    for p in foreign:
        p.write_text("not ours")
    (path,) = save()
    assert all(p.exists() for p in foreign) and path.exists()


def test_pruning_never_deletes_the_log_just_written_even_if_the_clock_ran_backwards(direct, monkeypatch):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 3)
    direct.mkdir()
    for i in range(3):                                   # three "future" logs that sort after anything written now
        (direct / f"9999999999999999999-20990101-00000{i}-calibrate-abc.log").write_text("future")
    (path,) = save()
    assert path.exists() and len(list(direct.glob("*.log"))) == 3


def test_pruning_with_room_for_only_one_keeps_the_new_log(direct, monkeypatch):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 1)
    first, second = save(2)
    assert not first.exists() and second.exists()


def test_a_failing_delete_does_not_stop_the_rest_and_is_reported(direct, monkeypatch):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 2)
    direct.mkdir()
    old = [direct / f"1{i}00000000000000000-20260101-00000{i}-calibrate-abc.log" for i in range(4)]
    for p in old:
        p.write_text("old")
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self.name == old[0].name:
            raise PermissionError("nope")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", unlink)
    out = condense_log("calibrate", "\n".join(distinct_lines(3000)))
    assert "could not delete 1 old log(s)" in out
    assert [p.exists() for p in old] == [True, False, False, True]       # the oldest could not go; the next did; the newest stays


def test_a_log_already_pruned_by_another_server_is_not_a_failure(direct, monkeypatch):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 2)
    direct.mkdir()
    old = [direct / f"1{i}00000000000000000-20260101-00000{i}-calibrate-abc.log" for i in range(3)]
    for p in old:
        p.write_text("old")
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self.name == old[0].name:
            real_unlink(self)
            raise FileNotFoundError(self.name)
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", unlink)
    out = condense_log("calibrate", "\n".join(distinct_lines(3000)))
    assert "could not delete" not in out and [p.exists() for p in old] == [False, False, True]


def test_an_unlistable_log_dir_counts_as_a_failed_prune(monkeypatch):
    class Unlistable:
        def glob(self, pattern):
            raise PermissionError("no")
    monkeypatch.setattr(siril_mcp, "LOG_DIR", Unlistable())
    assert _prune_logs(Path("x.log")) == 1


def test_the_header_path_may_contain_spaces(session, tmp_path, monkeypatch):
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path / "my logs")
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", LIMIT)
    path = saved_path(siril_mcp.session.run_checked("spam"))[2]
    assert path.parent.name == "my logs" and path.exists()


def test_the_log_dir_is_private(direct):
    (path,) = save()
    assert stat.S_IMODE(direct.stat().st_mode) == 0o700 and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_command_verb_is_part_of_the_file_name_and_is_made_filename_safe(direct):
    path = siril_mcp._save_full_log("we!rd/verb ../x", "text")[0]
    assert path is not None and path.parent == direct and siril_mcp._OWN_LOG.fullmatch(path.name)
    assert "-werdverb-" in path.name


@pytest.mark.parametrize("command, verb", [
    ("a" * LOG_VERB_CHARS, "a" * LOG_VERB_CHARS),
    ("a" * (LOG_VERB_CHARS + 1), "a" * LOG_VERB_CHARS),               # capped, not dropped
    ("a" * (LOG_VERB_CHARS - 1), "a" * (LOG_VERB_CHARS - 1)),
    ("", "cmd"), ("   ", "cmd"), ("!!!", "cmd"),                      # no letters, or no command at all: no crash either
])
def test_the_verb_in_the_file_name_is_capped_and_has_a_fallback(direct, command, verb):
    path = siril_mcp._save_full_log(command, "text")[0]
    assert path is not None and re.fullmatch(rf"\d+-\d{{8}}-\d{{6}}-{verb}-\w+\.log", path.name)


def test_the_saved_file_is_the_log_exactly(direct):
    path = siril_mcp._save_full_log("x", "abc\n\ncafé")[0]
    assert path is not None and path.read_bytes() == "abc\n\ncafé".encode()


# -- settings from the environment ------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [(None, 7), ("", 7), ("   ", 7), ("2000", 2000), ("2001", 2001), (" 5000 ", 5000)])
def test_env_number_accepts_good_values_and_defaults_when_unset_or_empty(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("X_SETTING", raising=False)
    else:
        monkeypatch.setenv("X_SETTING", raw)
    assert _env_number("X_SETTING", 7, int, lambda v: v >= 2000, "an integer of at least 2000") == expected


@pytest.mark.parametrize("raw", ["1999", "20k", "1.5", "abc", "-1", "0"])
def test_env_number_rejects_bad_values_naming_the_setting_and_the_value(monkeypatch, raw):
    monkeypatch.setenv("X_SETTING", raw)
    with pytest.raises(SystemExit) as exc:
        _env_number("X_SETTING", 7, int, lambda v: v >= 2000, "an integer of at least 2000")
    assert str(exc.value) == f"X_SETTING must be an integer of at least 2000 (got {raw!r})"


@pytest.mark.parametrize("var, raw, ok", [
    ("SIRIL_MCP_LOG_LIMIT", "2000", True), ("SIRIL_MCP_LOG_LIMIT", "1999", False), ("SIRIL_MCP_LOG_LIMIT", "20k", False),
    ("SIRIL_MCP_TIMEOUT", "0.5", True), ("SIRIL_MCP_TIMEOUT", "0", False), ("SIRIL_MCP_TIMEOUT", "-3", False),
    ("SIRIL_MCP_TIMEOUT", "nan", False), ("SIRIL_MCP_TIMEOUT", "inf", False), ("SIRIL_MCP_TIMEOUT", "soon", False),
])
def test_the_server_refuses_to_start_on_a_bad_setting(var, raw, ok):
    env = {**os.environ, "PYTHONPATH": str(REPO), var: raw}
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import siril_mcp",
                        str(REPO)], env=env, capture_output=True, text=True, check=False)
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert f"{var} must be " in r.stderr and repr(raw) in r.stderr


def test_the_log_dir_setting_is_expanded_and_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIRIL_MCP_LOG_DIR", "~/chosen")
    assert _log_dir_from_env() == tmp_path / "chosen"
    monkeypatch.setenv("SIRIL_MCP_LOG_DIR", "relative/dir")
    assert _log_dir_from_env() == tmp_path / "relative" / "dir"
    monkeypatch.setenv("SIRIL_MCP_LOG_DIR", "")
    assert _log_dir_from_env() == tmp_path / ".cache" / "siril-mcp" / "logs"
    monkeypatch.delenv("SIRIL_MCP_LOG_DIR")
    assert _log_dir_from_env() == tmp_path / ".cache" / "siril-mcp" / "logs"

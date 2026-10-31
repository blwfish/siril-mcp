"""Long-log summarising (condense_log / run_checked).

A command like convert or register logs one or two lines per file, which can be 60-100k characters: more than the
client will show. Past RETURN_LOG_CHARS the server returns a summary that says it is one, with the full log saved to a
file. Nothing may be dropped silently: rare lines stay verbatim, repeated kinds are counted, omissions are announced.
"""
import asyncio
import re

import pytest

import siril_mcp
from siril_mcp import RARE_LINE_MAX, _shape, condense_log

LIMIT = 5000


@pytest.fixture
def logs(session, tmp_path, monkeypatch):
    """The fake-Siril session, with saved logs going to tmp_path and a small return limit."""
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path / "saved-logs")
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", LIMIT)
    return tmp_path / "saved-logs"


def saved_path(text: str):
    m = re.match(r"\[log shortened: (\d+) lines, (\d+) characters; full log saved to (\S+\.log)\]", text)
    assert m, text.splitlines()[0]
    return int(m.group(1)), int(m.group(2)), siril_mcp.Path(m.group(3))


# -- what counts as "the same kind of line" -----------------------------------------------------------------------

@pytest.mark.parametrize("line, shape", [
    ("Reading FITS: file lights_00031.fit, 3 layer(s), 6064x4040 pixels", "Reading FITS: file lights_N.fit, N layer(s), NxN pixels"),
    ("background noise 4.389 (6.698e-05)", "background noise N (Ne-N)"),   # only digits are masked; the sign stays
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

def test_long_log_is_summarised_and_the_full_log_is_saved_verbatim(logs):
    raw = siril_mcp.session.run("spam", 30)[1]
    out = siril_mcp.session.run_checked("spam")
    n_lines, n_chars, path = saved_path(out)
    assert (n_lines, n_chars) == (len(raw.split("\n")), len(raw))
    assert path.parent == logs and path.read_text() == raw + "\n"      # complete and unaltered
    assert len(out) <= LIMIT                                            # bounded
    assert "300x  Reading FITS: file frame_00000.fit" in out            # repeated kinds are counted, with an example
    assert "300x  Saving FITS: file out_00000.fit" in out
    assert "Warning: something unusual happened in the middle" in out   # a rare line survives verbatim
    assert "Execution time: 3 min 7 s" in out                           # so do the closing totals
    assert "frame_00150" not in out                                     # repeated lines are not listed one by one


def test_rare_lines_stay_in_their_original_order(logs):
    out = siril_mcp.session.run_checked("spam")
    assert out.index("Warning: something unusual") < out.index("Execution time: 3 min 7 s")


def test_boundary_a_kind_seen_three_times_is_verbatim_four_times_is_collapsed(monkeypatch, tmp_path):
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", 1500)
    filler = ["filler line"] * 200                       # pushes the log over the limit
    text = "\n".join(["ok 1"] * RARE_LINE_MAX + ["same x 1"] * (RARE_LINE_MAX + 1) + filler)
    out = condense_log("x", text)
    assert out.count("  ok 1") == RARE_LINE_MAX                          # 3 occurrences: shown verbatim
    assert f"{RARE_LINE_MAX + 1}x  same x 1" in out                      # 4 occurrences: collapsed with a count


def test_blank_lines_are_counted_and_labelled(monkeypatch, tmp_path):
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", 1000)
    out = condense_log("x", "\n".join([""] * 1500 + ["the one rare line"]))
    assert "1500x  (blank line)" in out and "the one rare line" in out


def test_very_long_single_line_is_cut_visibly_not_silently(monkeypatch, tmp_path):
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", 2000)
    out = condense_log("x", "start\n" + "y" * 5000 + "\nend")
    assert "...[line cut, 5000 characters]" in out


def test_only_the_most_repeated_kinds_are_listed_and_the_rest_are_counted(monkeypatch, tmp_path):
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", 100)
    kinds = [chr(ord("a") + i) * 3 for i in range(siril_mcp.MAX_SHAPES_SHOWN + 5)]      # 20 distinct kinds, 5 lines each
    out = condense_log("x", "\n".join(k for k in kinds for _ in range(5)))
    assert out.count("   5x  ") == siril_mcp.MAX_SHAPES_SHOWN
    assert "[... 5 more repeated kind(s) not listed; see the full log]" in out


def test_lines_that_are_all_different_are_shown_until_the_budget_then_the_rest_is_counted(logs):
    raw_lines = siril_mcp.session.run("distinct", 30)[1].split("\n")
    out = siril_mcp.session.run_checked("distinct")
    assert len(out) <= LIMIT
    omitted = int(re.search(r"\[\.\.\. (\d+) more such line\(s\) omitted", out).group(1))
    shown = [ln for ln in out.splitlines() if ln.startswith("  item-")]
    assert len(shown) + omitted == len(raw_lines) == 400                # every line is either shown or counted
    assert len(shown) > 50                                              # and a useful number are shown


# -- never silent ------------------------------------------------------------------------------------------------

def test_a_failed_save_is_reported_in_the_summary(session, tmp_path, monkeypatch):
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    monkeypatch.setattr(siril_mcp, "LOG_DIR", blocker / "logs")          # cannot be created under a file
    monkeypatch.setattr(siril_mcp, "RETURN_LOG_CHARS", LIMIT)
    out = siril_mcp.session.run_checked("spam")
    assert out.startswith("[log shortened:") and "full log could NOT be saved (" in out
    assert "Execution time: 3 min 7 s" in out                             # the summary itself is still useful


def test_a_failing_command_with_a_long_log_reports_the_summary_in_the_error(logs):
    with pytest.raises(RuntimeError) as exc:
        siril_mcp.session.run_checked("spamfail")
    msg = str(exc.value)
    assert msg.startswith("Siril command failed: spamfail\n[log shortened:")
    assert len(msg) < LIMIT + 200
    saved_path(msg.split("\n", 1)[1])                                     # and the saved log really exists


def test_the_tool_boundary_returns_the_summary(logs):
    out = asyncio.run(siril_mcp.siril_command("spam"))
    assert out.startswith("[log shortened:") and len(out) <= LIMIT


# -- housekeeping ------------------------------------------------------------------------------------------------

def test_only_the_newest_saved_logs_are_kept(logs, monkeypatch):
    monkeypatch.setattr(siril_mcp, "MAX_LOG_FILES", 3)
    paths = []
    for _ in range(5):
        paths.append(saved_path(siril_mcp.session.run_checked("spam"))[2])
    assert sorted(p.name for p in logs.glob("*.log")) == sorted(p.name for p in paths[-3:])


def test_the_command_verb_is_part_of_the_file_name_and_is_made_filename_safe(logs):
    path = siril_mcp._save_full_log("we!rd/verb ../x", "text")[0]
    assert path is not None and path.name.endswith(".log") and "/" not in path.name[len(str(path.parent)):]
    assert "werdverb" in path.name

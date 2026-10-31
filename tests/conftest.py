import asyncio
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import siril_mcp

FAKE_SIRIL = Path(__file__).resolve().parent / "fake_siril.py"


@pytest.fixture
def command_log(tmp_path, monkeypatch) -> Path:
    """File the fake Siril appends every received command to (read it with `logged(command_log)`)."""
    path = tmp_path / "commands.log"
    monkeypatch.setenv("FAKE_SIRIL_LOG", str(path))
    return path


@pytest.fixture
def session(tmp_path, monkeypatch, command_log):
    """A fresh SirilSession wired to the fake Siril, installed as siril_mcp.session so the tools use it.

    Pipe dirs go under tmp_path (not the real temp dir) so tests can inspect and sweep them, and saved logs go to
    tmp_path/saved-logs, never the real ~/.cache/siril-mcp/logs."""
    FAKE_SIRIL.chmod(FAKE_SIRIL.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(siril_mcp, "SIRIL_BIN", str(FAKE_SIRIL))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(siril_mcp, "LOG_DIR", tmp_path / "saved-logs")
    monkeypatch.setattr(siril_mcp, "_gate", asyncio.Lock())  # a lock binds to the first loop that contends on it
    s = siril_mcp.SirilSession()
    s.workdir = str(tmp_path)
    monkeypatch.setattr(siril_mcp, "session", s)
    yield s
    s.stop()


def logged(command_log: Path) -> list[str]:
    return command_log.read_text().splitlines() if command_log.exists() else []

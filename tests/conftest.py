import sys
from pathlib import Path

import pytest

import app.cli
from app.cli.config import clear_settings_cache
from app.config import settings

HERE = Path(__file__).parent
FAKE_CLAUDE = HERE / "fake_claude.py"
FAKE_CODEX = HERE / "fake_codex.py"


async def _no_wait(_seconds):
    return None


async def _online(_host):
    return True


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Never touch the real app/admin/inputs, app/output, the Z: drive or the real CLIs."""
    monkeypatch.setattr(settings, "admin_dir", tmp_path / "admin")
    monkeypatch.setattr(settings, "scripts_config_dir", tmp_path / "z" / "_scripts")
    monkeypatch.setenv("CLI_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("CLI_DEFAULT_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_CLI_BIN", f"{sys.executable} {FAKE_CLAUDE}")
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("CLAUDE_CLI_EFFORT", "medium")
    monkeypatch.setenv("CODEX_CLI_BIN", f"{sys.executable} {FAKE_CODEX}")
    monkeypatch.setenv("CODEX_CLI_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("CODEX_CLI_REASONING_EFFORT", "medium")
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_MODE", raising=False)
    monkeypatch.setattr(app.cli, "_sleep", _no_wait)  # retries without the 3-5 s pauses
    monkeypatch.setattr(app.cli, "_reachable", _online)  # never check the real network
    clear_settings_cache()
    yield
    clear_settings_cache()

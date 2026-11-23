import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin import scripts_config as sc
from app.cli import CliError, CliTimeout, run_codex
from app.cli.codex_client import build_command, parse_events
from app.cli.config import CodexCliSettings, clear_settings_cache, get_cli_settings, provider_info
from app.main import app
from tests.test_admin import TSV

client = TestClient(app)


@pytest.fixture
def config():
    cfg = sc.parse_tsv(TSV).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    return cfg


def echoed(text: str) -> dict:
    assert text.startswith("RESULT\n")
    return json.loads(text.split("\n", 1)[1])


# ------------------------------------------------------------ settings + command

def test_codex_defaults_are_gpt_5_6_sol(monkeypatch):
    for var in ("CODEX_CLI_MODEL", "CODEX_CLI_REASONING_EFFORT"):
        monkeypatch.delenv(var)
    s = CodexCliSettings(_env_file=None)
    assert (s.model, s.reasoning_effort, s.web_search, s.sandbox) == (
        "gpt-5.6-sol", "medium", "live", "read-only")


def test_codex_settings_from_env(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_MODEL", "gpt-6-sol")
    monkeypatch.setenv("CODEX_CLI_REASONING_EFFORT", "high")
    monkeypatch.setenv("CLI_DEFAULT_PROVIDER", "codex")
    clear_settings_cache()
    info = provider_info()
    assert info["default"] == "codex"
    assert {p["key"]: (p["model"], p["effort"]) for p in info["providers"]} == {
        "claude": ("claude-sonnet-5", "medium"), "codex": ("gpt-6-sol", "high")}


def test_codex_command():
    cmd = build_command(CodexCliSettings(_env_file=None, bin="codex", extra_args="--oss"),
                        Path("/tmp/w"))
    assert cmd[:3] == ["codex", "--no-daemon", "exec"]  # its own process, not the daemon
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
    assert "model_reasoning_effort=medium" in cmd and "web_search=live" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("-C") + 1] == "/tmp/w"
    assert cmd[cmd.index("-o") + 1] == "/tmp/w/last_message.txt"
    assert {"--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--oss"} <= set(cmd)
    assert cmd[-1] == "-"


def test_codex_command_with_daemon_and_resume():
    s = CodexCliSettings(_env_file=None, bin="codex", no_daemon=False)
    cmd = build_command(s, Path("/tmp/w"), resume="t1")
    assert cmd[:2] == ["codex", "exec"] and "--no-daemon" not in cmd
    assert cmd[-3:] == ["resume", "t1", "-"] and "--ephemeral" not in cmd
    cmd = build_command(CodexCliSettings(_env_file=None, bin="codex"), Path("/tmp/w"), resume="t1")
    assert cmd[:3] == ["codex", "--no-daemon", "exec"] and cmd[-3:] == ["resume", "t1", "-"]


def test_parse_events():
    stream = b"\n".join(json.dumps(e).encode() for e in [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}},
        {"type": "item.completed", "item": {"type": "web_search"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Final"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]) + b"\nnot json\n"
    ev = parse_events(stream)
    assert ev["thread_id"] == "t1" and ev["messages"] == ["Final"]
    assert ev["tool_calls"] == 1 and ev["usage"]["output_tokens"] == 2


# ------------------------------------------------------------ client

def test_run_codex_prepends_instructions_and_runs_in_temp_dir():
    result = asyncio.run(run_codex("Écris — ✓"))
    data = echoed(result.text)
    # The system prompt from the brands file (tests use app/brands.example.json).
    assert data["prompt"].startswith("# Instructions\nYou are the writer for the example brands.")
    assert data["prompt"].endswith("Écris — ✓")
    assert "codex-run-" in data["cwd"] and not Path(data["cwd"]).exists()  # cleaned up
    assert result.provider == "codex" and result.model == "gpt-5.6-sol"
    assert result.cost_usd is None and result.usage == {"input_tokens": 100, "output_tokens": 50}
    assert result.num_turns == 2


def test_falls_back_to_last_message_event(monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "nofile")
    assert asyncio.run(run_codex("hi")).text.startswith("RESULT\n")


def test_codex_error_is_reported(monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "error")
    with pytest.raises(CliError, match="usage limit reached"):
        asyncio.run(run_codex("hi"))


def test_codex_timeout(monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "slow")
    monkeypatch.setenv("CODEX_CLI_TIMEOUT_SECONDS", "1")
    clear_settings_cache()
    with pytest.raises(CliTimeout, match="CODEX_CLI_TIMEOUT_SECONDS"):
        asyncio.run(run_codex("hi"))


def test_codex_missing_binary(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_BIN", "/nope/codex")
    clear_settings_cache()
    with pytest.raises(CliError, match="CODEX_CLI_BIN"):
        asyncio.run(run_codex("hi"))


# ------------------------------------------------------------ endpoint

def run(**overrides):
    body = {"brand": "brand_a", "step": "outline", "language": "English",
            "inputs": {"video_title": "The Long Night", "inspirational_script": "Source text"}}
    body.update(overrides)
    return client.post("/api/scripting/run", json=body)


def test_run_with_codex(config):
    r = run(provider="codex")
    assert r.status_code == 200, r.text
    data = r.json()
    prompt = echoed(data["text"])["prompt"]
    assert "# Task\nOutline P" in prompt and "The Long Night" in prompt
    assert data["meta"]["provider"] == "codex" and data["meta"]["model"] == "gpt-5.6-sol"
    assert data["meta"]["cost_usd"] is None and data["usage"]["output_tokens"] == 50

    meta = json.loads((get_cli_settings().output_dir / "outline" / data["file"]["name"])
                      .with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["provider"] == "codex"
    listed = client.get("/api/scripting/outputs", params={"step": "outline"}).json()["outputs"]
    assert listed[0]["provider"] == "codex"


def test_provider_defaults_to_setting(config, monkeypatch):
    monkeypatch.setenv("CLI_DEFAULT_PROVIDER", "codex")
    clear_settings_cache()
    assert run().json()["meta"]["provider"] == "codex"


def test_claude_still_selectable(config):
    assert run(provider="claude").json()["meta"]["provider"] == "claude"


def test_unknown_provider_rejected(config):
    assert run(provider="gemini").status_code == 422


def test_codex_failure_is_502(config, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "error")
    r = run(provider="codex")
    assert r.status_code == 502 and "usage limit reached" in r.json()["detail"]


def test_providers_endpoint_and_page_dropdown(config):
    info = client.get("/api/scripting/providers").json()
    assert [p["key"] for p in info["providers"]] == ["claude", "codex"]
    page = client.get("/scripting").text
    assert "Generate with" in page and 'data-provider="codex"' in page and "gpt-5.6-sol" in page

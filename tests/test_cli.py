import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.admin import scripts_config as sc
from app.cli import CliError, CliTimeout, run_claude
from app.cli.claude_client import build_command
from app.cli.config import ClaudeCliSettings, clear_settings_cache, get_claude_settings, get_cli_settings
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
    """The fake CLI answers 'RESULT\\n{argv, prompt}'."""
    assert text.startswith("RESULT\n")
    return json.loads(text.split("\n", 1)[1])


# ------------------------------------------------------------ settings + command

def test_settings_come_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "opus")
    monkeypatch.setenv("CLAUDE_CLI_EFFORT", "high")
    monkeypatch.setenv("CLAUDE_CLI_TOOLS", "")
    monkeypatch.setenv("CLAUDE_CLI_MAX_BUDGET_USD", "1.5")
    clear_settings_cache()
    s = get_claude_settings()
    assert (s.model, s.effort, s.tools, s.max_budget_usd) == ("opus", "high", "", 1.5)


def test_defaults_are_sonnet_5_medium(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MODEL")
    monkeypatch.delenv("CLAUDE_CLI_EFFORT")
    s = ClaudeCliSettings(_env_file=None)
    assert (s.model, s.effort) == ("claude-sonnet-5", "medium")


def test_build_command():
    s = ClaudeCliSettings(_env_file=None, bin="claude", tools="WebSearch, WebFetch",
                          fallback_model="sonnet", max_budget_usd=2, extra_args="--verbose")
    cmd = build_command(s)
    assert cmd[:4] == ["claude", "-p", "--output-format", "json"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    i = cmd.index("--allowedTools")
    assert cmd[i + 1:i + 3] == ["WebSearch", "WebFetch"]
    assert cmd[cmd.index("--fallback-model") + 1] == "sonnet"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.0"
    assert {"--safe-mode", "--no-session-persistence", "--verbose"} <= set(cmd)


def test_no_tools_disables_all():
    cmd = build_command(ClaudeCliSettings(_env_file=None, tools=""))
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd


# ------------------------------------------------------------ client

def test_run_sends_prompt_on_stdin_unicode_safe():
    result = asyncio.run(run_claude("Écris en français — ✓"))
    data = echoed(result.text)
    assert data["prompt"] == "Écris en français — ✓"
    assert result.cost_usd == 0.0123 and result.model == "claude-sonnet-5"


@pytest.mark.parametrize("mode, message", [
    ("error", "Model overloaded"),
    ("garbage", "without a usable response"),
])
def test_run_reports_cli_errors(monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(CliError, match=message):
        asyncio.run(run_claude("hi"))


def test_run_times_out(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setenv("CLAUDE_CLI_TIMEOUT_SECONDS", "1")
    clear_settings_cache()
    with pytest.raises(CliTimeout):
        asyncio.run(run_claude("hi"))


def test_missing_binary(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_BIN", "/nope/claude")
    clear_settings_cache()
    with pytest.raises(CliError, match="CLAUDE_CLI_BIN"):
        asyncio.run(run_claude("hi"))


# ------------------------------------------------------------ run endpoint

def run(**overrides):
    body = {"brand": "brand_a", "branch": 0, "step": "outline", "language": "French",
            "inputs": {"video_title": "The Long Night", "inspirational_script": "Source text",
                       "guest_name": "Sam Rivers", "unrelated": "dropped"}}
    body.update(overrides)
    return client.post("/api/scripting/run", json=body)


def test_run_step_builds_prompt_saves_and_serves_txt(config):
    r = run()
    assert r.status_code == 200, r.text
    data = r.json()
    sent = echoed(data["text"])
    prompt = sent["prompt"]

    assert prompt.startswith("# Task\nOutline P")           # admin prompt from config
    assert '<input name="video_title" label="Video title">\nThe Long Night\n</input>' in prompt
    assert "Source text" in prompt and "Sam Rivers" in prompt
    assert "dropped" not in prompt                           # unknown inputs ignored
    assert "Output language: French" in prompt
    assert "3,000–3,500 words" in prompt                     # brand targets from catalog
    assert "Outline P" in prompt and "Script P" not in prompt
    assert sent["argv"][sent["argv"].index("--model") + 1] == "claude-sonnet-5"

    f = data["file"]
    assert f["step"] == "outline" and f["name"].startswith("brand_a__the-long-night__")
    path = get_cli_settings().output_dir / "outline" / f["name"]
    assert path.read_text(encoding="utf-8") == data["text"] + "\n"
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["language"] == "French" and meta["inputs"]["video_title"] == "The Long Night"

    dl = client.get(f["url"])
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.text == data["text"] + "\n"

    listed = client.get("/api/scripting/outputs", params={"step": "outline", "brand": "brand_a"})
    assert [o["name"] for o in listed.json()["outputs"]] == [f["name"]]
    assert listed.json()["outputs"][0]["title"] == "The Long Night"


def test_saved_outline_feeds_script_step_manually(config):
    outline = run().json()
    text = client.get(outline["file"]["url"] + "?inline=true").text
    r = run(step="script", inputs={"video_title": "The Long Night", "outline": text})
    assert r.status_code == 200
    prompt = echoed(r.json()["text"])["prompt"]
    assert prompt.startswith("# Task\nScript P")
    assert '<input name="outline" label="Outline" from="outline step">' in prompt
    assert (get_cli_settings().output_dir / "script" / r.json()["file"]["name"]).is_file()


@pytest.mark.parametrize("overrides, status, message", [
    ({"brand": "nope"}, 404, "Unknown brand"),
    ({"step": "research"}, 404, "isn't part"),
    ({"branch": 5}, 404, "no longer exists"),
    ({"language": "Klingon"}, 400, "doesn't publish"),
    ({"inputs": {"video_title": "x"}}, 400, "Inspirational script"),
])
def test_run_validation(config, overrides, status, message):
    r = run(**overrides)
    assert r.status_code == status
    assert message in r.json()["detail"]


def test_run_cli_error_is_502_and_nothing_saved(config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    r = run()
    assert r.status_code == 502
    assert "Model overloaded" in r.json()["detail"]
    assert not (get_cli_settings().output_dir / "outline").exists()


def test_conditional_branch_runs_paraphrasing(config):
    r = run(brand="brand_d", branch=1, step="paraphrasing", language="English",
            inputs={"competitor_script": "Their script"}, project_name="Top 10 rivals")
    assert r.status_code == 200, r.text
    assert "Para P" in echoed(r.json()["text"])["prompt"]


@pytest.mark.parametrize("url", [
    "/api/scripting/outputs/outline/..%2F..%2Fsecret.txt",
    "/api/scripting/outputs/etc/passwd.txt",
    "/api/scripting/outputs/outline/missing.txt",
])
def test_download_rejects_bad_paths(url):
    assert client.get(url).status_code == 404


def test_outputs_rejects_bad_brand():
    assert client.get("/api/scripting/outputs", params={"step": "outline", "brand": "../x"}).status_code == 400


def test_run_saves_exact_payload_next_to_output(config):
    from app.web.outputs import payload_path

    data = run().json()
    sent = echoed(data["text"])  # what the fake CLI actually received
    out = get_cli_settings().output_dir / "outline" / data["file"]["name"]
    dump = payload_path(out).read_text(encoding="utf-8")

    stdin = dump.split(" STDIN (", 1)[1].split("\n", 1)[1].rsplit("\n" + "=" * 22 + " END OF STDIN", 1)[0]
    assert stdin == sent["prompt"]
    assert "] --system-prompt\n" in dump and "<SYSTEM PROMPT, see next section>" in dump
    assert "--model" in dump and "claude-sonnet-5" in dump
    # payload files are not listed or downloadable as outputs
    names = [o["name"] for o in client.get("/api/scripting/outputs", params={"step": "outline"}).json()["outputs"]]
    assert names == [data["file"]["name"]]
    assert client.get(f"/api/scripting/outputs/outline/{payload_path(out).name}").status_code == 404


# ------------------------------------------------------------ per-step effort

def test_step_efforts_defaults():
    from app.cli.config import CliSettings

    s = CliSettings(_env_file=None)
    assert {st: s.effort_for(st) for st in ("research", "outline", "script", "audits", "paraphrasing")} == {
        "research": "high", "outline": "high", "script": "medium", "audits": "high", "paraphrasing": "high"}
    assert s.effort_for("Outline") == "high"     # case-insensitive
    assert s.effort_for("voiceover") is None      # unknown step -> provider default


def test_step_efforts_from_env(monkeypatch):
    monkeypatch.setenv("CLI_STEP_EFFORTS", "outline:low; script:xhigh")
    clear_settings_cache()
    s = get_cli_settings()
    assert (s.effort_for("outline"), s.effort_for("script"), s.effort_for("audits")) == ("low", "xhigh", None)


@pytest.mark.parametrize("raw", ["outline=high", "outline:extreme", ":high"])
def test_bad_step_efforts_rejected(raw):
    from pydantic import ValidationError

    from app.cli.config import CliSettings

    with pytest.raises(ValidationError, match="CLI_STEP_EFFORTS"):
        CliSettings(_env_file=None, step_efforts=raw)


def _argv_effort(argv, flag="--effort"):
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize("step, inputs, expected", [
    ("outline", {"video_title": "T", "inspirational_script": "C"}, "high"),
    ("script", {"video_title": "T", "outline": "O"}, "medium"),
    ("audits", {"video_title": "T", "script": "S"}, "high"),
])
def test_run_uses_step_effort_for_claude(config, step, inputs, expected):
    data = run(step=step, inputs=inputs, new_project=True).json()
    assert _argv_effort(echoed(data["text"])["argv"]) == expected
    assert data["meta"]["effort"] == expected
    dump = (get_cli_settings().output_dir / step / data["file"]["name"]).with_name(
        data["file"]["name"][:-4] + ".payload.txt").read_text(encoding="utf-8")
    assert f"--effort\n[" in dump and f"] {expected}\n" in dump


def test_run_uses_step_effort_for_codex(config):
    data = run(provider="codex").json()
    assert "model_reasoning_effort=high" in echoed(data["text"])["argv"]
    assert data["meta"]["effort"] == "high"


def test_unlisted_step_uses_provider_default(config, monkeypatch):
    monkeypatch.setenv("CLI_STEP_EFFORTS", "script:medium")
    monkeypatch.setenv("CLAUDE_CLI_EFFORT", "low")
    clear_settings_cache()
    data = run().json()  # outline: not listed -> CLAUDE_CLI_EFFORT
    assert _argv_effort(echoed(data["text"])["argv"]) == "low"


def test_ultra_becomes_max_for_claude(config, monkeypatch):
    monkeypatch.setenv("CLI_STEP_EFFORTS", "outline:ultra")
    clear_settings_cache()
    claude = run().json()
    assert _argv_effort(echoed(claude["text"])["argv"]) == "max"
    codex = run(provider="codex").json()
    assert "model_reasoning_effort=ultra" in echoed(codex["text"])["argv"]


def test_page_gets_step_efforts(config):
    import re

    page = client.get("/scripting").text
    block = re.search(r'"step_efforts": (\{[^}]*\})', page)
    assert block and json.loads(block.group(1)) == {
        "research": "high", "outline": "high", "script": "medium", "audits": "high",
        "paraphrasing": "high"}


# ------------------------------------------------------------ effort picked on the page

def test_picked_effort_overrides_the_step_effort(config):
    data = run(effort="low", new_project=True).json()   # outline is "high" in CLI_STEP_EFFORTS
    assert _argv_effort(echoed(data["text"])["argv"]) == "low" and data["meta"]["effort"] == "low"
    codex = run(provider="codex", effort="ultra", new_project=True).json()
    assert "model_reasoning_effort=ultra" in echoed(codex["text"])["argv"]


def test_empty_effort_keeps_the_step_effort(config):
    data = run(effort=None, new_project=True).json()
    assert data["meta"]["effort"] == "high"


@pytest.mark.parametrize("provider, effort", [("claude", "ultra"), ("codex", "extreme")])
def test_effort_the_model_doesnt_have_is_rejected(config, provider, effort):
    r = run(provider=provider, effort=effort, new_project=True)
    assert r.status_code == 400 and effort in r.json()["detail"]


def test_providers_list_their_effort_levels():
    from app.cli.config import provider_info
    levels = {p["key"]: p["efforts"] for p in provider_info()["providers"]}
    assert levels == {"claude": ["low", "medium", "high", "xhigh", "max"],
                      "codex": ["low", "medium", "high", "xhigh", "max", "ultra"]}

"""Automatic retries for failed CLI calls (app/cli/__init__.py run_cli)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.cli import CliBusy, CliError, CliResult, CliTimeout, run_cli
from app.main import app

ROUTING = ("Codex returned an error: Reconnecting... 5/5 (workspace routing discovery failed); "
           "workspace routing discovery failed")


class Flaky:
    """_run_once stand-in: fails `fails` times with `error`, then answers."""

    def __init__(self, fails, error=None):
        self.fails, self.error, self.calls = fails, error or CliError(ROUTING), []

    async def __call__(self, provider, prompt, effort, **kw):
        self.calls.append(kw.get("resume"))
        if len(self.calls) <= self.fails:
            raise self.error
        return CliResult("done", provider, "m", effort or "medium", 1.0, session_id="s1")


@pytest.fixture
def waits(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(cli, "_sleep", fake_sleep)
    return slept


def run(**kw):
    return asyncio.run(run_cli("codex", "p", resume="thread-1", **kw))


def test_a_connection_failure_is_retried_in_the_same_conversation(monkeypatch, waits):
    flaky = Flaky(2)
    monkeypatch.setattr(cli, "_run_once", flaky)
    seen = []
    r = run(on_retry=lambda n, total, exc, wait: seen.append((n, total, wait)))
    assert r.text == "done" and r.retries == 2
    assert flaky.calls == ["thread-1"] * 3              # same conversation every time
    assert waits == [3.0, 4.0] and seen == [(1, 3, 3.0), (2, 3, 4.0)]
    assert all(3 <= w <= 5 for w in cli.RETRY_WAITS_S)


def test_at_most_three_retries_then_the_error_says_so(monkeypatch, waits):
    flaky = Flaky(99)
    monkeypatch.setattr(cli, "_run_once", flaky)
    with pytest.raises(CliError, match=r"still failing after 3 retries") as err:
        run()
    assert "workspace routing discovery failed" in str(err.value)
    assert len(flaky.calls) == 4 and waits == [3.0, 4.0, 5.0] and cli.MAX_RETRIES == 3
    flaky2 = Flaky(99)
    monkeypatch.setattr(cli, "_run_once", flaky2)
    with pytest.raises(CliError):
        run(retries=10)                                   # never more than 3
    assert len(flaky2.calls) == 4


def test_first_try_working_means_no_wait(monkeypatch, waits):
    monkeypatch.setattr(cli, "_run_once", Flaky(0))
    assert run().retries == 0 and waits == []


@pytest.mark.parametrize("error", [
    CliTimeout("Codex didn't finish within 1200 s."),              # already used the time limit
    CliError("Codex CLI not found (codex). Set CODEX_CLI_BIN in .env."),
    CliError("No conversation found with session ID: abc"),
    CliError("Codex returned an error: 401 Unauthorized. Please log in again."),
    CliError("Claude has no 'ultra' effort. Use one of: low, medium."),
])
def test_failures_a_retry_cant_fix_are_not_retried(monkeypatch, waits, error):
    flaky = Flaky(99, error)
    monkeypatch.setattr(cli, "_run_once", flaky)
    with pytest.raises(type(error)):
        run()
    assert len(flaky.calls) == 1 and waits == []


def test_busy_keeps_its_type_after_retries(monkeypatch, waits):
    monkeypatch.setattr(cli, "_run_once", Flaky(99, CliBusy("2 Codex run(s) already in progress.")))
    with pytest.raises(CliBusy, match="still failing after 3 retries"):
        run()


# ------------------------------------------------------------ through a real step and rules

def test_a_step_retries_and_says_how_many_it_took(monkeypatch, waits):
    real = cli._run_once
    state = {"n": 0}

    async def flaky_once(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise CliError(ROUTING)
        return await real(*a, **kw)
    monkeypatch.setattr(cli, "_run_once", flaky_once)
    from tests.test_cli import run as run_step
    _setup_config()
    r = run_step(new_project=True)
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["retries"] == 1 and waits == [3.0]


def _setup_config():
    from app.admin import scripts_config as sc
    from tests.test_cli import TSV
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(sc.parse_tsv(TSV).config), encoding="utf-8")


def test_rule_prompt_retries_show_on_the_page(monkeypatch, waits, tmp_path):
    from app.admin import scripts_config as sc
    from tests import test_rules as tr
    cfg = sc.parse_tsv(tr.tsv([tr.NBA2_ROW])).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    real = cli._run_once
    state = {"n": 0}

    async def flaky_once(provider, prompt, *a, **kw):
        if prompt.startswith("# Rule: APPLY_CHANGES") and state["n"] == 0:
            state["n"] += 1
            raise CliError(ROUTING)
        return await real(provider, prompt, *a, **kw)
    monkeypatch.setattr(cli, "_run_once", flaky_once)
    audits = tr.flow()
    done = TestClient(app).get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert done["status"] == "done", done
    apply = next(e for e in done["log"] if e["name"] == "APPLY_CHANGES")
    assert apply["retries"] == 1 and "retry" not in apply
    assert done["final"]["text"].startswith("REVISED SCRIPT")

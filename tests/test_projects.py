"""Projects: the first step starts a conversation, later steps resume it."""

import json

import pytest
from fastapi.testclient import TestClient

from app.admin import scripts_config as sc
from app.cli.claude_client import build_command as claude_command
from app.cli.codex_client import build_command as codex_command
from app.cli.config import ClaudeCliSettings, CodexCliSettings, get_cli_settings
from app.main import app
from app.web import projects
from tests.test_admin import TSV

client = TestClient(app)
TITLE = "Kobe's 81"


@pytest.fixture(autouse=True)
def config():
    cfg = sc.parse_tsv(TSV).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    return cfg


def echoed(text: str) -> dict:
    assert text.startswith("RESULT\n")
    return json.loads(text.split("\n", 1)[1])


def run(step="outline", inputs=None, **overrides):
    defaults = {
        "outline": {"video_title": TITLE, "inspirational_script": "Competitor text"},
        "script": {"video_title": TITLE},
        "audits": {"video_title": TITLE},
    }
    body = {"brand": "nba_brand_1", "branch": 0, "step": step, "language": "English",
            "inputs": defaults[step] if inputs is None else inputs}
    body.update(overrides)
    return client.post("/api/scripting/run", json=body)


def argv_of(response) -> list[str]:
    return echoed(response.json()["text"])["argv"]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


# ------------------------------------------------------------ first step

def test_first_step_starts_a_project_with_a_saved_session():
    r = run()
    assert r.status_code == 200, r.text
    argv = argv_of(r)
    assert "--resume" not in argv
    assert "--no-session-persistence" not in argv  # must be saved to be resumed later

    p = r.json()["project"]
    assert p["name"] == TITLE and p["steps"] == ["outline"] and p["provider"] == "claude"
    assert "session_id" not in p  # session ids stay on the server

    saved = projects.load(p["id"])
    assert saved["session_id"].startswith("fake-session-")
    assert saved["steps"][0]["file"] == r.json()["file"]["name"]
    assert r.json()["meta"]["resumed"] is False


def test_first_step_always_new_by_default():
    a, b = run().json()["project"], run().json()["project"]
    assert a["id"] != b["id"]
    assert len(projects.find("nba_brand_1", 0, TITLE)) == 2


# ------------------------------------------------------------ next steps resume

def test_next_steps_resume_the_same_conversation():
    first = run()
    session = projects.load(first.json()["project"]["id"])["session_id"]

    script = run("script")  # toggle off by default for a later step
    assert script.status_code == 200, script.text
    assert flag(argv_of(script), "--resume") == session
    prompt = echoed(script.json()["text"])["prompt"]
    # The outline wasn't pasted: the model is pointed at its own earlier answer.
    assert '<input name="outline" label="Outline" from="outline step">' in prompt
    assert "use your Outline step response from earlier in this conversation" in prompt
    assert f"- Project: {TITLE}. This continues the same conversation" in prompt

    audits = run("audits")
    assert flag(argv_of(audits), "--resume") == session
    p = audits.json()["project"]
    assert p["id"] == first.json()["project"]["id"]
    assert p["steps"] == ["outline", "script", "audits"]
    assert audits.json()["meta"]["resumed"] is True

    meta = json.loads((get_cli_settings().output_dir / "audits" / audits.json()["file"]["name"])
                      .with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["project_id"] == p["id"] and meta["resumed_session_id"] == session


def test_pasted_output_is_still_sent_when_continuing():
    run()
    prompt = echoed(run("script", {"video_title": TITLE, "outline": "Outline B only"})
                    .json()["text"])["prompt"]
    block = '<input name="outline" label="Outline" from="outline step">\nOutline B only\n</input>'
    assert block in prompt
    # Already sent with the outline step, so only pointed at.
    assert "use the video title sent with the Outline step earlier in this conversation" in prompt


def test_name_match_ignores_case_and_spacing():
    run(project_name="Kobe   Night")
    assert run("script", project_name="kobe night").status_code == 200


def test_step_not_run_in_project_must_be_pasted():
    run()
    r = run("audits")  # Script never ran in this project, so its output isn't in the chat
    assert r.status_code == 400
    assert "Script" in r.json()["detail"] and "hasn't run the Script step" in r.json()["detail"]


# ------------------------------------------------------------ not found / toggle on

def test_later_step_without_project_is_not_found():
    r = run("script")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "project_not_found" and d["first_step"] == "outline"
    assert "Turn on New project" in d["message"] and "Outline" in d["message"]
    assert not projects.all_projects()


def test_toggle_on_starts_a_new_project_at_any_step():
    run()
    r = run("script", {"video_title": TITLE, "outline": "O"}, new_project=True)
    assert r.status_code == 200
    assert "--resume" not in argv_of(r)
    assert r.json()["project"]["steps"] == ["script"]
    assert len(projects.find("nba_brand_1", 0, TITLE)) == 2


def test_toggle_on_needs_earlier_outputs_pasted():
    r = run("script", new_project=True)
    assert r.status_code == 400 and "Outline" in r.json()["detail"]


def test_toggle_off_on_first_step_continues():
    first = run().json()["project"]
    again = run(new_project=False)
    assert again.json()["project"]["id"] == first["id"]
    assert again.json()["project"]["steps"] == ["outline", "outline"]


def test_project_needs_a_name():
    r = run(inputs={"inspirational_script": "C", "video_title": "  "})
    assert r.status_code == 400 and "name" in r.json()["detail"]


# ------------------------------------------------------------ several with the same name

def test_same_named_projects_ask_to_choose_then_continue():
    older = run().json()["project"]
    newer = run().json()["project"]
    r = run("script")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "choose_project"
    assert [p["id"] for p in d["projects"]] == [newer["id"], older["id"]]  # recent first

    chosen = run("script", project_id=older["id"])
    assert chosen.status_code == 200
    assert flag(argv_of(chosen), "--resume") == projects.load(older["id"])["session_id"]
    assert projects.load(older["id"])["steps"][-1]["step"] == "script"
    assert projects.step_names(projects.load(newer["id"])) == ["outline"]  # untouched


def test_unknown_project_id_is_not_found():
    r = run("script", project_id="nba_brand_1__nope__20260101-000000")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "project_not_found"


def test_projects_are_per_brand():
    run()
    r = run("script", brand="nba_brand_2", inputs={"video_title": TITLE})
    assert r.status_code == 404


# ------------------------------------------------------------ providers

def test_project_keeps_its_provider():
    run(provider="codex")
    assert run("script").json()["meta"]["provider"] == "codex"  # no provider sent
    r = run("audits", provider="claude")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "provider_mismatch"
    assert r.json()["detail"]["provider"] == "codex"


def test_codex_project_resumes_thread_in_its_own_folder():
    first = run(provider="codex")
    assert first.status_code == 200, first.text
    pid = first.json()["project"]["id"]
    thread = projects.load(pid)["session_id"]
    assert thread.startswith("fake-thread-")
    sent1 = echoed(first.json()["text"])
    assert "--ephemeral" not in sent1["argv"] and "resume" not in sent1["argv"]
    assert sent1["cwd"] == str(projects.workdir(pid))

    second = run("script", provider="codex")
    sent2 = echoed(second.json()["text"])
    i = sent2["argv"].index("resume")
    assert sent2["argv"][i:i + 3] == ["resume", thread, "-"]
    assert sent2["cwd"] == sent1["cwd"]


# ------------------------------------------------------------ failures

def test_failed_first_step_leaves_no_project(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    assert run().status_code == 502
    assert projects.all_projects() == []
    assert not any(projects.projects_dir().iterdir())


def test_failed_resume_suggests_new_project(monkeypatch):
    pid = run().json()["project"]["id"]
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "resume_error")
    r = run("script")
    assert r.status_code == 502
    d = r.json()["detail"]
    assert d["code"] == "resume_failed" and "No conversation found" in d["message"]
    assert projects.step_names(projects.load(pid)) == ["outline"]  # unchanged


def test_projects_list_endpoint():
    run()
    run(project_name="Other")
    listed = client.get("/api/scripting/projects", params={"brand": "nba_brand_1"}).json()
    assert {p["name"] for p in listed["projects"]} == {TITLE, "Other"}
    one = client.get("/api/scripting/projects",
                     params={"brand": "nba_brand_1", "name": "other"}).json()
    assert [p["name"] for p in one["projects"]] == ["Other"]
    assert client.get("/api/scripting/projects", params={"brand": "../x"}).status_code == 400


# ------------------------------------------------------------ commands

def test_commands_keep_one_off_runs_unsaved():
    claude = ClaudeCliSettings(_env_file=None)
    assert "--no-session-persistence" in claude_command(claude)
    assert "--no-session-persistence" not in claude_command(claude, keep_session=True)
    cmd = claude_command(claude, resume="s1")
    assert cmd[cmd.index("--resume") + 1] == "s1" and "--no-session-persistence" not in cmd

    from pathlib import Path
    codex = CodexCliSettings(_env_file=None)
    assert "--ephemeral" in codex_command(codex, Path("/w"))
    cmd = codex_command(codex, Path("/w"), resume="t1")
    assert cmd[-3:] == ["resume", "t1", "-"] and "--ephemeral" not in cmd


def test_page_has_project_controls():
    page = client.get("/scripting").text
    assert 'id="project-name"' in page and 'id="new-project"' in page and 'role="switch"' in page

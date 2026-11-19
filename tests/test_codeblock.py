"""Code-block results, {{name}} placeholders and << >> context markers (nba_brand_2 style)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.admin import scripts_config as sc
from app.cli.config import get_cli_settings
from app.main import app
from app.web import outputs, projects
from app.web.extract import code_block, wants_code_block
from app.web.scripting import find_brand
from tests.test_admin import TSV

client = TestClient(app)
TITLE = "Durant drops 25"
COMPETITOR = "COMPETITOR SCRIPT TEXT " * 20
FENCE = "```"

PROMPTS = {
    "research": None,
    "outline": ("Outline task for {{video_title}}. Competitor scripts: {{competitor_script}}. "
                f"Put the recommended outline inside a Markdown code block:\n{FENCE}\n"
                f"[Actual complete recommended outline]\n{FENCE}"),
    "script": ('Script task. APPROVED OUTLINE: """ {{outline}} """ '
               "Write the script inside a single Markdown code block."),
    "audits": ('Audit task. Outline """ << Used For Script Generation >> """ '
               'Script """ << Current Script >> """ '
               'Competitor """ << Used in outline generation >> """ '
               "Final script inside a single Markdown code block."),
    "paraphrasing": None,
}
BRAND2 = {"nba_brand_2": {
    "brand_name": "NBA Brand 2",
    "prompts": PROMPTS,
    "steps": [{"condition": None, "run": ["outline", "script", "audits"]}],
    "rules": None,
    "inputs": {
        "outline": ["video_title", "inspirational_script", "outline_prompt"],
        "script": ["video_title", "outline", "script_prompt"],
        "audits": ["video_title", "outline", "audits_prompt", "script", "inspirational_script"],
    },
}}


@pytest.fixture(autouse=True)
def config(monkeypatch):
    cfg = sc.parse_tsv(TSV).config + [BRAND2]
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_CODEX_MODE", "codeblock")
    return cfg


def run(step="outline", inputs=None, brand="nba_brand_2", **overrides):
    defaults = {
        "outline": {"video_title": TITLE, "inspirational_script": COMPETITOR},
        "script": {"video_title": TITLE},
        "audits": {"video_title": TITLE, "inspirational_script": COMPETITOR},
    }
    body = {"brand": brand, "branch": 0, "step": step, "language": "English",
            "inputs": defaults[step] if inputs is None else inputs}
    body.update(overrides)
    r = client.post("/api/scripting/run", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def sent_prompt(data) -> str:
    """The fake CLI echoes what it got; with codeblock mode that echo is the code block."""
    assert data["text"].startswith("RESULT\n")
    return json.loads(data["text"].split("\n", 1)[1])["prompt"]


def saved(data) -> str:
    return (get_cli_settings().output_dir / data["file"]["step"] / data["file"]["name"]) \
        .read_text(encoding="utf-8")


# ------------------------------------------------------------ the extractor

@pytest.mark.parametrize("response, text, status", [
    ("```\nThe outline\n```\nNotes", "The outline", "ok"),
    ("Intro first.\n\n```markdown\nA\n\nB\n```\n\nAfter", "A\n\nB", "ok"),
    ("```\nFirst\n```\n```\nSecond\n```", "First", "ok"),
    ("````\nHas ``` inside\n````", "Has ``` inside", "ok"),
    ("~~~\nTilde\n~~~", "Tilde", "ok"),
    ("```\r\nWindows lines\r\n```\r\n", "Windows lines", "ok"),
    ("```\nCut off mid-way", "Cut off mid-way", "unclosed"),
    ("No block at all", "No block at all", "missing"),
    ("Inline ``` fence ``` only", "Inline ``` fence ``` only", "missing"),
    ("```\n\n```\nrest", "```\n\n```\nrest", "empty"),
])
def test_code_block(response, text, status):
    got = code_block(response)
    assert (got.text, got.status) == (text, status)
    assert (got.warning is None) == (status == "ok")


def test_wants_code_block():
    assert wants_code_block(PROMPTS["outline"]) and wants_code_block(PROMPTS["audits"])
    assert wants_code_block("Return it in a code panel.")
    assert not wants_code_block("Outline P") and not wants_code_block(None)


# ------------------------------------------------------------ brand view

def test_brand_view_marks_placeholders_context_and_code_blocks():
    _, view = find_brand("nba_brand_2")
    outline, script, audits = view["branches"][0]["steps"]
    assert outline["code_block"] and script["code_block"] and audits["code_block"]
    assert outline["unknown_placeholders"] == []  # {{competitor_script}} = inspirational script
    assert {f["name"]: f["placed"] for f in outline["fields"]} == {
        "video_title": True, "inspirational_script": True}
    assert {f["name"]: f["placed"] for f in script["fields"]}["outline"] is True
    assert audits["uses_context"] and not script["uses_context"]
    assert not any(f["placed"] for f in audits["fields"])


# ------------------------------------------------------------ step 1: save the code block

def test_outline_saves_only_the_code_block():
    data = run()
    prompt = sent_prompt(data)
    # {{video_title}} and {{competitor_script}} (the inspirational script) filled in place.
    assert f"Outline task for {TITLE}." in prompt and "{{video_title}}" not in prompt
    assert f"Competitor scripts: {COMPETITOR.strip()}." in prompt
    assert "{{competitor_script}}" not in prompt
    assert "<input " not in prompt  # both placed, so nothing repeated after the task
    assert "goes inside the one ``` code block" in prompt
    assert "Return only the finished result as plain text" not in prompt

    # The .txt is the code block only; the whole response is kept beside it.
    assert saved(data) == data["text"] + "\n"
    assert not data["text"].startswith(FENCE) and "Research notes" not in data["text"]
    assert data["response"].startswith(FENCE + "\n") and "Research notes" in data["response"]
    assert data["extract"] == {"expected": True, "status": "ok", "warning": None}
    assert data["words"] == len(data["text"].split())

    full = client.get(data["file"]["response_url"])
    assert full.status_code == 200 and full.text == data["response"] + "\n"
    meta = json.loads((get_cli_settings().output_dir / "outline" / data["file"]["name"])
                      .with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["extracted"] is True and meta["response_words"] > meta["words"]

    listed = client.get("/api/scripting/outputs", params={"step": "outline"}).json()["outputs"]
    assert [o["name"] for o in listed] == [data["file"]["name"]]  # not the .response.txt
    assert listed[0]["extracted"] is True
    name = data["file"]["name"][:-4] + outputs.RESPONSE_SUFFIX
    assert client.get(f"/api/scripting/outputs/outline/{name}").status_code == 404


def test_missing_code_block_saves_the_whole_response(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "")
    data = run()
    assert data["extract"]["status"] == "missing" and "no code block" in data["extract"]["warning"]
    assert data["response"] is None and data["file"]["response_url"] is None
    assert saved(data) == data["text"] + "\n"


def test_prompts_without_a_code_block_keep_the_whole_response():
    data = run(brand="nba_brand_1", inputs={"video_title": TITLE, "inspirational_script": "C"})
    assert data["text"].startswith(FENCE)  # fences kept: this brand's prompt didn't ask
    assert data["extract"] == {"expected": False, "status": None, "warning": None}


# ------------------------------------------------------------ step 2: {{outline}} from the file

def test_next_step_fills_placeholder_with_the_saved_code_block():
    outline = run()
    data = run("script")  # outline left empty: taken from the project's saved result
    prompt = sent_prompt(data)
    assert f'APPROVED OUTLINE: """ {outline["text"]} """' in prompt
    assert "{{outline}}" not in prompt
    assert '<input name="outline"' not in prompt and "Research notes" not in prompt
    meta = json.loads((get_cli_settings().output_dir / "script" / data["file"]["name"])
                      .with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["inputs"]["outline"] == outline["text"]


def test_pasted_outline_wins_over_the_saved_one():
    run()
    prompt = sent_prompt(run("script", {"video_title": TITLE, "outline": "My edited outline"}))
    assert 'APPROVED OUTLINE: """ My edited outline """' in prompt


def test_placeholder_values_are_not_substituted_again():
    run()
    prompt = sent_prompt(run("script", {"video_title": TITLE, "outline": "Keep {{video_title}}"}))
    assert 'APPROVED OUTLINE: """ Keep {{video_title}} """' in prompt


# ------------------------------------------------------------ step 3: << >> from the conversation

def test_context_markers_use_the_conversation_instead_of_resending():
    run()
    run("script")
    data = run("audits")  # page still holds the competitor script typed for the outline step
    prompt = sent_prompt(data)
    assert "<< Current Script >>" in prompt and "<< Used in outline generation >>" in prompt
    assert "Text inside << >> in the task means: take it from this conversation" in prompt
    assert "COMPETITOR SCRIPT TEXT" not in prompt  # same as sent in step 1: not sent again
    assert ("use the inspirational script sent with the Outline step earlier in this "
            "conversation") in prompt
    assert ("use your Script step response from earlier in this conversation (the part inside "
            "its code block)") in prompt
    assert data["project"]["steps"] == ["outline", "script", "audits"]
    assert set(data["project"]["inputs"]) == {"video_title", "inspirational_script"}


def test_changed_input_is_sent_again():
    run()
    run("script")
    prompt = sent_prompt(run("audits", {"video_title": TITLE, "inspirational_script": "NEW one"}))
    assert '<input name="inspirational_script" label="Inspirational script">\nNEW one' in prompt


def test_inputs_already_sent_are_optional_when_continuing():
    run()
    run("script")
    data = run("audits", {}, project_name=TITLE)  # nothing typed (e.g. after a reload)
    assert "COMPETITOR SCRIPT TEXT" not in sent_prompt(data)


def test_new_project_at_a_marker_step_sends_the_inputs():
    data = run("audits", {"video_title": TITLE, "inspirational_script": COMPETITOR,
                          "outline": "O", "script": "S"}, new_project=True)
    prompt = sent_prompt(data)
    assert "COMPETITOR SCRIPT TEXT" in prompt and '<input name="script"' in prompt
    assert "refers to the matching input under Inputs for this run" in prompt


def test_sent_input_recovered_for_a_placeholder():
    first = run()
    project = projects.load(first["project"]["id"])
    assert projects.sent_input(project, "video_title") == TITLE
    assert projects.sent_input(project, "inspirational_script") == COMPETITOR
    assert projects.sent_input(project, "nope") is None


def test_codex_extracts_the_code_block_too():
    outline = run(provider="codex")
    assert outline["extract"]["status"] == "ok" and outline["text"].startswith("RESULT")
    prompt = sent_prompt(run("script", provider="codex"))
    assert f'APPROVED OUTLINE: """ {outline["text"]} """' in prompt


def test_page_shows_code_block_and_placeholder_hints():
    page = client.get("/scripting").text
    assert "Saves the code block as the result" in page and "data-show-full" in page
    assert '"unknown_placeholders": []' in page
    # Jinja must not eat the brace helper (two braces in a row would be a template tag).
    assert "const tag = (name) => '{' + '{' + name + '}' + '}';" in page


def test_real_competitor_script_input_wins_over_the_alias():
    from app.web.scripting import input_for
    assert input_for("competitor_script", {"inspirational_script"}) == "inspirational_script"
    assert input_for("competitor_script", {"competitor_script", "inspirational_script"})         == "competitor_script"
    assert input_for("other", {"inspirational_script"}) is None

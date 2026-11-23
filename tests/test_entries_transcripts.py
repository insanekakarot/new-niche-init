"""Used entries (admin JSON), the source-script names, NEXT MOVE checks and video transcripts."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import keys
from app.admin import scripts_config as sc
from app.admin import used_entries as ue
from app.main import app
from app.rules import is_check
from app.rules.engine import parse_tag
from app.rules.prompts import fill
from app.web import transcripts
from app.web.scripting import find_brand
from tests.test_rules import tsv

client = TestClient(app)

DOC = """ALL USED ENTRIES\v
DOCUMENT 1 — Sample Tombs (3 entries)
Number 3 — King Alpha’s Sealed Tomb
Number 2 — Lost Tomb of King Beta
Number 1 — Çatal Ruins

DOCUMENT 2 — Sample Doors (2 entries)
Number 2 — Catal Ruins
Number 1 — Stone Lion

Other too:\vOld Mound\vOld Walls, Nowhere.
• Island Heads
"""

ENTRIES_CHECK = (
    'ENTRIES_CHECK = """ Title: {{ video_title }} Entries covered before: '
    "{{ used_entries }} Transcript: {{ source_script }} Reply with ... "
    'NEXT MOVE - [CONTINUE or RESEARCH] """')
T_ROW = {
    "Research": "Research {{video_title}}. Entries covered before: {{used_entries}} END",
    "Outline": "Outline from {{research}}.", "Script": "Script from {{research}}.",
    "Audits": "Audit it.",
    "Paraphrasing": "Title {{video_title}}. SOURCE SCRIPT: {{competitor_script}} "
                    "USED: {{used_entries}} Put it in a code block.",
    "Brand": "Brand D",
    "Steps": "if (!competitor_script) { Research + Outline + Script + Audits } "
             "else { Paraphrasing }",
    "Rules": '< paraphrasing ENTRIES_CHECK; IF (TAG == "CONTINUE") { STOP; } '
             'ELSE IF (TAG == "RESEARCH") { CANCEL; }',
    "Rule Prompts": ENTRIES_CHECK,
    "Inputs": "research = video_title + research_prompt "
              "outline = video_title + research_content + outline_prompt "
              "script = video_title + outline_content + script_prompt "
              "audits = video_title + script + audits_prompt "
              "paraphrasing = video_title + competitor_script",
}


@pytest.fixture
def t_brand(tmp_path, monkeypatch):
    result = sc.parse_tsv(tsv([T_ROW]))
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(result.config), encoding="utf-8")
    ue.save("brand_d", ue.parse(DOC), source="https://docs.google.com/document/d/x/edit")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    return result


# ------------------------------------------------------------ used entries

def test_parse_the_doc_format():
    groups = ue.parse(DOC)
    assert [g["title"] for g in groups] == ["Sample Tombs", "Sample Doors", "Other too"]
    assert groups[0]["entries"] == ["King Alpha’s Sealed Tomb", "Lost Tomb of King Beta",
                                    "Çatal Ruins"]
    assert groups[2]["entries"] == ["Old Mound", "Old Walls, Nowhere", "Island Heads"]
    assert ue.parse(ue.to_text(groups)) == groups                    # the admin text round-trips
    # "Çatal Ruins" and "Catal Ruins" are the same entry: listed once.
    assert ue.unique(groups).count("Çatal Ruins") == 1 and "Catal Ruins" not in ue.unique(groups)
    assert ue.render(groups).splitlines()[0] == "- King Alpha’s Sealed Tomb"
    assert ue.render([]) == ue.EMPTY


def test_saved_next_to_the_prompts_config_and_kept_per_brand():
    ue.save("brand_d", ue.parse(DOC), source="doc")
    ue.save("brand_b", ue.parse("Sample Item"))
    assert ue.path().parent == sc.config_path().parent and ue.path().name == "__used_entries.json"
    data = json.loads(ue.path().read_text(encoding="utf-8"))
    assert set(data["brands"]) == {"brand_d", "brand_b"}
    assert data["brands"]["brand_d"]["source"] == "doc"
    assert ue.value_for("brand_b") == "- Sample Item" and ue.value_for("nope") == ue.EMPTY


def test_upload_has_no_warnings_for_the_new_names(t_brand):
    assert t_brand.warnings == []
    assert is_check(t_brand.config[0]["brand_d"]["rule_prompts"]["ENTRIES_CHECK"])


def _admin():
    from app.config import settings
    settings.admin_api_key = "k"
    c = TestClient(app)
    c.post("/admin/login", data={"api_key": "k", "next": "/"})
    return c


def test_admin_page_shows_and_saves_entries(t_brand, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_api_key", "k")
    c = _admin()
    page = c.get("/admin/scripts/entries").text
    assert "Used entries" in page and "Brand D" in page and "Lost Tomb of King Beta" in page
    assert "__used_entries.json" in page
    check = c.post("/admin/scripts/entries?dry_run=true",
                   json={"brand": "brand_d", "text": "# New\nPlace One\nPlace Two"}).json()
    assert check["saved"] is False and check["summary"]["unique"] == 2
    assert ue.summary("brand_d", ue.for_brand("brand_d"))["unique"] == 7   # unchanged
    saved = c.post("/admin/scripts/entries",
                   json={"brand": "brand_d", "text": "# New\nPlace One\nPlace Two"}).json()
    assert saved["saved"] and ue.value_for("brand_d") == "- Place One\n- Place Two"
    assert ue.for_brand("brand_d")["source"] == "https://docs.google.com/document/d/x/edit"
    assert c.get("/admin/scripts/entries.json").json()["brands"]["brand_d"]["groups"][0][
        "entries"] == ["Place One", "Place Two"]
    assert c.post("/admin/scripts/entries", json={"brand": "zzz", "text": ""}).status_code == 404


def test_entries_api_needs_the_admin_key(t_brand, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_api_key", "k")   # a key is set; this client has none
    assert TestClient(app).post("/admin/scripts/entries",
                                json={"brand": "brand_d", "text": "x"}).status_code == 401
    assert TestClient(app).get("/admin/scripts/entries.json").status_code == 401


# ------------------------------------------------------------ names, labels, NEXT MOVE

def test_source_script_competitor_script_and_inspirational_script_are_one_input():
    assert keys.same_input("source_script") == ("source_script", "competitor_script",
                                                "inspirational_script")
    assert fill("S: {{ source_script }}", {"competitor_script": "TRANSCRIPT"}) == "S: TRANSCRIPT"
    assert fill("S: {{competitor_script}}", {"inspirational_script": "T2"}) == "S: T2"


def test_next_move_is_a_status_line():
    assert parse_tag("Remaining: 4\nNEXT MOVE - CONTINUE") == "CONTINUE"
    assert parse_tag("NEXT MOVE - [RESEARCH]") == "RESEARCH"
    assert is_check("Output ... NEXT MOVE - [CONTINUE or RESEARCH]")


def test_view_labels_the_source_script_and_offers_video(t_brand):
    _, view = find_brand("brand_d")
    para = view["branches"][1]["steps"][0]
    field = next(f for f in para["fields"] if f["name"] == "competitor_script")
    assert field["label"] == "Source script" and field["transcript"] and field["multiline"]
    research = view["branches"][0]["steps"][0]
    assert research["builtin_placeholders"] == ["used_entries"]
    assert "used_entries" not in research["unknown_placeholders"]
    assert para["rules_before"] == ["ENTRIES_CHECK"]


# ------------------------------------------------------------ the runs use them

def _sent(data):
    """The prompt the fake CLI echoed back (inside or outside a code block)."""
    text = data.get("response") or data["text"]
    start = text.index("RESULT\n") + len("RESULT\n")
    return json.JSONDecoder().raw_decode(text[start:])[0]["prompt"]


def test_research_prompt_gets_the_used_entries(t_brand):
    r = client.post("/api/scripting/run", json={
        "brand": "brand_d", "branch": 0, "step": "research", "language": "English",
        "new_project": True, "inputs": {"video_title": "Top 6 Sample Places"}})
    assert r.status_code == 200, r.text
    prompt = _sent(r.json())
    assert "Entries covered before: - King Alpha’s Sealed Tomb\n- Lost Tomb of King Beta" \
        in prompt and "{{used_entries}}" not in prompt


def test_paraphrasing_validates_entries_first_with_source_script_filled(t_brand):
    from app.web import projects
    r = client.post("/api/scripting/run", json={
        "brand": "brand_d", "branch": 1, "step": "paraphrasing", "language": "English",
        "new_project": True,
        "inputs": {"video_title": "Top 6 Sample Places", "competitor_script": "THE TRANSCRIPT"}})
    assert r.status_code == 200, r.text
    data = r.json()
    before = data["rules_before"]
    assert [(e["name"], e["tag"]) for e in before["log"]] == [("ENTRIES_CHECK", "CONTINUE")]
    folder = (projects.projects_dir() / data["project"]["id"] / "rules"
              / f"before-paraphrasing-{before['id']}")
    rule = (folder / "01-entries_check.prompt.txt").read_text(encoding="utf-8")
    assert "Transcript: THE TRANSCRIPT" in rule and "- Stone Lion" in rule
    prompt = _sent(data)
    assert "SOURCE SCRIPT: THE TRANSCRIPT" in prompt and "USED: - King Alpha" in prompt


def test_research_answer_cancels_paraphrasing(t_brand, monkeypatch):
    monkeypatch.setenv("FAKE_NEXT_MOVE", "RESEARCH")
    r = client.post("/api/scripting/run", json={
        "brand": "brand_d", "branch": 1, "step": "paraphrasing", "language": "English",
        "new_project": True,
        "inputs": {"video_title": "Top 6 Sample Places", "competitor_script": "THE TRANSCRIPT"}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "step_cancelled"
    assert "RESEARCH" in r.json()["detail"]["message"]


# ------------------------------------------------------------ video transcripts

@pytest.mark.parametrize("url, vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://vimeo.com/123456", None),
    ("https://www.youtube.com/watch?v=short", None),
])
def test_youtube_ids(url, vid):
    assert transcripts.youtube_id(url) == vid


def test_tidy_drops_cue_markup_and_repeats():
    text = transcripts.tidy(["[Music]", "In 1986 two workers", "In 1986 two workers",
                             "<c>hit</c> metal. &amp; then", "♪♪"])
    assert text == "In 1986 two workers hit metal. & then"


def test_transcript_endpoint(monkeypatch):
    monkeypatch.setattr(transcripts, "_youtube",
                        lambda vid: transcripts.Transcript("Hello there.", "en", "captions",
                                                           "youtube"))
    r = client.post("/api/scripting/transcript", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200 and r.json() == {
        "text": "Hello there.", "words": 2, "language": "en", "kind": "captions",
        "source": "youtube", "title": ""}


def test_transcript_errors_are_readable(monkeypatch):
    def no_captions(vid):
        raise transcripts.TranscriptError("This video has no captions, so there's no transcript")
    monkeypatch.setattr(transcripts, "_youtube", no_captions)
    monkeypatch.setattr(transcripts, "_yt_dlp", lambda url: no_captions(url))
    r = client.post("/api/scripting/transcript", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 422 and "no captions" in r.json()["detail"]
    r = client.post("/api/scripting/transcript", json={"url": "ftp://x.example/video"})
    assert r.status_code == 422 and "https://" in r.json()["detail"]


def test_page_has_the_video_tab(t_brand):
    page = client.get("/scripting").text
    assert "tab('video', 'Video')" in page and "/api/scripting/transcript" in page
    assert re.search(r"data-get-transcript", page)

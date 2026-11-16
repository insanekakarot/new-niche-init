import json

from fastapi.testclient import TestClient

from app.admin import scripts_config as sc
from app.main import app
from app.web.scripting import brand_view, load_brand_views
from tests.test_admin import TSV

client = TestClient(app)


def write_config(config):
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(config), encoding="utf-8")


def views():
    write_config(sc.parse_tsv(TSV).config)
    result, error = load_brand_views()
    assert error is None
    return {v["key"]: v for v in result}


def kinds(step):
    return {i["name"]: i["kind"] for i in step["inputs"]}


def test_nba_inputs_are_classified():
    nba = views()["nba_brand_1"]
    assert [f["name"] for f in nba["fields"]] == ["video_title", "inspirational_script", "player_name"]
    assert {f["name"]: f["optional"] for f in nba["fields"]} == {
        "video_title": False, "inspirational_script": False, "player_name": True}

    (branch,) = nba["branches"]
    outline, script, audits = branch["steps"]
    assert kinds(outline) == {"video_title": "user", "inspirational_script": "user",
                              "player_name": "user", "outline_prompt": "prompt"}
    assert kinds(script)["outline"] == "generated"
    assert kinds(audits)["script"] == "generated"
    assert branch["required"] == ["video_title", "inspirational_script"]
    assert nba["catalog"]["slug"] == "nba-stories"


def test_conditional_brand_switches_required_fields():
    t = views()["t_brand_1"]
    names = [f["name"] for f in t["fields"]]
    assert names[0] == "competitor_script"
    assert t["fields"][0]["condition"] and t["fields"][0]["multiline"]

    full, para = t["branches"]
    assert full["condition"] == "!competitor_script"
    assert full["required"] == ["video_title"]
    assert kinds(full["steps"][1])["research_content"] == "generated"
    assert kinds(full["steps"][2])["outline_content"] == "generated"

    assert para["condition"] == "competitor_script"
    assert para["required"] == ["competitor_script"]
    assert [s["name"] for s in para["steps"]] == ["paraphrasing"]


def test_step_output_not_produced_earlier_is_asked_from_user():
    view = brand_view("x", {
        "brand_name": "X",
        "prompts": {"script": "S"},
        "steps": [{"condition": None, "run": ["script"]}],
        "inputs": {"script": ["video_title", "outline", "script_prompt"]},
    })
    assert kinds(view["branches"][0]["steps"][0])["outline"] == "user"
    assert view["branches"][0]["required"] == ["video_title", "outline"]


def test_missing_prompt_is_flagged():
    view = brand_view("x", {
        "prompts": {}, "steps": [{"condition": None, "run": ["script"]}],
        "inputs": {"script": ["video_title", "script_prompt"]},
    })
    step = view["branches"][0]["steps"][0]
    assert step["has_prompt"] is False
    assert step["inputs"][1] == {"name": "script_prompt", "kind": "prompt",
                                 "source": "script", "available": False}


def test_page_lists_config_brands_without_leaking_prompts():
    config = sc.parse_tsv(TSV).config
    write_config(config)
    r = client.get("/scripting")
    assert r.status_code == 200
    for name in ("NBA Brand 1", "A Brand 1", "T Brand 1"):
        assert name in r.text
    for entry in config:
        for brand in entry.values():
            for prompt in filter(None, brand["prompts"].values()):
                assert prompt not in r.text


def test_page_without_config_shows_empty_state():
    r = client.get("/scripting")
    assert r.status_code == 200
    assert "No brands configured yet" in r.text


def test_page_with_broken_config_shows_error():
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text("{not json", encoding="utf-8")
    r = client.get("/scripting")
    assert r.status_code == 200
    assert "can't load its config" in r.text


def test_each_step_lists_its_own_fields():
    nba = views()["nba_brand_1"]
    outline, script, audits = nba["branches"][0]["steps"]

    assert [(f["name"], f["kind"], f["required"]) for f in outline["fields"]] == [
        ("video_title", "user", True),
        ("inspirational_script", "user", True),
        ("player_name", "user", False),
    ]
    assert [p["name"] for p in outline["prompt_inputs"]] == ["outline_prompt"]

    # Running Script on its own asks for the Outline (output of step 1), required.
    assert [(f["name"], f["kind"], f["required"]) for f in script["fields"]] == [
        ("video_title", "user", True),
        ("outline", "generated", True),
    ]
    assert script["fields"][1]["source_label"] == "Outline"
    assert script["fields"][1]["multiline"] is True
    assert {f["name"] for f in audits["fields"]} == {"video_title", "script"}


def test_research_step_fields_in_conditional_brand():
    t = views()["t_brand_1"]
    research = t["branches"][0]["steps"][0]
    assert [f["name"] for f in research["fields"]] == ["video_title"]
    assert [p["name"] for p in research["prompt_inputs"]] == ["research_prompt"]

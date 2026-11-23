import json

import pytest
from fastapi.testclient import TestClient

from app.admin import scripts_config as sc
from app.config import settings
from app.main import app

KEY = "test-key-123"
HEADER = {"X-API-Key": KEY}
COLS = "Research\tOutline\tScript\tAudits\tParaphrasing\tBrand\tSteps\tRules\tInputs"
TSV = "\r\n".join([
    COLS,
    "\tOutline P\tScript P\tAudit P\t\tBrand A\tOutline + Script + Audits\t\t"
    "outline = video_title + inspirational_script + guest_name(optional) + outline_prompt "
    "script = video_title + outline + script_prompt audits = video_title + audits_prompt + script",
    "\t\tScript only\t\t\tBrand C\tScript\t\tscript = video_title + inspirational_script + script_prompt",
    "R P\tO P\tS P\tA P\tPara P\tBrand D\t"
    "if (!competitor_script) {   Research + Outline + Script + Audits }  else {   Paraphrasing }\t\t"
    "research = video_title + research_prompt outline = video_title + research_content + outline_prompt "
    "script = video_title + outline_content + script_prompt audits = video_title + script + audits_prompt",
]).encode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", KEY)
    monkeypatch.setattr(settings, "admin_dir", tmp_path / "admin")
    monkeypatch.setattr(settings, "scripts_config_dir", tmp_path / "z" / "_scripts")
    return TestClient(app)


def upload(client, data=TSV, name="config.tsv", dry_run=False, headers=HEADER):
    url = "/admin/scripts/prompts/edit" + ("?dry_run=true" if dry_run else "")
    return client.post(url, files={"file": (name, data, "text/tab-separated-values")},
                       headers=headers)


# ------------------------------------------------------------ parsing

def test_parse_brands_prompts_steps_inputs():
    result = sc.parse_tsv(TSV)
    assert result.brands == ["brand_a", "brand_c", "brand_d"]

    brand = result.config[0]["brand_a"]
    assert brand["brand_name"] == "Brand A"
    assert brand["prompts"] == {"research": None, "outline": "Outline P", "script": "Script P",
                              "audits": "Audit P", "paraphrasing": None}
    assert brand["steps"] == [{"condition": None, "run": ["outline", "script", "audits"]}]
    assert brand["inputs"]["outline"] == [
        "video_title", "inspirational_script", "guest_name(optional)", "outline_prompt"]
    assert brand["inputs"]["audits"] == ["video_title", "audits_prompt", "script"]
    assert brand["rules"] is None

    t = result.config[2]["brand_d"]
    assert t["steps"] == [
        {"condition": "!competitor_script", "run": ["research", "outline", "script", "audits"]},
        {"condition": "competitor_script", "run": ["paraphrasing"]},
    ]
    assert set(t["inputs"]) == {"research", "outline", "script", "audits"}


def test_parse_warns_about_gaps():
    warnings = sc.parse_tsv(TSV).warnings
    assert "Brand D: step 'paraphrasing' runs but has no inputs listed." in warnings


@pytest.mark.parametrize("bad, message", [
    (b"Brand\tSteps\nX\tScript", "Missing column"),
    ((COLS + "\n\t\t\t\t\t\tScript\t\t").encode(), "Brand column is empty"),
    ((COLS + "\n\t\tS\t\t\tB\tScript + Dance\t\t").encode(), "unknown step"),
    ((COLS + "\n\t\tS\t\t\tB\tScript\t\t\n\t\tS\t\t\tB\tScript\t\t").encode(), "already appears"),
    (COLS.encode(), "no brand rows"),
])
def test_parse_rejects_bad_files(bad, message):
    with pytest.raises(sc.TsvError, match=message):
        sc.parse_tsv(bad)


# ------------------------------------------------------------ auth

@pytest.mark.parametrize("path", ["/admin/scripts/dashboard", "/admin/scripts/prompts/edit"])
def test_pages_redirect_to_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login?next=")


def test_upload_needs_key(client):
    assert upload(client, headers={}).status_code == 401
    assert upload(client, headers={"X-API-Key": "wrong"}).status_code == 401


def test_login_sets_cookie_and_logout_clears(client):
    assert client.post("/admin/login", data={"api_key": "nope"}).status_code == 401
    r = client.post("/admin/login", data={"api_key": KEY, "next": "/admin/scripts/dashboard"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert KEY not in r.headers["set-cookie"]
    assert client.get("/admin/scripts/dashboard").status_code == 200
    client.post("/admin/logout")
    assert client.get("/admin/scripts/dashboard", follow_redirects=False).status_code == 303


def test_login_ignores_offsite_next(client):
    r = client.post("/admin/login", data={"api_key": KEY, "next": "https://evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/admin/scripts/dashboard"


def test_locked_without_configured_key(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")
    assert upload(client).status_code == 503


# ------------------------------------------------------------ upload + dashboard

def test_dry_run_saves_nothing(client):
    r = upload(client, dry_run=True)
    assert r.status_code == 200
    assert r.json()["saved"] is False
    assert not sc.tsv_path().exists() and not sc.config_path().exists()


def test_upload_writes_tsv_and_json(client):
    r = upload(client, name="New Niche Overall - Scripting Prompts Config.tsv")
    assert r.status_code == 200, r.text
    assert r.json()["brands"] == ["brand_a", "brand_c", "brand_d"]

    assert sc.tsv_path().name == "__script_prompts.tsv"
    assert sc.tsv_path().parent.parts[-3:] == ("admin", "inputs", "tsv")
    assert sc.tsv_path().read_bytes() == TSV

    assert sc.config_path().name == "__scripts.prompt.json"
    saved = json.loads(sc.config_path().read_text(encoding="utf-8"))
    assert saved == sc.parse_tsv(TSV).config

    page = client.get("/admin/scripts/dashboard", headers=HEADER)
    assert page.status_code == 200
    assert "Config matches TSV" in page.text
    for name in ("Brand A", "Brand C", "Brand D"):
        assert name in page.text

    dl = client.get("/admin/scripts/prompts.json", headers=HEADER)
    assert dl.json() == saved


def test_bad_upload_keeps_existing_config(client):
    upload(client)
    before = sc.config_path().read_bytes()
    r = upload(client, data=b"Brand\tSteps\nX\tScript")
    assert r.status_code == 422
    assert "Missing column" in r.json()["detail"]
    assert sc.config_path().read_bytes() == before


def test_rejects_non_tsv(client):
    assert upload(client, name="config.xlsx").status_code == 400


def test_dashboard_empty_state(client):
    r = client.get("/admin/scripts/dashboard", headers=HEADER)
    assert r.status_code == 200
    assert "No config yet" in r.text

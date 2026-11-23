"""The private brands file (app/brands_file.py): brands, shown or hidden, tool texts, system prompt.

Every test starts with app/brands.example.json as the brands file (tests/conftest.py).
"""

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app import brands_file
from app.admin import scripts_config as sc
from app.cli.config import (
    DEFAULT_SYSTEM_PROMPT,
    clear_settings_cache,
    get_claude_settings,
    system_prompt_for,
)
from app.config import settings
from app.main import app
from app.web import catalog
from app.web.scripting import load_brand_views
from tests.test_admin import TSV
from tests.test_scripting import write_config

client = TestClient(app)
REPO = Path(__file__).resolve().parent.parent


def edit(change):
    """Change the brands file in place (a new mtime, so it's read again)."""
    path = brands_file.path()
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def by_slug(data, slug):
    return next(b for b in data["brands"] if b["slug"] == slug)


def test_the_file_sits_with_the_admin_configs():
    assert brands_file.path() == settings.scripts_config_dir.parent / "___brands.json"
    assert brands_file.FILE_NAME == "___brands.json"


def test_brands_come_from_the_file_and_hidden_ones_are_left_out():
    assert [b.name for b in catalog.all_brands()] == ["Brand A", "Brand B", "Brand C", "Brand D",
                                                       "Brand E"]
    assert [b.name for b in catalog.brands()] == ["Brand A", "Brand B", "Brand C", "Brand D"]
    a = catalog.brand_for_key("BRAND_A")                      # keys ignore case
    assert a.slug == "brand-a" and a.languages == ("English", "French", "Spanish")
    assert a.word_count == "3,000–3,500 words" and a.enabled
    assert catalog.brand_for_key("nope") is None and catalog.key_enabled("nope")  # no profile: shown
    assert catalog.languages()[0] == "English"


def test_home_and_tool_pages_show_only_enabled_brands():
    home = client.get("/").text
    assert "Brand A" in home and "Brand D" in home and "Brand E" not in home
    assert "Write scripts for all 4 brand profiles" in home        # tool text from the file
    page = client.get("/translations").text
    assert "Will cover 4 brands" in page and "Brand E" not in page
    edit(lambda d: by_slug(d, "brand-a").update(enabled=False))    # no restart needed
    home = client.get("/").text
    assert "Brand A" not in home and "Write scripts for all 3 brand profiles" in home


def test_a_hidden_brand_is_off_the_scripting_page_and_the_api():
    write_config(sc.parse_tsv(TSV).config)
    keys = [v["key"] for v in load_brand_views()[0]]
    assert keys == ["brand_a", "brand_c", "brand_d"]
    edit(lambda d: by_slug(d, "brand-c").update(enabled=False))
    assert [v["key"] for v in load_brand_views()[0]] == ["brand_a", "brand_d"]
    assert "Brand C" not in client.get("/scripting").text
    r = client.post("/api/scripting/run", json={
        "brand": "brand_c", "branch": 0, "step": "script", "language": "English",
        "inputs": {"video_title": "T", "inspirational_script": "S"}})
    assert r.status_code == 404


def test_tool_texts_fill_in_brands_and_languages():
    tools = {t.slug: t for t in catalog.tools()}
    assert tools["translations"].description == (
        "Translate finished scripts into each brand's languages: French, German, Polish, Spanish.")
    assert tools["seo"].description == catalog.TOOLS_BY_SLUG["seo"].description  # not in the file
    edit(lambda d: d.pop("tools"))
    assert catalog.tool("scripting").description == catalog.TOOLS_BY_SLUG["scripting"].description


def test_system_prompt_comes_from_the_file_unless_set_in_env(monkeypatch):
    s = get_claude_settings()
    assert system_prompt_for(s).startswith("You are the writer for the example brands.\n")
    assert "\n\n- Follow the task prompt exactly." in system_prompt_for(s)  # list -> lines
    edit(lambda d: d.update(system_prompt="One line prompt."))
    assert system_prompt_for(s) == "One line prompt."
    edit(lambda d: d.pop("system_prompt"))
    assert system_prompt_for(s) == DEFAULT_SYSTEM_PROMPT
    monkeypatch.setenv("CLAUDE_CLI_SYSTEM_PROMPT", "From env")
    clear_settings_cache()
    assert system_prompt_for(get_claude_settings()) == "From env"


def test_claude_gets_the_files_system_prompt():
    from app.cli.claude_client import build_command
    cmd = build_command(get_claude_settings())
    assert cmd[cmd.index("--system-prompt") + 1].startswith("You are the writer for the example")


def test_no_file_or_a_broken_one_means_no_brands_and_no_crash():
    path = brands_file.path()
    path.unlink()
    assert catalog.brands() == () and brands_file.error() is None
    assert client.get("/").status_code == 200
    path.write_text("{ not json", encoding="utf-8")
    assert catalog.brands() == () and "isn't valid JSON" in brands_file.error()
    assert system_prompt_for(get_claude_settings()) == DEFAULT_SYSTEM_PROMPT
    path.write_text("[1, 2]", encoding="utf-8")
    assert catalog.brands() == () and "one JSON object" in brands_file.error()


def test_entries_without_a_slug_or_name_are_skipped():
    edit(lambda d: d["brands"].extend([{"name": "No slug"}, {"slug": "no-name"}, "text"]))
    assert len(catalog.all_brands()) == 5


def test_the_repo_has_no_brand_data():
    assert not (REPO / "app" / "brand_links.example.json").exists()
    example = json.loads((REPO / "app" / "brands.example.json").read_text(encoding="utf-8"))
    assert {b["name"] for b in example["brands"]} == {f"Brand {c}" for c in "ABCDE"}

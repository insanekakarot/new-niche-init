import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web.catalog import TOOLS

client = TestClient(app)


def test_home_lists_all_tools():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for tool in TOOLS:
        assert tool.name in response.text
        assert f'href="{tool.path}"' in response.text


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.slug)
def test_tool_pages_render(tool):
    response = client.get(tool.path)
    assert response.status_code == 200
    assert tool.name in response.text


def test_unknown_tool_404():
    assert client.get("/does-not-exist").status_code == 404


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "new-niche-overall"}


# ------------------------------------------------------------ back button and error pages


HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _back(page: str) -> tuple[str, bool]:
    m = re.search(r'<button type="button" id="nav-back" data-fallback="([^"]+)"[^>]*class="([^"]+)"',
                  page)
    assert m, "no back button"
    return m.group(1), "hidden" in m.group(2).split()


@pytest.mark.parametrize("path, fallback, hidden", [
    ("/", "/", True),                       # home: shown only after moving around in the app
    ("/scripting", "/", False),
    ("/translations", "/", False),
    ("/admin/login", "/", False),
])
def test_every_page_has_a_back_button(path, fallback, hidden):
    page = client.get(path).text
    assert _back(page) == (fallback, hidden)
    assert "history.back()" in page


def test_admin_pages_go_back_to_the_dashboard(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_api_key", "k")
    c = TestClient(app)
    c.post("/admin/login", data={"api_key": "k", "next": "/"})
    assert _back(c.get("/admin/scripts/rules-guide").text) == ("/admin/scripts/dashboard", False)
    assert _back(c.get("/admin/scripts/prompts/edit").text) == ("/admin/scripts/dashboard", False)
    assert _back(c.get("/admin/scripts/dashboard").text) == ("/", False)


def test_unknown_address_shows_a_page_with_a_way_back():
    r = client.get("/no-such-page", headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Page not found" in r.text and "data-go-back" in r.text and 'id="nav-back"' in r.text
    assert _back(r.text) == ("/", False)


def test_api_and_fetch_errors_stay_json():
    assert client.get("/no-such-page").json() == {"detail": "Tool not found"}
    r = client.get("/api/scripting/rules/nope", headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_health_link_opens_outside_the_app():
    assert 'href="/health" target="_blank"' in client.get("/").text

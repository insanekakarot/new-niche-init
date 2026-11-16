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
    assert response.json() == {"status": "ok"}

"""The desktop window (app/desktop.py) without opening a window: pywebview is faked."""

import socket
import sys
import types

import pytest

from app import desktop


class FakeWebview(types.ModuleType):
    def __init__(self):
        super().__init__("webview")
        self.settings = {}
        self.windows, self.started = [], None

    def create_window(self, title, url, **kwargs):
        self.windows.append({"title": title, "url": url, **kwargs})

    def start(self, **kwargs):
        self.started = kwargs


@pytest.fixture
def fake_webview(monkeypatch, tmp_path):
    fake = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "data"))
    return fake


def test_reuses_the_running_web_app(fake_webview, monkeypatch):
    monkeypatch.setattr(desktop, "ours", lambda port, **_: port == 1023)
    started = []
    monkeypatch.setattr(desktop.Server, "start", lambda self, **_: started.append(self))
    assert desktop.main(["--port", "1023", "--path", "admin"]) == 0
    win = fake_webview.windows[0]
    assert win["url"] == "http://127.0.0.1:1023/admin" and win["title"] == desktop.TITLE
    assert win["min_size"] == desktop.MIN_SIZE and not started   # no second server
    assert fake_webview.started["private_mode"] is False         # logins and choices persist
    assert fake_webview.settings["ALLOW_DOWNLOADS"] is True


def test_starts_its_own_server_on_a_free_port_when_the_port_is_taken(fake_webview, monkeypatch):
    monkeypatch.setattr(desktop, "ours", lambda *a, **k: False)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = taken.getsockname()[1]
        events = []
        monkeypatch.setattr(desktop.Server, "start",
                            lambda self, **_: events.append(("start", self.port)))
        monkeypatch.setattr(desktop.Server, "stop", lambda self: events.append(("stop", self.port)))
        assert desktop.main(["--port", str(busy)]) == 0
    (kind, port), (kind2, port2) = events
    assert (kind, kind2) == ("start", "stop") and port == port2 != busy
    assert fake_webview.windows[0]["url"] == f"http://127.0.0.1:{port}/scripting"


def test_server_really_serves_the_app_and_stops():
    server = desktop.Server(desktop.free_port())
    server.start(timeout=20)
    try:
        assert desktop.ours(server.port)
    finally:
        server.stop()
    assert not desktop.ours(server.port, timeout=0.5)


def test_without_pywebview_it_explains_how_to_install(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "webview", None)   # import fails
    assert desktop.main([]) == 1
    assert 'pip install -e ".[desktop]"' in capsys.readouterr().err


def test_web_app_does_not_need_pywebview():
    import app.main  # the web app never imports webview
    assert "webview" not in getattr(app.main, "__dict__", {})


def test_pages_have_a_menu_for_narrow_windows():
    from fastapi.testclient import TestClient

    from app.main import app
    page = TestClient(app).get("/").text
    assert 'aria-label="Menu"' in page and "sm:hidden" in page


def test_display_env_fills_in_wslg_when_the_shell_has_none(monkeypatch, tmp_path):
    wayland = tmp_path / "wayland-0"
    wayland.touch()
    x0 = tmp_path / "X0"
    x0.touch()
    monkeypatch.setattr(desktop, "WSLG_WAYLAND", wayland)
    monkeypatch.setattr(desktop, "WSLG_X11", x0)
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert desktop.display_env({}) == {"WAYLAND_DISPLAY": str(wayland), "DISPLAY": ":0",
                                       "QT_QPA_PLATFORM": "wayland;xcb"}
    # Arch + systemd: WAYLAND_DISPLAY set, but the socket isn't in XDG_RUNTIME_DIR.
    got = desktop.display_env({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0",
                               "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    assert got == {"WAYLAND_DISPLAY": str(wayland), "QT_QPA_PLATFORM": "wayland;xcb"}
    # A working setup is left alone.
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "wayland-0").touch()
    ok = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_RUNTIME_DIR": str(tmp_path / "run"),
          "QT_QPA_PLATFORM": "wayland"}
    assert desktop.display_env(ok) == {}
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop.display_env({}) == {}


def test_render_env_turns_off_animated_wheel_scrolling_unless_flags_are_set(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert desktop.render_env({}) == {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-smooth-scrolling"}
    assert desktop.render_env({"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"}) == {}
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop.render_env({}) == {}



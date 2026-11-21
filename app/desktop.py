"""The same app in its own window (pywebview), next to the web app.

    python -m app.desktop                 # opens /scripting in a window
    python -m app.desktop --path /admin   # opens another page first

If the web app is already running on PORT (python -m app.main), the window uses that server, so
both share the same projects and locks. Otherwise it starts its own server on 127.0.0.1 (PORT,
or a free port) for as long as the window is open. The pages, API and data are exactly the web
app's: nothing here changes them.

Needs the desktop extra: pip install -e ".[desktop]"  (pywebview with the Qt backend).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

from app.config import settings

TITLE = "New Niche"
DEFAULT_PATH = "/scripting"
WINDOW_SIZE = (1280, 860)
MIN_SIZE = (380, 560)       # the pages lay out down to phone width
START_TIMEOUT_S = 30


def ours(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Whether this app's server already answers on the port."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            data = json.loads(r.read().decode() or "{}")
    except (OSError, ValueError):
        return False
    return data.get("status") == "ok" and data.get("app") == settings.app_name


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def storage_dir() -> Path:
    """Where the window keeps its browser data (cookies, saved choices) between runs."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / settings.app_name / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


# WSLg (Windows 11) always serves its display here, whatever the shell's environment says.
WSLG_WAYLAND = Path("/mnt/wslg/runtime-dir/wayland-0")
WSLG_X11 = Path("/tmp/.X11-unix/X0")


def display_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Display variables to add so the window can open from any shell on Linux / WSL.

    A shell opened with su, ssh or some editors has no DISPLAY / WAYLAND_DISPLAY, and on
    Arch + systemd WSLg's Wayland socket isn't in XDG_RUNTIME_DIR. Qt then aborts with
    "could not connect to display". Only missing or unreachable values are filled in.
    """
    env = os.environ if env is None else env
    if not sys.platform.startswith("linux"):
        return {}
    changes: dict[str, str] = {}
    wayland = env.get("WAYLAND_DISPLAY", "")
    runtime = env.get("XDG_RUNTIME_DIR", "")
    reachable = bool(wayland) and (
        Path(wayland).exists() if wayland.startswith("/")
        else bool(runtime) and (Path(runtime) / wayland).exists())
    if not reachable and WSLG_WAYLAND.exists():
        changes["WAYLAND_DISPLAY"] = str(WSLG_WAYLAND)  # an absolute path works anywhere
        reachable = True
    if not env.get("DISPLAY") and WSLG_X11.exists():
        changes["DISPLAY"] = ":0"
    if reachable and not env.get("QT_QPA_PLATFORM"):
        changes["QT_QPA_PLATFORM"] = "wayland;xcb"      # Wayland first, X11 if it fails
    return changes


def render_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Qt web engine settings for a responsive window (Linux only; yours win if set).

    On WSL the engine draws without a GPU. Each mouse-wheel tick then plays an animated
    scroll that can't keep up, which feels laggy, so wheel scrolling jumps straight to the
    new position instead. Set QTWEBENGINE_CHROMIUM_FLAGS yourself to choose other flags.
    """
    env = os.environ if env is None else env
    if not sys.platform.startswith("linux") or "QTWEBENGINE_CHROMIUM_FLAGS" in env:
        return {}
    return {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-smooth-scrolling"}


class Server:
    """The web app (uvicorn) in a background thread, only while the window is open."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        import uvicorn

        self.port, self.host = port, host
        self._server = uvicorn.Server(uvicorn.Config("app.main:app", host=host, port=port,
                                                     log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, name="web-app", daemon=True)

    def start(self, timeout: float = START_TIMEOUT_S) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                raise RuntimeError(f"The app's server stopped while starting on port {self.port}.")
            time.sleep(0.05)
        raise RuntimeError(f"The app's server didn't start within {timeout:.0f} s.")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def resolve_server(port: int) -> tuple[str, Server | None]:
    """(base URL, our own server or None when an already running one is used)."""
    if ours(port):
        return f"http://127.0.0.1:{port}", None
    server = Server(port if port_free(port) else free_port())
    server.start()
    return f"http://127.0.0.1:{server.port}", server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open New Niche in its own window.")
    parser.add_argument("--path", default=DEFAULT_PATH, help="first page (default /scripting)")
    parser.add_argument("--port", type=int, default=settings.port,
                        help="use the server on this port, or start one there (default PORT)")
    parser.add_argument("--debug", action="store_true", help="allow the web inspector")
    args = parser.parse_args(argv)
    os.environ.update(display_env())  # before Qt loads
    os.environ.update(render_env())

    try:
        import webview
    except ImportError:
        print('pywebview isn\'t installed. Install the desktop extra: pip install -e ".[desktop]"',
              file=sys.stderr)
        return 1

    base, server = resolve_server(args.port)
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    # Save buttons (.txt results) download like in a browser; target=_blank links (Drive
    # folders) open in the system browser.
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.create_window(TITLE, base + path, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                          min_size=MIN_SIZE, text_select=True)
    try:
        # private_mode=False + storage_path: the admin login and the page's saved choices
        # (model, effort per step) survive a restart, as in a browser.
        # Linux: Qt (the desktop extra) directly, without first trying GTK and printing errors.
        gui = "qt" if sys.platform.startswith("linux") else None
        webview.start(gui=gui, private_mode=False, storage_path=str(storage_dir()),
                      debug=args.debug)
    finally:
        if server:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

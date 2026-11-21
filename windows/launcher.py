"""New Niche for Windows: double-click to open the app in its own window.

The app itself stays in WSL (this project folder): the launcher starts its server there if it
isn't running yet, then shows the same pages in a native Windows window (Edge WebView2, which
draws with the graphics card, so scrolling follows the monitor's refresh rate). Closing the
window stops only a server this launcher started; one you started yourself keeps running.

Built into "New Niche.exe" by windows/build.ps1. Only the standard library and pywebview are
used here, nothing from the app, so the exe never needs rebuilding when the app changes.

Where the project is (WSL distro, user, folder) comes from %LOCALAPPDATA%\\New Niche\\launcher.json,
which build.ps1 writes from the folder it runs in; the options below override it.

    "New Niche.exe" [--path /admin] [--distro NAME] [--user NAME] [--project /path] [--port N]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_NAME = "new-niche-overall"
DEFAULT_PORT = 1023
FIRST_PAGE = "/scripting"
PID_FILE = "/tmp/new-niche-windows-launcher.pid"
START_TIMEOUT_S = 90       # a cold WSL start can take a while
NO_WINDOW = 0x08000000     # CREATE_NO_WINDOW: no console flashing up for wsl.exe

LOADING = """<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{height:100%;margin:0;font-family:Inter,"Segoe UI",system-ui,sans-serif;background:#fff;color:#1e1c1a}
 body{display:flex;align-items:center;justify-content:center}
 .box{text-align:center;max-width:32rem;padding:2rem}
 .logo{display:inline-flex;width:44px;height:44px;border-radius:10px;background:#c8322b;color:#fff;
       font:28px Georgia,serif;align-items:center;justify-content:center}
 h1{font:400 28px Georgia,serif;margin:16px 0 6px} p{color:#525150;font-size:14px;line-height:1.6;margin:0}
 .spin{width:22px;height:22px;margin:18px auto 0;border:3px solid #f6d9d4;border-top-color:#c8322b;
       border-radius:50%;animation:s 0.8s linear infinite} @keyframes s{to{transform:rotate(360deg)}}
 .err{color:#a3241e;white-space:pre-wrap;text-align:left;background:#fdf3f1;border-radius:8px;padding:12px 14px}
</style></head><body><div class="box"><span class="logo">N</span><h1>New Niche</h1>
<p id="msg">MESSAGE</p>SPINNER</div></body></html>"""


def page(message: str, *, error: bool = False) -> str:
    html = LOADING.replace("SPINNER", "" if error else '<div class="spin"></div>')
    text = message.replace("&", "&amp;").replace("<", "&lt;")
    return html.replace('<p id="msg">MESSAGE</p>',
                        f'<p class="err">{text}</p>' if error else f'<p id="msg">{text}</p>')


def wsl(args: argparse.Namespace, command: str, **kwargs):
    """Run a shell command in the project folder inside WSL, without a console window."""
    return subprocess.run(
        ["wsl.exe", "-d", args.distro, "-u", args.user, "--cd", args.project, "--",
         "bash", "-c", command],
        capture_output=True, text=True, creationflags=NO_WINDOW, check=False, **kwargs)


def ours(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
            data = json.loads(r.read().decode() or "{}")
    except (OSError, ValueError):
        return False
    return data.get("status") == "ok" and data.get("app") == APP_NAME


def app_port(args: argparse.Namespace) -> int:
    """PORT from the app's own settings (.env), read inside WSL."""
    if args.port:
        return args.port
    if ours(DEFAULT_PORT):
        return DEFAULT_PORT
    r = wsl(args, ".venv/bin/python -c 'from app.config import settings; print(settings.port)'",
            timeout=120)
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("Couldn't read the app's settings in WSL:\n"
                           + (r.stderr.strip() or r.stdout.strip() or "no output")) from None


class Server:
    """The app's server, started by this launcher inside WSL (stopped when the window closes)."""

    def __init__(self, args: argparse.Namespace, port: int):
        self.args, self.port = args, port
        # $$ is this shell; exec keeps that pid for the server, so it can be stopped exactly.
        cmd = (f"echo $$ > {PID_FILE}; exec .venv/bin/python -m uvicorn app.main:app "
               f"--host 127.0.0.1 --port {port}")
        self.proc = subprocess.Popen(
            ["wsl.exe", "-d", args.distro, "-u", args.user, "--cd", args.project, "--",
             "bash", "-c", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW)

    def wait(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if ours(self.port):
                return
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(f"The app's server stopped while starting:\n{err[-1500:]}")
            time.sleep(0.4)
        raise RuntimeError(f"The app's server didn't answer within {START_TIMEOUT_S} s.")

    def stop(self) -> None:
        # Stop only the process this launcher started (its pid is in PID_FILE).
        wsl(self.args, f'p=$(cat {PID_FILE} 2>/dev/null) && kill "$p" 2>/dev/null; '
                       f"rm -f {PID_FILE}", timeout=30)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def storage_dir() -> Path:
    path = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "New Niche" / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "New Niche" / "launcher.json"


def local_config() -> dict:
    """This PC's project location, written by build.ps1 (nothing personal in the source)."""
    try:
        data = json.loads(config_file().read_text(encoding="utf-8-sig"))  # with or without BOM
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    cfg = local_config()
    parser = argparse.ArgumentParser(description="Open New Niche in its own window.")
    parser.add_argument("--path", default=FIRST_PAGE)
    parser.add_argument("--distro", default=cfg.get("distro"))
    parser.add_argument("--user", default=cfg.get("user"))
    parser.add_argument("--project", default=cfg.get("project"))
    parser.add_argument("--port", type=int, default=0, help="default: PORT from the app's .env")
    parser.add_argument("--debug", action="store_true", help="allow the web inspector")
    args = parser.parse_args(argv)

    import webview

    path = args.path if args.path.startswith("/") else f"/{args.path}"
    window = webview.create_window("New Niche", html=page("Starting the app in WSL…"),
                                   width=1280, height=860, min_size=(380, 560),
                                   text_select=True, background_color="#FFFFFF")
    state: dict = {"server": None}

    def boot(win):
        try:
            missing = [k for k in ("distro", "user", "project") if not getattr(args, k)]
            if missing:
                raise RuntimeError(
                    f"The project's location isn't set ({', '.join(missing)}). Run "
                    "windows\\build.ps1 once from the project folder: it saves the location in "
                    f"{config_file()}.")
            port = app_port(args)
            if not ours(port):
                state["server"] = Server(args, port)
                state["server"].wait()
            win.load_url(f"http://127.0.0.1:{port}{path}")
        except Exception as exc:  # noqa: BLE001 -- shown in the window instead of a crash
            win.load_html(page(f"Couldn't open the app.\n\n{exc}", error=True))

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    try:
        webview.start(boot, window, private_mode=False, storage_path=str(storage_dir()),
                      debug=args.debug)
    finally:
        if state["server"]:
            state["server"].stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

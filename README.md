# new-niche-overall

A FastAPI app for writing video scripts step by step (Outline → Script → Audits …) with the
Claude or Codex CLI. Brand prompts come from a TSV uploaded in the admin panel, and each
brand's Rules can loop checks and fixes after a step.

## Requirements (Linux)

- Python 3.11 or newer, with `venv` (Debian/Ubuntu: `sudo apt install python3 python3-venv`;
  Arch: `sudo pacman -S python`)
- At least one generation CLI, installed and logged in:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code): `claude` (log in once with `claude`)
  - [Codex CLI](https://github.com/openai/codex): `codex` (log in once with `codex login`)

## Setup

```bash
git clone <repo-url> new-niche-overall
cd new-niche-overall

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
cp app/brand_links.example.json app/brand_links.json   # optional: brand Drive folders
```

Then edit `.env`:

- `ADMIN_API_KEY`: required for `/admin`. Generate one:
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- `CLAUDE_CLI_BIN` / `CODEX_CLI_BIN`: the CLI paths, if they aren't on your `PATH`
  (check with `which claude codex`).
- `SCRIPTS_CONFIG_DIR` (optional): a shared folder for the prompts config. By default it is
  kept in `app/admin/configs/_scripts`.

`.env`, `app/brand_links.json`, uploaded TSVs and everything in `app/output/` stay on your
machine. They are in `.gitignore`.

## Run

```bash
source .venv/bin/activate
python -m app.main            # http://localhost:1023 (PORT in .env), reloads on code changes
```

Or run it with uvicorn directly: `uvicorn app.main:app --host 0.0.0.0 --port 1023`.

### As a desktop app (same pages, in their own window)

```bash
pip install -e ".[desktop]"      # once: pywebview with the Qt backend
python -m app.desktop            # or: new-niche-desktop
python -m app.desktop --path /admin/scripts/dashboard   # open another page first
```

If the web app is already running on `PORT`, the window uses it (both share the same projects).
Otherwise the window starts its own server and stops it when you close the window. Logins and
the page's saved choices (model, effort per step) are kept between runs. Result downloads ask
where to save, and Drive links open in your browser.

The Qt web engine needs a few system libraries. On Arch:
`sudo pacman -S --needed nss alsa-lib xcb-util xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil libxkbfile`
(Debian/Ubuntu: `sudo apt install libnss3 libasound2 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxkbfile1`).
On WSL, the window opens through WSLg (Windows 11).

### As a Windows app (New Niche.exe)

Double-click **New Niche** on the Desktop. It starts the app inside WSL from this folder (unless
it's already running), and shows it in a native Windows window that draws with the graphics
card, so scrolling runs at the monitor's full frame rate. Closing the window stops only a server
it started itself.

To build or rebuild it (only needed after changing `windows/launcher.py`), in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File \\wsl.localhost\<distro>\home\<user>\new-niche-overall\windows\build.ps1
```

It installs to `%LOCALAPPDATA%\Programs\New Niche\New Niche.exe`, using its own build venv in
`%LOCALAPPDATA%\New Niche\build`. The build works out the WSL distro, user and project folder
from where it runs and saves them in `%LOCALAPPDATA%\New Niche\launcher.json` for the exe; they
can also be passed as `--distro`, `--user`, `--project`.

### In the browser

- Scripting tool: http://localhost:1023/scripting
- Admin (upload the prompts TSV): http://localhost:1023/admin
- Rules guide (keys for the Rules column): http://localhost:1023/admin/scripts/rules-guide
- API docs: http://localhost:1023/docs

## Test

```bash
source .venv/bin/activate
pytest                 # uses fake CLIs, so nothing is sent to Claude or Codex
ruff check app tests
```

## Where things live

| Path | What |
|---|---|
| `app/keys.py` | Every symbol the TSV understands (steps, inputs, `{{ }}`, Rules keywords) |
| `app/rules/` | The Rules language: parser, engine, line-by-line explanation |
| `app/web/` | Scripting page API, projects (CLI sessions), rule jobs, brand catalog |
| `app/admin/` | Admin panel: TSV upload, dashboard, rules guide |
| `app/cli/` | Claude and Codex CLI clients and their settings |
| `app/output/` | Step results and projects (local only, not in git) |

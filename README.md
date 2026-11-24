# new-niche-overall

A FastAPI app for writing scripts step by step with the Claude or Codex CLI. Each brand's
steps and prompts come from a TSV uploaded in the admin panel, and its Rules can run checks
and fixes before or after a step. Brand profiles and the system prompt live in one private
file, outside the repo.

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
mkdir -p app/admin/configs
cp app/brands.example.json app/admin/configs/___brands.json   # then put your brands in it
```

Then edit `.env`:

- `ADMIN_API_KEY`: required for `/admin`. Generate one:
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
- `CLAUDE_CLI_BIN` / `CODEX_CLI_BIN`: the CLI paths, if they aren't on your `PATH`
  (check with `which claude codex`).
- `SCRIPTS_CONFIG_DIR` (optional): a shared folder for the prompts config. By default it is
  kept in `app/admin/configs/_scripts`.

### The brands file (`___brands.json`)

One private JSON file holds everything about the brands. It sits in the admin configs folder,
the folder above `SCRIPTS_CONFIG_DIR` (default `app/admin/configs/___brands.json`; set
`BRANDS_FILE` for another path). Changes show up without a restart.

| Key | What |
|---|---|
| `brands` | One entry per brand: `slug`, `name`, `languages`, `video_length`, `word_count`, `production`, `drive_url`, `notes` |
| `brands[].enabled` | `false` hides the brand on every page and in the API (default `true`) |
| `brands[].config_keys` | The TSV brands it is, by key (`Brand A` in the TSV → `brand_a`): their word and length targets come from here |
| `system_prompt` | The instructions sent with every Claude and Codex run: a string, or a list of lines |
| `tools` | Optional tool texts for the home page: `{"scripting": {"description": "..."}}`; `{brands}` and `{languages}` are filled in |

Without the file the app runs with no brand profiles and a neutral built-in system prompt.
`app/brands.example.json` shows every key.

`.env`, the brands file, uploaded TSVs, the prompts config and everything in `app/output/` stay
on your machine. They are in `.gitignore`.

### New-niche research (a real browser)

The Research page (`/research`) opens YouTube links in Chromium through Playwright: it watches
each video, follows the related videos and collects their details. Install the browser once:

```bash
pip install -e ".[research]"
python -m playwright install chromium
```

Each link runs in its own browser, at the same time as the others; `RESEARCH_MAX_BROWSERS`
(default 4) caps how many are open at once, and the rest wait their turn. The page's **Show the
browsers** switch opens a window per link (on WSL through WSLg); `RESEARCH_HEADLESS=true` makes
hidden the default. Runs are saved to
`app/output/_research/` (not in git).

Each run keeps to its start video's niche: related videos of another niche are skipped without
being opened, and when none is left the run ends with a warning. Words from the start video
rank the related titles and an AI (Claude or Codex, through the same CLIs as Scripting, with
its own instructions and its own limit of 2 calls at a time) labels every video's niche and
picks the next ones. **Research › Settings** sets the run defaults (videos, watch time, date
range, browsers) and the niche check (words / AI, Claude or Codex, model, effort, same niche or
same topic). They're saved in `___research.json` beside the brands file, never in git.

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
| `app/brands_file.py` | Reads the private brands file (`___brands.json`) |
| `app/web/` | Scripting page API, projects (CLI sessions), rule jobs, brand catalog |
| `app/admin/` | Admin panel: TSV upload, dashboard, rules guide |
| `app/cli/` | Claude and Codex CLI clients and their settings |
| `app/research/` | New-niche research: YouTube page parser, the Playwright explorer, runs and their API |
| `app/output/` | Step results and projects (local only, not in git) |

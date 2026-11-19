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

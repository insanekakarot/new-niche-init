# new-niche-overall

FastAPI service.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## Run

```bash
fastapi dev app/main.py
```

Open http://127.0.0.1:8000/docs for the interactive API docs.

## Test

```bash
pytest
```

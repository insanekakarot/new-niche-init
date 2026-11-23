"""Projects: one brand flow, run as one continuous CLI conversation.

A project's first step starts a new Claude / Codex session; every later step resumes
that session (`claude --resume` / `codex exec resume`), so the model keeps the full
context of the earlier steps instead of starting a new chat each time.

Stored at <output_dir>/_projects/<id>/project.json:

    {"id", "name", "brand", "brand_name", "branch", "language", "provider", "model",
     "session_id", "created_at", "updated_at",
     "steps": [{"step", "file", "words", "model", "effort", "session_id", "resumed",
                "language", "extracted", "created_at"}, ...],
     "inputs": {name: {"sha256", "step", "file"}}}

`steps[].file` is the step's saved result (app/output/<step>/<file>): for a code-block prompt,
just the code block. Later steps read it to fill in `{{name}}` in their prompt.
`inputs` records the typed-in inputs (video title, source script, ...) already sent in
this conversation, so later steps don't send them again.

Codex projects also get a fixed working folder, <id>/workdir, so every step runs in the
same place. Projects are matched by brand + flow branch + name (case and spacing ignored).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.web import outputs

PROJECTS_DIRNAME = "_projects"
RECORD_FILENAME = "project.json"
MAX_NAME = 200
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,239}$")

# One step at a time per project: two steps resuming the same session would fork it.
_locks: dict[str, asyncio.Lock] = {}


def projects_dir() -> Path:
    return outputs.output_dir() / PROJECTS_DIRNAME


def clean_name(name: str | None) -> str:
    """Collapse whitespace; '' if nothing is left."""
    return " ".join((name or "").split())[:MAX_NAME]


def same_name(a: str, b: str) -> bool:
    return clean_name(a).casefold() == clean_name(b).casefold()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_path(project_id: str) -> Path:
    return projects_dir() / project_id / RECORD_FILENAME


def workdir(project_id: str) -> Path:
    return projects_dir() / project_id / "workdir"


def lock(project_id: str) -> asyncio.Lock:
    return _locks.setdefault(project_id, asyncio.Lock())


# ---------------------------------------------------------------- read

def load(project_id: str | None) -> dict | None:
    if not project_id or not _ID.match(project_id):
        return None
    try:
        data = json.loads(_record_path(project_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("id") == project_id else None


def all_projects() -> list[dict]:
    root = projects_dir()
    if not root.is_dir():
        return []
    return [p for d in root.iterdir() if d.is_dir() and (p := load(d.name))]


def find(brand: str, branch: int, name: str) -> list[dict]:
    """Projects of this brand and flow with this name, most recently used first."""
    matches = [p for p in all_projects()
               if p.get("brand") == brand and p.get("branch", 0) == branch
               and same_name(p.get("name", ""), name)]
    return sorted(matches, key=lambda p: p.get("updated_at", ""), reverse=True)


def step_names(project: dict) -> list[str]:
    return [s["step"] for s in project.get("steps", [])]


def summary(project: dict) -> dict:
    """What the browser gets: no session ids."""
    return {k: project.get(k) for k in ("id", "name", "brand", "brand_name", "branch",
                                         "language", "provider", "model", "created_at",
                                         "updated_at")} | {
        "steps": step_names(project), "inputs": sorted(project.get("inputs") or {})}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def last_step(project: dict, step: str) -> dict | None:
    """The newest run of `step` in this project."""
    return next((s for s in reversed(project.get("steps", [])) if s.get("step") == step), None)


def step_output(project: dict, step: str) -> str | None:
    """The saved result of the project's newest `step` run (the code block, if extracted)."""
    run = last_step(project, step)
    return outputs.read_output(step, run["file"]) if run and run.get("file") else None


def sent_input(project: dict, name: str) -> str | None:
    """The value of a typed-in input as it was sent earlier in this conversation."""
    sent = (project.get("inputs") or {}).get(name)
    if not sent:
        return None
    value = (outputs.read_meta(sent["step"], sent["file"]).get("inputs") or {}).get(name)
    return value if isinstance(value, str) and fingerprint(value) == sent["sha256"] else None


# ---------------------------------------------------------------- write

def reserve_id(brand: str, name: str) -> str:
    """Create the folder of a new project and return its id (unique even within a second)."""
    root = projects_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    base = f"{brand}__{outputs.slug(name, 40)}__{stamp}"
    n = 1
    while True:
        project_id = base if n == 1 else f"{base}-{n}"
        try:
            (root / project_id).mkdir()
            return project_id
        except FileExistsError:
            n += 1


def discard(project_id: str) -> None:
    """Remove a reserved project whose first step failed (it has no record yet)."""
    if _ID.match(project_id) and not _record_path(project_id).exists():
        shutil.rmtree(projects_dir() / project_id, ignore_errors=True)


def start(project_id: str, *, name: str, brand: str, brand_name: str, branch: int,
          language: str, provider: str) -> dict:
    now = _now()
    return {"id": project_id, "name": name, "brand": brand, "brand_name": brand_name,
            "branch": branch, "language": language, "provider": provider, "model": None,
            "session_id": None, "created_at": now, "updated_at": now, "steps": []}


def record_step(project: dict, *, step: str, file: str, words: int, model: str, effort: str,
                session_id: str | None, resumed: bool, language: str, extracted: bool = False,
                sent: dict[str, str] | None = None, rules: dict | None = None) -> dict:
    """Add a finished step and save the project. The newest session id is the one to resume.

    `sent`: typed-in inputs this step sent into the conversation (name -> value).
    `rules`: set when this result is the final script of the step's rules (app/rules).
    """
    now = _now()
    entry = {
        "step": step, "file": file, "words": words, "model": model, "effort": effort,
        "session_id": session_id, "resumed": resumed, "language": language,
        "extracted": extracted, "created_at": now,
    }
    if rules:
        entry["rules"] = rules
    project.setdefault("steps", []).append(entry)
    known = project.setdefault("inputs", {})
    for name, value in (sent or {}).items():
        known[name] = {"sha256": fingerprint(value), "step": step, "file": file}
    if session_id:
        project["session_id"] = session_id
    project.update(model=model, language=language, updated_at=now)
    _write(_record_path(project["id"]), project)
    return project


def update_session(project_id: str, session_id: str | None) -> None:
    """Save the newest session id (each rule prompt continues the conversation)."""
    project = load(project_id)
    if project and session_id and session_id != project.get("session_id"):
        project.update(session_id=session_id, updated_at=_now())
        _write(_record_path(project_id), project)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

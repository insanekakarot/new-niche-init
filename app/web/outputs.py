"""Step results on disk: <output_dir>/<step>/<brand>__<title>__<timestamp>.txt

Each .txt holds only Claude's response. A .json file with the same name records the run
(brand, language, model, cost, the inputs used) so the result can be found and reused
by the next step.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.admin.scripts_config import STEP_COLUMNS
from app.cli.config import get_cli_settings

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*\.txt$")


def output_dir() -> Path:
    return get_cli_settings().output_dir


def _slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "untitled"


PAYLOAD_SUFFIX = ".payload.txt"
_RULE = "=" * 22


def format_payload(payload: dict, header: dict) -> str:
    """Human-readable dump of exactly what was sent to the CLI.

    The STDIN section is the exact text written to the CLI, between its marker lines.
    For Claude the system prompt travels as a command-line argument; for Codex it is
    already at the top of STDIN.
    """
    lines = [f"# {k}: {v}" for k, v in header.items()]
    lines += ["", f"{_RULE} COMMAND (argv, one argument per line) {_RULE}"]
    for i, arg in enumerate(payload["argv"]):
        shown = "<SYSTEM PROMPT, see next section>" if arg == payload["system_prompt"] else arg
        lines.append(f"[{i:02d}] {shown}")
    if payload["provider"] == "claude":
        lines += ["", f"{_RULE} SYSTEM PROMPT (sent as the --system-prompt argument) {_RULE}",
                  payload["system_prompt"].rstrip("\n")]
    else:
        lines += ["", f"{_RULE} SYSTEM PROMPT {_RULE}",
                  "(Codex has no system-prompt flag: it is the '# Instructions' block at the "
                  "top of STDIN.)"]
    stdin = payload["stdin"]
    lines += ["", f"{_RULE} STDIN (exact text sent; {len(stdin):,} characters, "
                  f"{len(stdin.split()):,} words) {_RULE}",
              stdin, f"{_RULE} END OF STDIN {_RULE}", ""]
    return "\n".join(lines)


def payload_path(output: Path) -> Path:
    """app/output/<step>/<name>.txt -> app/output/<step>/<name>.payload.txt"""
    return output.with_name(output.name[: -len(".txt")] + PAYLOAD_SUFFIX)


def save_output(*, step: str, brand_key: str, title: str, text: str, meta: dict,
                payload: dict | None = None) -> Path:
    folder = output_dir() / step
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    base = f"{brand_key}__{_slug(title)}__{stamp}"
    path = folder / f"{base}.txt"
    n = 2
    while path.exists():
        path = folder / f"{base}-{n}.txt"
        n += 1
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if payload is not None:
        header = {"Step": step, "Brand": brand_key, "Provider": payload["provider"],
                  "Output": path.name, "Recorded": "at run time (exact)"}
        payload_path(path).write_text(format_payload(payload, header), encoding="utf-8")
    return path


def resolve(step: str, name: str) -> Path | None:
    """The file for a download request, or None. Blocks path tricks like '../'."""
    if step not in STEP_COLUMNS or not _SAFE_NAME.match(name) or name.endswith(PAYLOAD_SUFFIX):
        return None
    path = output_dir() / step / name
    return path if path.is_file() else None


def download_url(step: str, name: str) -> str:
    return f"/api/scripting/outputs/{step}/{name}"


def _provider_from_model(model: str) -> str:
    """For results saved before the provider was recorded."""
    if model.startswith("claude") or model in ("sonnet", "opus", "haiku", "fable"):
        return "claude"
    if model.startswith(("gpt", "o3", "o4", "codex")):
        return "codex"
    return ""


def describe(path: Path) -> dict:
    meta: dict = {}
    sidecar = path.with_suffix(".json")
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    step = path.parent.name
    return {
        "name": path.name,
        "step": step,
        "brand": meta.get("brand") or path.name.split("__", 1)[0],
        "title": meta.get("video_title") or "",
        "language": meta.get("language") or "",
        "words": meta.get("words"),
        "provider": meta.get("provider") or _provider_from_model(meta.get("model") or ""),
        "created_at": meta.get("created_at")
        or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "url": download_url(step, path.name),
    }


def list_outputs(step: str, brand: str | None = None, limit: int = 50) -> list[dict]:
    if step not in STEP_COLUMNS:
        return []
    folder = output_dir() / step
    if not folder.is_dir():
        return []
    pattern = f"{brand}__*.txt" if brand else "*.txt"
    files = sorted((p for p in folder.glob(pattern) if not p.name.endswith(PAYLOAD_SUFFIX)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return [describe(p) for p in files[:limit]]

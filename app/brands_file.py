"""The private brands file, ___brands.json: one file for everything about the brands.

It sits with the admin configs (by default <SCRIPTS_CONFIG_DIR>/../___brands.json; BRANDS_FILE
sets another path) and is never in git. Start from app/brands.example.json (the // notes are
only here: JSON itself has no comments):

    {
      "system_prompt": ["line 1", "line 2"],   // sent with every Claude / Codex run
      "tools": {"scripting": {"description": "Write scripts for all {brands} brands ..."}},
      "brands": [
        {
          "slug": "brand-a", "name": "Brand A",
          "enabled": true,                  // false: hidden on every page and in the API
          "config_keys": ["brand_a"],       // the TSV brands it is ('Brand A' -> brand_a)
          "group": "Multi-language", "languages": ["English", "French"],
          "video_length": "10–12 min", "word_count": "1,500–1,700 words",
          "production": "Manual edit", "drive_url": "https://drive.google.com/...",
          "notes": "", "tags": []
        }
      ]
    }

`system_prompt` can be one string or a list of lines. In tool descriptions, {brands} is the
number of shown brands and {languages} their languages besides English.

The file is read again whenever it changes, so edits show up without a restart.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app.config import settings

FILE_NAME = "___brands.json"

log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict = {"key": None, "data": {}, "error": None}


def path() -> Path:
    return settings.brands_file or settings.scripts_config_dir.parent / FILE_NAME


def _read(p: Path) -> tuple[dict, str | None]:
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        return {}, f"can't read {p}: {exc}"
    except ValueError as exc:
        return {}, f"{p} isn't valid JSON: {exc}"
    if not isinstance(data, dict):
        return {}, f"{p} must hold one JSON object ({{...}})"
    return data, None


def load() -> dict:
    """The file's contents; {} when it's missing or can't be read (see error())."""
    p = path()
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), None, None)
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
    if key[1] is None:
        data, error = {}, None  # no file yet: no brands, built-in defaults
    else:
        data, error = _read(p)
        if error:
            log.warning("Brands file: %s", error)
    with _lock:
        _cache.update(key=key, data=data, error=error)
    return data


def error() -> str | None:
    """Why the file couldn't be used (None when it's fine or simply not there)."""
    load()
    return _cache["error"]


def text(value) -> str:
    """A string, or a list of lines joined with newlines; anything else is ''."""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return value if isinstance(value, str) else ""


def system_prompt() -> str:
    """The system prompt from the file ('' if it doesn't set one). A list of lines ends with
    a newline, like a text file."""
    value = load().get("system_prompt")
    prompt = text(value)
    return prompt + "\n" if isinstance(value, list) and prompt.strip() else prompt

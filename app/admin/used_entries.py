"""Used entries: what each brand's videos already covered, so new videos don't repeat them.

Kept next to the prompts config, in <SCRIPTS_CONFIG_DIR>/__used_entries.json (admin files,
not the repo), and edited in Admin → Used entries. {{ used_entries }} in a step or rule
prompt is filled with the brand's list, one entry per line, each named once.

    {"version": 1,
     "brands": {"<brand key>": {"updated_at": "...", "source": "<doc link>",
                                "groups": [{"title": "<a past video>",
                                            "entries": ["<entry>", ...]}]}}}

The text format (what the admin page edits, and what a Google Doc export looks like):
    DOCUMENT 1 — Some Video Title (6 entries)      a group (a past video)
    # Some Video Title                             a group, short form
    Other too:                                     a group ("Other too")
    Number 6 — Some Entry                          an entry ("Number n —" is dropped)
    - Some Entry / 3. Some Entry / Some Entry      entries (bullets and numbering dropped)
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

FILENAME = "__used_entries.json"
MAX_TEXT = 500_000
EMPTY = "None: no entries have been used yet."

_TITLE = re.compile(r"^(?:all\s+)?used\s+entries\s*:?$", re.IGNORECASE)
_DOC = re.compile(r"^document\s+\d+\s*[—–:-]\s*(?P<title>.+?)\s*(?:\(\s*\d+\s+entr(?:y|ies)\s*\))?$",
                  re.IGNORECASE)
_HASH = re.compile(r"^#+\s*(?P<title>.+)$")
_NUMBER = re.compile(r"^(?:number|no\.?|#)\s*\d+\s*[—–:.-]\s*", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*•·▪◦]\s+|\d+\s*[.)]\s+)")


def path() -> Path:
    return settings.scripts_config_dir / FILENAME


def parse(text: str) -> list[dict]:
    """Text (a doc export, or the admin page's text) -> [{"title", "entries": [...]}]."""
    groups: list[dict] = []
    current: dict | None = None
    lines = re.sub(r"[\v\r\f]", "\n", text or "").split("\n")
    for raw in lines:
        line = raw.strip().replace(" ", " ")
        if not line or _TITLE.match(line):
            continue
        m = _DOC.match(line) or _HASH.match(line)
        if m or (line.endswith(":") and len(line) <= 60):
            title = (m.group("title") if m else line[:-1]).strip()
            current = {"title": title, "entries": []}
            groups.append(current)
            continue
        entry = _BULLET.sub("", _NUMBER.sub("", line)).strip().rstrip(".").strip()
        if not entry:
            continue
        if current is None:
            current = {"title": "", "entries": []}
            groups.append(current)
        current["entries"].append(entry)
    return [g for g in groups if g["entries"]]


def to_text(groups: list[dict]) -> str:
    """The editable text for the admin page (parse(to_text(g)) == g)."""
    parts = []
    for g in groups:
        block = ([f"# {g['title']}"] if g.get("title") else []) + list(g["entries"])
        parts.append("\n".join(block))
    return "\n\n".join(parts) + ("\n" if parts else "")


def _key(entry: str) -> str:
    """For spotting the same entry written twice: case, accents and punctuation ignored."""
    s = unicodedata.normalize("NFKD", entry).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def unique(groups: list[dict]) -> list[str]:
    seen, out = set(), []
    for g in groups:
        for e in g["entries"]:
            k = _key(e) or e
            if k not in seen:
                seen.add(k)
                out.append(e)
    return out


def render(groups: list[dict] | None) -> str:
    """{{ used_entries }}: every entry once, one per line."""
    items = unique(groups or [])
    return "\n".join(f"- {e}" for e in items) if items else EMPTY


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "brands": {}}
    if not isinstance(data, dict) or not isinstance(data.get("brands"), dict):
        return {"version": 1, "brands": {}}
    return data


def for_brand(key: str) -> dict | None:
    brand = load()["brands"].get(key)
    return brand if isinstance(brand, dict) and isinstance(brand.get("groups"), list) else None


def value_for(key: str) -> str:
    """What {{ used_entries }} becomes for this brand."""
    brand = for_brand(key)
    return render(brand["groups"] if brand else None)


def save(key: str, groups: list[dict], source: str | None = None) -> dict:
    """Replace one brand's entries (the others stay as they are)."""
    from app.admin.scripts_config import _atomic_write

    data = load()
    old = data["brands"].get(key) or {}
    entry = {"updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
             "source": source if source is not None else old.get("source"),
             "groups": [{"title": g.get("title", ""), "entries": list(g["entries"])}
                        for g in groups if g.get("entries")]}
    data["brands"][key] = entry
    data["version"] = 1
    _atomic_write(path(), (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
    return entry


def summary(key: str, brand: dict | None) -> dict:
    groups = (brand or {}).get("groups") or []
    return {"key": key, "groups": len(groups), "entries": sum(len(g["entries"]) for g in groups),
            "unique": len(unique(groups)), "updated_at": (brand or {}).get("updated_at"),
            "source": (brand or {}).get("source")}

"""Research settings (Research › Settings): the private file ___research.json.

It sits next to the brands file, in the folder above SCRIPTS_CONFIG_DIR (the admin configs
folder), and is never in git. Missing file or keys: the defaults below. A value that isn't
valid is replaced by its default, so a hand-edited file can't break a run.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.cli.config import PROVIDER_EFFORTS
from app.config import settings

FILE_NAME = "___research.json"

# How the next video is chosen (see app/research/niche.py).
MATCHING = {
    "words_ai": "Words + AI",
    "ai": "AI only",
    "words": "Words only",
    "off": "Off (first related video)",
}
PROVIDERS = {"claude": "Claude", "codex": "Codex"}
# How close a video must be to the start video (the AI judges at this level).
STRICTNESS = {
    "niche": "Same niche",    # what a channel is built around: "History › Railways"
    "topic": "Same topic",    # the start video's subject too: "Europe's night trains"
}

# key: (default, minimum, maximum) for whole numbers
_NUMBERS = {
    "videos": (10, 1, 50),
    "watch_s": (10, 1, 600),
    "max_age_days": (30, 0, 3650),      # 0 = any age
    "max_browsers": (4, 1, 12),
    "ai_candidates": (15, 3, 40),       # related videos sent to the AI per check
    "min_words": (2, 1, 10),            # shared words for a words-only match
}


def path() -> Path:
    return settings.scripts_config_dir.parent / FILE_NAME


def defaults() -> dict:
    values = {k: d for k, (d, _, _) in _NUMBERS.items()}
    values["max_browsers"] = max(1, settings.research_max_browsers)
    return values | {
        "show_browser": not settings.research_headless,
        "matching": "words_ai",
        "strictness": "niche",
        "provider": "claude",
        "model": "",                    # empty: the CLI's model from .env
        "effort": "low",                # a quick judgement; empty: the CLI's effort from .env
    }


def clean(raw: dict | None) -> dict:
    """The settings from `raw`, every value checked; anything missing or wrong: the default."""
    raw = raw if isinstance(raw, dict) else {}
    out = defaults()
    for key, (_, low, high) in _NUMBERS.items():
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            out[key] = value
    if isinstance(raw.get("show_browser"), bool):
        out["show_browser"] = raw["show_browser"]
    if raw.get("matching") in MATCHING:
        out["matching"] = raw["matching"]
    if raw.get("strictness") in STRICTNESS:
        out["strictness"] = raw["strictness"]
    if raw.get("provider") in PROVIDERS:
        out["provider"] = raw["provider"]
    if isinstance(raw.get("model"), str) and len(raw["model"]) <= 100:
        out["model"] = raw["model"].strip()
    effort = raw.get("effort")
    if effort == "" or effort in PROVIDER_EFFORTS[out["provider"]]:
        out["effort"] = effort
    elif out["effort"] not in PROVIDER_EFFORTS[out["provider"]]:
        out["effort"] = ""
    return out


def load() -> dict:
    try:
        return clean(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return clean(None)


def save(raw: dict) -> dict:
    """Check and write the settings; returns what was saved."""
    values = clean(raw)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, indent=2), encoding="utf-8")
    tmp.replace(p)
    return values


def describe(values: dict) -> str:
    """'Words + AI · Claude, low effort' for the Research page."""
    mode = MATCHING[values["matching"]]
    if values["matching"] in ("words_ai", "ai"):
        ai = PROVIDERS[values["provider"]]
        extra = ", ".join(x for x in (values["model"], f"{values['effort']} effort"
                                      if values["effort"] else "") if x)
        return (f"{mode} · {ai}{f' ({extra})' if extra else ''} · "
                f"{STRICTNESS[values['strictness']].lower()}")
    return mode

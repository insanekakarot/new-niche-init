"""Research runs: one per start link, each in its own browser, several at the same time.

A run explores from its link (app/research/explorer.py) in the background; the page polls
GET /api/research/runs/{id}. At most "Browsers at once" runs (Research › Settings, default
RESEARCH_MAX_BROWSERS = 4) have a browser open at once; more wait their turn ("queued").
Each run keeps to its start video's niche as set in the settings (app/research/niche.py). Every run is saved after each video to
<output_dir>/_research/<run id>.json, so results survive a restart.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime
from pathlib import Path

from app.research import explorer, options
from app.research.niche import Matcher
from app.research.stats import with_stats
from app.web import outputs

_runs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}
_stop: set[str] = set()
_slots: set[int] = set()   # window positions in use by shown browsers
_gate: asyncio.Semaphore | None = None
_gate_key: tuple[int, int] | None = None

# Swapped out by the tests (no browser there).
explore = explorer.explore


def folder() -> Path:
    return outputs.output_dir() / "_research"


def _gate_for_loop() -> asyncio.Semaphore:
    """The limit on open browsers (made again if the setting or the event loop changed)."""
    global _gate, _gate_key
    key = (options.load()["max_browsers"], id(asyncio.get_running_loop()))
    if _gate is None or _gate_key != key:
        _gate, _gate_key = asyncio.Semaphore(key[0]), key
    return _gate


def _save(run: dict) -> None:
    path = folder() / f"{run['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def summary(run: dict) -> dict:
    """A run without its videos' long fields: for the list of runs."""
    first = run["videos"][0] if run["videos"] else None
    return {k: run.get(k) for k in ("id", "url", "status", "videos_wanted", "watch_s",
                                    "max_age_days", "show_browser", "created_at", "finished_at",
                                    "error", "ended", "warning", "progress", "niche",
                                    "matching")} | {
        "videos_done": len(run["videos"]),
        "start_title": first["title"] if first else "",
        "start_thumbnail": first["thumbnail"] if first else "",
    }


def start(url: str, videos: int, watch_s: int, show_browser: bool | None = None,
          max_age_days: int | None = None, opts: dict | None = None) -> dict:
    """Create a run and start it in the background (needs a running event loop).
    show_browser: None = the settings' default. max_age_days: None = any age.
    opts: the research settings (Research › Settings); the niche matching comes from them."""
    opts = opts or options.load()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run = {
        "id": f"{stamp}-{secrets.token_hex(3)}",
        "url": url.strip(),
        "videos_wanted": videos,
        "watch_s": watch_s,
        "max_age_days": max_age_days,
        "ended": None,                # why it stopped before videos_wanted, if it did
        "warning": False,             # ended because nothing in the niche was left
        "show_browser": opts["show_browser"] if show_browser is None else show_browser,
        # How the niche was kept, as set when the run started.
        "matching": {k: opts[k] for k in ("matching", "strictness", "provider", "model",
                                          "effort", "ai_candidates", "min_words")}
                    | {"text": options.describe(opts)},
        "niche": None,                # the start video's niche: {label, description, keywords}
        "off_niche": [],              # related videos skipped as another niche (not opened)
        "status": "queued",           # queued | running | done | stopped | failed
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "progress": {"n": 0, "second": 0, "of": watch_s, "ad": False, "note": "Waiting for a browser"},
        "skipped": [],
        "videos": [],
    }
    _runs[run["id"]] = run
    _save(run)
    _tasks[run["id"]] = asyncio.get_running_loop().create_task(_run(run))
    return run


def _matcher(run: dict) -> Matcher | None:
    m = run.get("matching") or {}
    if m.get("matching", "off") == "off":
        return None
    return Matcher(options.clean(m))


def _take_slot() -> int:
    """The lowest free window position, so shown browsers don't open on top of each other."""
    slot = next(i for i in range(len(_slots) + 1) if i not in _slots)
    _slots.add(slot)
    return slot


async def _run(run: dict) -> None:
    rid = run["id"]

    async def on_event(e: dict) -> None:
        p = run["progress"]
        if e["type"] == "opening":
            p.update(n=e["n"], second=0, ad=False, note="Opening the video")
        elif e["type"] == "watching":
            p.update(n=e["n"], second=e["second"], ad=e["ad"],
                     note="Checking the niche" if e.get("checking")
                     else "Ad playing" if e["ad"] else "Watching")
        elif e["type"] == "niche":
            run["niche"] = e["niche"]
            _save(run)
        elif e["type"] == "off_niche":
            run["off_niche"].extend(e["items"])
        elif e["type"] == "video":
            run["videos"].append(e["video"])
            p.update(note="Reading the details")
            _save(run)
        elif e["type"] == "skipped":
            run["skipped"].append({"id": e["id"], "error": e["error"]})
        elif e["type"] == "end":
            run["ended"] = e["reason"]
            run["warning"] = bool(e.get("warning"))

    try:
        async with _gate_for_loop():
            if rid in _stop:
                run["status"] = "stopped"
                return
            run["status"], run["started_at"] = "running", time.time()
            run["progress"]["note"] = "Starting the browser"
            _save(run)
            slot = _take_slot()
            try:
                await explore(run["url"], videos=run["videos_wanted"], watch_s=run["watch_s"],
                              headless=not run["show_browser"], slot=slot,
                              max_age_days=run["max_age_days"], matcher=_matcher(run),
                              on_event=on_event,
                              stopped=lambda: rid in _stop)
            finally:
                _slots.discard(slot)
            run["status"] = "stopped" if rid in _stop else "done"
    except asyncio.CancelledError:
        run["status"] = "stopped"
        raise
    except explorer.ExploreError as exc:
        run["status"], run["error"] = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - shown on the page, never crashes the server
        run["status"], run["error"] = "failed", f"{type(exc).__name__}: {exc}"[:500]
    finally:
        run["finished_at"] = time.time()
        run["progress"]["note"] = {"done": "Finished", "stopped": "Stopped"}.get(
            run["status"], "Failed")
        _stop.discard(rid)
        _tasks.pop(rid, None)
        _save(run)


def get(rid: str) -> dict | None:
    """The run with its medians (app/research/stats.py), or None."""
    run = _get(rid)
    return with_stats(run) if run else None


def _get(rid: str) -> dict | None:
    if rid in _runs:
        return _runs[rid]
    path = folder() / f"{rid}.json"
    if not rid.replace("-", "").isalnum() or not path.is_file():
        return None
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if run.get("status") in ("queued", "running"):  # the app was closed mid-run
        run["status"], run["error"] = "stopped", "The app was closed while this run was going."
    return run


def recent(limit: int = 30) -> list[dict]:
    """The newest runs first: this session's and the saved ones."""
    ids = set(_runs)
    if folder().is_dir():
        ids |= {p.stem for p in folder().glob("*.json")}
    runs = [r for r in (_get(i) for i in sorted(ids, reverse=True)[:limit]) if r]
    return [summary(r) for r in sorted(runs, key=lambda r: r["created_at"], reverse=True)]


def stop(rid: str) -> bool:
    run = _runs.get(rid)
    if not run or run["status"] not in ("queued", "running"):
        return False
    _stop.add(rid)
    run["progress"]["note"] = "Stopping after this second"
    return True

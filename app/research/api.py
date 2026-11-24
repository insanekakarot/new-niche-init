"""Research API: start runs (one per link, all at once), follow them, stop them, download them."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.research import options, runs
from app.research.youtube import video_id

router = APIRouter(prefix="/api/research", tags=["research"])

MAX_LINKS = 20


class StartRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1)
    # Left out: the value from Research › Settings.
    videos: int | None = Field(None, ge=1, le=50)     # videos to open per link, the start one included
    watch_s: int | None = Field(None, ge=1, le=600)   # seconds to watch each
    max_age_days: int | None = Field(None, ge=0, le=3650)  # open only videos this new; 0 / null = any
    show_browser: bool | None = None


@router.post("/runs")
async def start_runs(req: StartRequest):
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(422, "Add at least one YouTube link.")
    if len(urls) > MAX_LINKS:
        raise HTTPException(422, f"At most {MAX_LINKS} links at a time.")
    bad = [u for u in urls if not video_id(u)]
    if bad:
        raise HTTPException(422, "Not a YouTube video link: " + ", ".join(bad[:3]))
    opts = options.load()
    days = req.max_age_days if "max_age_days" in req.model_fields_set else opts["max_age_days"]
    started = [runs.start(u, req.videos or opts["videos"], req.watch_s or opts["watch_s"],
                          req.show_browser, days or None, opts) for u in urls]
    return {"runs": [runs.summary(r) for r in started]}


class SettingsRequest(BaseModel):
    values: dict


@router.get("/settings")
def get_settings():
    return {"values": options.load(), "defaults": options.defaults(),
            "file": options.FILE_NAME}


@router.post("/settings")
def save_settings(req: SettingsRequest):
    try:
        values = options.save(req.values)
    except OSError as exc:
        raise HTTPException(500, f"The settings couldn't be saved: {exc}") from exc
    return {"values": values, "text": options.describe(values)}


@router.get("/runs")
def list_runs():
    return {"runs": runs.recent()}


def _run_or_404(run_id: str) -> dict:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    return _run_or_404(run_id)


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str):
    _run_or_404(run_id)
    return {"stopping": runs.stop(run_id)}


@router.get("/runs/{run_id}/download")
def download_run(run_id: str):
    run = _run_or_404(run_id)
    return Response(json.dumps(run, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="research-{run_id}.json"'})

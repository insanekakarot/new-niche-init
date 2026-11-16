"""Run one scripting step through Claude or Codex, save the result, list/download results."""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.cli import (
    CliBusy,
    CliError,
    CliTimeout,
    Provider,
    build_payload,
    provider_info,
    run_cli,
)
from app.cli.config import get_cli_settings
from app.web import outputs
from app.web.scripting import build_step_prompt, find_brand

router = APIRouter(prefix="/api/scripting", tags=["scripting"])

_BRAND_KEY = re.compile(r"^[a-z0-9_]{1,80}$")
MAX_INPUT_CHARS = 400_000


class RunRequest(BaseModel):
    brand: str
    branch: int = Field(0, ge=0)
    step: str
    language: str = Field(..., min_length=1, max_length=40)
    inputs: dict[str, str] = Field(default_factory=dict)
    # Which CLI generates the step; empty = CLI_DEFAULT_PROVIDER.
    provider: Provider | None = None


def _word_count(text: str) -> int:
    return len(text.split())


@router.post("/run")
async def run_step(req: RunRequest):
    try:
        found = find_brand(req.brand)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(503, f"The scripting config couldn't be read: {exc}") from exc
    if not found:
        raise HTTPException(404, f"Unknown brand {req.brand!r}. Reload the page.")
    brand, view = found

    if req.branch >= len(view["branches"]):
        raise HTTPException(404, "That flow no longer exists for this brand. Reload the page.")
    branch = view["branches"][req.branch]
    step = next((s for s in branch["steps"] if s["name"] == req.step), None)
    if step is None:
        raise HTTPException(404, f"Step {req.step!r} isn't part of this brand's flow.")

    languages = (view["catalog"] or {}).get("languages") or ["English"]
    if req.language not in languages:
        raise HTTPException(400, f"{view['name']} doesn't publish in {req.language}.")
    if not step["has_prompt"]:
        raise HTTPException(400, f"No {step['label']} prompt is set for {view['name']} in admin.")

    allowed = {f["name"] for f in step["fields"]}
    inputs = {k: v for k, v in req.inputs.items() if k in allowed}
    missing = [f["label"] for f in step["fields"] if f["required"] and not inputs.get(f["name"], "").strip()]
    if missing:
        raise HTTPException(400, f"Missing required input(s): {', '.join(missing)}.")
    if sum(len(v) for v in inputs.values()) > MAX_INPUT_CHARS:
        raise HTTPException(413, "The inputs are too long for one request.")

    prompt = build_step_prompt(brand=brand, view=view, branch=branch, step=step,
                               inputs=inputs, language=req.language)
    provider = req.provider or get_cli_settings().default_provider
    # Effort is set per step type (CLI_STEP_EFFORTS); unlisted steps use the provider default.
    effort = get_cli_settings().effort_for(step["name"])
    try:
        result = await run_cli(provider, prompt, effort=effort)
    except CliBusy as exc:
        raise HTTPException(429, str(exc)) from exc
    except CliTimeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except CliError as exc:
        raise HTTPException(502, str(exc)) from exc

    words = _word_count(result.text)
    title = inputs.get("video_title", "") or step["label"]
    meta = {
        "brand": req.brand,
        "brand_name": view["name"],
        "step": step["name"],
        "branch": req.branch,
        "language": req.language,
        "video_title": inputs.get("video_title", ""),
        "words": words,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": result.provider,
        "model": result.model,
        "models_used": result.models_used,
        "effort": result.effort,
        "duration_s": round(result.duration_s, 1),
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "session_id": result.session_id,
        "inputs": inputs,
    }
    path = outputs.save_output(step=step["name"], brand_key=req.brand, title=title,
                               text=result.text, meta=meta,
                               payload=build_payload(provider, prompt, effort=effort))
    return {
        "text": result.text,
        "words": words,
        "file": {"step": step["name"], "name": path.name,
                 "url": outputs.download_url(step["name"], path.name)},
        "meta": {k: meta[k] for k in ("provider", "model", "effort", "duration_s", "cost_usd",
                                      "num_turns", "language", "created_at")},
        "usage": result.usage,
    }


@router.get("/outputs")
def list_outputs(step: str = Query(...), brand: str | None = Query(None)):
    if brand is not None and not _BRAND_KEY.match(brand):
        raise HTTPException(400, "Invalid brand.")
    return {"outputs": outputs.list_outputs(step, brand)}


@router.get("/outputs/{step}/{name}")
def get_output(step: str, name: str, inline: bool = False):
    path = outputs.resolve(step, name)
    if path is None:
        raise HTTPException(404, "Output not found.")
    if inline:
        return FileResponse(path, media_type="text/plain; charset=utf-8")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)


@router.get("/providers")
def providers():
    """The CLIs that can generate, with their configured model and effort (no secrets)."""
    return provider_info()

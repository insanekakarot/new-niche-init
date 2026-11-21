"""Run one scripting step through Claude or Codex, save the result, list/download results.

Every step belongs to a project (app/web/projects.py). With `new_project` on (the default
for a flow's first step) the step starts a new project and a new conversation. With it off
(the default for later steps) the step is sent into the saved project's conversation via
resume, found by `project_id` or by name.
"""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
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
from app.cli.config import PROVIDER_EFFORTS, get_cli_settings
from app.web import extract, outputs, projects, rule_jobs
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
    # Which CLI generates the step; empty = the project's provider, else CLI_DEFAULT_PROVIDER.
    provider: Provider | None = None
    # On: start a new project (new conversation) with this step. Off: continue a saved one.
    # Empty = on for the first step of the flow, off for the others.
    new_project: bool | None = None
    # The project's name; empty = the video title.
    project_name: str = Field("", max_length=projects.MAX_NAME)
    # Continue exactly this project (e.g. picked when several share a name).
    project_id: str | None = Field(None, max_length=240)
    # Run the brand's rules after this step ('> audits LOOP { ... }'), when it has any.
    run_rules: bool = True
    # Effort for this step (and the rules after it); empty = CLI_STEP_EFFORTS, else the
    # provider's default. Must be one of the provider's levels (PROVIDER_EFFORTS).
    effort: str | None = Field(None, max_length=20)


class RulesRequest(BaseModel):
    """Run a step's rules again in a project, starting from that step's latest result."""
    brand: str
    branch: int = Field(0, ge=0)
    step: str
    project_id: str = Field(..., max_length=240)
    effort: str | None = Field(None, max_length=20)  # as in RunRequest


def _check_effort(provider: str, effort: str | None) -> str | None:
    """The chosen effort, if the provider has that level (None = use the configured one)."""
    effort = (effort or "").strip().lower() or None
    levels = PROVIDER_EFFORTS.get(provider, ())
    if effort and effort not in levels:
        raise HTTPException(400, f"{provider.capitalize()} has no {effort!r} effort. Use one of: "
                                 f"{', '.join(levels)}.")
    return effort


def _word_count(text: str) -> int:
    return len(text.split())


def _fail(status: int, code: str, message: str, **extra) -> HTTPException:
    """An error the page can act on: {"code", "message", ...} as the detail."""
    return HTTPException(status, {"code": code, "message": message, **extra})


def _find_project(req: RunRequest, view: dict, branch: dict, name: str) -> dict:
    """The saved project this step continues, or an error saying what to do instead."""
    if req.project_id:
        project = projects.load(req.project_id)
        if project and project.get("brand") == req.brand and project.get("branch", 0) == req.branch:
            return project
        raise _fail(404, "project_not_found",
                    "That project no longer exists for this brand and flow. Turn on New project "
                    "to start a new one, or pick another.")

    matches = projects.find(req.brand, req.branch, name)
    if not matches:
        first = branch["steps"][0]["label"]
        raise _fail(404, "project_not_found",
                    f"No project named “{name}” found for {view['name']}. Turn on New project to "
                    f"start one with this step, or run the flow from its first step ({first}).",
                    first_step=branch["steps"][0]["name"])
    if len(matches) > 1:
        raise _fail(409, "choose_project",
                    f"{len(matches)} projects are named “{name}” for {view['name']}. "
                    "Choose the one to continue.",
                    projects=[projects.summary(p) for p in matches])
    return matches[0]


def _resolve_inputs(inputs: dict[str, str], step: dict, branch: dict,
                    project: dict | None) -> tuple[dict[str, str], dict[str, str]]:
    """(values to send, inputs the project's conversation already has: name -> where).

    Continuing a project:
      * `{{name}}` in the prompt, left empty: filled with the project's saved result for it
        (an earlier step's code block, or a typed-in input sent before).
      * Not placed in the prompt, and empty or the same as what the conversation already
        has: not sent again; the prompt points at it instead (that's what << >> is for).
    Anything typed in that differs from the conversation is sent as usual.
    """
    values = {k: v for k, v in inputs.items() if v.strip()}
    in_chat: dict[str, str] = {}
    if not project:
        return values, in_chat
    labels = {s["name"]: s["label"] for s in branch["steps"]}
    sent_before = project.get("inputs") or {}
    for f in step["fields"]:
        name = f["name"]
        typed = values.get(name, "")
        if f["kind"] == "generated":
            run = projects.last_step(project, f["source"])
            if run is None:
                continue  # that step hasn't run in this project: it must be pasted
            saved = projects.step_output(project, f["source"])
            known = projects.fingerprint(saved) if saved is not None else None
            where = (f"your {f['source_label']} step response from earlier in this conversation"
                     + (" (the part inside its code block)" if run.get("extracted") else ""))
        else:
            if name not in sent_before:
                continue
            saved = projects.sent_input(project, name)
            known = sent_before[name].get("sha256")
            first = labels.get(sent_before[name].get("step"), "an earlier")
            where = (f"the {f['label'].lower()} sent with the {first} step earlier in this "
                     "conversation")
        if f.get("placed"):
            if not typed and saved:
                values[name] = saved
            elif not typed:
                in_chat[name] = where
        elif not typed or projects.fingerprint(typed) == known:
            in_chat[name] = where
            values.pop(name, None)
    return values, in_chat


def _check_idle(project: dict) -> None:
    """One thing at a time per project: two runs resuming one session would fork it."""
    if projects.lock(project["id"]).locked():
        raise _fail(409, "project_busy",
                    f"Project “{project['name']}” is already running a step. Wait for it "
                    "to finish.")
    job = rule_jobs.active(project["id"])
    if job:
        raise _fail(409, "project_busy",
                    f"Project “{project['name']}” is still running the rules after its "
                    f"{job.step_label} step. Wait for them to finish.", job=job.id)


class _StepCancelled(Exception):
    """CANCEL ran in the rules before the step ('< step'): the step doesn't run."""


def _before_job(project, project_id, name, req, view, step, provider, values, inputs, effort,
                session_id):
    """A rules job for '< step': starts from the project's latest result ("" if none)."""
    start_file, start_text = "", ""
    if project and project.get("steps"):
        last = project["steps"][-1]
        start_file = last.get("file") or ""
        start_text = projects.step_output(project, last["step"]) or ""
    job = rule_jobs.create(
        project={"id": project_id, "name": name, "brand": req.brand, "branch": req.branch},
        view=view, step=step, provider=provider, language=req.language,
        title=values.get("video_title", "") or name, start_file=start_file,
        start_text=start_text,
        values=rule_jobs.values_for(view, step, {**inputs, **values}, name, req.language),
        effort=effort, when="before")
    job.session_id = session_id
    return job


@router.post("/run")
async def run_step(req: RunRequest, background: BackgroundTasks):
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
    if sum(len(v) for v in inputs.values()) > MAX_INPUT_CHARS:
        raise HTTPException(413, "The inputs are too long for one request.")

    # ---- project: start a new one, or continue a saved one
    step_index = next(i for i, s in enumerate(branch["steps"]) if s is step)
    new = req.new_project if req.new_project is not None else step_index == 0
    name = projects.clean_name(req.project_name) or projects.clean_name(inputs.get("video_title"))
    if not name:
        raise HTTPException(400, "Give the project a name (or fill in the video title).")

    project = None if new else _find_project(req, view, branch, name)
    provider = req.provider or (project or {}).get("provider") or get_cli_settings().default_provider
    if project and provider != project.get("provider"):
        started = project.get("provider", "?").capitalize()
        raise _fail(409, "provider_mismatch",
                    f"Project “{project['name']}” is a conversation with {started}. Switch "
                    f"Generate with to {started} to continue it, or turn on New project.",
                    provider=project.get("provider"))
    if project and not project.get("session_id"):
        raise _fail(409, "resume_failed",
                    f"Project “{project['name']}” has no saved conversation to continue. "
                    "Turn on New project to start a fresh one.")

    values, in_chat = _resolve_inputs(inputs, step, branch, project)
    missing = [f for f in step["fields"] if f["required"] and f["name"] not in in_chat
               and not values.get(f["name"], "").strip()]
    if missing:
        message = f"Missing required input(s): {', '.join(f['label'] for f in missing)}."
        not_run = [f["source_label"] for f in missing if f["kind"] == "generated"]
        if project and not_run:
            message += (f" Project “{project['name']}” hasn't run the {', '.join(not_run)} "
                        "step yet: run it first, or paste its result.")
        raise HTTPException(400, message)

    # Effort: the one picked on the page for this step, else CLI_STEP_EFFORTS, else the
    # provider's default.
    chosen = _check_effort(provider, req.effort)
    effort = chosen or get_cli_settings().effort_for(step["name"])

    if project:
        project_id, resume = project["id"], project["session_id"]
        _check_idle(project)
    else:
        project_id, resume = projects.reserve_id(req.brand, name), None
    workdir = projects.workdir(project_id) if provider == "codex" else None

    # Rules before this step ('< outline ...'): they run first, in the same conversation (for
    # a new project they start it), and their results can fill {{ name }} in the step prompt.
    before_hook = rule_jobs.hook_for(brand, step["name"], before=True) if req.run_rules else None
    before = None
    try:
        async with projects.lock(project_id):
            if before_hook:
                before = _before_job(project, project_id, name, req, view, step, provider,
                                     values, inputs, chosen, resume)
                await rule_jobs.run_before(before, *before_hook)
                resume = before.session_id
                if before.state.cancelled:
                    raise _StepCancelled
            prompt = build_step_prompt(
                brand=brand, view=view, branch=branch, step=step, inputs=values,
                language=req.language, project_name=name,
                continuing=project is not None or resume is not None, in_chat=in_chat,
                rule_results=before.outputs if before else None,
                builtins=rule_jobs.values_for(view, step, {}, name, req.language))
            run_args = {"effort": effort, "resume": resume, "workdir": workdir,
                        "keep_session": True}
            result = await run_cli(provider, prompt, **run_args)
    except _StepCancelled:
        if project is None:
            projects.discard(project_id)
        else:
            projects.update_session(project["id"], before.session_id)
        tag = before.state.tag
        raise _fail(409, "step_cancelled",
                    f"The rules before {step['label']} said CANCEL"
                    + (f" (TAG is “{tag}”)" if tag else "")
                    + f", so {step['label']} didn't run.", rules=before.public()) from None
    except CliError as exc:
        if project is None:
            projects.discard(project_id)  # a project only exists once its first step worked
        if isinstance(exc, CliBusy):
            raise HTTPException(429, str(exc)) from exc
        if isinstance(exc, CliTimeout):
            raise HTTPException(504, str(exc)) from exc
        if before is not None and before.status == "failed":
            raise _fail(502, "rules_before_failed", str(exc), rules=before.public()) from exc
        if resume and project:
            raise _fail(502, "resume_failed",
                        f"{exc} Couldn't continue project “{project['name']}”'s conversation. "
                        "Turn on New project to start a fresh one.") from exc
        raise HTTPException(502, str(exc)) from exc

    if project is None:
        project = projects.start(project_id, name=name, brand=req.brand, brand_name=view["name"],
                                 branch=req.branch, language=req.language, provider=provider)

    # A code-block prompt: the block is the result the next step uses; keep the rest beside it.
    output, extracted, status, warning = result.text, False, None, None
    if step.get("code_block"):
        block = extract.code_block(result.text)
        output, extracted, status, warning = block.text, block.found, block.status, block.warning

    words = _word_count(output)
    title = values.get("video_title", "") or name
    meta = {
        "brand": req.brand,
        "brand_name": view["name"],
        "step": step["name"],
        "branch": req.branch,
        "language": req.language,
        "video_title": values.get("video_title", ""),
        "words": words,
        "extracted": extracted,
        "extract_status": status,
        "response_words": _word_count(result.text),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": result.provider,
        "model": result.model,
        "models_used": result.models_used,
        "effort": result.effort,
        "duration_s": round(result.duration_s, 1),
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "session_id": result.session_id,
        "project_id": project["id"],
        "project_name": project["name"],
        "resumed": resume is not None,
        "resumed_session_id": resume,
        # The values sent (typed in, or taken from the project's saved results).
        "inputs": values,
        "in_conversation": sorted(in_chat),
        # Rules that ran just before this step ('< step'): job id, log and what they returned.
        "rules_before": ({"job": before.id, "log": before.public()["log"],
                          "outputs": sorted(before.outputs)} if before else None),
    }
    path = outputs.save_output(step=step["name"], brand_key=req.brand, title=title,
                               text=output, meta=meta,
                               payload=build_payload(provider, prompt, **run_args),
                               response=result.text if extracted else None)
    sent = {f["name"]: values[f["name"]] for f in step["fields"]
            if f["kind"] == "user" and values.get(f["name"], "").strip()}
    projects.record_step(project, step=step["name"], file=path.name, words=words,
                         model=result.model, effort=result.effort,
                         session_id=result.session_id, resumed=resume is not None,
                         language=req.language, extracted=extracted, sent=sent)

    # Rules after this step ('> audits ...'): they run next, in the background, in this
    # conversation, starting from the result just saved. The page follows them.
    job = None
    hook = rule_jobs.hook_for(brand, step["name"]) if req.run_rules else None
    if hook:
        job = rule_jobs.create(
            project=project, view=view, step=step, provider=provider, language=req.language,
            title=title, start_file=path.name, start_text=output,
            values=rule_jobs.values_for(view, step, {**inputs, **values}, project["name"],
                                        req.language),
            effort=chosen)
        background.add_task(rule_jobs.run, job, *hook)
    return {
        "rules": job.public() if job else None,
        # The rules that ran before this step ('< step'), if any.
        "rules_before": before.public() if before else None,
        "text": output,
        "words": words,
        # The whole response when `text` is only its code block, else None.
        "response": result.text if extracted else None,
        "extract": {"expected": bool(step.get("code_block")), "status": status,
                    "warning": warning},
        "file": {"step": step["name"], "name": path.name,
                 "url": outputs.download_url(step["name"], path.name),
                 "response_url": outputs.download_url(step["name"], path.name, full=True)
                 if extracted else None},
        "meta": {k: meta[k] for k in ("provider", "model", "effort", "duration_s", "cost_usd",
                                      "num_turns", "language", "created_at", "resumed")},
        "project": projects.summary(project),
        "usage": result.usage,
    }


@router.get("/rules/{job_id}")
def rules_progress(job_id: str):
    """Progress of the rules running after a step: each check / revision, then the result."""
    job = rule_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Those rules aren't running any more (was the server "
                                 "restarted?). The project keeps every finished result.")
    return job.public()


@router.post("/rules")
async def run_rules_again(req: RulesRequest, background: BackgroundTasks):
    """Run a step's rules again, from that step's latest result in the project."""
    try:
        found = find_brand(req.brand)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(503, f"The scripting config couldn't be read: {exc}") from exc
    if not found:
        raise HTTPException(404, f"Unknown brand {req.brand!r}. Reload the page.")
    brand, view = found
    if req.branch >= len(view["branches"]):
        raise HTTPException(404, "That flow no longer exists for this brand. Reload the page.")
    step = next((s for s in view["branches"][req.branch]["steps"] if s["name"] == req.step), None)
    hook = rule_jobs.hook_for(brand, req.step) if step else None
    if hook is None:
        raise HTTPException(404, f"No rules run after the {req.step} step for {view['name']}.")
    project = projects.load(req.project_id)
    if not project or project.get("brand") != req.brand or project.get("branch", 0) != req.branch:
        raise _fail(404, "project_not_found", "That project no longer exists for this brand.")
    last = projects.last_step(project, req.step)
    text = projects.step_output(project, req.step) if last else None
    if not text:
        raise HTTPException(400, f"Project “{project['name']}” has no {step['label']} result "
                                 "to run the rules on yet.")
    if not project.get("session_id"):
        raise _fail(409, "resume_failed", f"Project “{project['name']}” has no saved "
                                          "conversation to continue.")
    _check_idle(project)
    provider = project.get("provider") or "claude"
    chosen = _check_effort(provider, req.effort)
    meta = outputs.read_meta(req.step, last["file"])
    language = meta.get("language") or project.get("language") or "English"
    inputs = meta.get("inputs") if isinstance(meta.get("inputs"), dict) else {}
    job = rule_jobs.create(
        project=project, view=view, step=step, provider=provider,
        language=language, title=meta.get("video_title") or project["name"],
        start_file=last["file"], start_text=text,
        values=rule_jobs.values_for(view, step, inputs, project["name"], language),
        effort=chosen)
    background.add_task(rule_jobs.run, job, *hook)
    return job.public()


@router.get("/outputs")
def list_outputs(step: str = Query(...), brand: str | None = Query(None)):
    if brand is not None and not _BRAND_KEY.match(brand):
        raise HTTPException(400, "Invalid brand.")
    return {"outputs": outputs.list_outputs(step, brand)}


@router.get("/projects")
def list_projects(brand: str = Query(...), branch: int | None = Query(None, ge=0),
                  name: str | None = Query(None, max_length=projects.MAX_NAME)):
    """Saved projects of a brand (optionally one flow / one name), most recently used first."""
    if not _BRAND_KEY.match(brand):
        raise HTTPException(400, "Invalid brand.")
    found = [p for p in projects.all_projects() if p.get("brand") == brand
             and (branch is None or p.get("branch", 0) == branch)
             and (not name or projects.same_name(p.get("name", ""), name))]
    found.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return {"projects": [projects.summary(p) for p in found]}


@router.get("/outputs/{step}/{name}")
def get_output(step: str, name: str, inline: bool = False, full: bool = False):
    """A saved result; `full`: the whole response saved next to a code-block result."""
    path = outputs.resolve(step, name, full=full)
    if path is None:
        raise HTTPException(404, "Output not found.")
    if inline:
        return FileResponse(path, media_type="text/plain; charset=utf-8")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


@router.get("/providers")
def providers():
    """The CLIs that can generate, with their configured model and effort (no secrets)."""
    return provider_info()

"""Run a step's rules after it (Rules column, app/rules) in the background, and report progress.

Each rule prompt is one CLI call that resumes the project's conversation. The page polls
GET /api/scripting/rules/{job_id} to show every check and revision as it happens.

Files: <output_dir>/_projects/<project>/rules/<job>/NN-<name>.prompt.txt (what was sent),
NN-<name>.txt (the answer) and log.json. When the rules revised the script, the final
script is saved like a step result (app/output/<step>/...) and recorded in the project, so
the next step uses it.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import rules
from app.admin import used_entries
from app.admin.scripts_config import STEP_COLUMNS
from app.cli import CliError, run_cli
from app.cli.config import get_cli_settings
from app.rules.engine import passed
from app.rules.lang import RuleSyntaxError
from app.web import outputs, projects
from app.web.extract import code_block, wants_code_block

_jobs: dict[str, Job] = {}
_KEEP = 200  # finished jobs remembered in memory


@dataclass
class Job:
    id: str
    project_id: str
    project_name: str
    brand: str
    brand_name: str
    branch: int
    step: str
    step_label: str
    provider: str
    language: str
    title: str
    start_file: str
    start_text: str
    values: dict[str, str]
    status: str = "queued"  # queued | running | done | failed
    state: rules.State = None  # type: ignore[assignment]
    error: str | None = None
    final: dict | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    # Picked on the page for the step; wins over CLI_STEP_EFFORTS for every rule prompt.
    chosen_effort: str | None = None
    when: str = "after"  # "after" ('> step', in the background) | "before" ('< step')
    # Each rule prompt's latest result (code block, else the whole answer), by lower-case
    # name: before a step these fill {{ name }} in the step's prompt.
    outputs: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict:
        st = self.state
        return {
            "id": self.id, "status": self.status, "step": self.step, "step_label": self.step_label,
            "when": self.when,
            "project_id": self.project_id, "project_name": self.project_name,
            "provider": self.provider,
            "tag": st.tag, "rating": st.rating if st.tag else None, "runs": st.runs,
            "ready": passed(st.tag),
            "log": [{k: v for k, v in e.items() if k != "started_at"} for e in st.log],
            "warnings": list(st.warnings), "error": self.error, "final": self.final,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
        }


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def active(project_id: str) -> Job | None:
    """The rules job running (or about to run) in this project, if any."""
    return next((j for j in _jobs.values()
                 if j.project_id == project_id and j.status in ("queued", "running")), None)


def word_range(word_count: str | None) -> str:
    """'1,200–1,500 words' -> '1,200–1,500'"""
    return re.sub(r"\s*words?\s*$", "", word_count or "", flags=re.IGNORECASE).strip()


def values_for(view: dict, step: dict, inputs: dict[str, str], project_name: str,
               language: str) -> dict[str, str]:
    """What {{ name }} in a rule prompt is filled with: the built-ins, then the run's inputs."""
    catalog = view.get("catalog") or {}
    values = {
        "words_range": word_range(catalog.get("word_count")),
        "word_count": catalog.get("word_count") or "",
        "video_length": catalog.get("video_length") or "",
        "language": language,
        "brand_name": view.get("name") or "",
        "project_name": project_name,
        "step": step["label"],
        # The brand's previously used entries (Admin → Used entries), one per line.
        "used_entries": used_entries.value_for(view.get("key") or ""),
    }
    values.update({k: v for k, v in inputs.items() if isinstance(v, str) and v.strip()})
    return {k: v for k, v in values.items() if v}


def create(*, project: dict, view: dict, step: dict, provider: str, language: str, title: str,
           start_file: str, start_text: str, values: dict[str, str],
           effort: str | None = None, when: str = "after") -> Job:
    job = Job(id=secrets.token_hex(6), project_id=project["id"], project_name=project["name"],
              brand=project["brand"], brand_name=view["name"], branch=project.get("branch", 0),
              step=step["name"], step_label=step["label"], provider=provider, language=language,
              title=title, start_file=start_file, start_text=start_text, values=values,
              chosen_effort=effort, when=when)
    job.state = rules.State(script=start_text)
    _jobs[job.id] = job
    for old in [j for j in _jobs.values() if j.finished_at][:-_KEEP]:
        _jobs.pop(old.id, None)
    return job


def _prompt(job: Job, name: str, text: str, check: bool, entry: dict) -> str:
    where = f"{job.when} the {job.step_label} step"
    if entry.get("iteration"):
        where += f" · loop pass {entry['iteration']}"
    parts = [f"# Rule: {name} ({where})", text.strip(), "",
             f"- Output language: {job.language}. Write the entire response in {job.language}."]
    if job.when == "before":
        parts.append(f"- This runs before the {job.step_label} step, which follows in this "
                     "conversation and uses this answer.")
    elif check:
        parts.append("- Check the current script: the most recent complete script in this "
                     "conversation.")
    elif not wants_code_block(text):
        parts.append("- Put the complete revised script inside one ``` code block; the pipeline "
                     "keeps only that block.")
    return "\n".join(parts)


def _caller(job: Job, folder, rule_prompts: dict[str, str]):
    """call(name, entry) for the engine: one rule prompt = one CLI call in the conversation."""
    workdir = projects.workdir(job.project_id) if job.provider == "codex" else None
    cli = get_cli_settings()

    async def call(name: str, entry: dict) -> str:
        check = rules.is_check(rule_prompts[name])
        # {{ name }}: the run's inputs and built-ins, plus the results of rule prompts that
        # already ran in this job (RESEARCH -> {{ research }} in a later rule prompt).
        values = {**job.values, **job.outputs}
        prompt = _prompt(job, name, rules.fill(rule_prompts[name], values), check, entry)
        base = f"{entry['n']:02d}-{name.lower()}"
        (folder / f"{base}.prompt.txt").write_text(prompt, encoding="utf-8")
        effort = job.chosen_effort or cli.effort_for(name.lower()) or cli.effort_for(job.step)
        def retrying(n: int, total: int, exc: CliError, wait: float) -> None:
            # Shown on the page while it waits: "retry 1/3".
            entry.update(retry=f"{n}/{total}", retry_error=str(exc)[:300])
            entry.pop("offline", None)

        def offline(host: str, waited: float) -> None:
            # "no connection to chatgpt.com, waiting (1 min)" until it answers again.
            entry["offline"] = f"{host} · {int(waited // 60)} min {int(waited % 60)} s"

        result = await run_cli(job.provider, prompt, effort, resume=job.session_id,
                               workdir=workdir, keep_session=True, on_retry=retrying,
                               on_offline=offline)
        entry.pop("retry", None)
        entry.pop("offline", None)
        job.session_id = result.session_id or job.session_id
        job.model, job.effort = result.model, result.effort
        projects.update_session(job.project_id, job.session_id)
        (folder / f"{base}.txt").write_text(result.text.rstrip() + "\n", encoding="utf-8")
        block = code_block(result.text)
        job.outputs[name.lower()] = (block.text if block.found else result.text).strip()
        entry.update(file=f"{base}.txt", model=result.model, effort=result.effort,
                     cost_usd=result.cost_usd, retries=result.retries)
        return result.text

    return call


def _folder(job: Job):
    folder = projects.projects_dir() / job.project_id / "rules" / (
        f"before-{job.step}-{job.id}" if job.when == "before" else job.id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _save_log(job: Job, folder) -> None:
    (folder / "log.json").write_text(json.dumps(job.public(), ensure_ascii=False, indent=2,
                                                default=str) + "\n", encoding="utf-8")


async def run_before(job: Job, stmts: list, rule_prompts: dict[str, str]) -> None:
    """Run the rules before a step ('< step'), now, inside the step's own request.

    The caller holds the project lock and set job.session_id (None for a new project: then the
    first rule prompt starts the conversation). Afterwards job.session_id is the conversation
    to send the step into and job.outputs fills {{ name }} in the step's prompt. Raises
    CliError if a rule prompt fails, so the step doesn't run on half the rules.
    """
    folder = _folder(job)
    job.status = "running"
    try:
        await rules.run_hook(stmts, script=job.start_text, prompts=rule_prompts,
                             call=_caller(job, folder, rule_prompts), state=job.state)
        job.status = "done"
    except (CliError, RuleSyntaxError) as exc:
        job.status, job.error = "failed", str(exc)
        raise CliError(f"The rules before {job.step_label} stopped: {exc}") from exc
    finally:
        job.finished_at = time.time()
        _save_log(job, folder)


async def run(job: Job, stmts: list, rule_prompts: dict[str, str]) -> None:
    """Run the hook's rules (a background task). Never raises: the job records what happened."""
    folder = _folder(job)
    project = projects.load(job.project_id) or {}
    job.session_id = project.get("session_id")
    call = _caller(job, folder, rule_prompts)

    job.status = "running"
    try:
        async with projects.lock(job.project_id):
            await rules.run_hook(stmts, script=job.start_text, prompts=rule_prompts, call=call,
                                 state=job.state)
        job.status = "done"
    except (CliError, RuleSyntaxError) as exc:
        job.status, job.error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 -- a background job must always finish readable
        job.status, job.error = "failed", f"Unexpected error: {exc}"
    try:
        _finish(job)
    except OSError as exc:
        job.status, job.error = "failed", f"Couldn't save the final script: {exc}"
    job.finished_at = time.time()
    _save_log(job, folder)


def _finish(job: Job) -> None:
    """Save the final script (if the rules revised it) and record it in the project."""
    st = job.state
    summary = {"job": job.id, "status": job.status, "tag": st.tag,
               "rating": st.rating if st.tag else None, "runs": st.runs}
    name = job.start_file
    if st.changed:
        meta = {
            "brand": job.brand, "brand_name": job.brand_name, "step": job.step,
            "branch": job.branch, "language": job.language, "video_title": job.title,
            "words": len(st.script.split()), "extracted": True, "final": True,
            "created_at": datetime.now(timezone.utc).isoformat(), "provider": job.provider,
            "model": job.model, "effort": job.effort, "session_id": job.session_id,
            "project_id": job.project_id, "project_name": job.project_name, "resumed": True,
            "rules": {**summary, "log": job.public()["log"], "from_file": job.start_file},
        }
        path = outputs.save_output(step=job.step, brand_key=job.brand, title=job.title,
                                   text=st.script, meta=meta)
        name = path.name
        project = projects.load(job.project_id)
        if project:
            projects.record_step(project, step=job.step, file=name,
                                 words=len(st.script.split()), model=job.model or "",
                                 effort=job.effort or "", session_id=job.session_id,
                                 resumed=True, language=job.language, extracted=True,
                                 rules=summary)
    project = projects.load(job.project_id)
    job.final = {
        "text": st.script, "words": len(st.script.split()), "changed": st.changed,
        "tag": st.tag, "ready": passed(st.tag),
        "file": {"step": job.step, "name": name, "url": outputs.download_url(job.step, name)},
        "project": projects.summary(project) if project else None,
    }


def hook_for(brand: dict, step_name: str, *, before: bool = False
             ) -> tuple[list, dict[str, str]] | None:
    """(statements to run after this step, or before it, rule prompts) or None if none."""
    try:
        program = rules.parse(brand.get("rules"), STEP_COLUMNS)
    except RuleSyntaxError:
        return None
    stmts = (program.before if before else program.hooks).get(step_name)
    return (stmts, brand.get("rule_prompts") or {}) if stmts else None

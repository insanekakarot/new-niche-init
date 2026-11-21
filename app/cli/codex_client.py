"""Send one request to OpenAI Codex (`codex exec`) and return its response.

Codex runs in an empty folder with a read-only sandbox, takes the prompt on stdin,
writes its final message to a file (-o) and streams events as JSONL (--json), which give
the token usage and any error.

`resume=<thread id>` continues an earlier conversation (`codex exec resume <id>`), so the
model keeps the full context of the previous steps. A project passes its own `workdir` so
every step runs in the same folder; one-off runs get a temp folder that is deleted after.
"""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path

from app.cli.common import CliError, CliResult, run_process, run_slot, tail
from app.cli.config import CodexCliSettings, get_codex_settings

LAST_MESSAGE_FILE = "last_message.txt"


def build_command(s: CodexCliSettings, workdir: Path, resume: str | None = None,
                  keep_session: bool = False) -> list[str]:
    """`resume` continues that thread; `keep_session` saves the session so it can be resumed."""
    cmd = [*shlex.split(s.bin), "exec",
           "--model", s.model,
           "-c", f"model_reasoning_effort={s.reasoning_effort}",
           "-c", f"web_search={s.web_search}",
           "--sandbox", s.sandbox,
           "--skip-git-repo-check",
           "--color", "never",
           "--json",
           "-C", str(workdir),
           "-o", str(workdir / LAST_MESSAGE_FILE)]
    # A session that is resumed, or will be, has to be saved.
    if s.ephemeral and not (keep_session or resume):
        cmd.append("--ephemeral")
    if s.ignore_user_config:
        cmd.append("--ignore-user-config")
    if s.ignore_rules:
        cmd.append("--ignore-rules")
    cmd += shlex.split(s.extra_args)
    if resume:
        cmd += ["resume", resume]
    cmd.append("-")  # read the prompt from stdin
    return cmd


def with_instructions(prompt: str, s: CodexCliSettings) -> str:
    if not s.system_prompt.strip():
        return prompt
    return f"# Instructions\n{s.system_prompt.strip()}\n\n{prompt}"


def parse_events(stdout: bytes) -> dict:
    """Pull what we need out of the --json event stream."""
    out: dict = {"usage": {}, "errors": [], "messages": [], "thread_id": None, "tool_calls": 0}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started":
            out["thread_id"] = event.get("thread_id")
        elif kind == "turn.completed":
            out["usage"] = event.get("usage") or {}
        elif kind in ("error", "turn.failed"):
            err = event.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            out["errors"].append(str(msg or event.get("message") or "unknown error"))
        elif kind == "item.completed":
            if item.get("type") == "agent_message" and item.get("text"):
                out["messages"].append(item["text"])
            elif item.get("type") not in ("agent_message", "reasoning"):
                out["tool_calls"] += 1
    return out


def parse_response(stdout: bytes, stderr: bytes, returncode: int, last_message: str | None,
                   s: CodexCliSettings, duration_s: float) -> CliResult:
    events = parse_events(stdout)
    text = (last_message or "").strip() or (events["messages"][-1].strip()
                                             if events["messages"] else "")
    if returncode != 0 or (events["errors"] and not text):
        reason = "; ".join(e for e in events["errors"] if e) or tail(stderr) or f"exit code {returncode}"
        raise CliError(f"Codex returned an error: {reason}")
    if not text:
        raise CliError("Codex finished but returned an empty response.")
    return CliResult(
        text=text,
        provider="codex",
        model=s.model,
        effort=s.reasoning_effort,
        duration_s=duration_s,
        session_id=events["thread_id"],
        num_turns=events["tool_calls"] + 1,
        usage=events["usage"],
        models_used=[s.model],
    )


def with_effort(s: CodexCliSettings, effort: str | None) -> CodexCliSettings:
    """Settings for one run at the step's reasoning effort."""
    return s.model_copy(update={"reasoning_effort": effort}) if effort else s


def build_payload(prompt: str, settings: CodexCliSettings | None = None,
                  workdir: Path | None = None, effort: str | None = None,
                  resume: str | None = None, keep_session: bool = False) -> dict:
    """Exactly what run_codex sends. The instructions travel inside stdin (no flag exists).

    Without a `workdir` the run uses a random temp folder, shown as <temp-dir>.
    """
    s = with_effort(settings or get_codex_settings(), effort)
    return {"provider": "codex",
            "argv": build_command(s, workdir or Path("<temp-dir>"), resume, keep_session),
            "stdin": with_instructions(prompt, s), "resume": resume,
            "system_prompt": s.system_prompt, "system_prompt_via": "top of stdin (# Instructions)"}


async def _run_in(workdir: Path, prompt: str, s: CodexCliSettings, resume: str | None,
                  keep_session: bool) -> CliResult:
    last = workdir / LAST_MESSAGE_FILE
    last.unlink(missing_ok=True)  # a reused folder must not hand back an earlier step's answer
    payload = build_payload(prompt, s, workdir, resume=resume, keep_session=keep_session)
    stdout, stderr, code, secs = await run_process(
        payload["argv"], payload["stdin"], cwd=workdir,
        timeout=s.timeout_seconds, label="Codex", setting_prefix="CODEX_CLI_",
    )
    last_message = last.read_text(encoding="utf-8") if last.is_file() else None
    result = parse_response(stdout, stderr, code, last_message, s, secs)
    result.session_id = result.session_id or resume
    return result


async def run_codex(prompt: str, settings: CodexCliSettings | None = None,
                    effort: str | None = None, resume: str | None = None,
                    workdir: Path | None = None, keep_session: bool = False) -> CliResult:
    s = with_effort(settings or get_codex_settings(), effort)
    async with run_slot("codex", s.max_concurrent_runs, "Codex"):
        if workdir is not None:
            workdir.mkdir(parents=True, exist_ok=True)
            return await _run_in(workdir, prompt, s, resume, keep_session)
        with tempfile.TemporaryDirectory(prefix="codex-run-") as tmp:
            return await _run_in(Path(tmp), prompt, s, resume, keep_session)

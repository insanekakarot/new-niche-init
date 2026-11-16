"""Send one request to OpenAI Codex (`codex exec`) and return its response.

Codex runs in a fresh empty folder with a read-only sandbox, takes the prompt on stdin,
writes its final message to a file (-o) and streams events as JSONL (--json), which give
the token usage and any error.
"""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path

from app.cli.common import CliError, CliResult, run_process, run_slot, tail
from app.cli.config import CodexCliSettings, get_codex_settings

LAST_MESSAGE_FILE = "last_message.txt"


def build_command(s: CodexCliSettings, workdir: Path) -> list[str]:
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
    if s.ephemeral:
        cmd.append("--ephemeral")
    if s.ignore_user_config:
        cmd.append("--ignore-user-config")
    if s.ignore_rules:
        cmd.append("--ignore-rules")
    cmd += shlex.split(s.extra_args)
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
                  workdir: Path = Path("<temp-dir>"), effort: str | None = None) -> dict:
    """Exactly what run_codex sends. The instructions travel inside stdin (no flag exists)."""
    s = with_effort(settings or get_codex_settings(), effort)
    return {"provider": "codex", "argv": build_command(s, workdir),
            "stdin": with_instructions(prompt, s),
            "system_prompt": s.system_prompt, "system_prompt_via": "top of stdin (# Instructions)"}


async def run_codex(prompt: str, settings: CodexCliSettings | None = None,
                    effort: str | None = None) -> CliResult:
    s = with_effort(settings or get_codex_settings(), effort)
    async with run_slot("codex", s.max_concurrent_runs, "Codex"):
        with tempfile.TemporaryDirectory(prefix="codex-run-") as tmp:
            workdir = Path(tmp)
            payload = build_payload(prompt, s, workdir)
            stdout, stderr, code, secs = await run_process(
                payload["argv"], payload["stdin"], cwd=workdir,
                timeout=s.timeout_seconds, label="Codex", setting_prefix="CODEX_CLI_",
            )
            last = workdir / LAST_MESSAGE_FILE
            last_message = last.read_text(encoding="utf-8") if last.is_file() else None
            return parse_response(stdout, stderr, code, last_message, s, secs)

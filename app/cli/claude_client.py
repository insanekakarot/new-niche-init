"""Send one request to Claude Code (`claude -p`) and return its response.

The prompt goes in on stdin (it can be far longer than a command line allows) and the
response comes back as the CLI's single-result JSON (`--output-format json`).
"""

from __future__ import annotations

import json
import shlex

from app.cli.common import CliError, CliResult, run_process, run_slot, tail
from app.cli.config import CLAUDE_EFFORTS, ClaudeCliSettings, get_claude_settings, get_cli_settings


def build_command(s: ClaudeCliSettings) -> list[str]:
    cmd = [*shlex.split(s.bin), "-p", "--output-format", "json",
           "--model", s.model, "--effort", s.effort,
           "--system-prompt", s.system_prompt]
    tools = [t.strip() for t in s.tools.split(",") if t.strip()]
    cmd += ["--tools", ",".join(tools)]
    if tools:
        cmd += ["--allowedTools", *tools]
    if s.fallback_model:
        cmd += ["--fallback-model", s.fallback_model]
    if s.max_budget_usd > 0:
        cmd += ["--max-budget-usd", str(s.max_budget_usd)]
    if s.safe_mode:
        cmd.append("--safe-mode")
    if s.no_session_persistence:
        cmd.append("--no-session-persistence")
    cmd += shlex.split(s.extra_args)
    return cmd


def parse_response(stdout: bytes, stderr: bytes, returncode: int, s: ClaudeCliSettings,
                   duration_s: float) -> CliResult:
    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw.splitlines()[-1] if raw else "")
    except (json.JSONDecodeError, IndexError):
        raise CliError(
            f"The Claude CLI exited with code {returncode} without a usable response. "
            f"{tail(stderr) or raw[-1500:]}"
        ) from None

    text = (data.get("result") or "").strip()
    # The CLI can report subtype "success" alongside is_error=true, so check is_error first.
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        reason = text or data.get("subtype") or data.get("terminal_reason") or "unknown error"
        raise CliError(f"Claude returned an error: {reason}")
    if not text:
        raise CliError("Claude finished but returned an empty response.")

    return CliResult(
        text=text,
        provider="claude",
        model=s.model,
        effort=s.effort,
        duration_s=duration_s,
        cost_usd=data.get("total_cost_usd"),
        session_id=data.get("session_id"),
        num_turns=data.get("num_turns"),
        usage=data.get("usage") or {},
        models_used=list((data.get("modelUsage") or {}).keys()),
    )


def with_effort(s: ClaudeCliSettings, effort: str | None) -> ClaudeCliSettings:
    """Settings for one run at the step's effort ("ultra" is Codex-only; Claude gets "max")."""
    if not effort:
        return s
    effort = "max" if effort == "ultra" else effort
    if effort not in CLAUDE_EFFORTS:
        raise CliError(f"Claude has no {effort!r} effort. Use one of: {', '.join(CLAUDE_EFFORTS)}.")
    return s.model_copy(update={"effort": effort})


def build_payload(prompt: str, settings: ClaudeCliSettings | None = None,
                  effort: str | None = None) -> dict:
    """Exactly what run_claude sends: the command line and the text written to stdin."""
    s = with_effort(settings or get_claude_settings(), effort)
    return {"provider": "claude", "argv": build_command(s), "stdin": prompt,
            "system_prompt": s.system_prompt, "system_prompt_via": "--system-prompt argument"}


async def run_claude(prompt: str, settings: ClaudeCliSettings | None = None,
                     effort: str | None = None) -> CliResult:
    s = with_effort(settings or get_claude_settings(), effort)
    async with run_slot("claude", s.max_concurrent_runs, "Claude"):
        cwd = get_cli_settings().output_dir
        cwd.mkdir(parents=True, exist_ok=True)  # neutral folder: no project files to pick up
        payload = build_payload(prompt, s)
        stdout, stderr, code, secs = await run_process(
            payload["argv"], payload["stdin"], cwd=cwd, timeout=s.timeout_seconds,
            label="Claude", setting_prefix="CLAUDE_CLI_",
        )
        return parse_response(stdout, stderr, code, s, secs)

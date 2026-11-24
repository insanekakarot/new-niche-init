"""Ask Claude or Codex a research question (the niche checks in app/research/niche.py).

Kept apart from the Scripting runs on purpose:
- its own instructions (RESEARCH_SYSTEM), never the brands file's writer prompt;
- its own limit on calls at the same time (MAX_AT_ONCE), waited for, never refused, so research
  can't take a Scripting run's slot (those are refused with "already in progress" when full);
- no tools (Claude), no saved sessions, a fresh folder each time.
The CLI path, login and other flags still come from .env (CLAUDE_CLI_* / CODEX_CLI_*).
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from app.cli import CliError, claude_client, codex_client
from app.cli.common import run_process
from app.cli.config import get_claude_settings, get_codex_settings

MAX_AT_ONCE = 2
RETRIES = 1
TIMEOUT_S = 180     # a niche check is short; never the Scripting runs' 20 minutes

RESEARCH_SYSTEM = """\
You classify YouTube videos by niche for a research tool.
- Answer with one JSON object only, exactly in the shape the task asks for: no prose, no
  Markdown fence.
- Judge the topic and audience, not the format or the clickbait words ("shocking", "truth").
- Titles may be in any language or mixed (Hindi, Urdu, English); read them as they mean.
"""

_gates: dict[int, asyncio.Semaphore] = {}


def _gate() -> asyncio.Semaphore:
    loop = id(asyncio.get_running_loop())
    if loop not in _gates:
        _gates.clear()
        _gates[loop] = asyncio.Semaphore(MAX_AT_ONCE)
    return _gates[loop]


def settings_for(provider: str, model: str = "", effort: str = ""):
    """The CLI settings for a research call: .env's, with the research instructions, no tools
    (Claude), at most TIMEOUT_S, and the model / effort from Research › Settings if set."""
    if provider == "claude":
        s = claude_client.with_effort(get_claude_settings(), effort or None)
        extra = {"tools": ""}
    elif provider == "codex":
        s = codex_client.with_effort(get_codex_settings(), effort or None)
        extra = {}
    else:
        raise CliError(f"Unknown AI {provider!r}: use claude or codex.")
    return s.model_copy(update={"system_prompt": RESEARCH_SYSTEM,
                                "timeout_seconds": min(s.timeout_seconds, TIMEOUT_S),
                                **extra, **({"model": model} if model else {})})


def build_payload(provider: str, prompt: str, *, model: str = "", effort: str = "",
                  workdir: Path | None = None) -> dict:
    """Exactly what ask() sends: {provider, argv, stdin, system_prompt, ...}. Codex runs in a
    fresh temp folder, shown as <temp-dir> when `workdir` isn't given."""
    s = settings_for(provider, model, effort)
    if provider == "claude":
        return claude_client.build_payload(prompt, s)
    return codex_client.build_payload(prompt, s, workdir=workdir)


async def _claude(prompt: str, model: str, effort: str, workdir: Path) -> str:
    s = settings_for("claude", model, effort)
    payload = build_payload("claude", prompt, model=model, effort=effort)
    stdout, stderr, code, secs = await run_process(
        payload["argv"], payload["stdin"], cwd=workdir, timeout=s.timeout_seconds,
        label="Claude", setting_prefix="CLAUDE_CLI_")
    return claude_client.parse_response(stdout, stderr, code, s, secs).text


async def _codex(prompt: str, model: str, effort: str, workdir: Path) -> str:
    s = settings_for("codex", model, effort)
    return (await codex_client._run_in(workdir, prompt, s, None, False)).text


async def ask(provider: str, prompt: str, *, model: str = "", effort: str = "") -> str:
    """The CLI's answer text. Raises CliError when it fails twice."""
    run = {"claude": _claude, "codex": _codex}.get(provider)
    if run is None:
        raise CliError(f"Unknown AI {provider!r}: use claude or codex.")
    async with _gate():
        for attempt in range(RETRIES + 1):
            try:
                with tempfile.TemporaryDirectory(prefix="research-ai-") as tmp:
                    return await run(prompt, model, effort, Path(tmp))
            except CliError:
                if attempt >= RETRIES:
                    raise
                await asyncio.sleep(3)
    raise AssertionError("unreachable")


def parse_json(text: str) -> dict:
    """The first JSON object in an answer (a ```json fence or prose around it is ignored)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:text.rfind("}") + 1])
    for c in candidates:
        try:
            value = json.loads(c)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("The AI's answer had no JSON object.")

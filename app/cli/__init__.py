"""CLI integrations for generation.

config.py         settings for every CLI, all from .env
claude_client.py  Claude Code  (`claude -p`)
codex_client.py   OpenAI Codex (`codex exec`)
"""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from app.cli import claude_client, codex_client
from app.cli.claude_client import run_claude
from app.cli.codex_client import run_codex
from app.cli.common import CliBusy, CliError, CliResult, CliTimeout
from app.cli.config import PROVIDERS, Provider, provider_info


def _unknown(provider: str) -> CliError:
    return CliError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.")


log = logging.getLogger(__name__)

# A failed call is tried again, up to MAX_RETRIES times, after these pauses (seconds): a short
# connection drop ("Reconnecting... workspace routing discovery failed") then doesn't stop a
# step or its rules. Only failures are retried: a running call is never cut short.
MAX_RETRIES = 3
RETRY_WAITS_S = (3.0, 4.0, 5.0)
# Failures another try can't fix: reported at once. A timeout isn't retried either (the call
# already ran for the whole time limit).
_PERMANENT = re.compile(
    r"CLI not found|Unknown provider|has no '[^']*' effort|No conversation found|"
    r"not logged in|log ?in again|please (?:log|sign) ?in|unauthori[sz]ed|\b401\b|"
    r"invalid api key|authentication", re.IGNORECASE)

# on_retry(retry_number, max_retries, error, wait_seconds), called before each pause.
OnRetry = Callable[[int, int, CliError, float], None]
# The pause between tries (tests make it instant).
_sleep = asyncio.sleep


def retryable(exc: CliError) -> bool:
    """Whether trying the same call again could work."""
    return not isinstance(exc, CliTimeout) and not _PERMANENT.search(str(exc))


async def run_cli(provider: str, prompt: str, effort: str | None = None, *,
                  resume: str | None = None, workdir: Path | None = None,
                  keep_session: bool = False, retries: int = MAX_RETRIES,
                  on_retry: OnRetry | None = None) -> CliResult:
    """Send the prompt to the chosen CLI and return its response.

    `effort` overrides the provider's default effort for this run (see CLI_STEP_EFFORTS).
    `resume` continues that session (Claude session id / Codex thread id) instead of
    starting a new conversation; `keep_session` saves the session so it can be resumed.
    `workdir` is the fixed folder Codex runs in (Claude always uses CLI_OUTPUT_DIR).
    A failure is retried up to `retries` times (at most MAX_RETRIES), 3–5 s apart, in the
    same conversation; result.retries says how many it took.
    """
    retries = max(0, min(retries, MAX_RETRIES))
    for attempt in range(retries + 1):
        try:
            result = await _run_once(provider, prompt, effort, resume=resume, workdir=workdir,
                                     keep_session=keep_session)
        except CliError as exc:
            if attempt >= retries or not retryable(exc):
                if attempt:
                    tries = f"{attempt} retr{'y' if attempt == 1 else 'ies'}"
                    raise type(exc)(f"{exc} (still failing after {tries})") from exc
                raise
            wait = RETRY_WAITS_S[min(attempt, len(RETRY_WAITS_S) - 1)]
            log.warning("%s failed (%s); retry %d of %d in %.0f s", provider,
                        str(exc)[:200], attempt + 1, retries, wait)
            if on_retry:
                on_retry(attempt + 1, retries, exc, wait)
            await _sleep(wait)
            continue
        result.retries = attempt
        return result
    raise AssertionError("unreachable")  # the loop always returns or raises


async def _run_once(provider: str, prompt: str, effort: str | None, *, resume: str | None,
                    workdir: Path | None, keep_session: bool) -> CliResult:
    if provider == "claude":
        return await run_claude(prompt, effort=effort, resume=resume, keep_session=keep_session)
    if provider == "codex":
        return await run_codex(prompt, effort=effort, resume=resume, workdir=workdir,
                               keep_session=keep_session)
    raise _unknown(provider)


def build_payload(provider: str, prompt: str, effort: str | None = None, *,
                  resume: str | None = None, workdir: Path | None = None,
                  keep_session: bool = False) -> dict:
    """The exact command line and stdin `run_cli(...)` with the same arguments would send."""
    if provider == "claude":
        return claude_client.build_payload(prompt, effort=effort, resume=resume,
                                           keep_session=keep_session)
    if provider == "codex":
        return codex_client.build_payload(prompt, workdir=workdir, effort=effort, resume=resume,
                                          keep_session=keep_session)
    raise _unknown(provider)


__all__ = [
    "MAX_RETRIES",
    "PROVIDERS",
    "CliBusy",
    "CliError",
    "CliResult",
    "CliTimeout",
    "Provider",
    "build_payload",
    "provider_info",
    "run_claude",
    "run_cli",
    "run_codex",
]

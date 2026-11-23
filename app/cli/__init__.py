"""CLI integrations for generation.

config.py         settings for every CLI, all from .env
claude_client.py  Claude Code  (`claude -p`)
codex_client.py   OpenAI Codex (`codex exec`)
"""

import asyncio
import logging
import re
import ssl
import time
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

# A lost connection to the provider's server. Such an outage can last a few minutes (one of
# chatgpt.com's addresses refusing connections until DNS hands out the other one), longer
# than the 3-5 s pauses. So after the pause, the next try waits until the server answers
# again: checked every CONNECTION_CHECK_S, for at most CONNECTION_WAIT_S over the whole call.
_CONNECTION = re.compile(
    r"routing discovery failed|Reconnecting\.\.\.|connection (?:failed|reset|refused|closed|"
    r"error|lost)|error sending request|stream disconnected|network is unreachable|"
    r"failed to connect|could not connect|dns error|failed to lookup|getaddrinfo|"
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up", re.IGNORECASE)
PROVIDER_HOSTS = {"codex": "chatgpt.com", "claude": "api.anthropic.com"}
CONNECTION_WAIT_S = 300.0
CONNECTION_CHECK_S = 10.0
_CHECK_TIMEOUT_S = 8.0

# on_retry(retry_number, max_retries, error, wait_seconds), called before each pause.
OnRetry = Callable[[int, int, CliError, float], None]
# on_offline(host, waited_seconds), called while waiting for the connection to come back.
OnOffline = Callable[[str, float], None]
# The pause between tries (tests make it instant).
_sleep = asyncio.sleep
_clock = time.monotonic


def retryable(exc: CliError) -> bool:
    """Whether trying the same call again could work."""
    return not isinstance(exc, CliTimeout) and not _PERMANENT.search(str(exc))


def connection_error(exc: CliError) -> bool:
    """Whether the call failed because the provider's server couldn't be reached."""
    return retryable(exc) and bool(_CONNECTION.search(str(exc)))


async def _reachable(host: str) -> bool:
    """A full TLS handshake with host:443, the way the CLI connects (first address it gets)."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 443, ssl=ssl.create_default_context(),
                                    server_hostname=host), _CHECK_TIMEOUT_S)
    except (TimeoutError, OSError, ssl.SSLError):
        return False
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (TimeoutError, OSError, ssl.SSLError):
        pass
    return True


async def _wait_for_connection(host: str, budget_s: float,
                               on_offline: OnOffline | None) -> tuple[bool, float]:
    """Wait until `host` answers, for at most `budget_s`. Returns (back online, seconds waited)."""
    start = _clock()
    while True:
        if await _reachable(host):
            return True, _clock() - start
        waited = _clock() - start
        if waited >= budget_s:
            return False, waited
        log.warning("%s isn't answering; checking again in %.0f s (waited %.0f s of %.0f s)",
                    host, CONNECTION_CHECK_S, waited, budget_s)
        if on_offline:
            on_offline(host, waited)
        await _sleep(min(CONNECTION_CHECK_S, budget_s - waited))


async def run_cli(provider: str, prompt: str, effort: str | None = None, *,
                  resume: str | None = None, workdir: Path | None = None,
                  keep_session: bool = False, retries: int = MAX_RETRIES,
                  on_retry: OnRetry | None = None,
                  on_offline: OnOffline | None = None) -> CliResult:
    """Send the prompt to the chosen CLI and return its response.

    `effort` overrides the provider's default effort for this run (see CLI_STEP_EFFORTS).
    `resume` continues that session (Claude session id / Codex thread id) instead of
    starting a new conversation; `keep_session` saves the session so it can be resumed.
    `workdir` is the fixed folder Codex runs in (Claude always uses CLI_OUTPUT_DIR).
    A failure is retried up to `retries` times (at most MAX_RETRIES), 3–5 s apart, in the
    same conversation; result.retries says how many it took. After a lost connection the
    next try also waits until the provider's server answers again (at most CONNECTION_WAIT_S
    in all; `on_offline` is told while it waits).
    """
    retries = max(0, min(retries, MAX_RETRIES))
    offline_left = CONNECTION_WAIT_S
    offline_note = ""
    for attempt in range(retries + 1):
        try:
            result = await _run_once(provider, prompt, effort, resume=resume, workdir=workdir,
                                     keep_session=keep_session)
        except CliError as exc:
            if attempt >= retries or not retryable(exc):
                if attempt:
                    tries = f"{attempt} retr{'y' if attempt == 1 else 'ies'}"
                    raise type(exc)(f"{exc} (still failing after {tries}{offline_note})") from exc
                raise
            wait = RETRY_WAITS_S[min(attempt, len(RETRY_WAITS_S) - 1)]
            log.warning("%s failed (%s); retry %d of %d in %.0f s", provider,
                        str(exc)[:200], attempt + 1, retries, wait)
            if on_retry:
                on_retry(attempt + 1, retries, exc, wait)
            await _sleep(wait)
            host = PROVIDER_HOSTS.get(provider)
            if host and offline_left > 0 and connection_error(exc):
                online, waited = await _wait_for_connection(host, offline_left, on_offline)
                offline_left -= waited
                if not online:
                    offline_note = (f"; no connection to {host} for "
                                    f"{CONNECTION_WAIT_S / 60:.0f} min")
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
    "CONNECTION_WAIT_S",
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

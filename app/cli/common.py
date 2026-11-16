"""Shared pieces for the CLI clients: result type, errors, subprocess runner, run slots."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path


class CliError(RuntimeError):
    """The CLI failed or returned an error. The message is safe to show to users."""


class CliTimeout(CliError):
    pass


class CliBusy(CliError):
    pass


@dataclass
class CliResult:
    text: str
    provider: str
    model: str
    effort: str
    duration_s: float
    # Claude reports an API-price estimate; on a subscription login it isn't billed.
    cost_usd: float | None = None
    session_id: str | None = None
    num_turns: int | None = None
    usage: dict = field(default_factory=dict)
    models_used: list[str] = field(default_factory=list)


_slots: dict[str, tuple[asyncio.Semaphore, int]] = {}


def run_slot(provider: str, size: int, label: str) -> asyncio.Semaphore:
    """A per-provider limit on simultaneous runs; raises CliBusy when it's full."""
    size = max(1, size)
    sem, current = _slots.get(provider, (None, 0))
    if sem is None or current != size:
        sem = asyncio.Semaphore(size)
        _slots[provider] = (sem, size)
    if sem.locked():
        raise CliBusy(f"{size} {label} run(s) already in progress. Try again in a moment.")
    return sem


async def run_process(cmd: list[str], stdin: str, *, cwd: Path, timeout: int, label: str,
                      setting_prefix: str) -> tuple[bytes, bytes, int, float]:
    """Run cmd with stdin, return (stdout, stderr, returncode, seconds)."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise CliError(f"{label} CLI not found ({cmd[0]}). Set {setting_prefix}BIN in .env.") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CliTimeout(
            f"{label} didn't finish within {timeout} seconds ({setting_prefix}TIMEOUT_SECONDS)."
        ) from None
    return stdout, stderr, proc.returncode, time.monotonic() - started


def tail(data: bytes, limit: int = 1500) -> str:
    return data.decode("utf-8", errors="replace").strip()[-limit:]

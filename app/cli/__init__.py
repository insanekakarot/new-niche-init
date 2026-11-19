"""CLI integrations for generation.

config.py         settings for every CLI, all from .env
claude_client.py  Claude Code  (`claude -p`)
codex_client.py   OpenAI Codex (`codex exec`)
"""

from pathlib import Path

from app.cli import claude_client, codex_client
from app.cli.claude_client import run_claude
from app.cli.codex_client import run_codex
from app.cli.common import CliBusy, CliError, CliResult, CliTimeout
from app.cli.config import PROVIDERS, Provider, provider_info

def _unknown(provider: str) -> CliError:
    return CliError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.")


async def run_cli(provider: str, prompt: str, effort: str | None = None, *,
                  resume: str | None = None, workdir: Path | None = None,
                  keep_session: bool = False) -> CliResult:
    """Send the prompt to the chosen CLI and return its response.

    `effort` overrides the provider's default effort for this run (see CLI_STEP_EFFORTS).
    `resume` continues that session (Claude session id / Codex thread id) instead of
    starting a new conversation; `keep_session` saves the session so it can be resumed.
    `workdir` is the fixed folder Codex runs in (Claude always uses CLI_OUTPUT_DIR).
    """
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
    "build_payload",
    "PROVIDERS",
    "CliBusy",
    "CliError",
    "CliResult",
    "CliTimeout",
    "Provider",
    "provider_info",
    "run_claude",
    "run_cli",
    "run_codex",
]

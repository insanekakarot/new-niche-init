"""CLI integrations for generation.

config.py         settings for every CLI, all from .env
claude_client.py  Claude Code  (`claude -p`)
codex_client.py   OpenAI Codex (`codex exec`)
"""

from app.cli import claude_client, codex_client
from app.cli.claude_client import run_claude
from app.cli.codex_client import run_codex
from app.cli.common import CliBusy, CliError, CliResult, CliTimeout
from app.cli.config import PROVIDERS, Provider, provider_info

_RUNNERS = {"claude": run_claude, "codex": run_codex}


async def run_cli(provider: str, prompt: str, effort: str | None = None) -> CliResult:
    """Send the prompt to the chosen CLI and return its response.

    `effort` overrides the provider's default effort for this run (see CLI_STEP_EFFORTS).
    """
    try:
        runner = _RUNNERS[provider]
    except KeyError:
        raise CliError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.") from None
    return await runner(prompt, effort=effort)


def build_payload(provider: str, prompt: str, effort: str | None = None) -> dict:
    """The exact command line and stdin `run_cli(provider, prompt, effort)` would send."""
    builders = {"claude": claude_client.build_payload, "codex": codex_client.build_payload}
    try:
        return builders[provider](prompt, effort=effort)
    except KeyError:
        raise CliError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.") from None


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

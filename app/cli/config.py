"""CLI settings. Every value comes from the environment / .env.

  CLI_*         shared: default provider, output folder
  CLAUDE_CLI_*  Claude Code (`claude -p`)
  CODEX_CLI_*   OpenAI Codex (`codex exec`)
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import APP_DIR

Provider = Literal["claude", "codex"]
PROVIDERS: tuple[str, ...] = ("claude", "codex")

DEFAULT_SYSTEM_PROMPT = """\
You are a senior YouTube documentary writer working inside an automated content pipeline.
You receive a task prompt written by the channel's editors, plus the inputs for this run.

- Follow the task prompt exactly. Its instructions on structure, length, tone and format win.
- Deliver only the finished work, ready to save as a .txt file: no preamble ("Here is..."),
  no closing remarks, no questions back to the user. Headings and lists are fine when the
  task calls for them.
- Code blocks: if the task asks for its result inside a Markdown code block (```), follow its
  code-block rules exactly. The pipeline saves only what's inside that block and passes it to
  the next step. Otherwise don't wrap the answer in code fences.
- Placeholders in the task prompt such as [INSERT TITLE], [ATTACH SCRIPT] or [] refer to the
  inputs provided; use the matching input in their place. Text inside << >> refers to
  something already in this conversation (an earlier step's input or result): use it from
  there.
- Never ask for missing information. Make sensible choices and complete the task.
- Write everything in the requested output language.
- Accuracy matters: if web tools are available, use them to check names, dates, scores and
  statistics. Never invent quotes, numbers or events.
"""


EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
# The levels each CLI accepts (the page offers these when that CLI is chosen).
PROVIDER_EFFORTS: dict[str, tuple[str, ...]] = {"claude": CLAUDE_EFFORTS, "codex": EFFORT_LEVELS}
DEFAULT_STEP_EFFORTS = "research:high,outline:high,script:medium,audits:high,paraphrasing:high"


def parse_step_efforts(raw: str) -> dict[str, str]:
    """'outline:high, script:medium' -> {"outline": "high", "script": "medium"}"""
    efforts: dict[str, str] = {}
    for item in raw.replace(";", ",").split(","):
        if not item.strip():
            continue
        step, sep, level = item.partition(":")
        step, level = step.strip().lower(), level.strip().lower()
        if not sep or not step or level not in EFFORT_LEVELS:
            raise ValueError(
                f"CLI_STEP_EFFORTS: can't read {item.strip()!r}. Use step:level pairs, "
                f"level one of {', '.join(EFFORT_LEVELS)}."
            )
        efforts[step] = level
    return efforts


class CliSettings(BaseSettings):
    # Provider preselected in the "Generate with" dropdown.
    default_provider: Provider = "claude"
    # Where step results are saved: <output_dir>/<step>/<file>.txt
    output_dir: Path = APP_DIR / "output"
    # Effort per step type, for every brand (current and future), both providers.
    # Steps not listed use the provider's own default (CLAUDE_CLI_EFFORT /
    # CODEX_CLI_REASONING_EFFORT). Claude has no "ultra"; it gets "max" instead.
    step_efforts: str = DEFAULT_STEP_EFFORTS

    model_config = SettingsConfigDict(env_prefix="CLI_", env_file=".env", extra="ignore")

    @field_validator("step_efforts")
    @classmethod
    def _check_step_efforts(cls, value: str) -> str:
        parse_step_efforts(value)
        return value

    def effort_for(self, step: str) -> str | None:
        """The configured effort for this step, or None to use the provider default."""
        return parse_step_efforts(self.step_efforts).get(step.lower())


class ClaudeCliSettings(BaseSettings):
    # How to start the CLI. May include arguments, e.g. "python3 fake_claude.py".
    # From WSL, point it at the Windows install: /mnt/c/Users/<you>/.local/bin/claude.exe
    bin: str = "claude"
    # Model alias ("sonnet", "opus") or full name ("claude-sonnet-5").
    model: str = "claude-sonnet-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Comma-separated models to try if the main one is overloaded/unavailable. Empty = none.
    fallback_model: str = ""
    # Built-in tools Claude may use. "WebSearch,WebFetch" lets it check facts; "" = no tools.
    tools: str = "WebSearch,WebFetch"
    timeout_seconds: int = 1200
    # Hard spending cap per run in USD (0 = no cap). Only matters for API-key billing.
    max_budget_usd: float = 0
    # Runs allowed at the same time; extra requests are refused with 429.
    max_concurrent_runs: int = 2
    # Start the CLI without CLAUDE.md, skills, plugins, hooks or MCP servers.
    safe_mode: bool = True
    # Don't save one-off sessions. Scripting steps belong to a project and always keep their
    # session, so the next step can resume the same conversation.
    no_session_persistence: bool = True
    # Replaces Claude Code's coding-agent system prompt for these runs.
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Any extra CLI flags, space-separated (shell quoting allowed).
    extra_args: str = ""

    model_config = SettingsConfigDict(env_prefix="CLAUDE_CLI_", env_file=".env", extra="ignore")


class CodexCliSettings(BaseSettings):
    bin: str = "codex"
    model: str = "gpt-5.6-sol"
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "medium"
    # live = search the web while writing, cached = Codex's cached index, disabled = no web.
    web_search: Literal["live", "cached", "disabled"] = "live"
    # Codex runs in an empty temp folder with a read-only sandbox: it can't change files.
    sandbox: Literal["read-only", "workspace-write"] = "read-only"
    timeout_seconds: int = 1200
    max_concurrent_runs: int = 2
    # Skip ~/.codex/config.toml (login still comes from ~/.codex) and execpolicy rules.
    ignore_user_config: bool = True
    ignore_rules: bool = True
    # Don't save one-off Codex sessions to disk. Project steps always keep theirs so the next
    # step can resume the same conversation.
    ephemeral: bool = True
    # Run each request in its own process (`codex --no-daemon exec`) instead of through Codex's
    # shared background server, which runs for days; a fresh process connects anew each time.
    # Sessions still resume the same way.
    no_daemon: bool = True
    # Codex has no system-prompt flag, so this is placed at the top of every prompt.
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    extra_args: str = ""

    model_config = SettingsConfigDict(env_prefix="CODEX_CLI_", env_file=".env", extra="ignore")


@lru_cache
def get_cli_settings() -> CliSettings:
    return CliSettings()


@lru_cache
def get_claude_settings() -> ClaudeCliSettings:
    return ClaudeCliSettings()


@lru_cache
def get_codex_settings() -> CodexCliSettings:
    return CodexCliSettings()


def clear_settings_cache() -> None:
    for fn in (get_cli_settings, get_claude_settings, get_codex_settings):
        fn.cache_clear()


def provider_info() -> dict:
    """What the page shows in the "Generate with" dropdown (no secrets)."""
    claude, codex = get_claude_settings(), get_codex_settings()
    return {
        "default": get_cli_settings().default_provider,
        "step_efforts": parse_step_efforts(get_cli_settings().step_efforts),
        "providers": [
            {"key": "claude", "label": "Claude", "model": claude.model, "effort": claude.effort,
             "efforts": list(PROVIDER_EFFORTS["claude"])},
            {"key": "codex", "label": "Codex", "model": codex.model,
             "effort": codex.reasoning_effort, "efforts": list(PROVIDER_EFFORTS["codex"])},
        ],
    }

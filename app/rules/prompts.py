"""The Rule Prompts column: named prompts the Rules run (patterns in app/keys.py).

    VALIDATION = \"\"\" Check the script ... STATUS - APPROVED ... \"\"\"
    REVISION = \"\"\" Make the fixes ... return the script in a code block. \"\"\"
"""

from __future__ import annotations

from app import keys
from app.rules.lang import RuleSyntaxError


def parse_rule_prompts(text: str | None) -> tuple[dict[str, str], list[str]]:
    """({NAME: prompt}, warnings). Names are upper case."""
    text = (text or "").strip()
    prompts: dict[str, str] = {}
    for m in keys.RULE_PROMPT.finditer(text):
        name, body = m.group(1).upper(), m.group(2).strip()
        if name in keys.KEYWORDS:
            raise RuleSyntaxError(f"Rule prompt {name} has the name of a keyword; rename it.")
        if name in prompts:
            raise RuleSyntaxError(f"Rule prompt {name} is declared twice.")
        if not body:
            raise RuleSyntaxError(f"Rule prompt {name} is empty.")
        prompts[name] = body
    warnings = []
    rest = keys.RULE_PROMPT.sub("", text).strip()
    if rest:
        snippet = " ".join(rest.split())[:80]
        warnings.append(f'Text outside NAME = """...""" in Rule Prompts is ignored: “{snippet}”.')
    if text.count('"""') % 2:
        warnings.append('Rule Prompts has an unclosed """: the last prompt may be missing.')
    return prompts, warnings


def is_check(prompt: str) -> bool:
    """A check defines a status line; its answer sets TAG and keeps the script."""
    return bool(keys.CHECK_PROMPT.search(prompt))


def fill(prompt: str, values: dict[str, str]) -> str:
    """Replace {{ name }} with the run's values; unknown names stay as written."""
    def sub(m):
        name = m.group(1)
        # The name itself first, then its other names (source_script = competitor_script ...).
        value = next((values[n] for n in keys.same_input(name)
                      if isinstance(values.get(n), str) and values[n].strip()), None)
        return value.strip() if isinstance(value, str) and value.strip() else m.group(0)

    return keys.PLACEHOLDER.sub(sub, prompt)

"""How the fake CLIs answer rule prompts ('# Rule: NAME ...' from app/web/rule_jobs.py).

A check (the prompt defines 'STATUS - ...') answers NOT READY until it has been asked
FAKE_READY_AT times (default 2), then READY. A revision answers a new script in a code block.
Counts are kept in the file FAKE_RULES_COUNTER (one per test).
"""

import json
import os
import re


def rule_answer(prompt: str) -> str | None:
    m = re.search(r"^# Rule: (\w+)", prompt, re.MULTILINE)  # Codex has instructions first
    if not m:
        return None
    path = os.environ.get("FAKE_RULES_COUNTER")
    counts = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            counts = json.load(fh)
    if re.search(r"NEXT MOVE\s*-", prompt):  # entries validation: FAKE_NEXT_MOVE (CONTINUE)
        return (f"```\nRemaining Usable Entries: 6\nNEXT MOVE - "
                f"{os.environ.get('FAKE_NEXT_MOVE', 'CONTINUE')}\n```")
    check = re.search(r"STATUS\s*-\s*[A-Z]", prompt) is not None
    key = "checks" if check else "revisions"
    counts[key] = counts.get(key, 0) + 1
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(counts, fh)
    if check:
        ready = counts[key] >= int(os.environ.get("FAKE_READY_AT", "2"))
        status = "APPROVED" if ready else "NOT APPROVED"
        rating = "9.1" if ready else "7.4"
        return (f"```\nOverall rating: {rating}/10\nSTATUS - {status}\n```\n\n"
                f"Checked {m.group(1)}. Fixes: tighten the intro.")
    return f"```\nREVISED SCRIPT v{counts[key]} by {m.group(1)}\n```\n\nApplied the fixes."

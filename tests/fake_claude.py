"""Stand-in for `claude -p --output-format json` used by the tests.

Echoes its arguments and the prompt it got on stdin back as the result.
With `--resume <id>` it answers in that session; otherwise it starts a new random one.
FAKE_CLAUDE_MODE=error|garbage|slow|resume_error|codeblock changes its behaviour.
"""

import json
import os
import sys
import time
import uuid

from fake_rules import rule_answer

argv = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
mode = os.environ.get("FAKE_CLAUDE_MODE", "")
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None


def wrap(text):
    """codeblock: answer like a code-block prompt asks (block first, notes after)."""
    rule = rule_answer(prompt)
    if rule is not None:
        return rule
    if mode == "codeblock":
        return f"```\n{text}\n```\n\nResearch notes and alternatives (outside the block)."
    return text


if mode == "garbage":
    print("not json at all", file=sys.stderr)
    sys.exit(3)
if mode == "resume_error" and resume:
    print(f"No conversation found with session ID: {resume}", file=sys.stderr)
    sys.exit(1)
if mode == "slow":
    time.sleep(5)

payload = {
    "type": "result",
    "subtype": "success",
    "is_error": mode == "error",
    "result": "Model overloaded" if mode == "error" else wrap("RESULT\n" + json.dumps(
        {"argv": argv, "prompt": prompt}, ensure_ascii=False)),
    "total_cost_usd": 0.0123,
    "num_turns": 1,
    "session_id": resume or f"fake-session-{uuid.uuid4().hex[:8]}",
    "modelUsage": {"claude-sonnet-5": {}},
}
sys.stdout.write(json.dumps(payload, ensure_ascii=False))

"""Stand-in for `claude -p --output-format json` used by the tests.

Echoes its arguments and the prompt it got on stdin back as the result.
FAKE_CLAUDE_MODE=error|garbage|slow changes its behaviour.
"""

import json
import os
import sys
import time

prompt = sys.stdin.buffer.read().decode("utf-8")
mode = os.environ.get("FAKE_CLAUDE_MODE", "")

if mode == "garbage":
    print("not json at all", file=sys.stderr)
    sys.exit(3)
if mode == "slow":
    time.sleep(5)

payload = {
    "type": "result",
    "subtype": "success",
    "is_error": mode == "error",
    "result": "Model overloaded" if mode == "error" else "RESULT\n" + json.dumps(
        {"argv": sys.argv[1:], "prompt": prompt}, ensure_ascii=False),
    "total_cost_usd": 0.0123,
    "num_turns": 1,
    "session_id": "fake-session",
    "modelUsage": {"claude-sonnet-5": {}},
}
sys.stdout.write(json.dumps(payload, ensure_ascii=False))

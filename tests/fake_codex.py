"""Stand-in for `codex exec --json -o FILE -` used by the tests.

Writes 'RESULT\n{argv, prompt}' to the -o file and prints JSONL events.
FAKE_CODEX_MODE=error|nofile|slow changes its behaviour.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
mode = os.environ.get("FAKE_CODEX_MODE", "")


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


emit({"type": "thread.started", "thread_id": "fake-thread"})
emit({"type": "turn.started"})
if mode == "slow":
    time.sleep(5)
if mode == "error":
    emit({"type": "turn.failed", "error": {"message": "usage limit reached"}})
    sys.exit(1)

text = "RESULT\n" + json.dumps({"argv": args, "prompt": prompt, "cwd": os.getcwd()},
                               ensure_ascii=False)
emit({"type": "item.completed", "item": {"type": "web_search", "query": "x"}})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
if mode != "nofile":
    with open(args[args.index("-o") + 1], "w", encoding="utf-8") as fh:
        fh.write(text)
emit({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}})

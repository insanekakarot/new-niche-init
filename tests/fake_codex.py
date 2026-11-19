"""Stand-in for `codex exec --json -o FILE [resume ID] -` used by the tests.

Writes 'RESULT\n{argv, prompt, cwd}' to the -o file and prints JSONL events.
With `resume <id>` it continues that thread; otherwise it starts a new random one.
FAKE_CODEX_MODE=error|nofile|slow|codeblock changes its behaviour.
"""

import json
import os
import sys
import time
import uuid

from fake_rules import rule_answer

args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
mode = os.environ.get("FAKE_CODEX_MODE", "")
resume = args[args.index("resume") + 1] if "resume" in args else None


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


emit({"type": "thread.started", "thread_id": resume or f"fake-thread-{uuid.uuid4().hex[:8]}"})
emit({"type": "turn.started"})
if mode == "slow":
    time.sleep(5)
if mode == "error":
    emit({"type": "turn.failed", "error": {"message": "usage limit reached"}})
    sys.exit(1)

text = "RESULT\n" + json.dumps({"argv": args, "prompt": prompt, "cwd": os.getcwd()},
                               ensure_ascii=False)
if mode == "codeblock":  # answer like a code-block prompt asks
    text = f"```\n{text}\n```\n\nResearch notes and alternatives (outside the block)."
rule = rule_answer(prompt)  # a rule prompt ('# Rule: NAME'): answer it like a model would
if rule is not None:
    text = rule
emit({"type": "item.completed", "item": {"type": "web_search", "query": "x"}})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
if mode != "nofile":
    with open(args[args.index("-o") + 1], "w", encoding="utf-8") as fh:
        fh.write(text)
emit({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}})

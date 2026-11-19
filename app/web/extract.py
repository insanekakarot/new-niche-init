"""The part of a step's response the next step uses.

Prompts can ask for the usable result inside a Markdown code block (```), with research
notes, alternatives and commentary outside it. For those steps the code block is what gets
saved as the step's output; the whole response is kept next to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import keys

# An opening fence (``` or ~~~, 3 or more), and how a prompt asks for a block: app/keys.py.
_FENCE = keys.CODE_FENCE
_WANTS = keys.WANTS_CODE_BLOCK


def wants_code_block(prompt: str | None) -> bool:
    return bool(prompt and _WANTS.search(prompt))


@dataclass
class Extracted:
    text: str     # what to save as the step's output
    status: str   # "ok" | "unclosed" | "missing" | "empty"

    @property
    def found(self) -> bool:
        return self.status in ("ok", "unclosed")

    @property
    def warning(self) -> str | None:
        return {
            "unclosed": "The code block was never closed, so the response may be cut off. "
                        "Everything after the opening ``` was saved; check the end of it.",
            "missing": "The response has no code block, so the whole response was saved. "
                       "Check it before using it in the next step.",
            "empty": "The response's code block is empty, so the whole response was saved. "
                     "Check it before using it in the next step.",
        }.get(self.status)


def code_block(response: str) -> Extracted:
    """The first fenced code block of `response`, or the whole response if there's none."""
    lines = response.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for start, line in enumerate(lines):
        m = _FENCE.match(line)
        if not m:
            continue
        fence = m.group("fence")
        body: list[str] = []
        for inner in lines[start + 1:]:
            stripped = inner.strip()
            # Closes with the same character, at least as long, and nothing after it.
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                text = "\n".join(body).strip("\n")
                return Extracted(text, "ok") if text.strip() else Extracted(response, "empty")
            body.append(inner)
        text = "\n".join(body).strip("\n")
        return Extracted(text, "unclosed") if text.strip() else Extracted(response, "empty")
    return Extracted(response, "missing")

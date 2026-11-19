"""Run a hook's rules: send rule prompts into the conversation and follow LOOP / IF / BREAK.

The engine keeps the *current script*: it starts as the step's result. A check prompt (one
that defines a STATUS line) sets TAG and RATING and leaves the script alone; any other rule
prompt is a revision, and the code block of its answer becomes the current script. When the
rules end, the current script is the step's final result.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app import keys
from app.rules import lang
from app.rules.prompts import is_check
from app.web.extract import code_block

# call(name, entry) -> the model's answer. `entry` is the log line for this run; the caller
# may add to it (e.g. the file it saved).
Call = Callable[[str, dict], Awaitable[str]]


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Stop(Exception):
    pass


@dataclass
class State:
    script: str
    tag: str = ""
    rating: float = 0.0
    runs: int = 0
    iteration: int = 0
    changed: bool = False
    log: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def var(self, name: str):
        return {"TAG": self.tag, "RATING": self.rating, "WORDS": len(self.script.split()),
                "ITERATION": self.iteration, "RUNS": self.runs}[name]


def parse_tag(text: str) -> str:
    m = keys.STATUS_LINE.search(text)
    return " ".join(m.group("tag").upper().split()) if m else ""


def passed(tag: str) -> bool:
    """Whether a check's tag is a pass (READY FOR PRODUCTION, TRUE, YES, ...)."""
    return bool(tag) and bool(keys.PASS_TAG.match(tag.strip()))


def parse_rating(text: str) -> float | None:
    m = keys.RATING.search(text)
    if not m:
        return None
    return float(m.group("n") or m.group("n2"))


# ---------------------------------------------------------------- conditions

def _norm(v):
    return " ".join(v.upper().split()) if isinstance(v, str) else v


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def evaluate(cond, state: State) -> bool:
    if isinstance(cond, lang.BoolOp):
        results = (evaluate(c, state) for c in cond.items)
        return all(results) if cond.op == "AND" else any(results)
    if isinstance(cond, lang.Not):
        return not evaluate(cond.item, state)
    left = _value(cond.left, state)
    if cond.op is None:
        return bool(left)
    right = _value(cond.right, state)
    if cond.op == "CONTAINS":
        return _norm(str(right)) in _norm(str(left))
    a, b = _num(left), _num(right)
    if cond.op in ("==", "!="):
        same = (a == b) if a is not None and b is not None else _norm(str(left)) == _norm(str(right))
        return same if cond.op == "==" else not same
    if a is None or b is None:
        return False  # < > on text: never true
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[cond.op]


def _value(node, state: State):
    return state.var(node.name) if isinstance(node, lang.Var) else node.value


# ---------------------------------------------------------------- running

@dataclass
class Result:
    script: str
    changed: bool
    tag: str
    rating: float
    log: list[dict]
    warnings: list[str]

    @property
    def ready(self) -> bool:
        return passed(self.tag)


async def run_hook(stmts: list, *, script: str, prompts: dict[str, str], call: Call,
                   state: State | None = None) -> Result:
    """Run the statements of one '> step' hook, starting from that step's result.

    Pass `state` to watch progress: its log fills in as each rule prompt runs.
    """
    state = state or State(script=script)
    try:
        await _block(stmts, state, prompts, call)
    except (_Break, _Continue, _Stop):
        pass  # BREAK / CONTINUE outside a loop, or STOP: the rules end here
    return Result(state.script, state.changed, state.tag, state.rating, state.log,
                  state.warnings)


async def _block(stmts, state, prompts, call):
    for s in stmts:
        if isinstance(s, lang.Run):
            await _run(s.name, state, prompts, call)
        elif isinstance(s, lang.If):
            await _block(s.then if evaluate(s.cond, state) else s.else_, state, prompts, call)
        elif isinstance(s, lang.Loop):
            await _loop(s, state, prompts, call)
        elif isinstance(s, lang.Break):
            raise _Break
        elif isinstance(s, lang.Continue):
            raise _Continue
        elif isinstance(s, lang.Stop):
            raise _Stop


async def _loop(loop: lang.Loop, state, prompts, call):
    limit = min(loop.max or keys.DEFAULT_MAX_LOOP, keys.HARD_MAX_LOOP)
    outer = state.iteration
    passes = 0
    try:
        while True:
            state.iteration = passes
            if loop.while_ is not None and not evaluate(loop.while_, state):
                break
            if passes >= limit:
                state.warnings.append(
                    f"Stopped after {limit} pass{'es' if limit != 1 else ''} of the loop"
                    + (f" (TAG is still “{state.tag}”)" if state.tag else "")
                    + ". The latest script is the result; run the rules again to keep going.")
                break
            passes += 1
            state.iteration = passes
            try:
                await _block(loop.body, state, prompts, call)
            except _Break:
                break
            except _Continue:
                pass
            if loop.until is not None and evaluate(loop.until, state):
                break
    finally:
        state.iteration = outer


async def _run(name: str, state: State, prompts: dict[str, str], call: Call):
    if name not in prompts:
        raise lang.RuleSyntaxError(f"Rule prompt {name} isn't declared in Rule Prompts.")
    if state.runs >= keys.MAX_RUNS:
        state.warnings.append(f"Stopped: the rules ran {keys.MAX_RUNS} prompts, the most "
                              "allowed for one step.")
        raise _Stop
    check = is_check(prompts[name])
    state.runs += 1
    entry = {"n": state.runs, "name": name, "kind": "check" if check else "revision",
             "iteration": state.iteration, "status": "running", "started_at": time.time()}
    state.log.append(entry)
    try:
        answer = await call(name, entry)
    except Exception:
        entry.update(status="failed", duration_s=round(time.time() - entry["started_at"], 1))
        raise
    block = code_block(answer)
    if check:
        source = block.text if block.found else answer
        state.tag = parse_tag(source) or parse_tag(answer)
        rating = parse_rating(source)
        rating = rating if rating is not None else parse_rating(answer)
        state.rating = rating if rating is not None else 0.0
        entry.update(tag=state.tag, rating=rating, passed=passed(state.tag))
        if not state.tag:
            entry["warning"] = "No STATUS line found in the answer, so TAG is empty."
    else:
        if block.found:
            state.script = block.text
        else:
            state.script = answer
            entry["warning"] = "No code block in the answer: the whole answer is the new script."
        state.changed = True
    entry.update(status="done", words=len(state.script.split()),
                 duration_s=round(time.time() - entry["started_at"], 1))

"""Show a brand's Rules the way people read them: one statement per line, with what each does.

    lines(program, prompts) -> [{"indent": 0, "code": "> audits", "note": "after the ..."}, ...]
    example_run(...)        -> a sample run: which prompt runs when, and the final script

Used by the admin dashboard. Nothing here calls a CLI.
"""

from __future__ import annotations

import asyncio
import re

from app import keys
from app.rules import lang
from app.rules.engine import run_hook
from app.rules.prompts import is_check

# ---------------------------------------------------------------- conditions as text

def _lit(v) -> str:
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return f'"{v}"'


def _operand(node) -> str:
    return node.name if isinstance(node, lang.Var) else _lit(node.value)


def cond_code(c, nested: bool = False) -> str:
    """The condition as it is written: TAG == "READY FOR PRODUCTION"."""
    if isinstance(c, lang.BoolOp):
        text = f" {c.op} ".join(cond_code(i, nested=True) for i in c.items)
        return f"({text})" if nested else text
    if isinstance(c, lang.Not):
        return f"NOT {cond_code(c.item, nested=True)}"
    if c.op is None:
        return _operand(c.left)
    return f"{_operand(c.left)} {c.op} {_operand(c.right)}"


_SAYS = {"==": "is", "!=": "is not", ">": "is more than", ">=": "is at least",
         "<": "is less than", "<=": "is at most", "CONTAINS": "contains"}


def cond_words(c) -> str:
    """The condition in words: TAG is "READY FOR PRODUCTION"."""
    if isinstance(c, lang.BoolOp):
        return f" {c.op.lower()} ".join(cond_words(i) for i in c.items)
    if isinstance(c, lang.Not):
        return f"not ({cond_words(c.item)})"
    if c.op is None:
        return f"{_operand(c.left)} is set"
    return f"{_operand(c.left)} {_SAYS[c.op]} {_operand(c.right)}"


# ---------------------------------------------------------------- statements as lines

def lines(program: lang.Program, prompts: dict[str, str]) -> list[dict]:
    """Every hook of the program, one statement per line, with a note on what it does."""
    out: list[dict] = []

    def add(indent, code, note=""):
        out.append({"indent": indent, "code": code, "note": note})

    def walk(stmts, indent, in_loop):
        for s in stmts:
            if isinstance(s, lang.Run):
                if s.name not in prompts:
                    note = "⚠ not declared in Rule Prompts"
                elif is_check(prompts[s.name]):
                    note = ("check: reads its status line (STATUS - … / Title Satisfied: …) into "
                            "TAG and the rating into RATING; the current script stays as it is")
                else:
                    note = "revision: its code block becomes the current script"
                add(indent, f"{s.name};", note)
            elif isinstance(s, lang.Loop):
                limit = s.max or keys.DEFAULT_MAX_LOOP
                passes = f"at most {limit} pass{'es' if limit != 1 else ''}"
                if s.while_ is not None:
                    add(indent, f"WHILE ({cond_code(s.while_)}) {{",
                        f"repeat while {cond_words(s.while_)} (checked before each pass, "
                        f"{passes})")
                else:
                    head = f"LOOP (MAX {s.max}) {{" if s.max else "LOOP {"
                    add(indent, head, f"repeat: pass 1, 2, … ({passes}"
                        + ("" if s.max else ", no MAX given") + ")")
                walk(s.body, indent + 1, True)
                if s.until is not None:
                    add(indent, f"}} UNTIL ({cond_code(s.until)});",
                        f"checked after each pass: stop when {cond_words(s.until)}")
                else:
                    add(indent, "}", "end of the loop")
            elif isinstance(s, lang.If):
                _if(s, indent, in_loop, first=True)
            elif isinstance(s, lang.Break):
                add(indent, "BREAK;", "stop the loop: the current script is the final result"
                    if in_loop else "end the rules: the current script is the final result")
            elif isinstance(s, lang.Continue):
                add(indent, f"{s.word};", "go back to the check: end this pass, and the next "
                    "pass starts with the check" if in_loop
                    else "ends the rules (it's outside a loop)")
            elif isinstance(s, lang.Stop):
                add(indent, "STOP;", "end the rules: the current script is the final result")

    def _if(s, indent, in_loop, first):
        code = f"IF ({cond_code(s.cond)}) {{" if first else f"}} ELSE IF ({cond_code(s.cond)}) {{"
        add(indent, code, f"when {cond_words(s.cond)}" if first
            else f"otherwise, when {cond_words(s.cond)}")
        walk(s.then, indent + 1, in_loop)
        if len(s.else_) == 1 and isinstance(s.else_[0], lang.If):
            _if(s.else_[0], indent, in_loop, first=False)
            return
        if s.else_:
            add(indent, "} ELSE {", "otherwise")
            walk(s.else_, indent + 1, in_loop)
        add(indent, "}")

    for steps, stmts in grouped(program.before):
        add(0, f"{keys.HOOK_BEFORE} {' OR '.join(steps)}", f"before the {_names(steps)} step, "
            "in the same project conversation (each result can fill {{ name }} in its prompt)")
        walk(stmts, 0, False)
    for steps, stmts in grouped(program.hooks):
        add(0, f"{keys.HOOK} {' OR '.join(steps)}", f"after the {_names(steps)} step, in the "
            "same project conversation")
        walk(stmts, 0, False)
    return out


def grouped(hooks: dict[str, list]) -> list[tuple[list[str], list]]:
    """Hooks as written: '> paraphrasing OR audits' is one hook for two steps."""
    out: list[tuple[list[str], list]] = []
    for step, stmts in hooks.items():
        same = next((g for g in out if g[1] is stmts), None)
        if same:
            same[0].append(step)
        else:
            out.append(([step], stmts))
    return out


def _names(steps: list[str]) -> str:
    return " or ".join(s.capitalize() for s in steps)


_SEGMENT = re.compile(r'(?P<str>"[^"]*")|(?P<word>[A-Za-z_]\w*)|(?P<num>\d+(?:\.\d+)?)|(?P<other>.)',
                      re.DOTALL)


def segments(code: str, prompt_names) -> list[tuple[str, str]]:
    """Split a line for colouring: [(kind, text)], kind = kw | name | var | str | num | ''."""
    out: list[tuple[str, str]] = []
    for m in _SEGMENT.finditer(code):
        text, kind = m.group(), ""
        if m.group("str"):
            kind = "str"
        elif m.group("num"):
            kind = "num"
        elif m.group("word"):
            w = text.upper()
            kind = ("kw" if w in keys.KEYWORDS else "var" if w in keys.RULE_VARIABLES
                    else "name" if w in prompt_names else "")
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))
    return out


def brand_rules(brand: dict, steps, values: dict[str, str] | None = None) -> dict | None:
    """Everything the dashboard shows about a brand's Rules and Rule Prompts (None if none)."""
    raw, prompts = brand.get("rules"), brand.get("rule_prompts") or {}
    if not raw and not prompts:
        return None
    values = values or {}
    try:
        program, error = lang.parse(raw, steps), None
    except lang.RuleSyntaxError as exc:
        program, error = lang.Program(None, {}), str(exc)
    names = set(prompts) | program.prompt_names()
    rows = [{**line, "segments": segments(line["code"], names)}
            for line in lines(program, prompts)]
    used = program.prompt_names()
    rule_prompts = []
    for name, text in prompts.items():
        found = list(dict.fromkeys(keys.PLACEHOLDER.findall(text)))
        rule_prompts.append({
            "name": name, "text": text, "words": len(text.split()), "used": name in used,
            "kind": "check" if is_check(text) else "revision",
            "placeholders": [{"tag": "{{ " + p + " }}", "value": values.get(p),
                              "about": keys.BUILTIN_PLACEHOLDERS.get(p)} for p in found],
        })
    runs = []
    for when, hooks in (("before", program.before), ("after", program.hooks)):
        for hook_steps, stmts in grouped(hooks):
            step = hook_steps[0]
            if all(n in prompts for n in lang.Program(None, {step: stmts}).prompt_names()):
                # Before a step, show the main path: each check passes the first time.
                runs.append({"step": step, "steps": hook_steps, "when": when,
                             **example_run(stmts, prompts, step, when=when,
                                           ready_at=1 if when == "before" else 2)})
    return {"guidance": program.guidance, "lines": rows, "error": error,
            "after": list(program.hooks), "before": list(program.before),
            "prompts": rule_prompts, "runs": runs}


# ---------------------------------------------------------------- a sample run

_OPPOSITE = {"TRUE": "FALSE", "YES": "NO", "PASS": "FAIL", "PASSED": "FAILED"}


def tag_literals(stmts) -> list[str]:
    """The texts the rules compare TAG with (TAG == "..." / TAG CONTAINS "..."), in order."""
    found: list[str] = []

    def cond(c):
        if isinstance(c, lang.BoolOp):
            for i in c.items:
                cond(i)
        elif isinstance(c, lang.Not):
            cond(c.item)
        elif c is not None and c.op in ("==", "!=", "CONTAINS"):
            for var, lit in ((c.left, c.right), (c.right, c.left)):
                if (isinstance(var, lang.Var) and var.name == "TAG" and isinstance(lit, lang.Lit)
                        and isinstance(lit.value, str)):
                    found.append(" ".join(lit.value.upper().split()))

    def walk(items):
        for s in items:
            if isinstance(s, lang.If):
                cond(s.cond)
                walk(s.then)
                walk(s.else_)
            elif isinstance(s, lang.Loop):
                cond(s.while_)
                walk(s.body)
                cond(s.until)

    walk(stmts)
    return list(dict.fromkeys(found))


def pass_fail_tags(prompt: str, literals: list[str]) -> tuple[str, str]:
    """The pass and fail tags a check prompt asks for, e.g. ("TRUE", "FALSE").

    The pass tag is the first TAG text of the rules that the prompt writes as 'STATUS - X'
    (or, failing that, mentions at all); "READY FOR PRODUCTION" if there's none.
    """
    text = " ".join(prompt.upper().split())
    passing = [t for t in literals if not t.startswith("NOT ") and t not in _OPPOSITE.values()]
    good = (next((t for t in passing if re.search(rf"{keys.STATUS_LABEL}{re.escape(t)}\b", text)),
                 None)
            or next((t for t in passing if re.search(rf"\b{re.escape(t)}\b", text)), None)
            or "READY FOR PRODUCTION")
    return good, _OPPOSITE.get(good, f"NOT {good}")


def example_run(stmts: list, prompts: dict[str, str], step: str, ready_at: int = 2,
                when: str = "after") -> dict:
    """Run the rules with made-up answers: each check fails until its `ready_at`-th run.

    Returns {"assumption", "rows": [{"what", "result"}], "final"} with scripts named A, B, C...
    `when="before"`: the rules before the step ('< step'); the step runs after them.
    """
    if when == "before":
        return _example_before(stmts, prompts, step, ready_at)
    counts = {"revisions": 0}
    checks: dict[str, int] = {}
    literals = tag_literals(stmts)
    current = ["script A"]

    def letter(n: int) -> str:
        return chr(ord("A") + n) if n < 26 else f"#{n + 1}"

    async def call(name, entry):
        if is_check(prompts[name]):
            checks[name] = checks.get(name, 0) + 1
            ok = checks[name] >= ready_at
            good, bad = pass_fail_tags(prompts[name], literals)
            entry["note"] = f"{current[0]} unchanged"
            return f"```\nOverall rating: {9 if ok else 7.5}/10\nSTATUS - {good if ok else bad}\n```"
        counts["revisions"] += 1
        current[0] = f"script {letter(counts['revisions'])}"
        entry["note"] = current[0]
        return f"```\n{current[0]}\n```"

    result = asyncio.run(run_hook(stmts, script="script A", prompts=prompts, call=call))
    seen: dict[str, int] = {}
    rows = [{"what": step.capitalize(), "result": "→ script A"}]
    for e in result.log:
        seen[e["name"]] = seen.get(e["name"], 0) + 1
        what = f"{e['name']} #{seen[e['name']]}"
        if e["kind"] == "check":
            rows.append({"what": what, "result": f"→ {e['tag']}  ({e['note']})",
                         "ready": e["passed"]})
        else:
            rows.append({"what": what, "result": f"→ {e['note']}"})
    nth = {1: "first", 2: "second", 3: "third"}.get(ready_at, f"{ready_at}th")
    names = [n for n in dict.fromkeys(e["name"] for e in result.log) if is_check(prompts[n])]
    tags = [pass_fail_tags(prompts[n], literals) for n in names]
    if len(names) > 1:
        assumption = (f"Sample run, if each check passes on its {nth} run and fails before "
                      "that (" + ", ".join(f"{n}: {g} / {b}" for n, (g, b) in zip(names, tags))
                      + "):")
    else:
        good, bad = tags[0] if tags else ("READY FOR PRODUCTION", "NOT READY FOR PRODUCTION")
        assumption = f"Sample run, if the {nth} check says {good} and any earlier one says {bad}:"
    return {
        "assumption": assumption,
        "rows": rows,
        "warnings": result.warnings,
        "final": f"Final {step.capitalize()} result = {result.script}",
    }


def _example_before(stmts: list, prompts: dict[str, str], step: str, ready_at: int) -> dict:
    """The sample run of '< step': the rule prompts, then the step itself."""
    literals = tag_literals(stmts)
    checks: dict[str, int] = {}
    ran: list[str] = []

    async def call(name, entry):
        if name.lower() not in ran:
            ran.append(name.lower())
        if is_check(prompts[name]):
            checks[name] = checks.get(name, 0) + 1
            good, bad = pass_fail_tags(prompts[name], literals)
            return f"```\nSTATUS - {good if checks[name] >= ready_at else bad}\n```"
        return f"```\n{name.lower()} result\n```"

    result = asyncio.run(run_hook(stmts, script="", prompts=prompts, call=call))
    seen: dict[str, int] = {}
    rows = []
    for e in result.log:
        seen[e["name"]] = seen.get(e["name"], 0) + 1
        what = f"{e['name']} #{seen[e['name']]}"
        if e["kind"] == "check":
            rows.append({"what": what, "result": f"→ {e['tag']}", "ready": e["passed"]})
        else:
            rows.append({"what": what,
                         "result": f"→ its result fills {{{{ {e['name'].lower()} }}}}"})
    if result.cancelled:
        rows.append({"what": step.capitalize(), "result": "→ doesn't run (CANCEL)", "ready": False})
    else:
        rows.append({"what": step.capitalize(),
                     "result": "→ the step runs, in the same conversation"})
    fills = ", ".join(f"{{{{ {n} }}}}" for n in ran)
    return {
        "assumption": f"Sample run before {step.capitalize()} (a new project: the first rule "
                      "prompt starts the conversation):",
        "rows": rows,
        "warnings": result.warnings,
        "final": (f"{step.capitalize()} doesn't run: the rules said CANCEL" if result.cancelled
                  else f"Then {step.capitalize()} runs"
                  + (f", with {fills} available" if fills else "")),
    }

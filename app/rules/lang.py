"""Parse the Rules column into a program (the symbols are listed in app/keys.py).

    'Be concise. > audits LOOP { CHECK; IF (TAG == "READY") { BREAK; } FIX; } UNTIL (TAG == "READY");'
      -> Program(guidance="Be concise.", hooks={"audits": [Loop(...)]})

    '< outline RESEARCH;'  (before a step)  -> Program(..., before={"outline": [Run("RESEARCH")]})
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import keys


class RuleSyntaxError(ValueError):
    """The Rules text can't be read. The message is shown to the admin."""


# ---------------------------------------------------------------- program

@dataclass
class Run:
    name: str  # a rule prompt, upper case


@dataclass
class Loop:
    body: list
    until: object | None = None    # checked after each pass
    while_: object | None = None   # checked before each pass
    max: int | None = None


@dataclass
class If:
    cond: object
    then: list
    else_: list = field(default_factory=list)


@dataclass
class Break:
    pass


@dataclass
class Continue:
    word: str = "CONTINUE"  # as written: CONTINUE or REVALIDATE


@dataclass
class Stop:
    cancel: bool = False  # CANCEL: before a step, the step doesn't run either


@dataclass
class Var:
    name: str


@dataclass
class Lit:
    value: str | float


@dataclass
class Cmp:
    left: object
    op: str | None = None  # None: the operand on its own (true when not empty / not 0)
    right: object | None = None


@dataclass
class BoolOp:
    op: str  # AND | OR
    items: list


@dataclass
class Not:
    item: object


@dataclass
class Program:
    guidance: str | None
    hooks: dict[str, list]                                   # '> step': runs after the step
    before: dict[str, list] = field(default_factory=dict)    # '< step': runs before the step

    def prompt_names(self) -> set[str]:
        names: set[str] = set()

        def walk(stmts):
            for s in stmts:
                if isinstance(s, Run):
                    names.add(s.name)
                elif isinstance(s, Loop):
                    walk(s.body)
                elif isinstance(s, If):
                    walk(s.then)
                    walk(s.else_)

        for stmts in [*self.hooks.values(), *self.before.values()]:
            walk(stmts)
        return names


# ---------------------------------------------------------------- tokens

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>"[^"]*"|'[^']*')
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_]\w*)
  | (?P<op>==|!=|>=|<=|&&|\|\||[<>=!(){};])
""", re.VERBOSE)


@dataclass
class Tok:
    kind: str   # string | number | ident | op | end
    value: str
    pos: int


def _tokens(text: str, offset: int = 0) -> list[Tok]:
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise RuleSyntaxError(f"Unexpected {text[i]!r} at {_where(text, i)}.")
        kind = m.lastgroup
        if kind != "ws":
            value = m.group()
            if kind == "string":
                value = value[1:-1]
            out.append(Tok(kind, value, offset + i))
        i = m.end()
    out.append(Tok("end", "", offset + len(text)))
    return out


def _where(text: str, i: int) -> str:
    return f"…{text[max(0, i - 25):i + 15]}…"


# ---------------------------------------------------------------- parser

# '> audits' / '< outline' at the start or after a space, ';' or '}' (so 'score>8' in
# guidance isn't one). Only a step name after it counts.
_HOOKS = (keys.HOOK, keys.HOOK_BEFORE)
_HOOK_AT = re.compile(r"(?:^|(?<=[\s;}]))[" + re.escape("".join(_HOOKS)) + r"]\s*([A-Za-z_]\w*)")


def split_guidance(text: str, steps) -> tuple[str | None, str]:
    """(plain guidance before the first '> step' / '< step', the rules program from there)."""
    for m in _HOOK_AT.finditer(text):
        if m.group(1).lower() in steps:
            return (text[:m.start()].strip() or None), text[m.start():]
    return (text.strip() or None), ""


def parse(text: str | None, steps) -> Program:
    """Parse a Rules cell. `steps`: the step names a '> step' hook may name."""
    text = (text or "").strip()
    guidance, code = split_guidance(text, set(steps))
    if not code:
        return Program(guidance, {})
    after, before = _Parser(code, set(steps)).program()
    return Program(guidance, after, before)


class _Parser:
    def __init__(self, code: str, steps: set[str]):
        self.code, self.steps = code, steps
        self.toks = _tokens(code)
        self.i = 0

    # -- helpers
    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def word(self, *words: str) -> bool:
        return self.tok.kind == "ident" and self.tok.value.upper() in words

    def op(self, *ops: str) -> bool:
        return self.tok.kind == "op" and self.tok.value in ops

    def take(self) -> Tok:
        t = self.tok
        self.i += 1
        return t

    def expect_op(self, op: str, what: str) -> None:
        if not self.op(op):
            self.fail(f"Expected '{op}' {what}")
        self.take()

    def fail(self, message: str):
        t = self.tok
        found = "the end" if t.kind == "end" else repr(t.value)
        raise RuleSyntaxError(f"{message}, found {found} at {_where(self.code, t.pos)}.")

    def semicolon(self) -> None:
        if self.op(";"):
            self.take()

    # -- program
    def program(self) -> tuple[dict[str, list], dict[str, list]]:
        """({step: statements after it}, {step: statements before it})."""
        after: dict[str, list] = {}
        before: dict[str, list] = {}
        while self.tok.kind != "end":
            if not self.op(*_HOOKS):
                self.fail("Expected '> step' (after a step) or '< step' (before a step) to "
                          "start the rules")
            sym = self.take().value
            hooks = after if sym == keys.HOOK else before
            names = [self.hook_step(sym, hooks)]
            # '> paraphrasing OR audits': the same rules for each of these steps.
            while self.word("OR") or self.op(","):
                self.take()
                names.append(self.hook_step(sym, hooks, names))
            stmts = self.statements(top=True)
            if not stmts:
                self.fail(f"'{sym} {' OR '.join(names)}' has no rules after it")
            for step in names:
                hooks[step] = stmts  # one list: the hook is shown once, as written
        return after, before

    def hook_step(self, sym: str, hooks: dict, taken=()) -> str:
        if self.tok.kind != "ident" or self.tok.value.lower() not in self.steps:
            self.fail(f"Expected a step name after '{sym}' ({', '.join(sorted(self.steps))})")
        step = self.take().value.lower()
        if step in hooks or step in taken:
            self.fail(f"'{sym} {step}' appears twice; put its rules together")
        return step

    def statements(self, top: bool = False) -> list:
        out = []
        while True:
            if self.tok.kind == "end" or self.op("}"):
                break
            if self.op(*_HOOKS):
                if top:
                    break
                self.fail(f"A '}}' is missing before the next '{self.tok.value}'")
            if self.op(";"):
                self.take()
                continue
            out.append(self.statement())
        return out

    def block(self, what: str) -> list:
        self.expect_op("{", f"to open the {what} block")
        body = self.statements()
        self.expect_op("}", f"to close the {what} block")
        return body

    def statement(self):
        t = self.tok
        if t.kind != "ident":
            self.fail("Expected a rule (a prompt name, LOOP, IF, BREAK, ...)")
        w = t.value.upper()
        if w == "LOOP":
            return self.loop()
        if w == "WHILE":
            self.take()
            cond = self.paren_cond("WHILE")
            loop = Loop(self.block("WHILE"), while_=cond)
            self.semicolon()
            return loop
        if w == "IF":
            return self.if_()
        if w in ("BREAK", "STOP") or w in keys.CONTINUE_ALIASES:
            self.take()
            self.semicolon()
            return Break() if w == "BREAK" else Stop() if w == "STOP" else Continue(w)
        if w == "RUN":
            self.take()
            if self.tok.kind != "ident":
                self.fail("Expected a rule prompt name after RUN")
            w = self.tok.value.upper()
        elif w in keys.KEYWORDS:
            self.fail(f"{w} can't start a rule here")
        self.take()
        if not self.op(";", "}") and self.tok.kind != "end" and not self.op(*_HOOKS):
            self.fail(f"Expected ';' after {w}")
        self.semicolon()
        return Run(w)

    def loop(self) -> Loop:
        self.take()
        limit = None
        if self.op("("):
            self.take()
            if not self.word("MAX"):
                self.fail("Expected MAX in LOOP (MAX n)")
            self.take()
            if self.tok.kind != "number" or "." in self.tok.value:
                self.fail("Expected a whole number after MAX")
            limit = int(self.take().value)
            if not 1 <= limit <= keys.HARD_MAX_LOOP:
                raise RuleSyntaxError(f"LOOP (MAX {limit}): use 1 to {keys.HARD_MAX_LOOP}.")
            self.expect_op(")", "after LOOP (MAX n")
        body = self.block("LOOP")
        until = None
        if self.word("UNTIL"):
            self.take()
            until = self.paren_cond("UNTIL")
        self.semicolon()
        return Loop(body, until=until, max=limit)

    def if_(self) -> If:
        self.take()
        cond = self.paren_cond("IF")
        then = self.block("IF")
        else_: list = []
        if self.word("ELSE"):
            self.take()
            else_ = [self.if_()] if self.word("IF") else self.block("ELSE")
        self.semicolon()
        return If(cond, then, else_)

    # -- conditions
    def paren_cond(self, what: str):
        self.expect_op("(", f"after {what}")
        cond = self.or_()
        self.expect_op(")", f"to close the {what} condition")
        return cond

    def or_(self):
        items = [self.and_()]
        while self.word("OR") or self.op("||"):
            self.take()
            items.append(self.and_())
        return items[0] if len(items) == 1 else BoolOp("OR", items)

    def and_(self):
        items = [self.not_()]
        while self.word("AND") or self.op("&&"):
            self.take()
            items.append(self.not_())
        return items[0] if len(items) == 1 else BoolOp("AND", items)

    def not_(self):
        if self.word("NOT") or self.op("!"):
            self.take()
            return Not(self.not_())
        if self.op("("):
            self.take()
            inner = self.or_()
            self.expect_op(")", "to close the brackets")
            return inner
        return self.comparison()

    def comparison(self) -> Cmp:
        left = self.operand()
        if self.word("CONTAINS"):
            self.take()
            return Cmp(left, "CONTAINS", self.operand())
        if self.op(*keys.COMPARISONS, "="):
            op = self.take().value
            return Cmp(left, "==" if op == "=" else op, self.operand())
        return Cmp(left)

    def operand(self):
        t = self.tok
        if t.kind == "string":
            self.take()
            return Lit(t.value)
        if t.kind == "number":
            self.take()
            return Lit(float(t.value))
        if t.kind == "ident" and t.value.upper() in keys.RULE_VARIABLES:
            self.take()
            return Var(t.value.upper())
        self.fail(f"Expected a value: \"text\", a number, or {', '.join(keys.RULE_VARIABLES)}")

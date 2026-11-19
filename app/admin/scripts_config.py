"""Turn the "Scripting Prompts Config" TSV into the admin JSON config.

TSV columns (one row per brand):
    Research | Outline | Script | Audits | Paraphrasing | Brand | Steps | Rules |
    Rule Prompts (optional) | Inputs

Output (__scripts.prompt.json) is a list with one object per brand:
    [{"nba_brand_1": {"brand_name": ..., "prompts": {...}, "steps": [...],
                      "rules": ..., "rule_prompts": {NAME: prompt}, "inputs": {...}}}, ...]

`rules` is kept as written; it is checked here (app/rules/lang.py) so a mistake shows up on
upload, not halfway through a run. Every symbol is listed in app/keys.py.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app import keys
from app.config import settings
from app.rules import RuleSyntaxError, parse_rule_prompts
from app.rules import parse as parse_rules
from app.rules.explain import tag_literals
from app.rules.prompts import is_check

TSV_FILENAME = "__script_prompts.tsv"
CONFIG_FILENAME = "__scripts.prompt.json"

# step key -> TSV column holding that step's prompt
STEP_COLUMNS: dict[str, str] = {
    "research": "Research",
    "outline": "Outline",
    "script": "Script",
    "audits": "Audits",
    "paraphrasing": "Paraphrasing",
}
REQUIRED_COLUMNS = (*STEP_COLUMNS.values(), "Brand", "Steps", "Rules", "Inputs")
RULE_PROMPTS_COLUMN = "Rule Prompts"  # optional: older sheets don't have it

# Symbols of the Steps and Inputs columns (all in app/keys.py).
_CONDITIONAL = keys.STEP_CONDITION
# "outline = a + b script = c + d"  ->  ("outline", "a + b"), ("script", "c + d")
_INPUT_ASSIGN = keys.INPUT_ASSIGN


class TsvError(ValueError):
    """The TSV can't be turned into a config. The message is shown to the admin."""


@dataclass
class ParseResult:
    config: list[dict]
    warnings: list[str] = field(default_factory=list)

    @property
    def brands(self) -> list[str]:
        return [next(iter(entry)) for entry in self.config]


# ---------------------------------------------------------------- paths

def tsv_path() -> Path:
    return settings.admin_dir / "inputs" / "tsv" / TSV_FILENAME


def config_path() -> Path:
    return settings.scripts_config_dir / CONFIG_FILENAME


# ---------------------------------------------------------------- parsing

def brand_key(name: str) -> str:
    """'NBA Brand 1' -> 'nba_brand_1'."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _step_list(text: str, brand: str) -> list[str]:
    steps = [s.strip().lower() for s in re.split(r"[+,]", text) if s.strip()]
    unknown = [s for s in steps if s not in STEP_COLUMNS]
    if unknown:
        raise TsvError(
            f"{brand}: unknown step(s) {', '.join(unknown)} in Steps. "
            f"Allowed: {', '.join(STEP_COLUMNS)}."
        )
    return steps


def parse_steps(text: str, brand: str) -> list[dict]:
    """Steps cell -> ordered branches; the first branch whose condition holds runs.

    'Outline + Script'                         -> [{"condition": None, "run": [...]}]
    'if (!competitor_script) { A + B } else { C }'
        -> [{"condition": "!competitor_script", "run": [a, b]},
            {"condition": "competitor_script",  "run": [c]}]
    """
    text = text.strip()
    if not text:
        raise TsvError(f"{brand}: the Steps column is empty.")
    m = _CONDITIONAL.match(text)
    if m:
        negated, var = m.group(1) == "!", m.group(2)
        cond, other = (f"!{var}", var) if negated else (var, f"!{var}")
        return [
            {"condition": cond, "run": _step_list(m.group("then"), brand)},
            {"condition": other, "run": _step_list(m.group("else"), brand)},
        ]
    if "{" in text or "if" in text.lower().split():
        raise TsvError(
            f"{brand}: couldn't read Steps {text!r}. "
            "Use 'A + B + C' or 'if (!var) { A + B } else { C }'."
        )
    return [{"condition": None, "run": _step_list(text, brand)}]


def parse_inputs(text: str) -> dict[str, list[str]]:
    """'outline = video_title + outline_prompt script = ...' -> {"outline": [...], ...}."""
    inputs: dict[str, list[str]] = {}
    for key, value in _INPUT_ASSIGN.findall(text.strip()):
        inputs[key.lower()] = [part.strip() for part in value.split("+") if part.strip()]
    return inputs


def _check_rules(name: str, cells: dict, used: set[str], inputs: dict[str, list[str]],
                 warnings: list[str]) -> dict[str, str]:
    """Read Rule Prompts and check the Rules program against it. Returns {NAME: prompt}."""
    try:
        rule_prompts, notes = parse_rule_prompts(cells.get(RULE_PROMPTS_COLUMN, ""))
    except RuleSyntaxError as exc:
        raise TsvError(f"{name}: Rule Prompts: {exc}") from exc
    warnings += [f"{name}: {n}" for n in notes]
    try:
        program = parse_rules(cells["Rules"], STEP_COLUMNS)
    except RuleSyntaxError as exc:
        raise TsvError(f"{name}: Rules: {exc} (The symbols are listed in app/keys.py.)") from exc

    missing = sorted(program.prompt_names() - set(rule_prompts))
    if missing:
        raise TsvError(f"{name}: Rules run {', '.join(missing)}, but Rule Prompts doesn't "
                       f'declare {"it" if len(missing) == 1 else "them"} (NAME = """...""").')
    for step in program.hooks:
        if step not in used:
            warnings.append(f"{name}: Rules run after '{step}', but that step never runs.")
    for unused in sorted(set(rule_prompts) - program.prompt_names()):
        warnings.append(f"{name}: rule prompt {unused} is declared but the Rules never run it.")
    checks = " ".join(" ".join(t.upper().split()) for t in rule_prompts.values() if is_check(t))
    stmts = [s for hook in program.hooks.values() for s in hook]
    for tag in tag_literals(stmts):
        if not re.search(rf"\b{re.escape(tag)}\b", checks):
            warnings.append(
                f'{name}: Rules compare TAG with "{tag}", but no check prompt asks for that '
                f"status, so it can never match. A check prompt needs a status line such as "
                f'"STATUS - {tag}".')
    known = set(keys.BUILTIN_PLACEHOLDERS) | set(keys.ALIASES)
    known |= {i.split("(")[0].strip() for items in inputs.values() for i in items}
    for rule, text in rule_prompts.items():
        for ph in dict.fromkeys(keys.PLACEHOLDER.findall(text)):
            if ph not in known:
                warnings.append(f"{name}: rule prompt {rule} uses {{{{{ph}}}}}, which isn't an "
                                "input or a built-in value; it is sent as written.")
    return rule_prompts


def parse_tsv(data: bytes | str) -> ParseResult:
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise TsvError("The file isn't UTF-8 text. Export it again as TSV.") from exc
    data = data.lstrip("﻿")

    reader = csv.DictReader(io.StringIO(data, newline=""), delimiter="\t")
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise TsvError(
            f"Missing column(s): {', '.join(missing)}. "
            f"Expected: {', '.join(REQUIRED_COLUMNS)}."
        )

    config: list[dict] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}

    for line_no, row in enumerate(reader, start=2):
        cells = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(cells.values()):
            continue
        name = cells["Brand"]
        if not name:
            raise TsvError(f"Row {line_no}: the Brand column is empty.")
        key = brand_key(name)
        if key in seen:
            raise TsvError(
                f"Row {line_no}: brand {name!r} ({key}) already appears on row {seen[key]}."
            )
        seen[key] = line_no

        prompts = {step: (cells[col] or None) for step, col in STEP_COLUMNS.items()}
        steps = parse_steps(cells["Steps"], name)
        inputs = parse_inputs(cells["Inputs"])

        used = {s for branch in steps for s in branch["run"]}
        for step in sorted(used, key=list(STEP_COLUMNS).index):
            if not prompts[step]:
                warnings.append(f"{name}: step '{step}' runs but its prompt is empty.")
            if step not in inputs:
                warnings.append(f"{name}: step '{step}' runs but has no inputs listed.")
        for step, prompt in prompts.items():
            if prompt and step not in used:
                warnings.append(f"{name}: '{step}' prompt is filled in but the step never runs.")
        for step in inputs:
            if step not in STEP_COLUMNS:
                warnings.append(f"{name}: inputs given for unknown step '{step}'.")

        rule_prompts = _check_rules(name, cells, used, inputs, warnings)
        config.append({
            key: {
                "brand_name": name,
                "prompts": prompts,
                "steps": steps,
                "rules": cells["Rules"] or None,
                "rule_prompts": rule_prompts,
                "inputs": inputs,
            }
        })

    if not config:
        raise TsvError("The file has a header but no brand rows.")
    return ParseResult(config=config, warnings=warnings)


# ---------------------------------------------------------------- storage

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump_config(config: list[dict]) -> bytes:
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def save(raw_tsv: bytes, result: ParseResult) -> None:
    """Store the uploaded TSV, then write the JSON config built from it."""
    _atomic_write(tsv_path(), raw_tsv)
    _atomic_write(config_path(), dump_config(result.config))


def load_config() -> list[dict] | None:
    path = config_path()
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def file_info(path: Path) -> dict:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    }


def status() -> dict:
    """What the dashboard shows: the current config, the stored TSV, and whether they match."""
    tsv, cfg = file_info(tsv_path()), file_info(config_path())
    out: dict = {"tsv": tsv, "config_file": cfg, "config": None, "error": None,
                 "warnings": [], "in_sync": None}
    try:
        out["config"] = load_config()
    except (OSError, json.JSONDecodeError) as exc:
        out["error"] = f"Couldn't read the config file: {exc}"
    if tsv["exists"]:
        try:
            parsed = parse_tsv(tsv_path().read_bytes())
            out["warnings"] = parsed.warnings
            if out["config"] is not None:
                out["in_sync"] = parsed.config == out["config"]
        except TsvError as exc:
            out["error"] = f"The stored TSV can't be processed: {exc}"
    return out

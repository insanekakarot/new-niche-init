"""Turn the "Scripting Prompts Config" TSV into the admin JSON config.

TSV columns (one row per brand):
    Research | Outline | Script | Audits | Paraphrasing | Brand | Steps | Rules | Inputs

Output (__scripts.prompt.json) is a list with one object per brand:
    [{"nba_brand_1": {"brand_name": ..., "prompts": {...}, "steps": [...],
                      "rules": ..., "inputs": {...}}}, ...]
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

from app.config import settings

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

_CONDITIONAL = re.compile(
    r"^\s*if\s*\(\s*(!?)\s*(\w+)\s*\)\s*\{(?P<then>.*?)\}\s*else\s*\{(?P<else>.*?)\}\s*$",
    re.IGNORECASE | re.DOTALL,
)
# "outline = a + b script = c + d"  ->  ("outline", "a + b"), ("script", "c + d")
_INPUT_ASSIGN = re.compile(r"(\w+)\s*=\s*(.*?)(?=\s+\w+\s*=|\s*$)", re.DOTALL)


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

        config.append({
            key: {
                "brand_name": name,
                "prompts": prompts,
                "steps": steps,
                "rules": cells["Rules"] or None,
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

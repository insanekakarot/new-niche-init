"""Build the Scripting page's form from the admin config (__scripts.prompt.json).

Every name in a step's `inputs` is one of:
  * prompt     -- ends in `_prompt`: the admin prompt, filled in by the server
  * generated  -- the output of an earlier step in the same branch
                  (`outline`, `outline_content`, `research_content`, `script`, ...)
  * user       -- anything else (`video_title`, `inspirational_script`, ...):
                  the person filling in the Scripting page provides it.
                  `name(optional)` makes it optional.
Variables used in Steps conditions (e.g. `competitor_script`) are optional user
fields too; whether they're filled in decides which branch runs.

Prompt text is never sent to the browser -- only whether each prompt is set.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from app.admin import scripts_config as sc
from app.web.catalog import BRANDS

# Admin config brand key -> catalog brand (languages, length & word-count targets).
CONFIG_BRAND_MAP: dict[str, str] = {
    "nba_brand_1": "nba-stories",
    "nba_brand_2": "nba-games",
    "a_brand_1": "athlete-records",
    "t_brand_1": "top-10",
}
_CATALOG = {b.slug: b for b in BRANDS}

_OPTIONAL = re.compile(r"^(?P<name>\w+)\s*\(\s*optional\s*\)$", re.IGNORECASE)
_MULTILINE_HINTS = ("script", "content", "outline", "research", "notes", "text")


def _split_optional(raw: str) -> tuple[str, bool]:
    m = _OPTIONAL.match(raw.strip())
    return (m.group("name"), True) if m else (raw.strip(), False)


def _label(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _field(name: str, optional: bool, condition: bool = False) -> dict:
    return {
        "name": name,
        "label": _label(name),
        "optional": optional,
        "condition": condition,
        "multiline": any(h in name for h in _MULTILINE_HINTS),
    }


def _step_fields(items: list[dict]) -> list[dict]:
    """Form fields for running one step on its own.

    User inputs are asked as usual. An earlier step's output is asked too (paste it,
    or it comes from running that step first), so it is always required.
    """
    fields = []
    for i in items:
        if i["kind"] == "user":
            f = _field(i["name"], i["optional"])
            fields.append({**f, "kind": "user", "required": not i["optional"]})
        elif i["kind"] == "generated":
            fields.append({"name": i["name"], "label": _label(i["name"]), "kind": "generated",
                           "source": i["source"], "source_label": _label(i["source"]),
                           "optional": False, "required": True, "multiline": True,
                           "condition": False})
    return fields


def brand_view(key: str, brand: dict) -> dict:
    prompts = brand.get("prompts") or {}
    inputs = brand.get("inputs") or {}
    steps = brand.get("steps") or []

    fields: dict[str, dict] = {}
    # Condition variables first: they decide which branch runs.
    for branch in steps:
        cond = branch.get("condition")
        if cond:
            var = cond.lstrip("!")
            fields.setdefault(var, _field(var, optional=True, condition=True))

    branches = []
    for branch in steps:
        produced: dict[str, str] = {}  # output name -> step that produced it
        out_steps = []
        user_fields: list[str] = []
        for step in branch.get("run", []):
            items = []
            for raw in inputs.get(step, []):
                name, optional = _split_optional(raw)
                if name.endswith("_prompt"):
                    src = name[: -len("_prompt")]
                    items.append({"name": name, "kind": "prompt", "source": src,
                                  "available": bool(prompts.get(src))})
                elif name in produced:
                    items.append({"name": name, "kind": "generated", "source": produced[name]})
                else:
                    existing = fields.get(name)
                    if existing is None:
                        fields[name] = _field(name, optional)
                    elif not optional and not existing["condition"]:
                        existing["optional"] = False  # required anywhere -> required
                    items.append({"name": name, "kind": "user", "optional": optional})
                    if name not in user_fields:
                        user_fields.append(name)
            out_steps.append({
                "name": step,
                "label": _label(step),
                "has_prompt": bool(prompts.get(step)),
                "inputs": items,
                "fields": _step_fields(items),
                "prompt_inputs": [i for i in items if i["kind"] == "prompt"],
            })
            produced.setdefault(step, step)
            produced.setdefault(f"{step}_content", step)

        cond = branch.get("condition")
        required = [n for n in user_fields
                    if not fields[n]["optional"] and n != (cond or "").lstrip("!")]
        # A positive condition ("competitor_script") means that field must be filled.
        if cond and not cond.startswith("!"):
            required.insert(0, cond)
        branches.append({"condition": cond, "steps": out_steps, "fields": user_fields,
                         "required": required})

    catalog = _CATALOG.get(CONFIG_BRAND_MAP.get(key, ""))
    return {
        "key": key,
        "name": brand.get("brand_name") or key,
        "rules": brand.get("rules"),
        "fields": list(fields.values()),
        "branches": branches,
        "catalog": asdict(catalog) if catalog else None,
    }


def find_brand(key: str) -> tuple[dict, dict] | None:
    """(raw admin config for the brand, its view) or None."""
    for entry in sc.load_config() or []:
        if key in entry:
            return entry[key], brand_view(key, entry[key])
    return None


def build_step_prompt(*, brand: dict, view: dict, branch: dict, step: dict,
                      inputs: dict[str, str], language: str) -> str:
    """The full request for one step: admin prompt + this run's inputs + run settings."""
    prompt = (brand.get("prompts") or {}).get(step["name"]) or ""
    parts = ["# Task", prompt.strip(), "", "# Inputs for this run"]
    for f in step["fields"]:
        value = (inputs.get(f["name"]) or "").strip()
        if not value:
            continue
        note = f' from="{f["source"]} step"' if f["kind"] == "generated" else ""
        parts += [f'<input name="{f["name"]}" label="{f["label"]}"{note}>', value, "</input>", ""]

    catalog = view.get("catalog")
    steps = branch["steps"]
    position = next(i for i, s in enumerate(steps) if s is step) + 1
    parts += ["# Run settings",
              f"- Brand: {view['name']}" + (f" ({catalog['name']})" if catalog else ""),
              f"- Step: {step['label']} (step {position} of {len(steps)}: "
              + " -> ".join(s["label"] for s in steps) + ")",
              f"- Output language: {language}. Write the entire response in {language}."]
    if catalog:
        parts.append(f"- Brand targets for the finished video: {catalog['video_length']}, "
                     f"{catalog['word_count']}. Respect these unless the task says otherwise.")
    if view.get("rules"):
        parts += ["", "# Brand rules", view["rules"].strip()]
    parts += ["", f"Now complete the {step['label'].lower()} step. "
                  "Return only the finished result as plain text."]
    return "\n".join(parts)


def load_brand_views() -> tuple[list[dict], str | None]:
    """(brand views, error message). An empty list with no error means no config yet."""
    try:
        config = sc.load_config()
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"The scripting config couldn't be read: {exc}"
    if not config:
        return [], None
    views = []
    try:
        for entry in config:
            for key, brand in entry.items():
                views.append(brand_view(key, brand))
    except (AttributeError, TypeError) as exc:
        return [], f"The scripting config has an unexpected shape: {exc}"
    return views, None

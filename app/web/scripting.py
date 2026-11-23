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

In a prompt:
  * `{{name}}` is replaced with that input's value (e.g. `{{outline}}` with the Outline
    step's saved result). Inputs placed this way aren't repeated after the prompt.
    `{{competitor_script}}` means the inspirational script (ALIASES).
  * `<< ... >>` is left as written: it tells the model to take that part from the
    conversation (the earlier steps of the project). Inputs the conversation already has
    aren't sent again.
  * Showing a code fence (```) or naming a "code block" means the step's result is the code
    block: only that is saved and passed on (app/web/extract.py).

Prompt text is never sent to the browser -- only whether each prompt is set, and which
input names it places.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict

from app import keys, rules
from app.admin import scripts_config as sc
from app.web import catalog as brand_catalog
from app.web.extract import wants_code_block

# Which brand profile (languages, length & word targets) a TSV brand is, and whether it's
# shown, both come from the private brands file ("config_keys", "enabled").

# Symbols (all in app/keys.py).
_OPTIONAL = keys.OPTIONAL_INPUT
_MULTILINE_HINTS = ("script", "content", "outline", "research", "notes", "text")
PLACEHOLDER = keys.PLACEHOLDER
CONTEXT_REF = keys.CONTEXT_REF
# Other names prompts use for an input: {{competitor_script}} is the inspirational script.
ALIASES = keys.ALIASES


def placeholders(prompt: str | None) -> list[str]:
    """Names a prompt places with {{name}}, in order, without repeats (as written)."""
    return list(dict.fromkeys(PLACEHOLDER.findall(prompt or "")))


def input_for(name: str, inputs: set[str] | dict) -> str | None:
    """The step input a {{name}} stands for: itself, else another name of the same input
    (source_script / competitor_script / inspirational_script); None if none of them."""
    return next((n for n in keys.same_input(name) if n in inputs), None)


def _split_optional(raw: str) -> tuple[str, bool]:
    m = _OPTIONAL.match(raw.strip())
    return (m.group("name"), True) if m else (raw.strip(), False)


def _label(name: str) -> str:
    return keys.INPUT_LABELS.get(name) or name.replace("_", " ").strip().capitalize()


def _field(name: str, optional: bool, condition: bool = False) -> dict:
    return {
        "name": name,
        "label": _label(name),
        "optional": optional,
        "condition": condition,
        "multiline": any(h in name for h in _MULTILINE_HINTS) or name in keys.TRANSCRIPT_INPUTS,
        # A video's transcript: the page can also take it from a video link.
        "transcript": name in keys.TRANSCRIPT_INPUTS,
    }


def _step_fields(items: list[dict], placed: list[str]) -> list[dict]:
    """Form fields for running one step on its own.

    User inputs are asked as usual. An earlier step's output is asked too (paste it,
    or it comes from running that step first), so it is always required.
    `placed`: whether the prompt puts the value in place with {{name}}.
    """
    fields = []
    for i in items:
        if i["kind"] == "user":
            f = _field(i["name"], i["optional"])
            fields.append({**f, "kind": "user", "required": not i["optional"],
                           "placed": i["name"] in placed})
        elif i["kind"] == "generated":
            fields.append({"name": i["name"], "label": _label(i["name"]), "kind": "generated",
                           "source": i["source"], "source_label": _label(i["source"]),
                           "optional": False, "required": True, "multiline": True,
                           "condition": False, "placed": i["name"] in placed})
    return fields


def brand_view(key: str, brand: dict) -> dict:
    prompts = brand.get("prompts") or {}
    inputs = brand.get("inputs") or {}
    steps = brand.get("steps") or []
    program, rules_error = _rules_program(brand)

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
            text = prompts.get(step) or ""
            known = {i["name"] for i in items if i["kind"] != "prompt"}
            written = placeholders(text)
            placed = [n for w in written if (n := input_for(w, known))]
            before = _rule_names(program.before.get(step, []))
            from_rules = {n.lower() for n in before}
            out_steps.append({
                "name": step,
                "label": _label(step),
                "has_prompt": bool(text),
                "inputs": items,
                "fields": _step_fields(items, placed),
                "prompt_inputs": [i for i in items if i["kind"] == "prompt"],
                # {{name}} in the prompt that isn't one of this step's inputs: left as written.
                "unknown_placeholders": [w for w in written if not input_for(w, known)
                                         and w.lower() not in from_rules
                                         and w not in keys.BUILTIN_PLACEHOLDERS],
                # {{name}} filled with a built-in value ({{ used_entries }}, {{ words_range }}...).
                "builtin_placeholders": [w for w in written if not input_for(w, known)
                                         and w in keys.BUILTIN_PLACEHOLDERS],
                # {{name}} filled by a rule prompt that runs before this step.
                "rule_placeholders": [w for w in written if w.lower() in from_rules],
                "uses_context": bool(CONTEXT_REF.search(text)),
                "code_block": wants_code_block(text),
                # The rule prompts that run after this step (Rules column), in order.
                "rules_after": _rule_names(program.hooks.get(step, [])),
                # ... and the ones that run before it ('< step').
                "rules_before": before,
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

    catalog = brand_catalog.brand_for_key(key)
    return {
        "key": key,
        "name": brand.get("brand_name") or key,
        # Plain guidance (text before the first '> step'): added to every step prompt.
        "rules": program.guidance,
        # The control part of the Rules column, laid out one statement per line.
        "rules_program": rules.lang.layout(rules.lang.split_guidance(
            brand.get("rules") or "", set(sc.STEP_COLUMNS))[1]) or None,
        "rules_error": rules_error,
        "fields": list(fields.values()),
        "branches": branches,
        "catalog": asdict(catalog) if catalog else None,
    }


def catalog_for(key: str) -> dict | None:
    """The catalog targets (languages, length, word count) linked to an admin brand key."""
    catalog = brand_catalog.brand_for_key(key)
    return asdict(catalog) if catalog else None


def _rules_program(brand: dict) -> tuple[rules.Program, str | None]:
    """The brand's Rules, parsed. A config saved before rules were checked may not parse:
    then nothing runs after the steps and only the guidance part is used."""
    try:
        return rules.parse(brand.get("rules"), sc.STEP_COLUMNS), None
    except rules.RuleSyntaxError as exc:
        guidance, _ = rules.lang.split_guidance(brand.get("rules") or "", set(sc.STEP_COLUMNS))
        return rules.Program(guidance, {}), f"The Rules can't be read, so they don't run: {exc}"


def _rule_names(stmts: list) -> list[str]:
    """Rule prompt names a hook runs, in the order they first appear."""
    names: list[str] = []

    def walk(items):
        for s in items:
            if isinstance(s, rules.lang.Run) and s.name not in names:
                names.append(s.name)
            elif isinstance(s, rules.lang.Loop):
                walk(s.body)
            elif isinstance(s, rules.lang.If):
                walk(s.then)
                walk(s.else_)

    walk(stmts)
    return names


def find_brand(key: str) -> tuple[dict, dict] | None:
    """(raw admin config for the brand, its view) or None (also when the brand is hidden)."""
    if not brand_catalog.key_enabled(key):
        return None
    for entry in sc.load_config() or []:
        if key in entry:
            return entry[key], brand_view(key, entry[key])
    return None


def build_step_prompt(*, brand: dict, view: dict, branch: dict, step: dict,
                      inputs: dict[str, str], language: str, project_name: str | None = None,
                      continuing: bool = False, in_chat: dict[str, str] | None = None,
                      rule_results: dict[str, str] | None = None,
                      builtins: dict[str, str] | None = None) -> str:
    """The full request for one step: admin prompt + this run's inputs + run settings.

    `inputs`: this run's values, including any the server took from the project's saved
    results. `{{name}}` in the prompt is replaced with them; the rest follow the prompt.
    `continuing`: the step is sent into the project's existing conversation.
    `in_chat`: inputs that conversation already has, so they aren't sent again:
    name -> where the model finds them ("your Outline step response from earlier ...").
    `rule_results`: what the rules before this step ('< step') produced, by lower-case rule
    prompt name; they fill {{ name }} too.
    `builtins`: the built-in values ({{ used_entries }}, {{ words_range }}, ...), used for a
    {{ name }} that is neither an input nor a rule result.
    """
    in_chat = in_chat or {}
    rule_results = rule_results or {}
    builtins = builtins or {}
    fields = {f["name"]: f for f in step["fields"]}

    def fill(m: re.Match) -> str:
        f = fields.get(input_for(m.group(1), fields) or "")
        if f is None:
            ran = m.group(1).lower()
            if ran in rule_results:
                return rule_results[ran]
            if builtins.get(m.group(1)):
                return builtins[m.group(1)]
            return m.group(0)  # not an input of this step: leave it for the admin to fix
        value = (inputs.get(f["name"]) or "").strip()
        if value:
            return value
        if f["name"] in in_chat:
            return f"[{f['label']}: not repeated here; use {in_chat[f['name']]}]"
        return f"[no {f['label'].lower()} given]"

    prompt = (brand.get("prompts") or {}).get(step["name"]) or ""
    # One pass, so text inside an input that looks like {{x}} is never replaced.
    parts = ["# Task", PLACEHOLDER.sub(fill, prompt.strip()), ""]
    blocks: list[str] = []
    for f in step["fields"]:
        if f.get("placed"):
            continue
        value = (inputs.get(f["name"]) or "").strip()
        note = f' from="{f["source"]} step"' if f["kind"] == "generated" else ""
        if not value and f["name"] in in_chat:
            value = f"Not repeated here: use {in_chat[f['name']]}."
        if not value:
            continue
        blocks += [f'<input name="{f["name"]}" label="{f["label"]}"{note}>', value, "</input>", ""]
    if blocks:
        parts += ["# Inputs for this run", *blocks]

    catalog = view.get("catalog")
    steps = branch["steps"]
    position = next(i for i, s in enumerate(steps) if s is step) + 1
    parts += ["# Run settings",
              f"- Brand: {view['name']}" + (f" ({catalog['name']})" if catalog else ""),
              f"- Step: {step['label']} (step {position} of {len(steps)}: "
              + " -> ".join(s["label"] for s in steps) + ")",
              f"- Output language: {language}. Write the entire response in {language}."]
    if continuing:
        parts.append(f"- Project: {project_name}. This continues the same conversation: the "
                     "earlier steps of this project are above.")
    if rule_results:
        parts.append("- Just before this step, this conversation ran: "
                     + ", ".join(n.upper() for n in rule_results)
                     + ". Their answers are above; use them for this step.")
    if step.get("uses_context"):
        parts.append("- Text inside << >> in the task means: take it from this conversation "
                     "(the inputs and results of the earlier steps above). It isn't repeated here."
                     if continuing else
                     "- Text inside << >> in the task refers to the matching input under "
                     "Inputs for this run.")
    if catalog:
        parts.append(f"- Brand targets for the finished video: {catalog['video_length']}, "
                     f"{catalog['word_count']}. Respect these unless the task says otherwise.")
    if view.get("rules"):
        parts += ["", "# Brand rules", view["rules"].strip()]
    finish = (("Follow the task's output format exactly: the result the next step uses goes "
               "inside the one ``` code block, everything else outside it.")
              if step.get("code_block") else "Return only the finished result as plain text.")
    parts += ["", f"Now complete the {step['label'].lower()} step. {finish}"]
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
                if brand_catalog.key_enabled(key):  # "enabled": false in the brands file: hidden
                    views.append(brand_view(key, brand))
    except (AttributeError, TypeError) as exc:
        return [], f"The scripting config has an unexpected shape: {exc}"
    return views, None

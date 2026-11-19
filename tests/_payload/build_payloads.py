"""Rebuild the exact payload the Scripting Tool sent to Claude / Codex for every saved output.

Run from the project root (uses the real .env, config and app/output):

    .venv/bin/python -m tests._payload.build_payloads

For each app/output/<step>/<name>.json it rebuilds the request with the same code the
server uses (build_step_prompt + build_payload) from the inputs recorded in that run, and
writes tests/_payload/<step>/<name>.<provider>.payload.txt plus an INDEX.md overview.

Runs made after this was added also save their exact payload at run time, next to the
output (<name>.payload.txt); when that file exists it is copied instead of rebuilt.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.admin import scripts_config as sc
from app.cli import build_payload
from app.web import outputs
from app.web.scripting import build_step_prompt, find_brand

HERE = Path(__file__).parent


def _provider(meta: dict) -> str:
    if meta.get("provider"):
        return meta["provider"]
    model = meta.get("model") or ""
    return "codex" if model.startswith("gpt") else "claude"


def _parts(prompt: str, brand: dict, step: dict, inputs: dict) -> list[tuple[str, int]]:
    """Rough size of each piece of the prompt, to see what dominates."""
    admin = (brand.get("prompts") or {}).get(step["name"]) or ""
    rows = [(f"admin {step['name']} prompt", len(admin.split()))]
    rows += [(f"input: {k}", len(v.split())) for k, v in inputs.items()]
    rest = prompt.split("# Run settings", 1)[1] if "# Run settings" in prompt else ""
    rows.append(("run settings + closing line", len(rest.split())))
    return rows


def main() -> None:
    root = outputs.output_dir()
    config_mtime = datetime.fromtimestamp(sc.config_path().stat().st_mtime, tz=timezone.utc)
    index = ["# Payloads sent by the Scripting Tool", "",
             f"Rebuilt {datetime.now().astimezone():%Y-%m-%d %H:%M} from `{root}`.",
             f"Admin config last changed: {config_mtime.astimezone():%Y-%m-%d %H:%M}.", "",
             "Each file shows the full command line, the system prompt and the exact STDIN text.", ""]

    # Step folders only (not _projects/).
    for step_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name in sc.STEP_COLUMNS):
        target = HERE / step_dir.name
        if target.exists():
            shutil.rmtree(target)
        rows = []
        for meta_file in sorted(step_dir.glob("*.json")):
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            output = meta_file.with_suffix(".txt")
            provider = _provider(meta)
            target.mkdir(parents=True, exist_ok=True)
            dest = target / f"{output.stem}.{provider}{outputs.PAYLOAD_SUFFIX}"

            recorded = outputs.payload_path(output)
            found = find_brand(meta["brand"])
            if found is None:
                print(f"skip {output.name}: brand {meta['brand']} no longer in config")
                continue
            brand, view = found
            branch = view["branches"][meta.get("branch", 0)]
            step = next(s for s in branch["steps"] if s["name"] == meta["step"])
            inputs = meta.get("inputs") or {}
            prompt = build_step_prompt(brand=brand, view=view, branch=branch, step=step,
                                       inputs=inputs, language=meta["language"])

            if recorded.is_file():
                shutil.copyfile(recorded, dest)
                how = "recorded at run time" + (" (resumed project chat)" if meta.get("resumed") else "")
            else:
                created = datetime.fromisoformat(meta["created_at"])
                exact = config_mtime <= created
                header = {
                    "Step": meta["step"], "Brand": f"{meta['brand']} ({meta.get('brand_name', '')})",
                    "Provider": f"{provider} · {meta.get('model')} · {meta.get('effort')}",
                    "Language": meta["language"],
                    "Output": f"app/output/{meta['step']}/{output.name}",
                    "Run at": f"{created.astimezone():%Y-%m-%d %H:%M:%S}",
                    "Rebuilt": ("yes — admin config and CLI settings unchanged since this run, "
                                "so this is what was sent" if exact else
                                "yes — WARNING: admin config changed after this run; the admin "
                                "prompt below is the current one, not necessarily the one used"),
                }
                if provider == "codex":
                    header["Note"] = ("Codex ran in a random temp folder; <temp-dir> stands in "
                                      "for it in -C and -o")
                dest.write_text(outputs.format_payload(build_payload(provider, prompt, effort=meta.get("effort")), header),
                                encoding="utf-8")
                how = "rebuilt" + ("" if exact else " (config changed since!)")

            out_words = len(output.read_text(encoding="utf-8").split()) if output.is_file() else 0
            parts = ", ".join(f"{name} {n:,}" for name, n in _parts(prompt, brand, step, inputs))
            rows.append(f"| [{dest.name}]({step_dir.name}/{dest.name}) | {meta.get('brand_name', meta['brand'])} "
                        f"| {provider} | {len(prompt.split()):,} | {parts} | {out_words:,} | {how} |")
            print(f"wrote {dest.relative_to(HERE)} ({how})")

        if rows:
            index += [f"## {step_dir.name.capitalize()}", "",
                      "| Payload | Brand | Provider | Prompt words | Prompt parts (words) | Response words | Source |",
                      "|---|---|---|---|---|---|---|", *rows, ""]

    (HERE / "INDEX.md").write_text("\n".join(index), encoding="utf-8")
    print(f"wrote {HERE / 'INDEX.md'}")


if __name__ == "__main__":
    main()

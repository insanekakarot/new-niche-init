"""Rebuild what the research niche checks send to Claude / Codex, from saved runs.

    .venv/bin/python -m tests._payload.build_research_payloads [run id ...]

For every run in app/output/_research/ that kept to a niche with the AI (or just the runs
named), writes tests/_payload/research/<run id>/NN-<video id>.<provider>.payload.txt for each
opened video, for both CLIs, plus INDEX.md. The run is replayed the way the explorer did it
(app/research/explorer.py): which videos were opened, which related ones were skipped as
another niche and which videos were in the niche, so each check gets the same related videos,
in the same order, as it had in the run. The prompt (app/research/niche.prompt) and the
command line (app/research/ai.build_payload) come from the code the server uses.

The files contain the runs' YouTube data: tests/_payload/*/ is in .gitignore.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.research import ai, niche, options, runs
from app.research.explorer import _surely_within
from app.web.outputs import format_payload

HERE = Path(__file__).parent / "research"


def _checks(run: dict) -> list[dict]:
    """For each opened video: the related videos its check saw, and the prompt."""
    opts = options.clean(run.get("matching") or {})
    matcher = niche.Matcher(opts)
    videos = run.get("videos") or []
    days = run.get("max_age_days")
    opened = {s["id"] for s in run.get("skipped") or []}   # failed to open: never candidates
    in_niche: list[dict] = []
    rejected: set[str] = set()
    out = []
    for i, video in enumerate(videos):
        start = i == 0
        opened.add(video["id"])
        cands, seen = [], set()
        for v in [video, *reversed(in_niche)]:
            for r in v.get("related") or []:
                if (r["id"] in opened or r["id"] in rejected or r["id"] in seen
                        or not _surely_within(r, days)):
                    continue
                seen.add(r["id"])
                cands.append(r)
        if start:
            matcher.words = niche.profile(video)
        ranked = matcher.rank(cands)
        prompt = niche.prompt(video, ranked, None if start else run.get("niche"), start,
                              opts["strictness"])
        skipped_here = [o for o in run.get("off_niche") or [] if o.get("seen_on") == video["id"]]
        out.append({"n": i + 1, "video": video, "candidates": ranked, "all_candidates": len(cands),
                    "prompt": prompt, "skipped_here": skipped_here,
                    "found": sum(o["id"] in {c["id"] for c in ranked} for o in skipped_here)})
        # After the check: what the explorer recorded (app/research/explorer._apply_check).
        if start and run.get("niche"):
            matcher.niche = run["niche"]
            matcher.words = niche.profile(video, run["niche"].get("keywords") or [])
        rejected |= {o["id"] for o in skipped_here}
        if (video.get("niche") or {}).get("match") is not False:
            in_niche.append(video)
    return out


def build(run: dict) -> list[str]:
    folder = HERE / run["id"]
    folder.mkdir(parents=True, exist_ok=True)
    m = run.get("matching") or {}
    opts = options.clean(m)
    lines = [f"## Run {run['id']}", "",
             f"- Start: {run['url']}",
             f"- Matching: {m.get('text') or options.describe(opts)} "
             f"(used in the run: {opts['provider']})",
             f"- Niche found: {(run.get('niche') or {}).get('label', '-')}"
             f" / topic: {(run.get('niche') or {}).get('topic', '-')}",
             f"- Videos: {len(run.get('videos') or [])}, skipped as another niche: "
             f"{len(run.get('off_niche') or [])}", "",
             "| # | Video | Related sent (of left) | Prompt chars | Skipped here (found in list) | Result |",
             "|---|---|---|---|---|---|"]
    for c in _checks(run):
        v = c["video"]
        for provider in ("claude", "codex"):
            payload = ai.build_payload(provider, c["prompt"], model=opts["model"],
                                       effort=opts["effort"])
            header = {"run": run["id"], "check": f"{c['n']} of {len(run['videos'])}"
                      + (" (start video: sets the niche)" if c["n"] == 1 else ""),
                      "video": f"{v['title']} ({v['url']})",
                      "provider": provider + (" (the one used in the run)"
                                              if provider == opts["provider"] else " (same check, if Codex were chosen)"
                                              if provider == "codex" else " (same check, if Claude were chosen)"),
                      "related videos sent": f"{len(c['candidates'])} of {c['all_candidates']} left",
                      "rebuilt": "from the saved run, with the server's code"}
            name = f"{c['n']:02d}-{v['id']}.{provider}.payload.txt"
            (folder / name).write_text(format_payload(payload, header), encoding="utf-8")
        nz = v.get("niche") or {}
        result = (f"{'✓' if nz.get('match') else '✗' if nz.get('match') is False else '·'} "
                  f"{nz.get('label', '')}").strip()
        lines.append(f"| {c['n']} | {v['title'][:60].replace('|', '/')} | "
                     f"{len(c['candidates'])} ({c['all_candidates']}) | {len(c['prompt']):,} | "
                     f"{len(c['skipped_here'])} ({c['found']}) | {result.replace('|', '/')} |")
    return lines + [""]


def main(ids: list[str]) -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    chosen = [runs.get(i) for i in ids] if ids else [
        runs.get(p.stem) for p in sorted(runs.folder().glob("*.json"))]
    chosen = [r for r in chosen if r and (r.get("matching") or {}).get("matching") in ("words_ai", "ai")]
    index = ["# What the research niche checks send to Claude / Codex", "",
             "One check per opened video. Each `.payload.txt` shows the command line, the system "
             "prompt (research instructions, never the brands file's) and the exact STDIN.",
             "\"Skipped here (found in list)\": related videos the run skipped after this check, "
             "and how many of them are in the rebuilt list (all = the rebuild matches the run).", ""]
    for run in chosen:
        index += build(run)
    (HERE / "INDEX.md").write_text("\n".join(index), encoding="utf-8")
    print(f"{len(chosen)} run(s) -> {HERE}")


if __name__ == "__main__":
    main(sys.argv[1:])

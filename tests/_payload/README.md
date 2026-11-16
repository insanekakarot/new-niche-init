# What the Scripting Tool sends to Claude / Codex

Every step run sends **one** request to a CLI. A payload has three parts:

| Part | Claude (`claude -p`) | Codex (`codex exec`) |
|---|---|---|
| **Command line** | model, effort, tools, `--safe-mode`, … | model, reasoning effort, web search, read-only sandbox, … |
| **System prompt** | our built-in writing rules, passed as `--system-prompt` | the same rules, placed at the top of STDIN under `# Instructions` |
| **STDIN** | the prompt below | `# Instructions` + the prompt below |

The prompt itself is always built in this order (`app/web/scripting.py → build_step_prompt`):

```
# Task
<the admin prompt for this brand + step, from __scripts.prompt.json>

# Inputs for this run
<input name="video_title" label="Video title">…</input>
<input name="outline" label="Outline" from="outline step">…</input>     ← one block per input

# Run settings
- Brand: …   - Step: … (step 2 of 3: Outline -> Script -> Audits)
- Output language: …   - Brand targets for the finished video: … (from catalog.py)

Now complete the <step> step. Return only the finished result as plain text.
```

## Files here

- `outline/`, `script/`, `audits/` — one `.payload.txt` per saved output in `app/output/`, named
  `<output name>.<provider>.payload.txt`. Each shows the full command line, the system prompt and the
  exact STDIN text between the `STDIN` and `END OF STDIN` lines.
- `INDEX.md` — every payload with its size broken down (admin prompt / each input / run settings)
  and the response length.
- `build_payloads.py` — regenerates all of the above:
  `.venv/bin/python -m tests._payload.build_payloads`

The 8 existing outputs were made before payloads were recorded, so they are **rebuilt** from the
inputs saved in each run's `.json` with the same code the server uses. The admin config (changed
11:35) and the CLI settings have not changed since those runs (12:24 onwards), so the rebuilt payloads
are what was sent. The only unknown is Codex's random temp folder, shown as `<temp-dir>`.
From now on every run also saves its exact payload next to the output
(`app/output/<step>/<name>.payload.txt`); the script copies those instead of rebuilding.

## Where the payloads conflict (NBA Brand 2: Outline → Script → Audits)

Ordered by how much they are likely to affect the response.

### 1. Outline asks for a choice that never happens
The Outline prompt asks for **three** outlines plus a recommendation and ends with
*"Do not write the complete script. Wait for my selection."* Nothing in the tool makes that selection.
The Script step then receives the **entire** Outline response (2,078–2,285 words: research, checklist,
all three outlines, recommendation) as `outline`, while its prompt says *"Using the approved outline"*.
The model has to guess which outline is "approved"; both scripts appear to follow the outline the
Outline step recommended, but nothing tells the model to do that.
→ Either paste only the chosen outline into the Script step, or change the Outline prompt to output one
final outline, or add a "chosen outline" input.

### 2. Script and Audits refer to inputs they never receive
| Step | Prompt refers to | Inputs actually sent (admin `Inputs` column) |
|---|---|---|
| Script | *"verified research from Step 1"*, *"quality of our competitors"*, *"Match the competitors' … energy"* | `video_title`, `outline` |
| Audits | *"Step 1 research"*, *"attached competitor scripts"*, *"the Step 1 coverage checklist"* | `video_title`, `script` |

The Step 1 research only reaches Script because it is inside the pasted Outline response. Audits gets
neither the research/checklist nor any competitor script, yet it must check the script against them.
→ Add `inspirational_script` (and `outline` for Audits) to those steps' Inputs in the TSV, or remove
those references from the prompts.

### 3. "Report the word count" vs. "return only the finished result"
The Script prompt says *"After the script, report the actual narration word count, estimated runtime,
and confirmation…"*, and Audits asks for a correction summary, word count, sources and a READY verdict.
Our system prompt says *"Deliver only the finished work … no closing remarks"* and the closing line
says *"Return only the finished result as plain text."* The admin prompt wins (the system prompt tells
the model to follow it), so the `.txt` files contain notes that are not narration, and those notes are
then passed on as part of `script` to Audits. The self-reported word counts are also estimates, not
real counts (e.g. Claude's script reports 1,612 words; Codex's says ~1,820 in a 2,027-word file).
→ Decide which one you want and make the admin prompt and our closing line agree.

### 4. "Clear section headings" vs. "plain text"
Script asks for *"clear section headings"*; our closing line says *"plain text"*. Claude answered with
Markdown (`# …`, `## …`, `**…**`), Codex with UPPERCASE heading lines. That is why the two providers'
files look different for the same step.
→ State the heading format you want in the admin prompt (e.g. "headings as UPPERCASE lines, no
Markdown").

### 5. The competitor script is called something else
Every NBA prompt says *"Competitor script(s): [ATTACH]"*, but the input is sent as
`<input name="inspirational_script" label="Inspirational script">`. The system prompt tells the model
that placeholders map to inputs, so it works, but the naming mismatch is a guess the model has to make.
(T Brand 1 separately uses an input actually named `competitor_script`.)
→ Rename the input to `competitor_script` in the TSV Inputs column, or rename the placeholder.

### Not a conflict, but worth knowing
- **Brand targets are added to every step**, including Outline and Audits. For NBA Brand 2 they match
  the admin prompts (1,600–2,000 words, 11–13 min). For other brands the targets come from
  `app/web/catalog.py` — if a prompt's own length rule differs, the two will disagree.
- **Output language** is stated twice (system prompt + Run settings). Harmless.
- `INPUT APPROVES OUTLINE: []` in the Script prompt is an empty placeholder; the system prompt tells the
  model `[]` means "use the matching input", which is how the outline gets connected.

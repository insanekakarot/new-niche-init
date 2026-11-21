"""The key: every control symbol and expression the Scripting Prompts Config TSV understands.

One place to look up (and change) what each symbol means. The parsers import their patterns
from here, and tests/test_rules.py parses every example in RULE_EXAMPLES, so this file stays
true.

Where each one is written in the TSV:

    Steps         Outline + Script + Audits            run these steps, in order
                  if (!competitor_script) { A + B } else { C }
                                                       pick a flow by whether an input is filled
    Inputs        script = video_title + outline + script_prompt
                  player_name(optional)                may be left empty
                  <step>_prompt                        that step's prompt from its column
                  <step> / <step>_content              an earlier step's result
    Prompts       {{name}}                             replaced with that input's value
                  << ... >>                            "take this from the conversation"
                  ``` ... ```                          the result is what's inside the code block
    Rule Prompts  NAME = \"\"\" ... \"\"\"                   a named prompt the Rules can run
    Rules         > audits  <statements>               after the Audits step, run the statements
                  < outline <statements>               before the Outline step, run the statements
                  LOOP / IF / BREAK / ...              see RULE_KEYWORDS below
"""

from __future__ import annotations

import re

# ============================================================== Steps column

STEP_JOIN = "+"  # 'Outline + Script + Audits' (a comma works too)
STEP_SPLIT = re.compile(r"[+,]")
# 'if (!var) { A + B } else { C }': `!var` = the input is empty.
STEP_CONDITION = re.compile(
    r"^\s*if\s*\(\s*(!?)\s*(\w+)\s*\)\s*\{(?P<then>.*?)\}\s*else\s*\{(?P<else>.*?)\}\s*$",
    re.IGNORECASE | re.DOTALL,
)

# ============================================================== Inputs column

# 'outline = a + b script = c + d'  ->  ("outline", "a + b"), ("script", "c + d")
INPUT_ASSIGN = re.compile(r"(\w+)\s*=\s*(.*?)(?=\s+\w+\s*=|\s*$)", re.DOTALL)
INPUT_JOIN = "+"
OPTIONAL_INPUT = re.compile(r"^(?P<name>\w+)\s*\(\s*optional\s*\)$", re.IGNORECASE)
PROMPT_SUFFIX = "_prompt"    # outline_prompt = the Outline column's prompt
CONTENT_SUFFIX = "_content"  # outline_content = the Outline step's result (same as `outline`)

# ============================================================== Prompt markers

# {{name}} -> that input's value. Spaces inside are fine: {{ words_range }}.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
# Names that mean the same input: the competitor's transcript is the source script (and the
# inspirational script). {{ any of them }} is filled from whichever one the step has.
SAME_INPUT: tuple[tuple[str, ...], ...] = (
    ("source_script", "competitor_script", "inspirational_script"),
)
# name -> the other names of the same input
ALIASES: dict[str, tuple[str, ...]] = {
    n: tuple(o for o in group if o != n) for group in SAME_INPUT for n in group}


def same_input(name: str) -> tuple[str, ...]:
    """The name first, then the other names of the same input (if any)."""
    return (name, *ALIASES.get(name, ()))


# The label the page shows for an input (default: its name, e.g. "Video title").
INPUT_LABELS: dict[str, str] = {"competitor_script": "Source script",
                                "source_script": "Source script"}
# Inputs that are a video's transcript: the page can also take them from a video link.
TRANSCRIPT_INPUTS: frozenset[str] = frozenset(SAME_INPUT[0])
# << Current Script >> -> left as written; the model takes it from the conversation.
CONTEXT_REF = re.compile(r"<<[^<>\n]{1,200}>>")
# A prompt showing a fence or naming a "code block"/"code panel": the result is the block.
WANTS_CODE_BLOCK = re.compile(r"```|~~~|\bcode (?:block|panel)\b", re.IGNORECASE)
CODE_FENCE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^`\s]*)[ \t]*$")

# Filled in for {{ }} in rule prompts (and usable in step prompts), besides the step's inputs.
BUILTIN_PLACEHOLDERS: dict[str, str] = {
    "words_range": "The brand's word target without 'words', e.g. 1,600–2,000.",
    "word_count": "The brand's word target as written, e.g. 1,600–2,000 words.",
    "video_length": "The brand's video length, e.g. 11–13 min.",
    "language": "The output language picked for the run.",
    "brand_name": "The brand's name, e.g. NBA Brand 2.",
    "project_name": "The project's name.",
    "step": "The step the rules run after, e.g. Audits.",
    "used_entries": "The brand's previously used entries, one per line (Admin → Used entries).",
}

# ============================================================== Rule Prompts column

# STRICT_VALIDATION = """ ... """   (names: letters, digits, _; case doesn't matter)
RULE_PROMPT = re.compile(r'([A-Za-z_]\w*)\s*=\s*"""(.*?)"""', re.DOTALL)

# The label of a status line, before '-' or ':':  'STATUS - ...',  'Title Satisfied: True',
# 'Satisfied: False', 'NEXT MOVE - CONTINUE'. Add a label here to make prompts that use it checks.
STATUS_LABEL = r"\b(?:STATUS|(?:TITLE\s+)?SATISFIED|NEXT\s+MOVE)\s*[-–—:]\s*"
# A rule prompt that defines a status line ("STATUS - READY FOR PRODUCTION", "Title Satisfied:
# True", "NEXT MOVE - [CONTINUE or RESEARCH]") is a *check*: its answer sets TAG (and RATING)
# and the script stays as it is. Any other rule prompt is a *revision*: the code block of its
# answer becomes the current script.
CHECK_PROMPT = re.compile(STATUS_LABEL + r"\[?[A-Z]", re.IGNORECASE)
# The tag in a check's answer: 'STATUS - NOT READY FOR PRODUCTION' -> 'NOT READY FOR PRODUCTION',
# 'Title Satisfied: True' -> 'TRUE'.
STATUS_LINE = re.compile(STATUS_LABEL + r"\[?(?P<tag>[A-Za-z][A-Za-z ]*[A-Za-z])", re.IGNORECASE)
# A check's tag that counts as a pass: 'READY FOR PRODUCTION', 'TRUE', 'YES', 'PASS', ...
# (used for the green / amber status, not by the rules themselves: they compare TAG as written).
PASS_TAG = re.compile(r"^(?:READY\b.*|TRUE|YES|PASS(?:ED)?|SATISFIED|APPROVED|CONTINUE)$",
                      re.IGNORECASE)
# The rating in a check's answer: 'Overall rating: 8.5/10' or 'Rating - 7'.
RATING = re.compile(
    r"\brating\b[^0-9\n]{0,30}(?P<n>\d+(?:\.\d+)?)(?:\s*/\s*(?P<of>\d+))?"
    r"|(?P<n2>\d+(?:\.\d+)?)\s*/\s*10\b",
    re.IGNORECASE,
)

# ============================================================== Rules column

# '> audits' starts the statements that run after that step (until the next '>' or the end).
# Text before the first '>' is plain brand guidance and is added to every step prompt.
HOOK = ">"
# '< outline' starts the statements that run *before* that step, in the same conversation, just
# before the step's prompt is sent (before the first step, they start the conversation). Each
# rule prompt's result (its code block, else the whole answer) fills {{name}} in the step's
# prompt, e.g. RESEARCH -> {{ research }}.
HOOK_BEFORE = "<"

RULE_KEYWORDS: dict[str, str] = {
    "NAME;": "Run the rule prompt NAME (declared in Rule Prompts) in the project's "
             "conversation, e.g. STRICT_VALIDATION;",
    "RUN NAME;": "Same as NAME; (reads better before a name that looks like a keyword).",
    "LOOP { ... } UNTIL (cond);": "Repeat the block, checking cond after each pass; stop "
                                  "when it is true.",
    "LOOP (MAX n) { ... }": "Repeat at most n passes (default DEFAULT_MAX_LOOP, never more "
                           "than HARD_MAX_LOOP).",
    "WHILE (cond) { ... }": "Repeat while cond is true, checking it before each pass.",
    "IF (cond) { ... } ELSE IF (cond) { ... } ELSE { ... }": "Run the first block whose "
                                                             "condition is true.",
    "BREAK;": "Leave the loop now. The current script is the result.",
    "CONTINUE;": "Skip the rest of this pass and go to the loop's next check.",
    "REVALIDATE;": "Same as CONTINUE: go back and check again (the loop starts with the check).",
    "STOP;": "End the rules now, keeping the current script.",
    "CANCEL;": "Before a step ('< step'): end the rules and don't run the step (the page says "
               "why). After a step it works like STOP.",
    "step OR step": "In a hook: the same rules after each of these steps, e.g. '> paraphrasing OR audits' "
                "(a comma works too). '< a OR b' works the same way before steps.",
}
KEYWORDS = frozenset({"RUN", "LOOP", "MAX", "UNTIL", "WHILE", "IF", "ELSE", "BREAK",
                      "CONTINUE", "REVALIDATE", "STOP", "CANCEL", "AND", "OR", "NOT",
                      "CONTAINS"})
CONTINUE_ALIASES = frozenset({"CONTINUE", "REVALIDATE"})

# Conditions: comparisons, joined with AND / OR, NOT for the opposite, ( ) to group.
# Text compares ignoring case and extra spaces; numbers compare as numbers.
RULE_OPERATORS: dict[str, str] = {
    "==": "equal ('=' works too)",
    "!=": "not equal",
    ">": "greater than",
    ">=": "greater than or equal",
    "<": "less than",
    "<=": "less than or equal",
    "CONTAINS": 'the text contains, e.g. TAG CONTAINS "READY"',
    "AND": "both (&& works too)",
    "OR": "either (|| works too)",
    "NOT": "the opposite (! works too)",
}
COMPARISONS = ("==", "!=", ">=", "<=", ">", "<", "CONTAINS")

RULE_VARIABLES: dict[str, str] = {
    "TAG": 'The status from the latest check, e.g. "READY FOR PRODUCTION" ("" before any).',
    "RATING": "The rating number from the latest check (0 if it gave none).",
    "WORDS": "Words in the current script.",
    "ITERATION": "The current pass of the innermost loop (1, 2, ...); 0 outside loops. In a "
                 "WHILE check it is the number of passes done so far.",
    "RUNS": "Rule prompts run so far in this hook.",
}

DEFAULT_MAX_LOOP = 3   # passes when a LOOP has no (MAX n)
HARD_MAX_LOOP = 10     # never more passes than this
MAX_RUNS = 20          # never more rule prompts than this per hook

# Every example here must parse (tests/test_rules.py checks).
RULE_EXAMPLES: list[tuple[str, str]] = [
    ("Validate after Audits; fix and check again until ready (the NBA Brand 2 rule).",
     ('> audits LOOP { STRICT_VALIDATION; IF (TAG == "READY FOR PRODUCTION") { BREAK; } '
      'ELSE { APPLY_CHANGES; REVALIDATE; } } UNTIL (TAG == "READY FOR PRODUCTION");')),
    ("The same, with at most 5 passes.",
     ('> audits LOOP (MAX 5) { STRICT_VALIDATION; IF (TAG == "READY FOR PRODUCTION") '
      "{ BREAK; } APPLY_CHANGES; }")),
    (("Two checks: when it's READY, also check the title; fix and start again if either "
      "fails. 6 checks / 5 fixes at most, and the last fix is always checked."),
     ('> audits LOOP (MAX 6) { STRICT_VALIDATION; IF (TAG == "READY FOR PRODUCTION") { '
      'TITLE_SATISFACTION; IF (TAG == "TRUE") { BREAK; } ELSE IF (ITERATION == 6) { STOP; } '
      'ELSE { APPLY_CHANGES; REVALIDATE; } } ELSE IF (ITERATION == 6) { STOP; } '
      'ELSE { APPLY_CHANGES; REVALIDATE; } } UNTIL (TAG == "TRUE");')),
    ("Before the first step: research first; the Outline prompt can use {{ research }}.",
     "< outline RESEARCH;"),
    ("Before and after: research before Outline, validate after Audits.",
     ('< outline RESEARCH; > audits LOOP (MAX 3) { STRICT_VALIDATION; '
      'IF (TAG == "READY FOR PRODUCTION") { BREAK; } APPLY_CHANGES; }')),
    (("Check the entries before Paraphrasing: go on, or don't paraphrase; then validate after "
      "Paraphrasing in one flow and after Audits in the other."),
     ('< paraphrasing ENTRIES_VALIDATION; IF (TAG == "CONTINUE") { STOP; } '
      'ELSE IF (TAG == "RESEARCH") { CANCEL; } '
      '> paraphrasing OR audits LOOP (MAX 6) { STRICT_VALIDATION; '
      'IF (TAG == "READY FOR PRODUCTION") { BREAK; } APPLY_CHANGES; }')),
    ("Check once; fix only if it isn't ready (no loop).",
     '> audits STRICT_VALIDATION; IF (TAG != "READY FOR PRODUCTION") { APPLY_CHANGES; }'),
    ("Keep fixing while the rating is low (ITERATION in a WHILE check = passes done).",
     ("> audits WHILE (RATING < 9 AND ITERATION < 3) { STRICT_VALIDATION; "
      "IF (RATING >= 9) { BREAK; } APPLY_CHANGES; }")),
    ("Fix the length when the script is outside the word range.",
     "> script IF (WORDS < 1600 OR WORDS > 2000) { FIX_LENGTH; }"),
    ("Rules after two steps; text before the first '>' is guidance added to every prompt.",
     ("Always write in a calm tone. > outline OUTLINE_CHECK; > audits STRICT_VALIDATION; "
      'IF (TAG CONTAINS "NOT") { APPLY_CHANGES; STRICT_VALIDATION; }')),
]

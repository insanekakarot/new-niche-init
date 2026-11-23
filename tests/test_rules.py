"""Rules after a step: the key file, the language, the engine, the admin checks and the API."""

import asyncio
import csv
import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import keys
from app.admin import scripts_config as sc
from app.cli.config import get_cli_settings
from app.main import app
from app.rules import RuleSyntaxError, is_check, parse, parse_rule_prompts, run_hook
from app.rules.engine import parse_rating, parse_tag
from app.rules.lang import Run
from app.web import projects, rule_jobs
from app.web.scripting import find_brand

client = TestClient(app)
STEPS = sc.STEP_COLUMNS
READY = "APPROVED"
BRAND_B_RULES = ('> audits LOOP {     VALIDATION;      IF (TAG == "APPROVED") {'
              '         BREAK;     } ELSE {         REVISION;         REVALIDATE;     } } '
              'UNTIL (TAG == "APPROVED");')
RULE_PROMPTS = (
    'VALIDATION = """ Review the whole script. Start your answer with a rating and one '
    "status line in a code block: STATUS - APPROVED or STATUS - NOT APPROVED. "
    'The narration must contain {{ words_range }} words. """  REVISION = """ Make the '
    'suggested fixes. Return the full revised script in a code panel. """'
)


# ------------------------------------------------------------ the key file

@pytest.mark.parametrize("description, text", keys.RULE_EXAMPLES)
def test_every_example_in_the_key_parses(description, text):
    program = parse(text, STEPS)
    assert (program.hooks or program.before) and program.prompt_names()


def test_key_lists_every_keyword_the_parser_knows():
    for word in ("LOOP", "UNTIL", "WHILE", "IF", "ELSE", "BREAK", "CONTINUE", "REVALIDATE",
                 "STOP", "RUN", "MAX"):
        assert word in keys.KEYWORDS
    assert set(keys.RULE_VARIABLES) == {"TAG", "RATING", "WORDS", "ITERATION", "RUNS"}


# ------------------------------------------------------------ the language

def test_parses_the_brand_b_rule():
    program = parse(BRAND_B_RULES, STEPS)
    assert program.guidance is None and list(program.hooks) == ["audits"]
    assert program.prompt_names() == {"VALIDATION", "REVISION"}


def test_guidance_before_the_first_hook_stays_plain_text():
    program = parse("Keep scores>8 in mind. Calm tone. > audits CHECK;", STEPS)
    assert program.guidance == "Keep scores>8 in mind. Calm tone."
    assert parse("Just guidance, no hooks.", STEPS).hooks == {}


@pytest.mark.parametrize("text, message", [
    ("> audits LOOP { CHECK; ", "close the LOOP block"),
    ("> audits IF (SCORE > 2) { CHECK; }", "Expected a value"),
    ("> audits IF TAG == 1 { CHECK; }", "Expected '\\('"),
    ("> audits LOOP (MAX 99) { CHECK; }", "use 1 to 10"),
    ("> audits", "no rules after it"),
    ("> audits CHECK; > audits FIX;", "appears twice"),
    ("> audits ELSE { CHECK; }", "ELSE can't start a rule"),
    ("> audits CHECK FIX;", "Expected ';' after CHECK"),
    ("> audits IF (TAG == \"X\") { CHECK; > script FIX;", "'}' is missing"),
])
def test_syntax_errors_say_what_is_wrong(text, message):
    with pytest.raises(RuleSyntaxError, match=message):
        parse(text, STEPS)


def test_rule_prompts_column():
    prompts, warnings = parse_rule_prompts(RULE_PROMPTS + " stray words")
    assert list(prompts) == ["VALIDATION", "REVISION"]
    assert is_check(prompts["VALIDATION"]) and not is_check(prompts["REVISION"])
    assert warnings and "stray words" in warnings[0]
    with pytest.raises(RuleSyntaxError, match="declared twice"):
        parse_rule_prompts('A = """x""" a = """y"""')


@pytest.mark.parametrize("text, tag, rating", [
    ("Overall rating: 8.5/10\nSTATUS - APPROVED", READY, 8.5),
    ("**Rating – 7**\nStatus: not approved.", "NOT APPROVED", 7.0),
    ("STATUS — APPROVED", READY, None),
    ("No status at all", "", None),
])
def test_parse_tag_and_rating(text, tag, rating):
    assert parse_tag(text) == tag and parse_rating(text) == rating


# ------------------------------------------------------------ the engine

def fake_model(ready_at=2, fail_on=None):
    """A stand-in model: checks are NOT READY until the ready_at-th check."""
    calls = []

    async def call(name, entry):
        calls.append(name)
        if name == fail_on:
            raise RuntimeError("boom")
        if name in ("VALIDATION", "CHECK"):
            n = calls.count(name)
            return f"```\nRating: {6 + n}/10\nSTATUS - {'' if n >= ready_at else 'NOT '}{READY}\n```"
        return f"```\nscript v{calls.count(name)}\n```\nnotes"

    return call, calls


PROMPTS = {"VALIDATION": "Check it. STATUS - APPROVED",
           "CHECK": "Check. STATUS - OK", "REVISION": "Fix it.", "FIX_LENGTH": "Fix length."}


def run(text, ready_at=2, script="original script"):
    call, calls = fake_model(ready_at)
    result = asyncio.run(run_hook(parse(text, STEPS).hooks["audits"], script=script,
                                  prompts=PROMPTS, call=call))
    return result, calls


def test_validate_fix_revalidate_until_ready():
    result, calls = run(BRAND_B_RULES, ready_at=2)
    assert calls == ["VALIDATION", "REVISION", "VALIDATION"]
    assert result.script == "script v1" and result.changed and result.ready
    assert result.tag == READY and result.rating == 8.0 and not result.warnings
    assert [e["kind"] for e in result.log] == ["check", "revision", "check"]
    assert [e["iteration"] for e in result.log] == [1, 1, 2]


def test_ready_on_the_first_check_keeps_the_step_script():
    result, calls = run(BRAND_B_RULES, ready_at=1)
    assert calls == ["VALIDATION"]
    assert result.script == "original script" and not result.changed and result.ready


def test_loop_stops_after_the_default_passes():
    result, calls = run(BRAND_B_RULES, ready_at=99)
    assert calls.count("VALIDATION") == keys.DEFAULT_MAX_LOOP
    assert result.script == f"script v{keys.DEFAULT_MAX_LOOP}" and not result.ready
    assert "Stopped after 3 passes" in result.warnings[0]


def test_loop_max_and_conditions():
    result, calls = run('> audits LOOP (MAX 5) { VALIDATION; IF (TAG == "APPROVED") '
                        '{ BREAK; } REVISION; }', ready_at=4)
    assert calls.count("VALIDATION") == 4 and result.script == "script v3"

    result, calls = run("> audits VALIDATION; IF (TAG CONTAINS \"NOT\" AND RATING < 9) "
                        "{ REVISION; }", ready_at=5)
    assert calls == ["VALIDATION", "REVISION"]

    result, calls = run("> audits WHILE (ITERATION < 2) { REVISION; }")
    assert calls == ["REVISION", "REVISION"]

    result, calls = run("> audits IF (WORDS < 5 OR NOT (RUNS == 0)) { FIX_LENGTH; } "
                        "ELSE IF (TAG == \"\") { STOP; REVISION; }", script="two words")
    assert calls == ["FIX_LENGTH"]


def test_stop_and_max_runs():
    result, calls = run("> audits REVISION; STOP; REVISION;")
    assert calls == ["REVISION"]
    result, calls = run("> audits LOOP (MAX 10) { REVISION; REVISION; "
                        "REVISION; }")
    assert len(calls) == keys.MAX_RUNS and "Stopped" in result.warnings[-1]


# ------------------------------------------------------------ admin

def tsv(rows, rule_prompts_col=True):
    cols = ["Research", "Outline", "Script", "Audits", "Paraphrasing", "Brand", "Steps", "Rules",
            *(["Rule Prompts"] if rule_prompts_col else []), "Inputs"]
    out = io.StringIO()
    w = csv.writer(out, delimiter="\t", lineterminator="\r\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return out.getvalue().encode()


BRAND_B_ROW = {
    "Outline": "Outline task for {{video_title}}. Competitor: {{competitor_script}}. "
               "Put the outline inside a Markdown code block.",
    "Script": 'Script task. APPROVED OUTLINE: """ {{outline}} """ Inside a code block.',
    "Audits": 'Audit. Outline << Outline used for the script >> Script << Current Script >>. '
              "Final script inside a single Markdown code block.",
    "Brand": "Brand B", "Steps": "Outline + Script + Audits",
    "Rules": BRAND_B_RULES, "Rule Prompts": RULE_PROMPTS,
    "Inputs": "outline = video_title + inspirational_script + outline_prompt "
              "script = video_title + outline + script_prompt "
              "audits = video_title + outline + audits_prompt + script + inspirational_script",
}


def test_admin_reads_rule_prompts_and_checks_the_rules():
    result = sc.parse_tsv(tsv([BRAND_B_ROW]))
    brand = result.config[0]["brand_b"]
    assert list(brand["rule_prompts"]) == ["VALIDATION", "REVISION"]
    assert brand["rules"] == BRAND_B_RULES and result.warnings == []


@pytest.mark.parametrize("change, message", [
    ({"Rule Prompts": 'VALIDATION = """ STATUS - X """'}, "doesn't declare it"),
    ({"Rules": "> audits LOOP { VALIDATION;"}, "Brand B: Rules:"),
    ({"Rule Prompts": RULE_PROMPTS + ' LOOP = """ x """'}, "name of a keyword"),
])
def test_admin_rejects_broken_rules(change, message):
    with pytest.raises(sc.TsvError, match=message):
        sc.parse_tsv(tsv([{**BRAND_B_ROW, **change}]))


def test_admin_warns_about_rule_gaps():
    row = {**BRAND_B_ROW, "Rules": BRAND_B_RULES.replace("> audits", "> research"),
           "Rule Prompts": RULE_PROMPTS + ' UNUSED = """ Uses {{nope}} """'}
    warnings = sc.parse_tsv(tsv([row])).warnings
    assert any("after 'research', but that step never runs" in w for w in warnings)
    assert any("UNUSED is declared but the Rules never run it" in w for w in warnings)
    assert any("{{nope}}" in w for w in warnings)


def test_sheet_without_rule_prompts_column_still_works():
    row = {k: v for k, v in BRAND_B_ROW.items() if k not in ("Rules", "Rule Prompts")}
    assert sc.parse_tsv(tsv([row], rule_prompts_col=False)).config[0]["brand_b"][
        "rule_prompts"] == {}


# ------------------------------------------------------------ API: rules after Audits

@pytest.fixture
def brand2(tmp_path, monkeypatch):
    cfg = sc.parse_tsv(tsv([BRAND_B_ROW])).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_CODEX_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    return cfg


def step(name, **overrides):
    inputs = {"video_title": "Day 25", "inspirational_script": "Source text"}
    body = {"brand": "brand_b", "branch": 0, "step": name, "language": "English",
            "inputs": inputs if name == "outline" else {"video_title": "Day 25"}}
    body.update(overrides)
    r = client.post("/api/scripting/run", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def flow(**audit_overrides):
    step("outline", **audit_overrides)
    step("script", **audit_overrides)
    return step("audits", **audit_overrides)


def test_brand_view_shows_rules_after_audits(brand2):
    _, view = find_brand("brand_b")
    assert view["rules"] is None and view["rules_program"].startswith("> audits LOOP")
    outline, _, audits = view["branches"][0]["steps"]
    assert audits["rules_after"] == ["VALIDATION", "REVISION"]
    assert outline["rules_after"] == []


def test_rules_run_after_audits_and_keep_the_last_script(brand2):
    audits = flow()
    assert "LOOP {" not in json.loads(audits["text"].split("\n", 1)[1])["prompt"]  # not guidance
    job = audits["rules"]
    assert job and job["step"] == "audits"

    done = client.get(f"/api/scripting/rules/{job['id']}").json()
    assert done["status"] == "done", done
    assert [(e["name"], e.get("tag")) for e in done["log"]] == [
        ("VALIDATION", "NOT APPROVED"), ("REVISION", None),
        ("VALIDATION", READY)]
    final = done["final"]
    assert final["ready"] and final["changed"] and final["text"] == "REVISED SCRIPT v1 by REVISION"
    assert done["rating"] == 9.1

    # Saved like a step result, recorded in the project: the next step would use it.
    out = get_cli_settings().output_dir / "audits" / final["file"]["name"]
    assert out.read_text(encoding="utf-8") == final["text"] + "\n"
    meta = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["final"] is True and meta["rules"]["tag"] == READY
    project = projects.load(job["project_id"])
    assert projects.step_output(project, "audits") == final["text"]
    assert project["steps"][-1]["rules"]["status"] == "done"

    # Each rule prompt went into the same conversation, with {{ words_range }} filled in.
    folder = projects.projects_dir() / job["project_id"] / "rules" / job["id"]
    sent = (folder / "01-validation.prompt.txt").read_text(encoding="utf-8")
    assert sent.startswith("# Rule: VALIDATION (after the Audits step · loop pass 1)")
    assert "must contain 1,200–1,500 words" in sent and "{{" not in sent
    assert (folder / "log.json").is_file() and (folder / "02-revision.txt").is_file()


def test_ready_at_once_keeps_the_audits_result(brand2, monkeypatch):
    monkeypatch.setenv("FAKE_READY_AT", "1")
    audits = flow()
    done = client.get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert [e["name"] for e in done["log"]] == ["VALIDATION"]
    assert done["final"]["changed"] is False
    assert done["final"]["file"]["name"] == audits["file"]["name"]
    assert done["final"]["text"] == audits["text"]


def test_rules_can_be_skipped_and_run_again(brand2, monkeypatch):
    monkeypatch.setenv("FAKE_READY_AT", "99")
    audits = flow(run_rules=False)
    assert audits["rules"] is None

    body = {"brand": "brand_b", "branch": 0, "step": "audits",
            "project_id": audits["project"]["id"]}
    first = client.post("/api/scripting/rules", json=body).json()
    done = client.get(f"/api/scripting/rules/{first['id']}").json()
    assert done["status"] == "done" and not done["ready"]
    assert "Stopped after 3 passes" in done["warnings"][0]
    assert done["final"]["text"] == "REVISED SCRIPT v3 by REVISION"

    again = client.get(f"/api/scripting/rules/{client.post('/api/scripting/rules', json=body).json()['id']}").json()
    # Starts from the latest revision; the counter carries on (v4, v5, v6).
    assert again["final"]["text"] == "REVISED SCRIPT v6 by REVISION"


def test_failed_rule_reports_the_error(brand2, monkeypatch):
    audits = flow(run_rules=False)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    body = {"brand": "brand_b", "branch": 0, "step": "audits",
            "project_id": audits["project"]["id"]}
    done = client.get(f"/api/scripting/rules/{client.post('/api/scripting/rules', json=body).json()['id']}").json()
    assert done["status"] == "failed" and "Model overloaded" in done["error"]
    assert done["log"][0]["status"] == "failed"
    assert done["final"]["changed"] is False


def test_project_is_busy_while_rules_run(brand2):
    audits = flow(run_rules=False)
    _, view = find_brand("brand_b")
    project = projects.load(audits["project"]["id"])
    job = rule_jobs.create(project=project, view=view, step=view["branches"][0]["steps"][2],
                           provider="claude", language="English", title="Day 25",
                           start_file=audits["file"]["name"], start_text="x", values={})
    job.status = "running"
    try:
        r = client.post("/api/scripting/run", json={
            "brand": "brand_b", "branch": 0, "step": "audits", "language": "English",
            "inputs": {"video_title": "Day 25"}})
        assert r.status_code == 409 and "still running the rules" in r.json()["detail"]["message"]
    finally:
        job.status = "done"


def test_rules_with_codex(brand2):
    audits = flow(provider="codex")
    done = client.get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert done["status"] == "done" and done["ready"]
    assert done["final"]["text"] == "REVISED SCRIPT v1 by REVISION"


def test_unknown_job_is_404():
    assert client.get("/api/scripting/rules/nope").status_code == 404


# ------------------------------------------------------------ admin dashboard: rules explained

def test_explain_lays_the_rule_out_line_by_line():
    from app.rules.explain import lines
    prompts, _ = parse_rule_prompts(RULE_PROMPTS)
    got = [(l["indent"], l["code"]) for l in lines(parse(BRAND_B_RULES, STEPS), prompts)]
    assert got == [
        (0, "> audits"),
        (0, "LOOP {"),
        (1, "VALIDATION;"),
        (1, 'IF (TAG == "APPROVED") {'),
        (2, "BREAK;"),
        (1, "} ELSE {"),
        (2, "REVISION;"),
        (2, "REVALIDATE;"),
        (1, "}"),
        (0, '} UNTIL (TAG == "APPROVED");'),
    ]
    notes = {l["code"]: l["note"] for l in lines(parse(BRAND_B_RULES, STEPS), prompts)}
    assert notes["VALIDATION;"].startswith("check:")
    assert notes["REVISION;"].startswith("revision:")


@pytest.mark.parametrize("description, text", keys.RULE_EXAMPLES)
def test_every_example_lays_out_and_reparses(description, text):
    from app.rules.explain import lines
    program = parse(text, STEPS)
    rebuilt = " ".join(l["code"] for l in lines(program, {}))
    if program.guidance:
        rebuilt = program.guidance + " " + rebuilt
    assert parse(rebuilt, STEPS) == program  # the layout is the same program


def test_example_run_follows_the_rules():
    from app.rules.explain import example_run
    prompts, _ = parse_rule_prompts(RULE_PROMPTS)
    run = example_run(parse(BRAND_B_RULES, STEPS).hooks["audits"], prompts, "audits")
    assert [r["what"] for r in run["rows"]] == [
        "Audits", "VALIDATION #1", "REVISION #1", "VALIDATION #2"]
    assert run["final"] == "Final Audits result = script B"


def test_dashboard_shows_rules_and_rule_prompts(brand2, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_api_key", "k")
    c = TestClient(app)
    c.post("/admin/login", data={"api_key": "k", "next": "/admin/scripts/dashboard"})
    page = c.get("/admin/scripts/dashboard").text
    assert "Rules after Audits" in page and "What it does" in page
    assert "go back to the check" in page and "Final Audits result = script B" in page
    assert "VALIDATION</span> = &#34;&#34;&#34;" in page or 'VALIDATION</span> = """' in page
    assert "{{ words_range }}" in page and "→ 1,200–1,500" in page


# ------------------------------------------------------------ two checks (validation + title)

TITLE_RULES = next(r for d, r in keys.RULE_EXAMPLES if "TITLE_CHECK" in r)
TITLE_PROMPTS = {"VALIDATION": "Check it. STATUS - APPROVED or "
                 "STATUS - NOT APPROVED",
                 "TITLE_CHECK": "Does it fulfil the title? STATUS - TRUE or STATUS - FALSE",
                 "REVISION": "Fix it."}


FENCE = "`" * 3


def run_answers(rules, answers):
    """Checks answer from  (the last answer repeats); revisions make script B, C..."""
    seq, calls, n = {k: list(v) for k, v in answers.items()}, [], [0]

    async def call(name, entry):
        if name in seq:
            tag = seq[name].pop(0) if len(seq[name]) > 1 else seq[name][0]
            calls.append(f"{name}={tag}")
            return f"{FENCE}\nSTATUS - {tag}\n{FENCE}"
        n[0] += 1
        calls.append(name)
        return f"{FENCE}\nscript {chr(65 + n[0])}\n{FENCE}"

    result = asyncio.run(run_hook(parse(rules, STEPS).hooks["audits"], script="script A",
                                  prompts=TITLE_PROMPTS, call=call))
    return result, calls


def test_title_check_runs_only_after_ready_and_true_is_ready():
    result, calls = run_answers(TITLE_RULES, {
        "VALIDATION": ["NOT APPROVED", "APPROVED"],
        "TITLE_CHECK": ["FALSE", "TRUE"]})
    assert calls == ["VALIDATION=NOT APPROVED", "REVISION",
                     "VALIDATION=APPROVED", "TITLE_CHECK=FALSE",
                     "REVISION", "VALIDATION=APPROVED",
                     "TITLE_CHECK=TRUE"]
    assert result.script == "script C" and result.tag == "TRUE" and result.ready
    assert not result.warnings


def test_title_never_true_stops_on_the_fourth_check_without_an_unchecked_fix():
    result, calls = run_answers(TITLE_RULES, {"VALIDATION": ["APPROVED"],
                                              "TITLE_CHECK": ["FALSE"]})
    assert calls.count("REVISION") == 3 and calls.count("TITLE_CHECK=FALSE") == 4
    assert calls[-1] == "TITLE_CHECK=FALSE"
    assert result.script == "script D" and not result.ready and not result.warnings


def test_passed_tags():
    from app.rules.engine import passed
    assert all(passed(t) for t in ("APPROVED", "TRUE", "yes", "PASS"))
    assert not any(passed(t) for t in ("NOT APPROVED", "FALSE", "", "READYISH"))


def test_example_run_uses_each_checks_own_tags():
    from app.rules.explain import example_run
    run = example_run(parse(TITLE_RULES, STEPS).hooks["audits"], TITLE_PROMPTS, "audits")
    assert [(r["what"], r["result"].split("  ")[0]) for r in run["rows"]] == [
        ("Audits", "→ script A"),
        ("VALIDATION #1", "→ NOT APPROVED"), ("REVISION #1", "→ script B"),
        ("VALIDATION #2", "→ APPROVED"), ("TITLE_CHECK #1", "→ FALSE"),
        ("REVISION #2", "→ script C"), ("VALIDATION #3", "→ APPROVED"),
        ("TITLE_CHECK #2", "→ TRUE")]
    assert run["rows"][-1]["ready"] and run["final"] == "Final Audits result = script C"
    assert "TITLE_CHECK: TRUE / FALSE" in run["assumption"]


@pytest.mark.parametrize(("text", "tag"), [
    ("Title Satisfied: True", "TRUE"), ("title satisfied - false", "FALSE"),
    ("Satisfied: True", "TRUE"), ("STATUS - APPROVED", "APPROVED")])
def test_title_satisfied_is_a_status_line(text, tag):
    assert parse_tag(text) == tag


def test_title_satisfied_prompt_is_a_check():
    from app.rules.prompts import is_check
    prompt = ("Does the title match the script? Answer with one line, Title Satisfied: True or "
              "Title Satisfied: False, then say why.")
    assert is_check(prompt)
    result, _calls = run_answers(TITLE_RULES, {
        "VALIDATION": ["APPROVED"], "TITLE_CHECK": ["TRUE"]})
    assert result.ready and result.script == "script A"


def test_upload_warns_when_no_check_asks_for_a_tag():
    from app.admin.scripts_config import _check_rules
    warnings = []
    cells = {"Rules": TITLE_RULES, "Rule Prompts": (
        'VALIDATION = """STATUS - APPROVED or STATUS - NOT READY"""\n'
        'TITLE_CHECK = """Is the title delivered? Answer yes or no."""\n'
        'REVISION = """Fix it."""')}
    _check_rules("Brand B", cells, {"audits"}, {}, warnings)
    assert any('compare TAG with "TRUE"' in w for w in warnings)
    warnings.clear()
    cells["Rule Prompts"] = cells["Rule Prompts"].replace(
        "Answer yes or no.", "Title Satisfied: True or Title Satisfied: False")
    _check_rules("Brand B", cells, {"audits"}, {}, warnings)
    assert not any("compare TAG" in w for w in warnings)


def test_rules_guide_page_shows_every_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_api_key", "k")
    c = TestClient(app)
    assert c.get("/admin/scripts/rules-guide", follow_redirects=False).status_code in (303, 401)
    c.post("/admin/login", data={"api_key": "k", "next": "/admin/scripts/rules-guide"})
    page = c.get("/admin/scripts/rules-guide")
    assert page.status_code == 200
    for key in [*keys.RULE_KEYWORDS, *keys.RULE_VARIABLES, *keys.BUILTIN_PLACEHOLDERS]:
        assert key.replace('"', "&#34;") in page.text or key in page.text, key
    assert "TITLE_CHECK" in page.text and "Rules guide" in page.text


def test_rules_use_the_effort_picked_for_the_step(brand2):
    step("outline")
    step("script")
    audits = step("audits", effort="max")
    assert audits["meta"]["effort"] == "max"
    done = client.get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert done["log"] and {e["effort"] for e in done["log"]} == {"max"}
    assert done["provider"] == "claude"
    again = client.post("/api/scripting/rules", json={
        "brand": "brand_b", "branch": 0, "step": "audits",
        "project_id": done["project_id"], "effort": "low"}).json()
    rerun = client.get(f"/api/scripting/rules/{again['id']}").json()
    assert {e["effort"] for e in rerun["log"]} == {"low"}


# ------------------------------------------------------------ rules before a step ('< step')

def test_parse_before_and_after_hooks():
    program = parse('Be calm. < outline RESEARCH; > audits VALIDATION; '
                    'IF (RATING < 9) { REVISION; }', STEPS)
    assert program.guidance == "Be calm."
    assert program.before == {"outline": [Run("RESEARCH")]}
    assert list(program.hooks) == ["audits"]
    assert program.prompt_names() == {"RESEARCH", "VALIDATION", "REVISION"}


@pytest.mark.parametrize("text, message", [
    ("< outline RESEARCH; < outline MORE;", "'< outline' appears twice"),
    ("< outline", "'< outline' has no rules"),
    ("> audits LOOP { CHECK; < outline RESEARCH;", "missing before the next '<'"),
])
def test_bad_before_hooks(text, message):
    with pytest.raises(RuleSyntaxError, match=re.escape(message)):
        parse(text, STEPS)


BEFORE_ROW = {
    **BRAND_B_ROW,
    "Outline": "Outline task for {{video_title}}. Research: {{ research }}. "
               "Competitor: {{competitor_script}}. Put the outline inside a Markdown code block.",
    "Rules": "< outline RESEARCH; " + BRAND_B_RULES,
    "Rule Prompts": RULE_PROMPTS + ' RESEARCH = """ Research the topic. Put the facts in a code '
                                   'block. """',
}


@pytest.fixture
def brand_before(tmp_path, monkeypatch):
    cfg = sc.parse_tsv(tsv([BEFORE_ROW])).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    return cfg


def test_view_lists_rules_before_a_step(brand_before):
    _, view = find_brand("brand_b")
    outline = view["branches"][0]["steps"][0]
    assert outline["rules_before"] == ["RESEARCH"] and outline["rules_after"] == []
    assert outline["rule_placeholders"] == ["research"]
    assert "research" not in outline["unknown_placeholders"]


def test_rules_before_the_first_step_start_the_conversation(brand_before):
    data = step("outline", new_project=True)
    before = data["rules_before"]
    assert before and before["when"] == "before" and before["status"] == "done"
    assert [e["name"] for e in before["log"]] == ["RESEARCH"]
    sent = json.loads(data["text"].split("\n", 1)[1])
    # The research result filled {{ research }}, and the step continued its conversation.
    assert "Research: REVISED SCRIPT v1 by RESEARCH." in sent["prompt"]
    assert "Just before this step, this conversation ran: RESEARCH" in sent["prompt"]
    resumed = sent["argv"][sent["argv"].index("--resume") + 1]
    assert resumed.startswith("fake-session-")
    out = get_cli_settings().output_dir / "outline" / data["file"]["name"]
    meta = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["rules_before"]["outputs"] == ["research"]
    project = projects.load(data["project"]["id"])
    assert project and project["session_id"] == resumed
    folder = projects.projects_dir() / project["id"] / "rules" / f"before-outline-{before['id']}"
    prompt = (folder / "01-research.prompt.txt").read_text(encoding="utf-8")
    assert prompt.startswith("# Rule: RESEARCH (before the Outline step)")
    assert (folder / "log.json").is_file()


def test_rules_switch_off_skips_the_rules_before(brand_before):
    data = step("outline", new_project=True, run_rules=False)
    assert data["rules_before"] is None
    sent = json.loads(data["text"].split("\n", 1)[1])
    assert "--resume" not in sent["argv"] and "{{ research }}" in sent["prompt"]


def test_a_failing_rule_before_stops_the_step(brand_before, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    body = {"brand": "brand_b", "branch": 0, "step": "outline", "language": "English",
            "new_project": True,
            "inputs": {"video_title": "Day 25", "inspirational_script": "Source text"}}
    r = client.post("/api/scripting/run", json=body)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "rules_before_failed" and "before Outline" in detail["message"]
    assert not list(projects.projects_dir().glob("*")) or all(
        not (p / "project.json").exists() for p in projects.projects_dir().iterdir())


def test_dashboard_explains_rules_before(brand_before):
    from app.rules.explain import brand_rules
    brand = brand_before[0]["brand_b"]
    view = brand_rules(brand, STEPS)
    assert view["before"] == ["outline"] and view["after"] == ["audits"]
    assert view["lines"][0]["code"] == "< outline"
    before_run = next(r for r in view["runs"] if r["when"] == "before")
    assert [r["what"] for r in before_run["rows"]] == ["RESEARCH #1", "Outline"]
    assert "{{ research }}" in before_run["final"]


def test_rule_prompts_before_a_step_get_the_inputs_and_earlier_results(tmp_path, monkeypatch):
    row = {**BEFORE_ROW,
           "Rules": "< outline RESEARCH; FACT_CHECK; " + BRAND_B_RULES,
           "Rule Prompts": RULE_PROMPTS
           + ' RESEARCH = """ Research {{ video_title }} using {{ competitor_script }}. """'
           + ' FACT_CHECK = """ Check these facts: {{ research }} for {{ brand_name }}. """'}
    cfg = sc.parse_tsv(tsv([row])).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    data = step("outline", new_project=True)
    folder = (projects.projects_dir() / data["project"]["id"] / "rules"
              / f"before-outline-{data['rules_before']['id']}")
    research = (folder / "01-research.prompt.txt").read_text(encoding="utf-8")
    assert "Research Day 25 using Source text." in research      # the step's inputs
    check = (folder / "02-fact_check.prompt.txt").read_text(encoding="utf-8")
    assert "Check these facts: REVISED SCRIPT v1 by RESEARCH for Brand B." in check


# ------------------------------------------------------------ '> a OR b' and CANCEL

def test_or_hook_runs_the_same_rules_after_each_step():
    program = parse("> paraphrasing OR audits VALIDATION; < script, outline RESEARCH;",
                    STEPS)
    assert set(program.hooks) == {"paraphrasing", "audits"}
    assert program.hooks["paraphrasing"] is program.hooks["audits"]
    assert set(program.before) == {"script", "outline"}
    with pytest.raises(RuleSyntaxError, match="appears twice"):
        parse("> audits OR audits CHECK;", STEPS)
    with pytest.raises(RuleSyntaxError, match="appears twice"):
        parse("> audits CHECK; > paraphrasing OR audits CHECK;", STEPS)


def test_cancel_ends_the_rules_and_marks_them_cancelled():
    stmts = parse('< paraphrasing CHECK; IF (TAG == "OK") { STOP; } ELSE { CANCEL; } '
                  "FIX_LENGTH;", STEPS).before["paraphrasing"]

    def answers(tag):
        async def call(name, entry):
            return f"```\nSTATUS - {tag}\n```"
        return call

    ok = asyncio.run(run_hook(stmts, script="", prompts=PROMPTS, call=answers("OK")))
    assert not ok.cancelled and [e["name"] for e in ok.log] == ["CHECK"]
    no = asyncio.run(run_hook(stmts, script="", prompts=PROMPTS, call=answers("RESEARCH")))
    assert no.cancelled and [e["name"] for e in no.log] == ["CHECK"]


def test_cancel_before_a_step_means_the_step_doesnt_run(tmp_path, monkeypatch):
    row = {**BEFORE_ROW,
           "Rules": '< outline VALIDATION; IF (TAG CONTAINS "NOT") { STOP; } '
                    "ELSE { CANCEL; } " + BRAND_B_RULES}
    cfg = sc.parse_tsv(tsv([row])).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    monkeypatch.setenv("FAKE_READY_AT", "1")     # the check says READY -> CANCEL
    body = {"brand": "brand_b", "branch": 0, "step": "outline", "language": "English",
            "new_project": True,
            "inputs": {"video_title": "Day 25", "inspirational_script": "Source text"}}
    r = client.post("/api/scripting/run", json=body)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "step_cancelled" and "Outline didn't run" in detail["message"]
    assert [e["name"] for e in detail["rules"]["log"]] == ["VALIDATION"]
    assert not list((get_cli_settings().output_dir / "outline").glob("*.txt"))
    monkeypatch.setenv("FAKE_READY_AT", "99")    # NOT READY -> STOP: the step runs
    assert client.post("/api/scripting/run", json=body).status_code == 200


def test_the_top10_paraphrasing_rules_file_works():
    from app.rules.explain import brand_rules
    rules = next(r for _, r in keys.RULE_EXAMPLES if "ENTRIES_CHECK" in r)
    prompts = {"ENTRIES_CHECK": "Check the entries. STATUS - CONTINUE or STATUS - RESEARCH",
               "VALIDATION": TITLE_PROMPTS["VALIDATION"],
               "TITLE_CHECK": TITLE_PROMPTS["TITLE_CHECK"],
               "REVISION": "Fix it."}
    program = parse(rules, STEPS)
    assert list(program.before) == ["paraphrasing"]
    assert set(program.hooks) >= {"paraphrasing", "audits"}
    view = brand_rules({"rules": rules, "rule_prompts": prompts}, STEPS)
    assert view["error"] is None
    heads = [line["code"] for line in view["lines"] if line["indent"] == 0
             and line["code"][0] in "<>"]
    assert heads == ["< paraphrasing", "> paraphrasing OR audits"]
    before = next(r for r in view["runs"] if r["when"] == "before")
    assert [r["what"] for r in before["rows"]] == ["ENTRIES_CHECK #1", "Paraphrasing"]
    assert "CONTINUE" in before["rows"][0]["result"]

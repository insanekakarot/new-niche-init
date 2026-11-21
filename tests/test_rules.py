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
READY = "READY FOR PRODUCTION"
NBA2_RULES = ('> audits LOOP {     STRICT_VALIDATION;      IF (TAG == "READY FOR PRODUCTION") {'
              '         BREAK;     } ELSE {         APPLY_CHANGES;         REVALIDATE;     } } '
              'UNTIL (TAG == "READY FOR PRODUCTION");')
RULE_PROMPTS = (
    'STRICT_VALIDATION = """ Check the complete script. Place the overall rating and production '
    "status at the very top, inside a code block only. Use exactly one of these formats: "
    "STATUS - READY FOR PRODUCTION STATUS - NOT READY FOR PRODUCTION The narration must contain "
    '{{ words_range }} words. """  APPLY_CHANGES = """ Apply all previously suggested fixes. '
    'Output only the complete revised script & put exactly inside the code panel only. """'
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

def test_parses_the_nba_brand_2_rule():
    program = parse(NBA2_RULES, STEPS)
    assert program.guidance is None and list(program.hooks) == ["audits"]
    assert program.prompt_names() == {"STRICT_VALIDATION", "APPLY_CHANGES"}


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
    assert list(prompts) == ["STRICT_VALIDATION", "APPLY_CHANGES"]
    assert is_check(prompts["STRICT_VALIDATION"]) and not is_check(prompts["APPLY_CHANGES"])
    assert warnings and "stray words" in warnings[0]
    with pytest.raises(RuleSyntaxError, match="declared twice"):
        parse_rule_prompts('A = """x""" a = """y"""')


@pytest.mark.parametrize("text, tag, rating", [
    ("Overall rating: 8.5/10\nSTATUS - READY FOR PRODUCTION", READY, 8.5),
    ("**Rating – 7**\nStatus: not ready for production.", "NOT READY FOR PRODUCTION", 7.0),
    ("STATUS — READY FOR PRODUCTION", READY, None),
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
        if name in ("STRICT_VALIDATION", "CHECK"):
            n = calls.count(name)
            return f"```\nRating: {6 + n}/10\nSTATUS - {'' if n >= ready_at else 'NOT '}{READY}\n```"
        return f"```\nscript v{calls.count(name)}\n```\nnotes"

    return call, calls


PROMPTS = {"STRICT_VALIDATION": "Check it. STATUS - READY FOR PRODUCTION",
           "CHECK": "Check. STATUS - OK", "APPLY_CHANGES": "Fix it.", "FIX_LENGTH": "Fix length."}


def run(text, ready_at=2, script="original script"):
    call, calls = fake_model(ready_at)
    result = asyncio.run(run_hook(parse(text, STEPS).hooks["audits"], script=script,
                                  prompts=PROMPTS, call=call))
    return result, calls


def test_validate_fix_revalidate_until_ready():
    result, calls = run(NBA2_RULES, ready_at=2)
    assert calls == ["STRICT_VALIDATION", "APPLY_CHANGES", "STRICT_VALIDATION"]
    assert result.script == "script v1" and result.changed and result.ready
    assert result.tag == READY and result.rating == 8.0 and not result.warnings
    assert [e["kind"] for e in result.log] == ["check", "revision", "check"]
    assert [e["iteration"] for e in result.log] == [1, 1, 2]


def test_ready_on_the_first_check_keeps_the_step_script():
    result, calls = run(NBA2_RULES, ready_at=1)
    assert calls == ["STRICT_VALIDATION"]
    assert result.script == "original script" and not result.changed and result.ready


def test_loop_stops_after_the_default_passes():
    result, calls = run(NBA2_RULES, ready_at=99)
    assert calls.count("STRICT_VALIDATION") == keys.DEFAULT_MAX_LOOP
    assert result.script == f"script v{keys.DEFAULT_MAX_LOOP}" and not result.ready
    assert "Stopped after 3 passes" in result.warnings[0]


def test_loop_max_and_conditions():
    result, calls = run('> audits LOOP (MAX 5) { STRICT_VALIDATION; IF (TAG == "READY FOR '
                        'PRODUCTION") { BREAK; } APPLY_CHANGES; }', ready_at=4)
    assert calls.count("STRICT_VALIDATION") == 4 and result.script == "script v3"

    result, calls = run("> audits STRICT_VALIDATION; IF (TAG CONTAINS \"NOT\" AND RATING < 9) "
                        "{ APPLY_CHANGES; }", ready_at=5)
    assert calls == ["STRICT_VALIDATION", "APPLY_CHANGES"]

    result, calls = run("> audits WHILE (ITERATION < 2) { APPLY_CHANGES; }")
    assert calls == ["APPLY_CHANGES", "APPLY_CHANGES"]

    result, calls = run("> audits IF (WORDS < 5 OR NOT (RUNS == 0)) { FIX_LENGTH; } "
                        "ELSE IF (TAG == \"\") { STOP; APPLY_CHANGES; }", script="two words")
    assert calls == ["FIX_LENGTH"]


def test_stop_and_max_runs():
    result, calls = run("> audits APPLY_CHANGES; STOP; APPLY_CHANGES;")
    assert calls == ["APPLY_CHANGES"]
    result, calls = run("> audits LOOP (MAX 10) { APPLY_CHANGES; APPLY_CHANGES; "
                        "APPLY_CHANGES; }")
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


NBA2_ROW = {
    "Outline": "Outline task for {{video_title}}. Competitor: {{competitor_script}}. "
               "Put the outline inside a Markdown code block.",
    "Script": 'Script task. APPROVED OUTLINE: """ {{outline}} """ Inside a code block.',
    "Audits": 'Audit. Outline << Used For Script Generation >> Script << Current Script >>. '
              "Final script inside a single Markdown code block.",
    "Brand": "NBA Brand 2", "Steps": "Outline + Script + Audits",
    "Rules": NBA2_RULES, "Rule Prompts": RULE_PROMPTS,
    "Inputs": "outline = video_title + inspirational_script + outline_prompt "
              "script = video_title + outline + script_prompt "
              "audits = video_title + outline + audits_prompt + script + inspirational_script",
}


def test_admin_reads_rule_prompts_and_checks_the_rules():
    result = sc.parse_tsv(tsv([NBA2_ROW]))
    brand = result.config[0]["nba_brand_2"]
    assert list(brand["rule_prompts"]) == ["STRICT_VALIDATION", "APPLY_CHANGES"]
    assert brand["rules"] == NBA2_RULES and result.warnings == []


@pytest.mark.parametrize("change, message", [
    ({"Rule Prompts": 'STRICT_VALIDATION = """ STATUS - X """'}, "doesn't declare it"),
    ({"Rules": "> audits LOOP { STRICT_VALIDATION;"}, "NBA Brand 2: Rules:"),
    ({"Rule Prompts": RULE_PROMPTS + ' LOOP = """ x """'}, "name of a keyword"),
])
def test_admin_rejects_broken_rules(change, message):
    with pytest.raises(sc.TsvError, match=message):
        sc.parse_tsv(tsv([{**NBA2_ROW, **change}]))


def test_admin_warns_about_rule_gaps():
    row = {**NBA2_ROW, "Rules": NBA2_RULES.replace("> audits", "> research"),
           "Rule Prompts": RULE_PROMPTS + ' UNUSED = """ Uses {{nope}} """'}
    warnings = sc.parse_tsv(tsv([row])).warnings
    assert any("after 'research', but that step never runs" in w for w in warnings)
    assert any("UNUSED is declared but the Rules never run it" in w for w in warnings)
    assert any("{{nope}}" in w for w in warnings)


def test_sheet_without_rule_prompts_column_still_works():
    row = {k: v for k, v in NBA2_ROW.items() if k not in ("Rules", "Rule Prompts")}
    assert sc.parse_tsv(tsv([row], rule_prompts_col=False)).config[0]["nba_brand_2"][
        "rule_prompts"] == {}


# ------------------------------------------------------------ API: rules after Audits

@pytest.fixture
def brand2(tmp_path, monkeypatch):
    cfg = sc.parse_tsv(tsv([NBA2_ROW])).config
    sc.config_path().parent.mkdir(parents=True, exist_ok=True)
    sc.config_path().write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "codeblock")
    monkeypatch.setenv("FAKE_CODEX_MODE", "codeblock")
    monkeypatch.setenv("FAKE_RULES_COUNTER", str(tmp_path / "rule_counts.json"))
    return cfg


def step(name, **overrides):
    inputs = {"video_title": "KD 25", "inspirational_script": "Competitor text"}
    body = {"brand": "nba_brand_2", "branch": 0, "step": name, "language": "English",
            "inputs": inputs if name == "outline" else {"video_title": "KD 25"}}
    body.update(overrides)
    r = client.post("/api/scripting/run", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def flow(**audit_overrides):
    step("outline", **audit_overrides)
    step("script", **audit_overrides)
    return step("audits", **audit_overrides)


def test_brand_view_shows_rules_after_audits(brand2):
    _, view = find_brand("nba_brand_2")
    assert view["rules"] is None and view["rules_program"].startswith("> audits LOOP")
    outline, _, audits = view["branches"][0]["steps"]
    assert audits["rules_after"] == ["STRICT_VALIDATION", "APPLY_CHANGES"]
    assert outline["rules_after"] == []


def test_rules_run_after_audits_and_keep_the_last_script(brand2):
    audits = flow()
    assert "LOOP {" not in json.loads(audits["text"].split("\n", 1)[1])["prompt"]  # not guidance
    job = audits["rules"]
    assert job and job["step"] == "audits"

    done = client.get(f"/api/scripting/rules/{job['id']}").json()
    assert done["status"] == "done", done
    assert [(e["name"], e.get("tag")) for e in done["log"]] == [
        ("STRICT_VALIDATION", "NOT READY FOR PRODUCTION"), ("APPLY_CHANGES", None),
        ("STRICT_VALIDATION", READY)]
    final = done["final"]
    assert final["ready"] and final["changed"] and final["text"] == "REVISED SCRIPT v1 by APPLY_CHANGES"
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
    sent = (folder / "01-strict_validation.prompt.txt").read_text(encoding="utf-8")
    assert sent.startswith("# Rule: STRICT_VALIDATION (after the Audits step · loop pass 1)")
    assert "must contain 1,600–2,000 words" in sent and "{{" not in sent
    assert (folder / "log.json").is_file() and (folder / "02-apply_changes.txt").is_file()


def test_ready_at_once_keeps_the_audits_result(brand2, monkeypatch):
    monkeypatch.setenv("FAKE_READY_AT", "1")
    audits = flow()
    done = client.get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert [e["name"] for e in done["log"]] == ["STRICT_VALIDATION"]
    assert done["final"]["changed"] is False
    assert done["final"]["file"]["name"] == audits["file"]["name"]
    assert done["final"]["text"] == audits["text"]


def test_rules_can_be_skipped_and_run_again(brand2, monkeypatch):
    monkeypatch.setenv("FAKE_READY_AT", "99")
    audits = flow(run_rules=False)
    assert audits["rules"] is None

    body = {"brand": "nba_brand_2", "branch": 0, "step": "audits",
            "project_id": audits["project"]["id"]}
    first = client.post("/api/scripting/rules", json=body).json()
    done = client.get(f"/api/scripting/rules/{first['id']}").json()
    assert done["status"] == "done" and not done["ready"]
    assert "Stopped after 3 passes" in done["warnings"][0]
    assert done["final"]["text"] == "REVISED SCRIPT v3 by APPLY_CHANGES"

    again = client.get(f"/api/scripting/rules/{client.post('/api/scripting/rules', json=body).json()['id']}").json()
    # Starts from the latest revision; the counter carries on (v4, v5, v6).
    assert again["final"]["text"] == "REVISED SCRIPT v6 by APPLY_CHANGES"


def test_failed_rule_reports_the_error(brand2, monkeypatch):
    audits = flow(run_rules=False)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    body = {"brand": "nba_brand_2", "branch": 0, "step": "audits",
            "project_id": audits["project"]["id"]}
    done = client.get(f"/api/scripting/rules/{client.post('/api/scripting/rules', json=body).json()['id']}").json()
    assert done["status"] == "failed" and "Model overloaded" in done["error"]
    assert done["log"][0]["status"] == "failed"
    assert done["final"]["changed"] is False


def test_project_is_busy_while_rules_run(brand2):
    audits = flow(run_rules=False)
    _, view = find_brand("nba_brand_2")
    project = projects.load(audits["project"]["id"])
    job = rule_jobs.create(project=project, view=view, step=view["branches"][0]["steps"][2],
                           provider="claude", language="English", title="KD 25",
                           start_file=audits["file"]["name"], start_text="x", values={})
    job.status = "running"
    try:
        r = client.post("/api/scripting/run", json={
            "brand": "nba_brand_2", "branch": 0, "step": "audits", "language": "English",
            "inputs": {"video_title": "KD 25"}})
        assert r.status_code == 409 and "still running the rules" in r.json()["detail"]["message"]
    finally:
        job.status = "done"


def test_rules_with_codex(brand2):
    audits = flow(provider="codex")
    done = client.get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert done["status"] == "done" and done["ready"]
    assert done["final"]["text"] == "REVISED SCRIPT v1 by APPLY_CHANGES"


def test_unknown_job_is_404():
    assert client.get("/api/scripting/rules/nope").status_code == 404


# ------------------------------------------------------------ admin dashboard: rules explained

def test_explain_lays_the_rule_out_line_by_line():
    from app.rules.explain import lines
    prompts, _ = parse_rule_prompts(RULE_PROMPTS)
    got = [(l["indent"], l["code"]) for l in lines(parse(NBA2_RULES, STEPS), prompts)]
    assert got == [
        (0, "> audits"),
        (0, "LOOP {"),
        (1, "STRICT_VALIDATION;"),
        (1, 'IF (TAG == "READY FOR PRODUCTION") {'),
        (2, "BREAK;"),
        (1, "} ELSE {"),
        (2, "APPLY_CHANGES;"),
        (2, "REVALIDATE;"),
        (1, "}"),
        (0, '} UNTIL (TAG == "READY FOR PRODUCTION");'),
    ]
    notes = {l["code"]: l["note"] for l in lines(parse(NBA2_RULES, STEPS), prompts)}
    assert notes["STRICT_VALIDATION;"].startswith("check:")
    assert notes["APPLY_CHANGES;"].startswith("revision:")


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
    run = example_run(parse(NBA2_RULES, STEPS).hooks["audits"], prompts, "audits")
    assert [r["what"] for r in run["rows"]] == [
        "Audits", "STRICT_VALIDATION #1", "APPLY_CHANGES #1", "STRICT_VALIDATION #2"]
    assert run["final"] == "Final Audits result = script B"


def test_dashboard_shows_rules_and_rule_prompts(brand2, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_api_key", "k")
    c = TestClient(app)
    c.post("/admin/login", data={"api_key": "k", "next": "/admin/scripts/dashboard"})
    page = c.get("/admin/scripts/dashboard").text
    assert "Rules after Audits" in page and "What it does" in page
    assert "go back to the check" in page and "Final Audits result = script B" in page
    assert "STRICT_VALIDATION</span> = &#34;&#34;&#34;" in page or 'STRICT_VALIDATION</span> = """' in page
    assert "{{ words_range }}" in page and "→ 1,600–2,000" in page


# ------------------------------------------------------------ two checks (validation + title)

TITLE_RULES = next(r for d, r in keys.RULE_EXAMPLES if "TITLE_SATISFACTION" in r)
TITLE_PROMPTS = {"STRICT_VALIDATION": "Check it. STATUS - READY FOR PRODUCTION or "
                 "STATUS - NOT READY FOR PRODUCTION",
                 "TITLE_SATISFACTION": "Does it fulfil the title? STATUS - TRUE or STATUS - FALSE",
                 "APPLY_CHANGES": "Fix it."}


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
        "STRICT_VALIDATION": ["NOT READY FOR PRODUCTION", "READY FOR PRODUCTION"],
        "TITLE_SATISFACTION": ["FALSE", "TRUE"]})
    assert calls == ["STRICT_VALIDATION=NOT READY FOR PRODUCTION", "APPLY_CHANGES",
                     "STRICT_VALIDATION=READY FOR PRODUCTION", "TITLE_SATISFACTION=FALSE",
                     "APPLY_CHANGES", "STRICT_VALIDATION=READY FOR PRODUCTION",
                     "TITLE_SATISFACTION=TRUE"]
    assert result.script == "script C" and result.tag == "TRUE" and result.ready
    assert not result.warnings


def test_title_never_true_stops_on_the_sixth_check_without_an_unchecked_fix():
    result, calls = run_answers(TITLE_RULES, {"STRICT_VALIDATION": ["READY FOR PRODUCTION"],
                                              "TITLE_SATISFACTION": ["FALSE"]})
    assert calls.count("APPLY_CHANGES") == 5 and calls.count("TITLE_SATISFACTION=FALSE") == 6
    assert calls[-1] == "TITLE_SATISFACTION=FALSE"
    assert result.script == "script F" and not result.ready and not result.warnings


def test_passed_tags():
    from app.rules.engine import passed
    assert all(passed(t) for t in ("READY FOR PRODUCTION", "TRUE", "yes", "PASS"))
    assert not any(passed(t) for t in ("NOT READY FOR PRODUCTION", "FALSE", "", "READYISH"))


def test_example_run_uses_each_checks_own_tags():
    from app.rules.explain import example_run
    run = example_run(parse(TITLE_RULES, STEPS).hooks["audits"], TITLE_PROMPTS, "audits")
    assert [(r["what"], r["result"].split("  ")[0]) for r in run["rows"]] == [
        ("Audits", "→ script A"),
        ("STRICT_VALIDATION #1", "→ NOT READY FOR PRODUCTION"), ("APPLY_CHANGES #1", "→ script B"),
        ("STRICT_VALIDATION #2", "→ READY FOR PRODUCTION"), ("TITLE_SATISFACTION #1", "→ FALSE"),
        ("APPLY_CHANGES #2", "→ script C"), ("STRICT_VALIDATION #3", "→ READY FOR PRODUCTION"),
        ("TITLE_SATISFACTION #2", "→ TRUE")]
    assert run["rows"][-1]["ready"] and run["final"] == "Final Audits result = script C"
    assert "TITLE_SATISFACTION: TRUE / FALSE" in run["assumption"]


@pytest.mark.parametrize(("text", "tag"), [
    ("Title Satisfied: True", "TRUE"), ("title satisfied - false", "FALSE"),
    ("Satisfied: True", "TRUE"), ("STATUS - READY FOR PRODUCTION", "READY FOR PRODUCTION")])
def test_title_satisfied_is_a_status_line(text, tag):
    assert parse_tag(text) == tag


def test_title_satisfied_prompt_is_a_check():
    from app.rules.prompts import is_check
    prompt = ("Place the result at the very top, inside a code block, using exactly one of the "
              "following formats: Title Satisfied: True Title Satisfied: False If True, ...")
    assert is_check(prompt)
    result, _calls = run_answers(TITLE_RULES, {
        "STRICT_VALIDATION": ["READY FOR PRODUCTION"], "TITLE_SATISFACTION": ["TRUE"]})
    assert result.ready and result.script == "script A"


def test_upload_warns_when_no_check_asks_for_a_tag():
    from app.admin.scripts_config import _check_rules
    warnings = []
    cells = {"Rules": TITLE_RULES, "Rule Prompts": (
        'STRICT_VALIDATION = """STATUS - READY FOR PRODUCTION or STATUS - NOT READY"""\n'
        'TITLE_SATISFACTION = """Is the title delivered? Answer yes or no."""\n'
        'APPLY_CHANGES = """Fix it."""')}
    _check_rules("NBA Brand 2", cells, {"audits"}, {}, warnings)
    assert any('compare TAG with "TRUE"' in w for w in warnings)
    warnings.clear()
    cells["Rule Prompts"] = cells["Rule Prompts"].replace(
        "Answer yes or no.", "Title Satisfied: True or Title Satisfied: False")
    _check_rules("NBA Brand 2", cells, {"audits"}, {}, warnings)
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
    assert "TITLE_SATISFACTION" in page.text and "Rules guide" in page.text


def test_rules_use_the_effort_picked_for_the_step(brand2):
    step("outline")
    step("script")
    audits = step("audits", effort="max")
    assert audits["meta"]["effort"] == "max"
    done = client.get(f"/api/scripting/rules/{audits['rules']['id']}").json()
    assert done["log"] and {e["effort"] for e in done["log"]} == {"max"}
    assert done["provider"] == "claude"
    again = client.post("/api/scripting/rules", json={
        "brand": "nba_brand_2", "branch": 0, "step": "audits",
        "project_id": done["project_id"], "effort": "low"}).json()
    rerun = client.get(f"/api/scripting/rules/{again['id']}").json()
    assert {e["effort"] for e in rerun["log"]} == {"low"}


# ------------------------------------------------------------ rules before a step ('< step')

def test_parse_before_and_after_hooks():
    program = parse('Be calm. < outline RESEARCH; > audits STRICT_VALIDATION; '
                    'IF (RATING < 9) { APPLY_CHANGES; }', STEPS)
    assert program.guidance == "Be calm."
    assert program.before == {"outline": [Run("RESEARCH")]}
    assert list(program.hooks) == ["audits"]
    assert program.prompt_names() == {"RESEARCH", "STRICT_VALIDATION", "APPLY_CHANGES"}


@pytest.mark.parametrize("text, message", [
    ("< outline RESEARCH; < outline MORE;", "'< outline' appears twice"),
    ("< outline", "'< outline' has no rules"),
    ("> audits LOOP { CHECK; < outline RESEARCH;", "missing before the next '<'"),
])
def test_bad_before_hooks(text, message):
    with pytest.raises(RuleSyntaxError, match=re.escape(message)):
        parse(text, STEPS)


BEFORE_ROW = {
    **NBA2_ROW,
    "Outline": "Outline task for {{video_title}}. Research: {{ research }}. "
               "Competitor: {{competitor_script}}. Put the outline inside a Markdown code block.",
    "Rules": "< outline RESEARCH; " + NBA2_RULES,
    "Rule Prompts": RULE_PROMPTS + ' RESEARCH = """ Research the game. Put the facts in a code '
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
    _, view = find_brand("nba_brand_2")
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
    body = {"brand": "nba_brand_2", "branch": 0, "step": "outline", "language": "English",
            "new_project": True,
            "inputs": {"video_title": "KD 25", "inspirational_script": "Competitor text"}}
    r = client.post("/api/scripting/run", json=body)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "rules_before_failed" and "before Outline" in detail["message"]
    assert not list(projects.projects_dir().glob("*")) or all(
        not (p / "project.json").exists() for p in projects.projects_dir().iterdir())


def test_dashboard_explains_rules_before(brand_before):
    from app.rules.explain import brand_rules
    brand = brand_before[0]["nba_brand_2"]
    view = brand_rules(brand, STEPS)
    assert view["before"] == ["outline"] and view["after"] == ["audits"]
    assert view["lines"][0]["code"] == "< outline"
    before_run = next(r for r in view["runs"] if r["when"] == "before")
    assert [r["what"] for r in before_run["rows"]] == ["RESEARCH #1", "Outline"]
    assert "{{ research }}" in before_run["final"]


def test_rule_prompts_before_a_step_get_the_inputs_and_earlier_results(tmp_path, monkeypatch):
    row = {**BEFORE_ROW,
           "Rules": "< outline RESEARCH; FACT_CHECK; " + NBA2_RULES,
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
    assert "Research KD 25 using Competitor text." in research      # the step's inputs
    check = (folder / "02-fact_check.prompt.txt").read_text(encoding="utf-8")
    assert "Check these facts: REVISED SCRIPT v1 by RESEARCH for NBA Brand 2." in check



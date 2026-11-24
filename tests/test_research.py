"""New-niche research: link parsing, the page-data parser, runs (fake browser) and the page."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.research import runs
from app.research import youtube as yt

# ---------------------------------------------------------------- made-up page data

PLAYER = {
    "videoDetails": {"videoId": "AAAAAAAAAA1", "title": "The Long Night", "lengthSeconds": "754",
                     "channelId": "UCexample", "author": "Sam Rivers", "viewCount": "120345",
                     "keywords": ["night", "story"], "isLiveContent": False,
                     "shortDescription": "A story. #nightstory #Story #nightstory",
                     "thumbnail": {"thumbnails": [{"url": "small.jpg", "width": 120},
                                                  {"url": "big.jpg", "width": 1280}]}},
    "microformat": {"playerMicroformatRenderer": {
        "category": "Entertainment", "publishDate": "2026-01-05T10:00:00-08:00",
        "uploadDate": "2026-01-05T10:00:00-08:00", "likeCount": "2400", "isFamilySafe": True,
        "isUnlisted": False, "availableCountries": ["US", "GB", "PK"],
        "ownerProfileUrl": "http://www.youtube.com/@samrivers"}},
}


def _lockup(vid, title, channel, views, age, length):
    return {"lockupViewModel": {
        "contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [{"thumbnailBottomOverlayViewModel": {
            "badges": [{"thumbnailBadgeViewModel": {"text": length}}]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": channel}}]},
                {"metadataParts": [{"text": {"content": views}, "accessibilityLabel": views},
                                   {"text": {"content": age}, "accessibilityLabel": age}]}]}}}}}}


DATA = {
    "contents": {"twoColumnWatchNextResults": {
        "results": {"results": {"contents": [
            {"videoPrimaryInfoRenderer": {"relativeDateText": {"simpleText": "2 days ago"}}},
            {"videoSecondaryInfoRenderer": {"owner": {"videoOwnerRenderer": {
                "subscriberCountText": {"simpleText": "1.5M subscribers"},
                "badges": [{"metadataBadgeRenderer": {"style": "BADGE_STYLE_TYPE_VERIFIED"}}]}}}},
        ]}},
        "secondaryResults": {"secondaryResults": {"results": [
            _lockup("BBBBBBBBBB2", "Day 25", "Other Channel", "1.2M views", "3 weeks ago", "10:05"),
            _lockup("BBBBBBBBBB2", "Day 25 again", "Other Channel", "1 view", "1 day ago", "1:00"),
            _lockup("CCCCCCCCCC3", "⁨Morning⁩", "Third", "980 views", "1 year ago", "1:02:03"),
        ]}},
    }},
    "engagementPanels": [{"engagementPanelSectionListRenderer": {
        "panelIdentifier": "engagement-panel-comments-section",
        "header": {"engagementPanelTitleHeaderRenderer": {"contextualInfo": {"runs": [{"text": "1,234"}]}}}}}],
    "playerOverlays": {"decoratedPlayerBarRenderer": {"chapters": [
        {"chapterRenderer": {"title": {"simpleText": "Intro"}, "timeRangeStartMillis": 0}},
        {"chapterRenderer": {"title": {"simpleText": "The night"}, "timeRangeStartMillis": 95000}}]}},
}


@pytest.mark.parametrize("link, vid", [
    ("https://www.youtube.com/watch?v=AAAAAAAAAA1&t=10s", "AAAAAAAAAA1"),
    ("https://youtu.be/AAAAAAAAAA1?si=x", "AAAAAAAAAA1"),
    ("youtube.com/shorts/AAAAAAAAAA1", "AAAAAAAAAA1"),
    ("https://m.youtube.com/live/AAAAAAAAAA1", "AAAAAAAAAA1"),
    ("AAAAAAAAAA1", "AAAAAAAAAA1"),
    ("https://example.com/watch?v=AAAAAAAAAA1", None),
    ("https://www.youtube.com/watch?v=short", None),
    ("", None),
])
def test_video_id(link, vid):
    assert yt.video_id(link) == vid


def test_numbers_and_clock():
    assert yt.number("229,159 views") == 229159
    assert yt.number("1.02M subscribers") == 1020000
    assert yt.number("10K") == 10000 and yt.number(42) == 42
    assert yt.number("No views") is None and yt.number(None) is None
    assert yt.clock(754) == "12:34" and yt.clock(3754) == "1:02:34" and yt.clock(None) == ""


def test_details_reads_every_field():
    d = yt.details(PLAYER, DATA)
    assert d["id"] == "AAAAAAAAAA1" and d["url"].endswith("v=AAAAAAAAAA1")
    assert d["title"] == "The Long Night" and d["thumbnail"] == "big.jpg"
    assert d["channel"] == {"name": "Sam Rivers", "id": "UCexample",
                            "url": "https://www.youtube.com/channel/UCexample",
                            "handle": "@samrivers", "subscribers": 1500000,
                            "subscribers_text": "1.5M subscribers", "verified": True}
    assert (d["views"], d["likes"], d["comments"]) == (120345, 2400, 1234)
    assert d["like_rate"] == 1.99 and d["comment_rate"] == 1.025
    assert (d["length_s"], d["length"], d["category"]) == (754, "12:34", "Entertainment")
    assert d["published"].startswith("2026-01-05") and d["age"] == "2 days ago"
    assert d["keywords"] == ["night", "story"]
    assert d["hashtags"] == ["#nightstory", "#Story"]          # from the description, once each
    assert d["countries"] == 3 and d["family_safe"] is True and d["live"] is False
    assert d["chapters"] == [{"start_s": 0, "title": "Intro", "start": "0:00"},
                             {"start_s": 95, "title": "The night", "start": "1:35"}]
    assert [r["id"] for r in d["related"]] == ["BBBBBBBBBB2", "CCCCCCCCCC3"]   # no repeats
    first, second = d["related"]
    assert first == {"id": "BBBBBBBBBB2", "url": "https://www.youtube.com/watch?v=BBBBBBBBBB2",
                     "title": "Day 25", "channel": "Other Channel", "views": 1200000,
                     "views_text": "1.2M views", "age": "3 weeks ago", "days_ago": 21,
                     "days_ago_max": 27, "length": "10:05"}
    assert second["title"] == "Morning" and second["length"] == "1:02:03"


def test_details_likes_from_the_like_button_and_nothing_breaks_on_empty_data():
    player = {"videoDetails": {"videoId": "AAAAAAAAAA1", "viewCount": "1000"}}
    data = {"x": {"buttonViewModel": {"accessibilityText": "like this video along with 49 other people"}}}
    assert yt.details(player, data)["likes"] == 50
    empty = yt.details({}, {})
    assert empty["id"] == "" and empty["views"] is None and empty["related"] == []
    assert empty["chapters"] == [] and empty["channel"]["name"] == ""


# ---------------------------------------------------------------- runs (fake browser)

@pytest.fixture
def fake_browser(monkeypatch):
    """Each 'browser' opens `videos` made-up videos, one tick each, and records how many
    browsers were open at the same time."""
    state = {"open": 0, "most": 0, "urls": []}

    async def explore(url, *, videos, watch_s, headless, slot, max_age_days, matcher, on_event,
                      stopped):
        state.setdefault("matchers", []).append(matcher)
        state["open"] += 1
        state.setdefault("calls", []).append({"url": url, "headless": headless, "slot": slot,
                                              "videos": videos, "watch_s": watch_s,
                                              "max_age_days": max_age_days})
        state["most"] = max(state["most"], state["open"])
        state["urls"].append(url)
        try:
            for n in range(1, videos + 1):
                if stopped():
                    break
                await on_event({"type": "opening", "n": n, "id": "AAAAAAAAAA1"})
                await on_event({"type": "watching", "n": n, "id": "AAAAAAAAAA1",
                                "second": 1, "of": watch_s, "ad": False})
                await asyncio.sleep(0.05)
                video = yt.details(PLAYER, DATA) | {"top_comments": [],
                                                    "visit": {"order": n, "from": None,
                                                              "watched_s": 1.0, "ad": False}}
                await on_event({"type": "video", "n": n, "video": video})
        finally:
            state["open"] -= 1
        return []

    monkeypatch.setattr(runs, "explore", explore)
    monkeypatch.setattr(runs, "_runs", {})
    monkeypatch.setattr(runs, "_tasks", {})
    monkeypatch.setattr(runs, "_stop", set())
    return state


def _wait(client, ids, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        got = [client.get(f"/api/research/runs/{i}").json() for i in ids]
        if all(r["status"] not in ("queued", "running") for r in got):
            return got
        time.sleep(0.05)
    raise AssertionError("runs didn't finish")


def test_links_run_at_the_same_time_each_in_its_own_browser(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    links = ["https://youtu.be/AAAAAAAAAA1", "https://youtu.be/BBBBBBBBBB2",
             "https://youtu.be/CCCCCCCCCC3"]
    with TestClient(app) as client:
        r = client.post("/api/research/runs", json={"urls": links, "videos": 3, "watch_s": 5})
        assert r.status_code == 200
        ids = [run["id"] for run in r.json()["runs"]]
        assert len(set(ids)) == 3
        done = _wait(client, ids)
        assert fake_browser["most"] == 3                     # all three at once
        assert sorted(fake_browser["urls"]) == sorted(links)
        for run in done:
            assert run["status"] == "done" and len(run["videos"]) == 3
            assert run["videos"][0]["title"] == "The Long Night"
        listed = client.get("/api/research/runs").json()["runs"]
        assert {x["id"] for x in listed} == set(ids)
        assert all(x["videos_done"] == 3 and "videos" not in x for x in listed)
        dl = client.get(f"/api/research/runs/{ids[0]}/download")
        assert dl.headers["content-disposition"].startswith("attachment")
        assert json.loads(dl.text)["id"] == ids[0]
    saved = json.loads((runs.folder() / f"{ids[0]}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "done" and len(saved["videos"]) == 3   # survives a restart


def test_more_links_than_browsers_wait_their_turn(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 2)
    links = [f"https://youtu.be/{c * 10}{n}" for c, n in zip("ABCD", "1234")]
    with TestClient(app) as client:
        ids = [r["id"] for r in client.post("/api/research/runs", json={
            "urls": links, "videos": 2, "watch_s": 1}).json()["runs"]]
        assert all(r["status"] == "done" for r in _wait(client, ids))
    assert fake_browser["most"] == 2


def test_a_saved_run_is_read_back_and_a_closed_mid_run_shows_stopped(fake_browser):
    runs.folder().mkdir(parents=True, exist_ok=True)
    (runs.folder() / "20260101-000000-abcdef.json").write_text(json.dumps({
        "id": "20260101-000000-abcdef", "url": "https://youtu.be/AAAAAAAAAA1", "status": "running",
        "videos_wanted": 5, "watch_s": 30, "created_at": 1.0, "finished_at": None, "error": None,
        "progress": {}, "videos": []}), encoding="utf-8")
    client = TestClient(app)
    run = client.get("/api/research/runs/20260101-000000-abcdef").json()
    assert run["status"] == "stopped" and "closed" in run["error"]
    assert client.get("/api/research/runs/nope").status_code == 404
    assert client.get("/api/research/runs/..%2F..%2Fsecret").status_code == 404


def test_stop_a_run(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    with TestClient(app) as client:
        rid = client.post("/api/research/runs", json={
            "urls": ["https://youtu.be/AAAAAAAAAA1"], "videos": 50, "watch_s": 1}).json()["runs"][0]["id"]
        assert client.post(f"/api/research/runs/{rid}/stop").json() == {"stopping": True}
        run = _wait(client, [rid])[0]
    assert run["status"] == "stopped" and len(run["videos"]) < 50


@pytest.mark.parametrize("body, message", [
    ({"urls": ["https://example.com/x"]}, "Not a YouTube video link"),
    ({"urls": ["  "]}, "at least one"),
    ({"urls": [f"https://youtu.be/AAAAAAAAA{n:02d}" for n in range(21)]}, "At most 20"),
])
def test_bad_requests(fake_browser, body, message):
    r = TestClient(app).post("/api/research/runs", json=body)
    assert r.status_code == 422 and message in r.json()["detail"]


def test_research_page_has_the_tool_and_others_stay_planned():
    client = TestClient(app)
    page = client.get("/research").text
    assert 'id="rs-form"' in page and 'id="rs-add"' in page and "/api/research/runs" in page
    other = client.get("/research/dashboard").text
    assert 'id="rs-form"' not in other and "Not built yet" in other


def test_show_the_browsers_per_start_or_from_the_setting(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    link = "https://youtu.be/AAAAAAAAAA1"
    with TestClient(app) as client:
        ids = []
        for show in (True, False):
            ids += [r["id"] for r in client.post("/api/research/runs", json={
                "urls": [link], "videos": 1, "watch_s": 1, "show_browser": show}).json()["runs"]]
        for headless_setting in (False, True):   # no switch sent: the setting decides
            monkeypatch.setattr(settings, "research_headless", headless_setting)
            ids += [r["id"] for r in client.post("/api/research/runs", json={
                "urls": [link], "videos": 1, "watch_s": 1}).json()["runs"]]
        done = _wait(client, ids)
    assert [r["show_browser"] for r in done] == [True, False, True, False]
    assert sorted(c["headless"] for c in fake_browser["calls"]) == [False, False, True, True]


def test_shown_browsers_get_their_own_window_place_and_the_wslg_display(monkeypatch):
    from app import desktop
    from app.research import explorer
    monkeypatch.setattr(desktop, "display_env", lambda env=None: {"DISPLAY": ":0"})
    hidden = explorer._launch_options(True, 0)
    assert hidden["headless"] is True and "env" not in hidden
    first, second = explorer._launch_options(False, 0), explorer._launch_options(False, 1)
    assert first["headless"] is False and first["env"]["DISPLAY"] == ":0"
    assert "--window-position=30,30" in first["args"] and "--window-position=100,80" in second["args"]
    assert "--mute-audio" in first["args"]


def test_window_places_are_reused(monkeypatch):
    monkeypatch.setattr(runs, "_slots", set())
    assert [runs._take_slot() for _ in range(3)] == [0, 1, 2]
    runs._slots.discard(1)
    assert runs._take_slot() == 1 and runs._take_slot() == 3


def test_the_page_switch_follows_the_setting(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(settings, "research_headless", False)
    assert 'id="rs-show" type="checkbox" class="peer sr-only" checked' in client.get("/research").text
    monkeypatch.setattr(settings, "research_headless", True)
    assert 'id="rs-show" type="checkbox" class="peer sr-only">' in client.get("/research").text


# ---------------------------------------------------------------- dates, range, medians

@pytest.mark.parametrize("age, days", [
    ("2d ago", 2), ("2 days ago", 2), ("1 day ago", 1), ("3 weeks ago", 21), ("2w ago", 14),
    ("11mo ago", 330), ("1 month ago", 30), ("1 year ago", 365), ("2y ago", 730),
    ("5 hours ago", 0), ("30 minutes ago", 0), ("Streamed 4 days ago", 4), ("", None),
    ("10M views", None),
])
def test_days_ago(age, days):
    assert yt.days_ago(age) == days


@pytest.mark.parametrize("age, oldest", [
    ("2 days ago", 2), ("5 hours ago", 1), ("3 weeks ago", 27), ("4 weeks ago", 34),
    ("1 month ago", 59), ("11mo ago", 359), ("1 year ago", 729), ("", None),
])
def test_days_ago_max(age, oldest):
    assert yt.days_ago_max(age) == oldest


def test_days_old_and_views_per_subscriber():
    from datetime import datetime, timezone
    now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    d = yt.details(PLAYER, DATA, now=now)
    assert d["days_old"] == 9                      # published 2026-01-05 18:00 UTC
    assert d["views_per_sub"] == 0.08              # 120,345 views / 1.5M subscribers
    assert [r["days_ago"] for r in d["related"]] == [21, 365]
    assert yt.days_since("not a date") is None


def test_next_video_keeps_to_the_days_set():
    from app.research.explorer import _next
    results = [{"related": [{"id": "old", "days_ago": 400}, {"id": "seen", "days_ago": 1},
                            {"id": "unknown", "days_ago": None},
                            {"id": "month", "days_ago": 30, "days_ago_max": 59},
                            {"id": "new", "days_ago": 6, "days_ago_max": 6}]}]
    assert _next(results, {"seen"}, 30) == "new"          # "1 month ago" may be 59 days: no
    assert _next(results, {"seen"}, 60) == "month"
    assert _next(results, {"seen"}, None) == "old"          # any age: YouTube's order
    assert _next(results, {"seen", "new"}, 30) is None       # nothing left in the range
    later = results + [{"related": [{"id": "fresh", "days_ago": 2}]}]
    assert _next(later, {"seen"}, 30) == "fresh"            # the newest page's list first


def test_medians_and_each_video_against_them():
    from app.research.stats import run_stats, with_stats
    vids = [{"views": 100, "likes": 10, "like_rate": 10.0, "channel": {"subscribers": 1000}},
            {"views": 5000, "likes": 50, "like_rate": 1.0, "channel": {"subscribers": 1000}},
            {"views": 300, "likes": None, "channel": {"subscribers": None}}]
    m = run_stats(vids)["median"]
    assert m["views"] == 300 and m["subscribers"] == 1000 and m["likes"] == 30
    run = with_stats({"id": "x", "videos": vids})
    assert [v["views_per_sub"] for v in run["videos"]] == [0.1, 5.0, None]
    assert run["stats"]["median"]["views_per_sub"] == 2.55   # median of 0.1 and 5.0
    assert [v["vs_median"] for v in run["videos"]] == [0.33, 16.67, 1.0]
    assert with_stats({"id": "y", "videos": []})["stats"] == {
        "count": 0, "median": {k: None for k in m}}


def test_new_defaults_and_the_days_reach_the_browser(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    link = "https://youtu.be/AAAAAAAAAA1"
    with TestClient(app) as client:
        a = client.post("/api/research/runs", json={"urls": [link]}).json()["runs"][0]
        b = client.post("/api/research/runs", json={"urls": [link], "videos": 1, "watch_s": 1,
                                                    "max_age_days": None}).json()["runs"][0]
        assert (a["videos_wanted"], a["watch_s"], a["max_age_days"]) == (10, 10, 30)
        assert b["max_age_days"] is None
        done = {r["id"]: r for r in _wait(client, [a["id"], b["id"]])}
    calls = {c["videos"]: c for c in fake_browser["calls"]}
    assert calls[10]["max_age_days"] == 30 and calls[1]["max_age_days"] is None
    run = done[a["id"]]
    assert run["stats"]["count"] == 10 and run["stats"]["median"]["views"] == 120345
    assert run["videos"][0]["vs_median"] == 1.0


def test_page_defaults():
    page = TestClient(app).get("/research").text
    assert 'id="rs-videos" type="number" min="1" max="50" value="10"' in page
    assert 'id="rs-watch" type="number" min="1" max="600" value="10"' in page
    assert 'id="rs-days" type="number" min="1" max="3650" value="30"' in page


# ---------------------------------------------------------------- settings (private file)

def test_settings_file_is_private_and_every_value_is_checked():
    from app.research import options
    assert options.path().name == "___research.json"
    assert options.path().parent == settings.scripts_config_dir.parent   # beside ___brands.json
    gitignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "___research.json" in gitignore.splitlines()
    assert options.load() == options.defaults()                  # no file yet
    saved = options.save({"videos": 7, "watch_s": "12", "max_age_days": 0, "matching": "ai",
                          "provider": "codex", "effort": "ultra", "model": " gpt-x ",
                          "max_browsers": 99, "min_words": "x", "show_browser": "yes"})
    assert saved["videos"] == 7 and saved["watch_s"] == 12 and saved["max_age_days"] == 0
    assert (saved["matching"], saved["provider"], saved["effort"], saved["model"]) == (
        "ai", "codex", "ultra", "gpt-x")
    d = options.defaults()
    assert saved["max_browsers"] == d["max_browsers"] and saved["min_words"] == d["min_words"]
    assert saved["show_browser"] == d["show_browser"]            # not a true/false: default
    assert options.load() == saved
    assert options.clean({"provider": "claude", "effort": "ultra"})["effort"] == "low"
    options.path().write_text("{broken", encoding="utf-8")
    assert options.load() == options.defaults()                  # a broken file never breaks a run
    assert options.describe(saved) == "AI only · Codex (gpt-x, ultra effort) · same niche"
    assert options.clean({"strictness": "topic"})["strictness"] == "topic"
    assert options.clean({"strictness": "x"})["strictness"] == "niche"
    assert options.describe(options.clean({"matching": "words"})) == "Words only"


def test_settings_api_and_page():
    client = TestClient(app)
    got = client.get("/api/research/settings").json()
    assert got["values"] == got["defaults"] and got["file"] == "___research.json"
    r = client.post("/api/research/settings", json={"values": {**got["values"], "videos": 3,
                                                               "matching": "words"}})
    assert r.status_code == 200 and r.json()["values"]["videos"] == 3
    assert r.json()["text"] == "Words only"
    page = client.get("/research/settings").text
    assert 'id="st-form"' in page and 'value="words" class="mt-1 accent-[#c8322b]" checked' in page
    research = client.get("/research").text                      # the Research page follows it
    assert 'id="rs-videos" type="number" min="1" max="50" value="3"' in research
    assert "Words only" in research and 'href="/research/settings"' in research


def test_runs_start_from_the_settings_and_keep_the_matching(fake_browser, monkeypatch):
    from app.research import options
    from app.research.niche import Matcher
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    options.save({**options.defaults(), "videos": 2, "watch_s": 3, "max_age_days": 7,
                  "matching": "words_ai", "provider": "codex"})
    with TestClient(app) as client:
        a = client.post("/api/research/runs", json={"urls": ["https://youtu.be/AAAAAAAAAA1"]}).json()["runs"][0]
        assert (a["videos_wanted"], a["watch_s"], a["max_age_days"]) == (2, 3, 7)
        assert a["matching"]["matching"] == "words_ai" and a["matching"]["provider"] == "codex"
        assert a["matching"]["text"].startswith("Words + AI · Codex")
        _wait(client, [a["id"]])
        options.save({**options.defaults(), "matching": "off"})
        b = client.post("/api/research/runs", json={"urls": ["https://youtu.be/AAAAAAAAAA1"],
                                                    "videos": 1}).json()["runs"][0]
        _wait(client, [b["id"]])
    first, second = fake_browser["matchers"]
    assert isinstance(first, Matcher) and first.mode == "words_ai" and second is None


# ---------------------------------------------------------------- the niche

START = {"title": "Why Night Trains Disappeared | Railway History", "keywords": ["night trains", "railway"],
         "hashtags": ["#RailwayHistory", "#trains"], "description": "", "channel": {"name": "Rails"},
         "related": []}
CANDS = [
    {"id": "R1", "title": "The Last Night Train of Europe", "channel": "Rails", "age": "2 days ago"},
    {"id": "R2", "title": "Top 10 Shocking Cooking Secrets", "channel": "Kitchen", "age": "1 day ago"},
    {"id": "R3", "title": "Railway History: Steam to Electric", "channel": "Rails", "age": "3 days ago"},
]


def test_words_and_niche_words():
    from app.research.niche import profile, shared, words
    assert words("The SHOCKING Truth about Night Trains!!") == ["night", "train"]
    assert words("Kya hai yeh? 2026 Railway") == ["railway"]
    prof = profile(START)
    assert prof["railway"] >= 3 and prof["train"] >= 3 and "history" in prof
    assert shared(prof, "The Last Night Train of Europe") == ["night", "train"]
    assert shared(prof, "Top 10 Shocking Cooking Secrets") == []


def _matcher(mode, ask=None, **extra):
    from app.research import options
    from app.research.niche import Matcher
    return Matcher(options.clean({"matching": mode, **extra}), ask=ask)


def test_words_only_keeps_titles_with_enough_niche_words():
    m = _matcher("words", min_words=2)
    first = asyncio.run(m.check(START, CANDS, start=True))
    assert first.match is True and first.via == "start" and first.picks == ["R1", "R3"]
    assert list(first.rejected) == ["R2"] and "0 of the 2" in first.rejected["R2"]
    strict = _matcher("words", min_words=3)
    assert asyncio.run(strict.check(START, CANDS, start=True)).picks == []
    assert first.niche["keywords"]
    later = asyncio.run(m.check({**START, "title": "Cooking pasta at home", "keywords": []}, [], start=False))
    assert later.match is False and later.via == "words"


def test_ai_names_the_niche_and_picks_by_number():
    asked = []

    async def ask(provider, prompt, *, model, effort):
        asked.append((provider, prompt, model, effort))
        if len(asked) == 1:
            return ('Sure: ```json\n{"niche": "History › Railways", "topic": "Night trains",'
                    ' "description": "Rail history.",'
                    ' "keywords": ["railway", "locomotive"], "same_niche": [2, 1, 9],'
                    ' "other": [{"n": 3, "why": "Cooking, not railways"}]}\n```')
        return '{"niche": "Travel › Trains", "same": false, "why": "A travel vlog.", "same_niche": [1]}'

    m = _matcher("words_ai", ask=ask, provider="codex", model="m1", effort="high")
    first = asyncio.run(m.check(START, CANDS, start=True))
    assert first.picks == ["R3", "R1"] and first.rejected == {"R2": "Cooking, not railways"}
    assert first.niche == {"label": "History › Railways", "topic": "Night trains",
                           "description": "Rail history.", "keywords": ["railway", "locomotive"],
                           "strictness": "niche"}
    assert "another country, person or case in the same niche counts" in asked[0][1]
    provider, prompt, model, effort = asked[0]
    assert (provider, model, effort) == ("codex", "m1", "high")
    assert "1. The Last Night Train of Europe" in prompt     # ranked by words: R1, R3 then R2
    assert "2. Railway History" in prompt and "3. Top 10 Shocking Cooking" in prompt
    assert prompt.index("Railway History") < prompt.index("Cooking")
    assert m.words["locomotive"] >= 3                          # the AI's keywords join the words
    later = asyncio.run(m.check({**START, "title": "Train trip vlog"}, CANDS[:1], start=False))
    assert later.match is False and later.label == "Travel › Trains" and later.picks == []
    assert "Niche: History › Railways" in asked[1][1]


def test_when_the_ai_fails_words_decide():
    from app.cli import CliError

    async def ask(*a, **k):
        raise CliError("Claude CLI not found")

    m = _matcher("ai", ask=ask, min_words=1)
    first = asyncio.run(m.check(START, CANDS, start=True))
    assert first.via.startswith("words (AI failed: Claude CLI not found")
    assert first.picks == ["R1", "R3"] and list(first.rejected) == ["R2"]


def test_parse_json():
    from app.research.ai import parse_json
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('Here you go:\n```json\n{"a": [1, 2]}\n```\nDone.') == {"a": [1, 2]}
    assert parse_json('The answer {"a": {"b": 2}} as asked') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_checks_skip_other_niches_and_the_run_ends_with_a_warning():
    from app.research.explorer import _apply_check, _end
    from app.research.niche import Check
    events = []
    approved, rejected, in_niche = [], {}, []
    known = {r["id"]: r for r in CANDS}
    video = {"id": "S", "related": CANDS}
    chk = Check(label="History › Railways", match=True, via="AI", picks=["R3", "R1"],
                rejected={"R2": "Cooking"}, niche={"label": "History › Railways"})
    asyncio.run(_apply_check(chk, video, approved, rejected, known, in_niche, events.append))
    assert approved == ["R3", "R1"] and rejected == {"R2": "Cooking"} and in_niche == [video]
    assert video["niche"] == {"label": "History › Railways", "topic": "", "match": True,
                              "reason": "", "via": "AI"}
    off = next(e for e in events if e["type"] == "off_niche")["items"]
    assert off[0]["title"] == "Top 10 Shocking Cooking Secrets" and off[0]["why"] == "Cooking"
    other = {"id": "X", "related": []}
    asyncio.run(_apply_check(Check(match=False, picks=["Z"]), other, approved, rejected, known,
                             in_niche, events.append))
    assert approved == ["R3", "R1"] and other not in in_niche     # another niche: not followed

    class M:
        niche = {"label": "History › Railways"}
    end = _end(M(), 30)
    assert end["warning"] is True and "History › Railways" in end["reason"] and "30 days" in end["reason"]
    assert _end(None, None)["warning"] is False


# ---------------------------------------------------------------- the AI calls stay apart

def test_research_ai_uses_its_own_instructions_and_never_a_scripting_slot(monkeypatch):
    from app.cli import common
    from app.research import ai
    sent = {}

    async def fake_process(argv, stdin, **kw):
        sent["argv"], sent["stdin"] = argv, stdin
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": '{"ok": true}', "session_id": "s"})
        return out.encode(), b"", 0, 0.1

    monkeypatch.setattr(ai, "run_process", fake_process)

    async def go():
        held = common.run_slot("claude", 2, "Claude")   # Scripting's Claude slots: all taken
        async with held:
            async with common.run_slot("claude", 2, "Claude"):
                return await ai.ask("claude", "Which niche?", effort="low")

    assert json.loads(asyncio.run(go())) == {"ok": True}
    argv = sent["argv"]
    system = argv[argv.index("--system-prompt") + 1]
    assert system == ai.RESEARCH_SYSTEM                    # never the brands file's writer prompt
    assert argv[argv.index("--tools") + 1] == "" and "--allowedTools" not in argv
    assert argv[argv.index("--effort") + 1] == "low"


# ---------------------------------------------------------------- delete a run (type DELETE)

def test_delete_needs_the_word_and_removes_the_results(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    with TestClient(app) as client:
        rid = client.post("/api/research/runs", json={
            "urls": ["https://youtu.be/AAAAAAAAAA1"], "videos": 1, "watch_s": 1}).json()["runs"][0]["id"]
        _wait(client, [rid])
        saved = runs.folder() / f"{rid}.json"
        assert saved.is_file()
        for word in ("", "delete", "Delete", "DELETE NOW", "yes"):
            r = client.post(f"/api/research/runs/{rid}/delete", json={"confirm": word})
            assert r.status_code == 422 and r.json()["detail"] == "Type DELETE to confirm."
        assert client.post(f"/api/research/runs/{rid}/delete", json={}).status_code == 422
        assert saved.is_file() and client.get(f"/api/research/runs/{rid}").status_code == 200
        r = client.post(f"/api/research/runs/{rid}/delete", json={"confirm": "DELETE"})
        assert r.status_code == 200 and r.json() == {"deleted": rid}
        assert not saved.exists()
        assert client.get(f"/api/research/runs/{rid}").status_code == 404
        assert rid not in {x["id"] for x in client.get("/api/research/runs").json()["runs"]}
        again = client.post(f"/api/research/runs/{rid}/delete", json={"confirm": "DELETE"})
        assert again.status_code == 404


def test_a_saved_run_from_an_earlier_session_can_be_deleted_and_paths_are_refused(fake_browser):
    runs.folder().mkdir(parents=True, exist_ok=True)
    old = runs.folder() / "20260101-000000-abcdef.json"
    old.write_text(json.dumps({"id": "20260101-000000-abcdef", "url": "u", "status": "done",
                               "videos_wanted": 1, "watch_s": 1, "created_at": 1.0,
                               "videos": []}), encoding="utf-8")
    outside = runs.folder().parent / "keep.json"
    outside.write_text("{}", encoding="utf-8")
    client = TestClient(app)
    for bad in ("..%2Fkeep", "..", "a.b", "x" * 80):
        assert client.post(f"/api/research/runs/{bad}/delete",
                           json={"confirm": "DELETE"}).status_code in (404, 405)
    assert outside.is_file()
    assert client.post("/api/research/runs/20260101-000000-abcdef/delete",
                       json={"confirm": "DELETE"}).status_code == 200
    assert not old.exists() and outside.is_file()


def test_a_run_that_is_still_going_must_be_stopped_first(fake_browser, monkeypatch):
    monkeypatch.setattr(settings, "research_max_browsers", 4)
    with TestClient(app) as client:
        rid = client.post("/api/research/runs", json={
            "urls": ["https://youtu.be/AAAAAAAAAA1"], "videos": 50, "watch_s": 1}).json()["runs"][0]["id"]
        r = client.post(f"/api/research/runs/{rid}/delete", json={"confirm": "DELETE"})
        assert r.status_code == 409 and "Stop it first" in r.json()["detail"]
        client.post(f"/api/research/runs/{rid}/stop")
        _wait(client, [rid])
        assert client.post(f"/api/research/runs/{rid}/delete",
                           json={"confirm": "DELETE"}).status_code == 200


def test_pages_have_the_delete_popup_and_the_effort_dropdown():
    client = TestClient(app)
    page = client.get("/research").text
    assert '<dialog id="rs-delete"' in page and 'id="rs-delete-word"' in page
    assert 'id="rs-delete-ok" disabled' in page and "Type <b" in page and ">DELETE</b>" in page
    assert "data-delete=" in page and "Delete</button>" in page          # icon + word
    st = client.get("/research/settings").text
    assert 'id="effort-panel" role="listbox"' in st and '<input type="hidden" id="effort">' in st
    assert "<select" not in st

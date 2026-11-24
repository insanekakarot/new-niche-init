"""New-niche research: link parsing, the page-data parser, runs (fake browser) and the page."""

import asyncio
import json
import time

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
                     "views_text": "1.2M views", "age": "3 weeks ago", "length": "10:05"}
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

    async def explore(url, *, videos, watch_s, headless, on_event, stopped):
        state["open"] += 1
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

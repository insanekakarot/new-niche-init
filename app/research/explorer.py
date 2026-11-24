"""Explore YouTube from one video in a real browser (Playwright + Chromium).

Each run gets its own Playwright and its own browser, so several runs go at the same time
without sharing anything. From the start video it:

  1. opens the video (English page: hl=en), dismisses a cookie/consent prompt if one shows;
  2. plays it muted for `watch_s` seconds (an ad's Skip button is pressed when it shows),
     scrolling down meanwhile so the comments load;
  3. reads the page's data (app/research/youtube.py) and the top comments;
  4. opens the first related video it hasn't opened yet, until `videos` have been opened.
     With max_age_days, only related videos published within that many days are opened
     (their age is shown beside them: "3 days ago"); the start video is always opened.

Install once:  pip install -e ".[research]"  &&  python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable

from app.research import youtube as yt

Event = Callable[[dict], Awaitable[None] | None]

_LAUNCH_ARGS = ["--autoplay-policy=no-user-gesture-required", "--mute-audio",
                "--disable-dev-shm-usage"]
_CONSENT = ['button[aria-label^="Accept all"]', 'button[aria-label^="Reject all"]',
            'form[action*="consent"] button']

# Runs in the page: the state of the player and the video.
_PLAYER_JS = """() => {
  const v = document.querySelector('video');
  const p = document.querySelector('#movie_player');
  return {t: v ? v.currentTime : null, paused: v ? v.paused : null,
          ad: !!(p && p.classList.contains('ad-showing'))};
}"""

_PLAY_JS = """() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; if (v.paused) v.play().catch(() => {}); }
}"""

# The top comments, once they have loaded (after scrolling down).
_COMMENTS_JS = """(limit) => {
  const count = document.querySelector('ytd-comments-header-renderer #count');
  const out = [];
  for (const c of document.querySelectorAll('ytd-comment-thread-renderer')) {
    if (out.length >= limit) break;
    const q = (s) => { const e = c.querySelector(s); return e ? e.textContent.trim() : ''; };
    const text = q('#content-text');
    if (!text) continue;
    out.push({author: q('#author-text'), text, likes: q('#vote-count-middle'),
              age: q('#published-time-text')});
  }
  return {count: count ? count.textContent.trim() : '', top: out};
}"""


class ExploreError(RuntimeError):
    """The start link isn't a YouTube video, or the browser can't start."""


async def _emit(on_event: Event | None, event: dict) -> None:
    if on_event:
        r = on_event(event)
        if asyncio.iscoroutine(r):
            await r


def _launch_options(headless: bool, slot: int) -> dict:
    """Chromium launch options. A shown browser gets its own window, set off from the other
    runs' windows (slot 0, 1, 2...), and WSLg's display if the server's shell has none."""
    if headless:
        return {"headless": True, "args": _LAUNCH_ARGS}
    from app.desktop import display_env  # no pywebview needed: plain functions

    x, y = 30 + (slot % 6) * 70, 30 + (slot % 6) * 50
    return {"headless": False, "env": {**os.environ, **display_env()},
            "args": [*_LAUNCH_ARGS, f"--window-position={x},{y}", "--window-size=1280,900"]}


async def explore(url: str, *, videos: int = 10, watch_s: int = 10, headless: bool = True,
                  slot: int = 0, max_age_days: int | None = None, matcher=None,
                  on_event: Event | None = None,
                  stopped: Callable[[], bool] = lambda: False) -> list[dict]:
    """Open `videos` videos starting at `url`, watching each for `watch_s` seconds.

    headless=False shows the browser window (slot sets it off from other runs' windows).
    max_age_days: open only related videos published within that many days.
    matcher (app/research/niche.Matcher): keep to the start video's niche; related videos
    of another niche are skipped without opening. None: the first related video, as is.
    on_event gets {"type": "opening" | "watching" | "video" | "skipped" | "niche" |
    "off_niche" | "end", ...}.
    Returns the details of every opened video (app/research/youtube.details + visit info).
    """
    start = yt.video_id(url)
    if not start:
        raise ExploreError("Not a YouTube video link.")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ExploreError('Playwright isn\'t installed: pip install -e ".[research]" and '
                           "python -m playwright install chromium") from exc

    results: list[dict] = []
    opened: set[str] = set()
    in_niche: list[dict] = []            # opened videos whose related lists feed the next pick
    approved: list[str] = []             # related ids the matcher picked, best first
    rejected: dict[str, str] = {}        # related ids skipped as another niche -> why
    known: dict[str, dict] = {}          # every related video seen, by id
    next_id, came_from = start, None

    def in_range(r: dict) -> bool:
        return _surely_within(r, max_age_days)

    def candidates(video: dict) -> list[dict]:
        """Related videos not opened or skipped yet: this video's first, then earlier ones."""
        out, seen = [], set()
        for v in [video, *reversed(in_niche)]:
            for r in v.get("related") or []:
                if r["id"] in opened or r["id"] in rejected or r["id"] in seen or not in_range(r):
                    continue
                seen.add(r["id"])
                out.append(r)
        return out

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(**_launch_options(headless, slot))
        except Exception as exc:  # noqa: BLE001 - missing browser / system libraries
            raise ExploreError(f"The browser couldn't start: {str(exc).splitlines()[0]} "
                               "(run: python -m playwright install chromium)") from exc
        try:
            # A shown window: the page follows the window's size; hidden: a fixed size.
            context = await browser.new_context(
                locale="en-US",
                **({"viewport": {"width": 1280, "height": 900}} if headless
                   else {"no_viewport": True}))
            page = await context.new_page()
            while next_id and len(results) < videos and not stopped():
                opened.add(next_id)
                n = len(results) + 1
                is_start = not results
                check = None
                if matcher is not None:
                    def check(v, _start=is_start):
                        return matcher.check(v, candidates(v), _start)
                await _emit(on_event, {"type": "opening", "n": n, "id": next_id})
                try:
                    video = await _visit(page, next_id, n, watch_s, on_event, stopped, check)
                except Exception as exc:  # noqa: BLE001 - one bad video doesn't end the run
                    await _emit(on_event, {"type": "skipped", "n": n, "id": next_id,
                                           "error": str(exc).splitlines()[0][:300]})
                    if not results:
                        raise ExploreError(f"The start video couldn't be opened: "
                                           f"{str(exc).splitlines()[0][:300]}") from exc
                    video = None
                if video:
                    video["visit"]["from"] = came_from
                    video["visit"]["in_range"] = (max_age_days is None or (
                        video["days_old"] is not None and video["days_old"] <= max_age_days))
                    for r in video.get("related") or []:
                        known.setdefault(r["id"], r)
                    result = video.pop("_check", None)
                    if matcher is not None:
                        await _apply_check(result, video, approved, rejected, known, in_niche,
                                           on_event)
                    results.append(video)
                    await _emit(on_event, {"type": "video", "n": n, "video": video})
                    came_from = video["id"]
                if matcher is None:
                    next_id = _next(results, opened, max_age_days)
                else:
                    next_id = next((a for a in approved if a not in opened), None)
                if next_id is None and len(results) < videos and not stopped():
                    await _emit(on_event, _end(matcher, max_age_days))
        finally:
            await browser.close()
    return results


async def _apply_check(result, video: dict, approved: list[str], rejected: dict[str, str],
                       known: dict[str, dict], in_niche: list[dict], on_event) -> None:
    """Record a niche check on the video and update what to open / skip next."""
    if result is None:                    # the check itself failed: keep going by YouTube's list
        video["niche"] = video.get("niche") or {"label": "", "match": None, "reason": "",
                                                "via": "not checked"}
        in_niche.append(video)
        approved[:0] = [r["id"] for r in video.get("related") or [] if r["id"] not in approved]
        return
    video["niche"] = result.as_dict()
    if result.niche:
        await _emit(on_event, {"type": "niche", "niche": result.niche})
    skipped = []
    for vid, why in result.rejected.items():
        if vid in rejected:
            continue
        rejected[vid] = why
        r = known.get(vid, {})
        skipped.append({"id": vid, "url": yt.watch_url(vid), "title": r.get("title", ""),
                        "channel": r.get("channel", ""), "age": r.get("age", ""),
                        "why": why, "via": result.via, "seen_on": video["id"]})
    if skipped:
        await _emit(on_event, {"type": "off_niche", "items": skipped})
    if result.match is not False:
        in_niche.append(video)
        approved[:] = result.picks + [a for a in approved if a not in result.picks]


def _end(matcher, max_age_days: int | None) -> dict:
    days = f" from the last {max_age_days} days" if max_age_days else ""
    if matcher is None:
        return {"type": "end", "reason": f"No more related videos{days}", "warning": False}
    label = (matcher.niche or {}).get("label")
    niche = f" in the niche “{label}”" if label else " in the start video's niche"
    return {"type": "end", "warning": True,
            "reason": f"No related video left{niche}{days}; the others were skipped as "
                      "another niche"}


def _next(results: list[dict], opened: set[str], max_age_days: int | None) -> str | None:
    """The next video: the first related one not opened yet, newest page's list first. With
    max_age_days, only those whose age ('3 days ago') is within it (unknown age: skipped)."""
    for v in reversed(results):
        for r in v["related"]:
            if r["id"] in opened:
                continue
            if not _surely_within(r, max_age_days):
                continue
            return r["id"]
    return None


def _surely_within(r: dict, max_age_days: int | None) -> bool:
    """A related video is in the date range only if the oldest its age ('1 month ago' = up
    to 59 days) can be is within it. Unknown age: not in range."""
    if max_age_days is None:
        return True
    oldest = r.get("days_ago_max", r.get("days_ago"))
    return oldest is not None and oldest <= max_age_days


async def _visit(page, vid: str, n: int, watch_s: int, on_event: Event | None,
                 stopped: Callable[[], bool], check=None) -> dict:
    """Open, watch and read one video. `check(video)` (the niche check) starts as soon as
    the page's data is read and runs while the video plays; its result is video["_check"]."""
    opened_at = time.time()
    await page.goto(yt.watch_url(vid) + "&hl=en&persist_hl=1", wait_until="domcontentloaded",
                    timeout=60_000)
    await _dismiss_consent(page)
    await page.wait_for_function("() => window.ytInitialPlayerResponse && window.ytInitialData",
                                 timeout=30_000)
    raw = await page.evaluate("() => JSON.stringify({p: window.ytInitialPlayerResponse,"
                              " d: window.ytInitialData})")
    data = json.loads(raw)
    video = yt.details(data["p"], data["d"])
    task = asyncio.ensure_future(check(video)) if check else None

    try:
        # Watch: keep the video playing for watch_s seconds; scroll down to load the comments.
        ad_seen, watched, last_t = False, 0.0, None
        began = time.monotonic()
        while (elapsed := time.monotonic() - began) < watch_s and not stopped():
            await page.evaluate(_PLAY_JS)
            state = await page.evaluate(_PLAYER_JS)
            if state["ad"]:
                ad_seen = True
                skip = page.locator(".ytp-skip-ad-button, .ytp-ad-skip-button-modern").first
                if await skip.is_visible():
                    await skip.click(timeout=2_000)
            elif state["t"] is not None:
                if last_t is not None and 0 < state["t"] - last_t < 3:
                    watched += state["t"] - last_t
                last_t = state["t"]
            if int(elapsed) in (3, 12):
                await page.mouse.wheel(0, 900)
            await _emit(on_event, {"type": "watching", "n": n, "id": vid,
                                   "second": min(int(elapsed) + 1, watch_s), "of": watch_s,
                                   "ad": state["ad"]})
            await asyncio.sleep(1)

        comments = await page.evaluate(_COMMENTS_JS, 5)
        if video["comments"] is None:
            video["comments"] = yt.number(comments["count"])
        video["top_comments"] = comments["top"]
        video["visit"] = {"order": n, "from": None, "opened_at": opened_at,
                          "watched_s": round(watched, 1), "ad": ad_seen}
        if task is not None:
            if not task.done():
                await _emit(on_event, {"type": "watching", "n": n, "id": vid, "second": watch_s,
                                       "of": watch_s, "ad": False, "checking": True})
            while not task.done() and not stopped():
                await asyncio.sleep(0.5)
            if task.done():
                try:
                    video["_check"] = task.result()
                except Exception as exc:  # noqa: BLE001 - a broken check never ends the run
                    video["niche"] = {"label": "", "match": None, "reason": "",
                                      "via": f"check failed: {str(exc).splitlines()[0][:160]}"}
                    video["_check"] = None
        return video
    finally:
        if task is not None and not task.done():
            task.cancel()


async def _dismiss_consent(page) -> None:
    if "consent." not in page.url:
        for sel in _CONSENT:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=3_000)
                return
        return
    for sel in _CONSENT:  # the full-page consent screen (consent.youtube.com)
        btn = page.locator(sel).first
        if await btn.count():
            await btn.click(timeout=5_000)
            await page.wait_for_url("**/watch?**", timeout=20_000)
            return

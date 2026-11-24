"""Explore YouTube from one video in a real browser (Playwright + Chromium).

Each run gets its own Playwright and its own browser, so several runs go at the same time
without sharing anything. From the start video it:

  1. opens the video (English page: hl=en), dismisses a cookie/consent prompt if one shows;
  2. plays it muted for `watch_s` seconds (an ad's Skip button is pressed when it shows),
     scrolling down meanwhile so the comments load;
  3. reads the page's data (app/research/youtube.py) and the top comments;
  4. opens the first related video it hasn't opened yet, until `videos` have been opened.

Install once:  pip install -e ".[research]"  &&  python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import json
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


async def explore(url: str, *, videos: int = 5, watch_s: int = 30, headless: bool = True,
                  on_event: Event | None = None,
                  stopped: Callable[[], bool] = lambda: False) -> list[dict]:
    """Open `videos` videos starting at `url`, watching each for `watch_s` seconds.

    on_event gets {"type": "opening" | "watching" | "video" | "skipped", ...} as it goes.
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
    next_id, came_from = start, None
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
        except Exception as exc:  # noqa: BLE001 - missing browser / system libraries
            raise ExploreError(f"The browser couldn't start: {str(exc).splitlines()[0]} "
                               "(run: python -m playwright install chromium)") from exc
        try:
            context = await browser.new_context(locale="en-US",
                                                viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            while next_id and len(results) < videos and not stopped():
                opened.add(next_id)
                n = len(results) + 1
                await _emit(on_event, {"type": "opening", "n": n, "id": next_id})
                try:
                    video = await _visit(page, next_id, n, watch_s, on_event, stopped)
                except Exception as exc:  # noqa: BLE001 - one bad video doesn't end the run
                    await _emit(on_event, {"type": "skipped", "n": n, "id": next_id,
                                           "error": str(exc).splitlines()[0][:300]})
                    if not results:
                        raise ExploreError(f"The start video couldn't be opened: "
                                           f"{str(exc).splitlines()[0][:300]}") from exc
                    video = None
                if video:
                    video["visit"]["from"] = came_from
                    results.append(video)
                    await _emit(on_event, {"type": "video", "n": n, "video": video})
                    came_from = video["id"]
                pool = [r["id"] for v in reversed(results) for r in v["related"]]
                next_id = next((i for i in pool if i not in opened), None)
        finally:
            await browser.close()
    return results


async def _visit(page, vid: str, n: int, watch_s: int, on_event: Event | None,
                 stopped: Callable[[], bool]) -> dict:
    opened_at = time.time()
    await page.goto(yt.watch_url(vid) + "&hl=en&persist_hl=1", wait_until="domcontentloaded",
                    timeout=60_000)
    await _dismiss_consent(page)
    await page.wait_for_function("() => window.ytInitialPlayerResponse && window.ytInitialData",
                                 timeout=30_000)
    raw = await page.evaluate("() => JSON.stringify({p: window.ytInitialPlayerResponse,"
                              " d: window.ytInitialData})")
    data = json.loads(raw)

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
        if int(elapsed) in (4, 12):
            await page.mouse.wheel(0, 900)
        await _emit(on_event, {"type": "watching", "n": n, "id": vid,
                               "second": min(int(elapsed) + 1, watch_s), "of": watch_s,
                               "ad": state["ad"]})
        await asyncio.sleep(1)

    comments = await page.evaluate(_COMMENTS_JS, 5)
    video = yt.details(data["p"], data["d"])
    if video["comments"] is None:
        video["comments"] = yt.number(comments["count"])
    video["top_comments"] = comments["top"]
    video["visit"] = {"order": n, "from": None, "opened_at": opened_at,
                      "watched_s": round(watched, 1), "ad": ad_seen}
    return video


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

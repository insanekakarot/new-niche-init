"""YouTube watch-page data -> clean video details.

A watch page carries two JSON objects: `ytInitialPlayerResponse` (the video: title, channel,
views, length, keywords, description; microformat: category, dates, likes, countries) and
`ytInitialData` (the page around it: likes button, subscribers, comment count, chapters and
the related videos). Both change shape now and then, so every field is looked up defensively:
a missing one is None / empty, never an error.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_ID = re.compile(r"^[\w-]{11}$")


# ---------------------------------------------------------------- links

def video_id(url: str) -> str | None:
    """The 11-character id from any YouTube video link (watch, youtu.be, shorts, embed, live)."""
    url = (url or "").strip()
    if _ID.match(url):
        return url
    try:
        u = urlparse(url if "//" in url else "https://" + url)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host == "youtube-nocookie.com":
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
        else:
            parts = u.path.strip("/").split("/")
            vid = parts[1] if len(parts) > 1 and parts[0] in ("shorts", "embed", "live", "v") else ""
    else:
        return None
    return vid if _ID.match(vid) else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


# ---------------------------------------------------------------- small helpers

def _walk(o, key):
    """Every value stored under `key`, anywhere in o (depth first, in document order)."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            if isinstance(v, (dict, list)):
                yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            if isinstance(v, (dict, list)):
                yield from _walk(v, key)


def _first(o, key, default=None):
    return next(_walk(o, key), default)


def _get(o, *path, default=None):
    for p in path:
        if isinstance(o, dict):
            o = o.get(p)
        elif isinstance(o, list) and isinstance(p, int) and -len(o) <= p < len(o):
            o = o[p]
        else:
            return default
        if o is None:
            return default
    return o


def text(v) -> str:
    """The text of a YouTube text object: {simpleText}, {runs: [{text}]}, {content} or a str."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if "simpleText" in v:
            return str(v["simpleText"])
        if "runs" in v:
            return "".join(str(r.get("text", "")) for r in v["runs"] if isinstance(r, dict))
        if "content" in v:
            return str(v["content"])
    return ""


_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
          "b": 1_000_000_000, "billion": 1_000_000_000}
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|b|thousand|million|billion)?\b", re.IGNORECASE)


def number(v) -> int | None:
    """'229,159 views' -> 229159, '1.02M subscribers' -> 1020000, 3824 -> 3824. None if none."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = _NUM.search(str(v or "").replace(" ", " "))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(round(n * _SCALE.get((m.group(2) or "").lower(), 1)))


def clock(seconds: int | None) -> str:
    """754 -> '12:34', 3754 -> '1:02:34'."""
    if seconds is None:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean(s: str) -> str:
    # YouTube wraps names in bidi isolation marks: '‎⁨@Name⁩'.
    return re.sub(r"[‎‏⁦-⁩]", "", s or "").strip()


# ---------------------------------------------------------------- the video

def details(player: dict | None, data: dict | None) -> dict:
    """Clean details of the video on a watch page from its two JSON objects."""
    player, data = player or {}, data or {}
    vd = player.get("videoDetails") or {}
    mf = _get(player, "microformat", "playerMicroformatRenderer", default={})
    primary = _first(data, "videoPrimaryInfoRenderer", {})
    secondary = _first(data, "videoSecondaryInfoRenderer", {})
    owner = _get(secondary, "owner", "videoOwnerRenderer", default={})

    vid = vd.get("videoId") or mf.get("externalVideoId") or ""
    length = number(vd.get("lengthSeconds") or mf.get("lengthSeconds"))
    views = number(vd.get("viewCount") or mf.get("viewCount"))
    likes = number(mf.get("likeCount")) or _likes(data)
    comments = _comment_count(data)
    description = vd.get("shortDescription") or text(mf.get("description")) or ""
    channel_id = vd.get("channelId") or mf.get("externalChannelId") or ""
    thumbs = _get(vd, "thumbnail", "thumbnails", default=[]) or \
        _get(mf, "thumbnail", "thumbnails", default=[])
    hashtags = [text(r) for r in _get(primary, "superTitleLink", "runs", default=[])
                if text(r).strip().startswith("#")]
    hashtags = [h.strip() for h in hashtags] or _hashtags(description)

    return {
        "id": vid,
        "url": watch_url(vid) if vid else "",
        "title": vd.get("title") or text(mf.get("title")) or text(primary.get("title")),
        "channel": {
            "name": vd.get("author") or mf.get("ownerChannelName") or "",
            "id": channel_id,
            "url": (f"https://www.youtube.com/channel/{channel_id}" if channel_id
                    else mf.get("ownerProfileUrl") or ""),
            "handle": _handle(mf.get("ownerProfileUrl") or "", owner),
            "subscribers": number(text(owner.get("subscriberCountText"))),
            "subscribers_text": text(owner.get("subscriberCountText")),
            "verified": bool(_first(owner.get("badges") or [], "metadataBadgeRenderer")),
        },
        "views": views,
        "likes": likes,
        "comments": comments,
        "like_rate": round(likes / views * 100, 2) if likes and views else None,
        "comment_rate": round(comments / views * 100, 3) if comments and views else None,
        "published": mf.get("publishDate") or "",
        "uploaded": mf.get("uploadDate") or "",
        "age": text(_get(primary, "relativeDateText")) or text(primary.get("dateText")),
        "length_s": length,
        "length": clock(length),
        "category": mf.get("category") or "",
        "keywords": list(vd.get("keywords") or []),
        "hashtags": hashtags,
        "description": description,
        "thumbnail": (max(thumbs, key=lambda t: t.get("width", 0)).get("url", "")
                      if thumbs else (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "")),
        "live": bool(vd.get("isLiveContent")),
        "family_safe": mf.get("isFamilySafe"),
        "unlisted": mf.get("isUnlisted"),
        "shorts_eligible": mf.get("isShortsEligible"),
        "countries": len(mf.get("availableCountries") or []) or None,
        "chapters": chapters(data),
        "related": related(data),
    }


def _likes(data: dict) -> int | None:
    for label in _walk(data, "accessibilityText"):
        m = re.search(r"along with ([\d,]+) other", str(label))
        if m:
            return number(m.group(1)) + 1  # "...along with 3,824 other people" -> 3825 likes
    return None


def _comment_count(data: dict) -> int | None:
    for panel in _walk(data, "engagementPanelSectionListRenderer"):
        if panel.get("panelIdentifier") == "engagement-panel-comments-section":
            n = number(text(_get(panel, "header", "engagementPanelTitleHeaderRenderer",
                                 "contextualInfo")))
            if n is not None:
                return n
    header = _first(data, "commentsHeaderRenderer", {})
    return number(text(header.get("countText"))) if header else None


def _handle(profile_url: str, owner: dict) -> str:
    m = re.search(r"/(@[\w.\-]+)", profile_url) or re.search(
        r"/(@[\w.\-]+)", str(_get(owner, "navigationEndpoint", "browseEndpoint",
                                  "canonicalBaseUrl", default="")))
    return m.group(1) if m else ""


def _hashtags(description: str) -> list[str]:
    seen: list[str] = []
    for h in re.findall(r"(?<![\w#])#(\w{2,})", description or ""):
        tag = "#" + h
        if tag.lower() not in (s.lower() for s in seen):
            seen.append(tag)
    return seen[:20]


def chapters(data: dict) -> list[dict]:
    """[{start_s, start, title}] from the chapter markers (or the 'key moments' list)."""
    out = []
    for ch in _walk(data, "chapterRenderer"):
        start = number(ch.get("timeRangeStartMillis"))
        out.append({"start_s": (start or 0) // 1000, "title": text(ch.get("title"))})
    if not out:
        for item in _walk(data, "macroMarkersListItemRenderer"):
            t = text(item.get("timeDescription"))
            parts = [int(p) for p in t.split(":") if p.isdigit()]
            secs = 0
            for p in parts:
                secs = secs * 60 + p
            out.append({"start_s": secs, "title": text(item.get("title"))})
    seen, unique = set(), []
    for c in out:
        if (c["start_s"], c["title"]) not in seen and c["title"]:
            seen.add((c["start_s"], c["title"]))
            unique.append({**c, "start": clock(c["start_s"])})
    return unique


def related(data: dict, limit: int = 20) -> list[dict]:
    """The related videos beside the watch page, in YouTube's order."""
    out, seen = [], set()
    for lk in _walk(data, "lockupViewModel"):
        if lk.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
            continue
        vid = lk.get("contentId") or ""
        if not _ID.match(vid) or vid in seen:
            continue
        meta = _get(lk, "metadata", "lockupMetadataViewModel", default={})
        rows = _get(meta, "metadata", "contentMetadataViewModel", "metadataRows", default=[]) or []
        parts = [p for r in rows for p in (r.get("metadataParts") or [])]
        texts = [_clean(text(p.get("text"))) for p in parts]
        labels = [str(p.get("accessibilityLabel") or "") for p in parts]
        views_i = next((i for i, l in enumerate(labels) if "view" in l.lower()), None)
        if views_i is None:
            views_i = next((i for i, t in enumerate(texts) if "view" in t.lower()), None)
        age = next((t for t in texts if re.search(r"\bago\b", t)), "")
        badge = _first(lk.get("contentImage") or {}, "thumbnailBadgeViewModel", {})
        seen.add(vid)
        out.append({
            "id": vid, "url": watch_url(vid),
            "title": _clean(text(meta.get("title"))),
            "channel": texts[0] if texts and views_i != 0 else "",
            "views": number(labels[views_i] or texts[views_i]) if views_i is not None else None,
            "views_text": texts[views_i] if views_i is not None else "",
            "age": age,
            "length": text(badge.get("text")) if badge else "",
        })
    for r in _walk(data, "compactVideoRenderer"):  # the older layout
        vid = r.get("videoId") or ""
        if not _ID.match(vid) or vid in seen:
            continue
        seen.add(vid)
        out.append({
            "id": vid, "url": watch_url(vid), "title": text(r.get("title")),
            "channel": text(r.get("longBylineText") or r.get("shortBylineText")),
            "views": number(text(r.get("viewCountText"))),
            "views_text": text(r.get("shortViewCountText") or r.get("viewCountText")),
            "age": text(r.get("publishedTimeText")), "length": text(r.get("lengthText")),
        })
    return out[:limit]

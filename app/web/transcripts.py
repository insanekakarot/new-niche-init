"""Get a video's transcript from its link (the source script for paraphrasing).

YouTube links use the video's caption track (youtube-transcript-api): a creator's own captions
first, else YouTube's automatic ones; English when there is one, else the video's language.
Other sites (and YouTube when that fails) go through yt-dlp's subtitles. Nothing is
downloaded but the text, and nothing is transcribed by us: a video without captions gives a
clear error, so paste or import the transcript instead.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

PREFERRED = ("en", "en-US", "en-GB")
MAX_URL = 500
TIMEOUT_S = 40

_YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtube-nocookie.com", "www.youtube-nocookie.com")
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Cue markup in captions: [Music], ♪, <c>…</c>, speaker arrows.
_NOISE = re.compile(r"\[(?:music|applause|laughter|♪+|\s*)\]|♪+|<[^>]+>|^>>\s*", re.IGNORECASE | re.MULTILINE)


class TranscriptError(Exception):
    """A message the page shows as is."""


@dataclass
class Transcript:
    text: str
    language: str          # e.g. "en"
    kind: str              # "captions" | "auto captions"
    source: str            # "youtube" | site name from yt-dlp
    title: str = ""

    @property
    def words(self) -> int:
        return len(self.text.split())

    def public(self) -> dict:
        return {"text": self.text, "words": self.words, "language": self.language,
                "kind": self.kind, "source": self.source, "title": self.title}


def youtube_id(url: str) -> str | None:
    """The 11-character id of a YouTube link (watch, youtu.be, shorts, embed, live), or None."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
        else:
            parts = [p for p in u.path.split("/") if p]
            vid = parts[1] if len(parts) > 1 and parts[0] in ("shorts", "embed", "live", "v") else ""
    else:
        return None
    return vid if _ID.match(vid or "") else None


def tidy(lines: list[str]) -> str:
    """Caption lines -> readable paragraphs (cue markup and repeats removed)."""
    out: list[str] = []
    for line in lines:
        line = _NOISE.sub(" ", html.unescape(line)).strip()
        line = re.sub(r"\s+", " ", line)
        if line and (not out or out[-1] != line):  # auto captions often repeat a line
            out.append(line)
    text = " ".join(out)
    # A paragraph every ~6 sentences, so the model (and you) can read it.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])", text)
    paras = [" ".join(sentences[i:i + 6]) for i in range(0, len(sentences), 6)]
    return "\n\n".join(p for p in paras if p.strip()).strip()


def fetch(url: str) -> Transcript:
    url = (url or "").strip()
    if not url or len(url) > MAX_URL or not re.match(r"^https?://", url, re.IGNORECASE):
        raise TranscriptError("Paste the full video link, starting with https://")
    vid = youtube_id(url)
    first_error: TranscriptError | None = None
    if vid:
        try:
            return _youtube(vid)
        except TranscriptError as exc:
            first_error = exc
    try:
        return _yt_dlp(url)
    except TranscriptError as exc:
        raise (first_error or exc) from None


def _youtube(vid: str) -> Transcript:
    from youtube_transcript_api import (
        CouldNotRetrieveTranscript,
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
        YouTubeTranscriptApi,
    )
    try:
        tracks = YouTubeTranscriptApi().list(vid)
        items = list(tracks)
        if not items:
            raise TranscriptError("This video has no captions, so there's no transcript to "
                                  "take. Paste the transcript or import a .txt file instead.")
        track = _pick(items)
        fetched = track.fetch()
    except TranscriptError:
        raise
    except (TranscriptsDisabled, NoTranscriptFound):
        raise TranscriptError("This video has no captions, so there's no transcript to take. "
                              "Paste the transcript or import a .txt file instead.") from None
    except VideoUnavailable:
        raise TranscriptError("YouTube says this video is unavailable (private, removed or "
                              "region-locked).") from None
    except CouldNotRetrieveTranscript as exc:
        reason = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
        raise TranscriptError(f"YouTube didn't give the transcript. {reason}".strip()) from None
    except OSError as exc:
        raise TranscriptError(f"Couldn't reach YouTube: {exc}") from None
    text = tidy([s.text for s in fetched.snippets])
    if not text:
        raise TranscriptError("The video's captions are empty.")
    return Transcript(text, track.language_code,
                      "auto captions" if track.is_generated else "captions", "youtube")


def _pick(tracks: list):
    """Own captions before automatic ones; English first, else the video's own language."""
    def rank(t):
        lang = t.language_code
        english = 0 if lang in PREFERRED else 1 if lang.startswith("en") else 2
        return (t.is_generated, english)
    return min(tracks, key=rank)


def _yt_dlp(url: str) -> Transcript:
    try:
        import yt_dlp
    except ImportError:
        raise TranscriptError("This kind of link needs yt-dlp. Install the app's requirements "
                              "again (pip install -e .).") from None
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": TIMEOUT_S}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 -- yt-dlp raises many kinds; its message is shown
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip().splitlines()[0][:240]
        raise TranscriptError(f"Couldn't read that video link: {msg}") from None
    if info.get("_type") == "playlist":
        raise TranscriptError("That's a playlist. Paste the link of one video.")
    for kind, tracks in (("captions", info.get("subtitles") or {}),
                         ("auto captions", info.get("automatic_captions") or {})):
        lang = _pick_lang(tracks)
        if lang:
            lines = _read_track(tracks[lang])
            text = tidy(lines)
            if text:
                return Transcript(text, lang, kind, info.get("extractor_key") or "video",
                                  info.get("title") or "")
    raise TranscriptError("This video has no captions, so there's no transcript to take. "
                          "Paste the transcript or import a .txt file instead.")


def _pick_lang(tracks: dict) -> str | None:
    langs = [lang for lang in tracks if lang != "live_chat"]
    for pref in PREFERRED:
        if pref in langs:
            return pref
    english = [lang for lang in langs if lang.startswith("en")]
    orig = [lang for lang in langs if lang.endswith("-orig")]
    return (english or orig or langs or [None])[0]


def _read_track(formats: list[dict]) -> list[str]:
    """Caption lines from the best format yt-dlp lists (json3 > vtt > srv/ttml)."""
    by_ext = {f.get("ext"): f.get("url") for f in formats if f.get("url")}
    for ext in ("json3", "vtt", "srv3", "ttml", "srv1"):
        if by_ext.get(ext):
            raw = _get(by_ext[ext])
            if ext == "json3":
                data = json.loads(raw)
                return ["".join(s.get("utf8", "") for s in e.get("segs") or [])
                        for e in data.get("events") or []]
            if ext == "vtt":
                return [ln for ln in raw.splitlines()
                        if ln.strip() and "-->" not in ln and not ln.startswith(("WEBVTT", "Kind:",
                                                                                 "Language:", "NOTE"))
                        and not ln.strip().isdigit()]
            return re.findall(r">([^<>]+)<", raw)
    return []


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise TranscriptError(f"Couldn't download the captions: {exc}") from None

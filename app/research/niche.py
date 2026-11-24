"""Keep a run in the start video's niche.

The niche comes from the start video. Every opened video is checked against it, and the next
video is chosen only from related videos that look like the same niche; the others are
skipped without being opened. When none is left, the run ends with a warning.

Two judges, set in Research › Settings ("matching"):
- Words (free, instant): the niche's words come from the start video's title, keywords and
  hashtags (and the AI's niche keywords when the AI is on). A related video's title scores
  one point per niche word it shares.
- AI (Claude or Codex, app/research/ai.py): one call per opened video, made while the video
  plays. It names the video's niche (a free label like "History › Railways") and topic
  ("Europe's night trains"), says whether it's the run's niche and picks the same-niche related
  videos. "strictness" sets the level for both: the niche (another country's railways count)
  or the start video's topic too.

  words_ai  the candidates are ranked by words and the best `ai_candidates` go to the AI
  ai        the AI sees the candidates in YouTube's order
  words     no AI: a candidate needs `min_words` shared words
  If an AI call fails, words decide for that video (noted on the video).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.cli import CliError
from app.research import ai

# Words that say nothing about a niche: grammar, YouTube filler and clickbait (English and
# romanized Hindi / Urdu).
_STOP = set("""
a an the and or but of in on at to for from by with without about into over under after
before is are was were be been being am it its this that these those there here what why how
who whom which when where will would can could should shall may might must do does did done
has have had not no yes you your yours we our they their them he she his her him i me my mine
just very more most much many all any some every each other than then too also only even ever
new full official video videos shorts short clip clips episode ep part live stream streamed
vs ft feat best top first last day days time times year years one two three four five ten
watch watching people world real reality truth true story stories shocking secret secrets
exposed dark untold mystery mysteries amazing incredible insane crazy biggest worst inside
explained explain explains documentary doc hindi urdu english subscribe channel podcast
ki ka ke ko ne se me mein hai hain tha thi the aur ya bhi kya kaise kyun kyon kab kahan
ek do teen sab koi kuch yeh woh ye wo jo jab tak liye wala wali wale nahi nahin
""".split())

_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def words(text: str) -> list[str]:
    out = []
    for w in _WORD.findall((text or "").lower()):
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]                                  # plural -> singular: cases -> case
        out.append(w)
    return out


def _hashtag_words(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        t = t.lstrip("#")
        out += words(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)) or words(t)
    return out


def profile(video: dict, extra: list[str] | None = None) -> Counter:
    """The niche's words, weighted: title 3, AI keywords 3, keywords / hashtags 2."""
    c: Counter = Counter()
    for w in set(words(video.get("title", ""))):
        c[w] += 3
    for w in set(w for k in (video.get("keywords") or []) for w in words(k)):
        c[w] += 2
    for w in set(_hashtag_words(video.get("hashtags") or [])):
        c[w] += 2
    for w in set(w for k in (extra or []) for w in words(k)):
        c[w] += 3
    return c


def shared(prof: Counter, title: str) -> list[str]:
    """The niche words in a title (each once, in title order)."""
    seen: list[str] = []
    for w in words(title):
        if w in prof and w not in seen:
            seen.append(w)
    return seen


@dataclass
class Check:
    """One video checked against the niche, and the related videos to open or skip."""
    label: str = ""                       # the video's own niche (free label from the AI)
    topic: str = ""                       # the video's own subject
    match: bool | None = None             # in the run's niche? None = not known
    reason: str = ""
    via: str = ""                         # "AI", "words", "words (AI failed: ...)", "start"
    picks: list[str] = field(default_factory=list)          # related ids to open, best first
    rejected: dict[str, str] = field(default_factory=dict)  # related id -> why skipped
    niche: dict | None = None             # the run's niche, from the start video's check

    def as_dict(self) -> dict:
        return {"label": self.label, "topic": self.topic, "match": self.match,
                "reason": self.reason, "via": self.via}


class Matcher:
    """The niche of one run. `check` is called once per opened video."""

    def __init__(self, opts: dict, ask=None):
        self.mode = opts["matching"]
        self.opts = opts
        self.ask = ask or ai.ask
        self.niche: dict | None = None    # {"label", "description", "keywords"}
        self.words: Counter = Counter()

    @property
    def uses_ai(self) -> bool:
        return self.mode in ("words_ai", "ai")

    def rank(self, candidates: list[dict]) -> list[dict]:
        """Candidates for a check: by shared words (words modes), cut to what the AI sees."""
        if self.mode == "ai":
            return candidates[:40]
        scored = sorted(candidates, key=lambda r: -len(shared(self.words, r.get("title", ""))))
        return scored[: self.opts["ai_candidates"]] if self.mode == "words_ai" else scored

    async def check(self, video: dict, candidates: list[dict], start: bool) -> Check:
        if start:
            self.words = profile(video)
        cands = self.rank(candidates)
        if self.uses_ai:
            try:
                return self._from_ai(video, cands, start, await self._ask(video, cands, start))
            except (CliError, ValueError) as exc:
                result = self._by_words(video, cands, start)
                result.via = f"words (AI failed: {str(exc).splitlines()[0][:160]})"
                return result
        return self._by_words(video, cands, start)

    # ------------------------------------------------------------ words

    def _by_words(self, video: dict, cands: list[dict], start: bool) -> Check:
        need = self.opts["min_words"]
        if start:
            match, reason = True, "The start video sets the niche."
        else:
            own = shared(self.words, " ".join([video.get("title", ""), *video.get("keywords", [])]))
            match = len(own) >= need
            reason = (f"Shares {len(own)} niche word{'s' if len(own) != 1 else ''}"
                      f"{': ' + ', '.join(own[:6]) if own else ''}.")
        picks, rejected = [], {}
        for r in cands:
            got = shared(self.words, r.get("title", ""))
            if len(got) >= need:
                picks.append(r["id"])
            else:
                rejected[r["id"]] = (f"Shares {len(got)} of the {need} niche words needed"
                                     f"{': ' + ', '.join(got) if got else ''}.")
        if start and self.niche is None:
            top = [w for w, _ in self.words.most_common(8)]
            self.niche = {"label": "", "description": "From the start video's words.",
                          "keywords": top}
        return Check(match=match, reason=reason, via="start" if start else "words",
                     picks=picks, rejected=rejected, niche=self.niche if start else None)

    # ------------------------------------------------------------ AI

    async def _ask(self, video: dict, cands: list[dict], start: bool) -> dict:
        text = await self.ask(self.opts["provider"],
                              prompt(video, cands, self.niche, start, self.opts["strictness"]),
                              model=self.opts["model"], effort=self.opts["effort"])
        return ai.parse_json(text)

    def _from_ai(self, video: dict, cands: list[dict], start: bool, answer: dict) -> Check:
        by_number = {str(i + 1): r["id"] for i, r in enumerate(cands)}

        def ids(values) -> list[str]:
            out = []
            for v in values if isinstance(values, list) else []:
                vid = by_number.get(str(v).strip().lstrip("#"))
                if vid and vid not in out:
                    out.append(vid)
            return out

        label = str(answer.get("niche") or answer.get("label") or "").strip()[:120]
        topic = str(answer.get("topic") or "").strip()[:120]
        picks = ids(answer.get("same_niche"))
        rejected = {}
        for item in answer.get("other") or []:
            if isinstance(item, dict):
                for vid in ids([item.get("n")]):
                    if vid not in picks:
                        rejected[vid] = str(item.get("why") or "Another niche.").strip()[:160]
        for r in cands:                     # not mentioned at all: not the niche either
            if r["id"] not in picks and r["id"] not in rejected:
                rejected[r["id"]] = "Not picked as the same niche."
        if start:
            keywords = [str(k).strip() for k in answer.get("keywords") or [] if str(k).strip()][:20]
            self.niche = {"label": label or "Unnamed niche", "topic": topic,
                          "description": str(answer.get("description") or "").strip()[:300],
                          "keywords": keywords, "strictness": self.opts["strictness"]}
            self.words = profile(video, keywords)
            return Check(label=label, topic=topic, match=True,
                         reason="The start video sets the niche.", via="AI", picks=picks,
                         rejected=rejected, niche=self.niche)
        same = answer.get("same")
        return Check(label=label, topic=topic, match=same if isinstance(same, bool) else None,
                     reason=str(answer.get("why") or "").strip()[:200], via="AI",
                     picks=picks if same is not False else [], rejected=rejected)


def _video_text(video: dict) -> str:
    desc = re.sub(r"\s+", " ", video.get("description") or "")[:600]
    lines = [f"Title: {video.get('title', '')}",
             f"Channel: {(video.get('channel') or {}).get('name', '')}",
             f"YouTube category: {video.get('category') or '-'}"]
    if video.get("keywords"):
        lines.append("Tags: " + ", ".join(video["keywords"][:15]))
    if video.get("hashtags"):
        lines.append("Hashtags: " + " ".join(video["hashtags"][:12]))
    if desc:
        lines.append(f"Description (start): {desc}")
    return "\n".join(lines)


def _candidates_text(cands: list[dict]) -> str:
    if not cands:
        return "(none)"
    return "\n".join(f"{i + 1}. {r.get('title', '')} | {r.get('channel', '')}"
                     f"{' | ' + r['age'] if r.get('age') else ''}" for i, r in enumerate(cands))


_LEVEL = {
    "niche": ("Same niche means the same kind of channel and audience: a video about another "
              "country, person or case in the same niche counts (Europe's night trains and "
              "India's hill railways are both History › Railways). Only the niche decides, not the "
              "topic."),
    "topic": ("Same niche here means the same niche AND the start video's topic: another "
              "subject in the same niche does not count (India's hill railways are not Europe's "
              "night trains)."),
}


def prompt(video: dict, cands: list[dict], niche: dict | None, start: bool,
           strictness: str = "niche") -> str:
    """The question for one opened video. Picking and checking follow one rule (_LEVEL)."""
    rule = _LEVEL.get(strictness, _LEVEL["niche"])
    if start:
        return f"""This is the start video of a niche research run.

{_video_text(video)}

Related videos YouTube shows beside it:
{_candidates_text(cands)}

1. Name this video's niche: what a YouTube channel would be built around, broad to narrow in
   two levels, like "History › Railways". Then its topic: the video's own subject,
   like "Europe's night trains". Describe the niche in one sentence.
2. Give 8 to 15 English keywords that titles in this niche would use.
3. From the related videos, list the numbers of those in the same niche, best first.
   {rule}
   For each of the others, say in 3 to 8 words why it's another niche.

Answer with this JSON only:
{{"niche": "...", "topic": "...", "description": "...", "keywords": ["..."],
 "same_niche": [numbers], "other": [{{"n": number, "why": "..."}}]}}"""
    n = niche or {}
    return f"""A niche research run keeps to one niche.
Niche: {n.get('label', '')}
Start video's topic: {n.get('topic', '')}
About: {n.get('description', '')}
{rule}

The video just opened:
{_video_text(video)}

Related videos YouTube shows beside it, or left from earlier videos:
{_candidates_text(cands)}

1. Name this video's own niche (two levels, broad to narrow) and its topic.
2. Say whether it's in the run's niche by the rule above (true or false) and why, in one
   short sentence.
3. From the related videos, list the numbers of those in the run's niche by the same rule,
   best first. For each of the others, say in 3 to 8 words why it's another niche.

Answer with this JSON only:
{{"niche": "...", "topic": "...", "same": true, "why": "...",
 "same_niche": [numbers], "other": [{{"n": number, "why": "..."}}]}}"""

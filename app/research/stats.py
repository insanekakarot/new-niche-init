"""A run's numbers: medians across its videos, and each video against them.

views_per_sub = a video's views ÷ its channel's subscribers (above 1: more views than the
channel has subscribers, a sign the video travelled beyond the channel). The medians are of
the videos the run opened, so one viral video doesn't pull them the way an average would.
"""

from __future__ import annotations

from statistics import median

_FIELDS = {
    "views": lambda v: v.get("views"),
    "subscribers": lambda v: (v.get("channel") or {}).get("subscribers"),
    "views_per_sub": lambda v: v.get("views_per_sub"),
    "likes": lambda v: v.get("likes"),
    "comments": lambda v: v.get("comments"),
    "like_rate": lambda v: v.get("like_rate"),
    "length_s": lambda v: v.get("length_s"),
    "days_old": lambda v: v.get("days_old"),
}


def _median(values) -> float | None:
    values = [x for x in values if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if not values:
        return None
    m = median(values)
    return round(m, 3) if isinstance(m, float) and not m.is_integer() else int(m)


def ratio(views, subscribers) -> float | None:
    return round(views / subscribers, 3) if views is not None and subscribers else None


def run_stats(videos: list[dict]) -> dict:
    """{"count", "median": {field: value}} over the videos."""
    return {"count": len(videos),
            "median": {k: _median(get(v) for v in videos) for k, get in _FIELDS.items()}}


def with_stats(run: dict) -> dict:
    """The run with its medians, and each video's ratio and comparison with the median views
    (filled in for runs saved before these existed)."""
    videos = run.get("videos") or []
    for v in videos:
        if v.get("views_per_sub") is None:
            v["views_per_sub"] = ratio(v.get("views"), (v.get("channel") or {}).get("subscribers"))
    stats = run_stats(videos)
    mid = stats["median"]["views"]
    for v in videos:
        v["vs_median"] = round(v["views"] / mid, 2) if mid and v.get("views") is not None else None
    return {**run, "stats": stats}

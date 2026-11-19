"""Static catalog of tools and scripting brand profiles (source: PLAN.md)."""

import json
from dataclasses import dataclass, field, replace

from app.config import settings


@dataclass(frozen=True)
class Tool:
    slug: str
    name: str
    icon: str
    description: str
    category: str = ""
    # "live" = usable now, "planned" = placeholder page
    status: str = "planned"
    # Accent for the tool's tile on the home page
    accent: str = "#101014"

    @property
    def path(self) -> str:
        return f"/{self.slug}"

    @property
    def short_name(self) -> str:
        return self.name.removesuffix(" Tool")


@dataclass(frozen=True)
class Brand:
    slug: str
    name: str
    group: str
    languages: tuple[str, ...]
    video_length: str
    word_count: str
    production: str
    drive_url: str = ""  # from app/brand_links.json (private, not in git)
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


TOOLS: tuple[Tool, ...] = (
    Tool(
        "scripting",
        "Scripting Tool",
        "📝",
        "Write scripts for all 9 brand profiles, with each brand's languages, video length "
        "and word-count targets built in.",
        category="Writing", status="live", accent="#b3261e",
    ),
    Tool(
        "translations",
        "Translations Tool",
        "🌐",
        "Translate finished scripts into each brand's channel languages: French, Russian, "
        "Portuguese, Spanish, Czech, German and Polish.",
        category="Localization", status="planned", accent="#1659c4",
    ),
    Tool(
        "seo",
        "SEO Tool",
        "🔎",
        "Generate titles, descriptions, tags and keywords for every brand and language channel.",
        category="Discovery", status="planned", accent="#017a54",
    ),
    Tool(
        "editing",
        "Editing Tool",
        "🎬",
        "Track manual edits and Claude-generated videos and animations from script to final cut.",
        category="Post-production", status="planned", accent="#ad4306",
    ),
    Tool(
        "uploading",
        "Uploading Tool",
        "🚀",
        "Prepare, schedule and upload finished videos to each brand's channels.",
        category="Publishing", status="planned", accent="#6a3fd6",
    ),
)

TOOLS_BY_SLUG: dict[str, Tool] = {t.slug: t for t in TOOLS}

_NBA_LANGS = ("English", "French", "Russian", "Portuguese", "Spanish")
_EN = ("English",)
_MANUAL = "Manual edit"
_CLAUDE_GEN = "Claude sourcing + video generation"
_CLAUDE_ANIM = "Claude animation"
_STD_LEN, _STD_WORDS = "10–12 min", "1,700–1,900 words"

BRANDS: tuple[Brand, ...] = (
    Brand(
        "athlete-records", "Athlete Records", "Multi-language",
        ("English", "French", "Russian", "Portuguese", "Spanish", "Czech", "German"),
        _STD_LEN, "1,500–1,700 words", _MANUAL,
    ),
    Brand(
        "nba-stories", "NBA Stories, Documentaries & Analysis", "Multi-language",
        _NBA_LANGS, "25–30 min", "3,750–4,500 words", _CLAUDE_GEN,
        notes="NBA brand 1",
    ),
    Brand(
        "nba-games", "NBA Current Games & Performances", "Multi-language",
        _NBA_LANGS, "11–13 min", "1,600–2,000 words", _MANUAL,
        notes="NBA brand 2",
    ),
    Brand(
        "top-10", "TOP 10 Documentary", "Multi-language",
        ("English", "German", "Polish", "French", "Portuguese", "Spanish"),
        "Depends on headings", "2,400–2,700 words", _MANUAL,
        notes="Word count depends on how many headings the script has.",
    ),
    Brand(
        "zombie-survival", "Zombie Survival", "English only", _EN, _STD_LEN, _STD_WORDS,
        _CLAUDE_ANIM,
    ),
    Brand(
        "bunker-survival", "Bunker Survival", "English only", _EN, _STD_LEN, _STD_WORDS,
        _CLAUDE_ANIM,
    ),
    Brand(
        "what-if-1", "What If — Brand 1", "English only", _EN, _STD_LEN, _STD_WORDS,
        _CLAUDE_ANIM,
    ),
    Brand(
        "what-if-2", "What If — Brand 2", "English only", _EN, _STD_LEN, _STD_WORDS,
        _CLAUDE_ANIM,
    ),
    Brand(
        "entire-history", "Entire History / Story", "English only", _EN, "20–25 min",
        "3,500–4,000 words", _CLAUDE_GEN,
    ),
)


def _with_links(brands: tuple[Brand, ...]) -> tuple[Brand, ...]:
    """Fill in each brand's Drive folder from the private links file (slug -> URL).

    The links are kept out of git: copy app/brand_links.example.json to the file named by
    BRAND_LINKS_FILE (default app/brand_links.json) and put the real folder URLs in it.
    Brands without a link just don't show the "Open brand folder" button.
    """
    try:
        links = json.loads(settings.brand_links_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return brands
    if not isinstance(links, dict):
        return brands
    return tuple(replace(b, drive_url=str(links.get(b.slug) or "")) for b in brands)


BRANDS = _with_links(BRANDS)

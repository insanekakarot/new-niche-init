"""Tools and brand profiles.

The tools are part of the app. The brands (names, languages, length and word targets, how
each is produced, Drive folders, which TSV brands they are, shown or hidden) come only from
the private brands file, ___brands.json (app/brands_file.py), so none of them are in git.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app import brands_file


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
    group: str = ""
    languages: tuple[str, ...] = ()
    video_length: str = ""
    word_count: str = ""
    production: str = ""
    drive_url: str = ""
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    # False: hidden on every page and in the Scripting API.
    enabled: bool = True
    # The admin TSV brands this profile is (their keys: 'Brand A' -> brand_a).
    config_keys: tuple[str, ...] = ()


# Descriptions: {brands} = number of shown brands, {languages} = their languages besides
# English. The brands file can replace a tool's description ("tools" -> slug -> description).
TOOLS: tuple[Tool, ...] = (
    Tool("scripting", "Scripting Tool", "📝",
         "Write scripts for every brand profile, with each brand's languages, video length "
         "and word-count targets built in.",
         category="Writing", status="live", accent="#b3261e"),
    Tool("translations", "Translations Tool", "🌐",
         "Translate finished scripts into each brand's languages.",
         category="Localization", status="planned", accent="#1659c4"),
    Tool("seo", "SEO Tool", "🔎",
         "Generate titles, descriptions, tags and keywords for every brand and language.",
         category="Discovery", status="planned", accent="#017a54"),
    Tool("editing", "Editing Tool", "🎬",
         "Track every video's edits from script to final cut.",
         category="Post-production", status="planned", accent="#ad4306"),
    Tool("uploading", "Uploading Tool", "🚀",
         "Prepare, schedule and upload finished videos to each brand's channels.",
         category="Publishing", status="planned", accent="#6a3fd6"),
)

TOOLS_BY_SLUG: dict[str, Tool] = {t.slug: t for t in TOOLS}


@dataclass(frozen=True)
class ResearchPage:
    """A page of the Research section (top bar: Research menu, before Tools)."""
    slug: str
    name: str
    description: str
    status: str = "planned"

    @property
    def path(self) -> str:
        return "/research" if self.slug == "research" else f"/research/{self.slug}"

    @property
    def short_name(self) -> str:
        return self.name


# New-niche research. How it researches is decided later; for now these are placeholders.
RESEARCH: tuple[ResearchPage, ...] = (
    ResearchPage("dashboard", "Dashboard",
                 "Every niche under research at a glance: where each one stands and what's next."),
    ResearchPage("research", "Research",
                 "Research a new niche: collect what's known about it and decide if it's worth "
                 "a brand."),
    ResearchPage("analytics", "Analytics",
                 "Compare researched niches by the numbers to pick the next one."),
)

RESEARCH_BY_SLUG: dict[str, ResearchPage] = {p.slug: p for p in RESEARCH}


def _strings(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(s for s in (str(v).strip() for v in value) if s)


def _brand(raw) -> Brand | None:
    """One brand from the file; None (skipped) without a slug and a name."""
    if not isinstance(raw, dict):
        return None
    slug, name = str(raw.get("slug") or "").strip(), str(raw.get("name") or "").strip()
    if not slug or not name:
        return None
    return Brand(
        slug=slug, name=name,
        group=str(raw.get("group") or ""),
        languages=_strings(raw.get("languages")),
        video_length=str(raw.get("video_length") or ""),
        word_count=str(raw.get("word_count") or ""),
        production=str(raw.get("production") or ""),
        drive_url=str(raw.get("drive_url") or ""),
        notes=str(raw.get("notes") or ""),
        tags=_strings(raw.get("tags")),
        enabled=raw.get("enabled", True) is not False,
        config_keys=tuple(k.lower() for k in _strings(raw.get("config_keys"))),
    )


def all_brands() -> tuple[Brand, ...]:
    """Every brand in the file, shown or hidden, in file order."""
    raw = brands_file.load().get("brands")
    brands = (_brand(b) for b in raw) if isinstance(raw, list) else ()
    return tuple(b for b in brands if b)


def brands() -> tuple[Brand, ...]:
    """The brands to show ("enabled": true or not set)."""
    return tuple(b for b in all_brands() if b.enabled)


def brand_for_key(config_key: str) -> Brand | None:
    """The profile (shown or hidden) of an admin TSV brand, by its key; None if none."""
    key = (config_key or "").lower()
    return next((b for b in all_brands() if key in b.config_keys), None)


def key_enabled(config_key: str) -> bool:
    """Whether a TSV brand is shown: hidden only when its profile says "enabled": false."""
    brand = brand_for_key(config_key)
    return brand is None or brand.enabled


def languages() -> list[str]:
    """The shown brands' languages, English first."""
    return sorted({lang for b in brands() for lang in b.languages},
                  key=lambda lang: (lang != "English", lang))


def tools() -> tuple[Tool, ...]:
    """The tools, with descriptions from the brands file where it has one."""
    texts = brands_file.load().get("tools")
    texts = texts if isinstance(texts, dict) else {}
    shown = brands()
    others = ", ".join(lang for lang in languages() if lang != "English")
    out = []
    for tool in TOOLS:
        own = texts.get(tool.slug)
        desc = brands_file.text(own.get("description")) if isinstance(own, dict) else ""
        desc = (desc or tool.description).replace("{brands}", str(len(shown)))
        if "{languages}" in desc:
            desc = desc.replace("{languages}", others) if others else \
                desc.replace(": {languages}", "").replace("{languages}", "")
        out.append(replace(tool, description=desc))
    return tuple(out)


def tool(slug: str) -> Tool | None:
    return next((t for t in tools() if t.slug == slug), None)

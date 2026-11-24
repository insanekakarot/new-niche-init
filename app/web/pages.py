from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.cli import provider_info
from app.research import options as research_options
from app.web import catalog
from app.web.scripting import load_brand_views

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
# The top bar's Research and Tools menus are on every page.
templates.env.globals["nav_research"] = catalog.RESEARCH
templates.env.globals["nav_tools"] = catalog.TOOLS

router = APIRouter(include_in_schema=False)


# Brands and tool texts are read on every request: the brands file can change any time.

@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    tools, brands = catalog.tools(), catalog.brands()
    live = [t for t in tools if t.status == "live"]
    stats = {
        "tools": len(tools),
        "brands": len(brands),
        "languages": catalog.languages(),
        "live": len(live),
        "live_names": [t.short_name for t in live],
    }
    return templates.TemplateResponse(
        request,
        "index.html",
        {"tools": tools, "brands": brands, "stats": stats, "providers": provider_info(),
         "active": "home"},
    )


@router.get("/scripting", response_class=HTMLResponse)
def scripting(request: Request):
    # Read on every request so a new admin upload shows up without a restart.
    views, error = load_brand_views()
    return templates.TemplateResponse(
        request,
        "scripting.html",
        {
            "tools": catalog.tools(),
            "tool": catalog.tool("scripting"),
            "brands": views,
            "config_error": error,
            "providers": provider_info(),
        },
    )


def _research(request: Request, slug: str):
    page = catalog.RESEARCH_BY_SLUG.get(slug)
    if page is None:
        raise HTTPException(status_code=404, detail="Research page not found")
    return templates.TemplateResponse(
        request, "research.html",
        {"page": page, "pages": catalog.RESEARCH,
         "opts": (opts := research_options.load()),
         "opts_text": research_options.describe(opts),
         "show_browser": opts["show_browser"],
         "matching_modes": research_options.MATCHING,
         "strictness": research_options.STRICTNESS,
         "ai_providers": research_options.PROVIDERS,
         "providers": provider_info(),
         "settings_file": research_options.FILE_NAME,
         "page_number": catalog.RESEARCH.index(page) + 1, "brands": catalog.brands()},
    )


@router.get("/research", response_class=HTMLResponse)
def research(request: Request):
    return _research(request, "research")


@router.get("/research/{slug}", response_class=HTMLResponse)
def research_page(request: Request, slug: str):
    if slug == "research":  # /research is its page
        raise HTTPException(status_code=404, detail="Research page not found")
    return _research(request, slug)


@router.get("/{slug}", response_class=HTMLResponse)
def tool_page(request: Request, slug: str):
    tools = catalog.tools()
    tool = next((t for t in tools if t.slug == slug), None)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return templates.TemplateResponse(
        request, "tool.html",
        {"tools": tools, "tool": tool, "brands": catalog.brands(),
         "tool_number": tools.index(tool) + 1},
    )

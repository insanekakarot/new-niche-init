from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.web.catalog import BRANDS, TOOLS, TOOLS_BY_SLUG
from app.cli import provider_info
from app.web.scripting import load_brand_views

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
# The top bar's Tools menu is on every page.
templates.env.globals["nav_tools"] = TOOLS

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    languages = sorted({lang for b in BRANDS for lang in b.languages},
                       key=lambda lang: (lang != "English", lang))
    live = [t for t in TOOLS if t.status == "live"]
    stats = {
        "tools": len(TOOLS),
        "brands": len(BRANDS),
        "languages": languages,
        "live": len(live),
        "live_names": [t.short_name for t in live],
    }
    return templates.TemplateResponse(
        request,
        "index.html",
        {"tools": TOOLS, "brands": BRANDS, "stats": stats, "providers": provider_info(),
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
            "tools": TOOLS,
            "tool": TOOLS_BY_SLUG["scripting"],
            "brands": views,
            "config_error": error,
            "providers": provider_info(),
        },
    )


@router.get("/{slug}", response_class=HTMLResponse)
def tool_page(request: Request, slug: str):
    tool = TOOLS_BY_SLUG.get(slug)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return templates.TemplateResponse(
        request, "tool.html",
        {"tools": TOOLS, "tool": tool, "brands": BRANDS,
         "tool_number": TOOLS.index(tool) + 1},
    )

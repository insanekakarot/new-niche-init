from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.cli import provider_info
from app.web import catalog
from app.web.scripting import load_brand_views

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
# The top bar's Tools menu is on every page.
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

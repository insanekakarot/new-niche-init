import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import keys
from app.admin import scripts_config as sc
from app.admin import used_entries
from app.admin.auth import (
    COOKIE_NAME,
    key_matches,
    require_admin_api,
    require_admin_page,
    safe_next,
    session_token,
)
from app.config import settings
from app.rules import explain, lang
from app.web.pages import templates
from app.web.rule_jobs import word_range
from app.web.scripting import catalog_for

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_SUFFIXES = (".tsv", ".txt")

router = APIRouter(prefix="/admin", include_in_schema=False)


# ---------------------------------------------------------------- login

@router.get("")
@router.get("/")
def admin_root():
    return RedirectResponse("/admin/scripts/dashboard", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None):
    return templates.TemplateResponse(
        request, "admin/login.html",
        {"next": safe_next(next), "error": None, "locked": not settings.admin_api_key},
    )


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, api_key: str = Form(""), next: str = Form("")):
    if not settings.admin_api_key:
        return templates.TemplateResponse(
            request, "admin/login.html", {"next": safe_next(next), "error": None, "locked": True},
            status_code=503,
        )
    if not key_matches(api_key.strip()):
        return templates.TemplateResponse(
            request, "admin/login.html",
            {"next": safe_next(next), "error": "That API key is not correct.", "locked": False},
            status_code=401,
        )
    response = RedirectResponse(safe_next(next), status_code=303)
    response.set_cookie(
        COOKIE_NAME, session_token(settings.admin_api_key),
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
        max_age=60 * 60 * 24 * 7, path="/admin",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return response


# ---------------------------------------------------------------- scripts config

@router.get("/scripts/dashboard", response_class=HTMLResponse,
            dependencies=[Depends(require_admin_page)])
def dashboard(request: Request):
    s = sc.status()
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"s": s, "step_names": list(sc.STEP_COLUMNS), "active": "dashboard",
         "rules": _rules_views(s.get("config") or [])},
    )


def _rules_views(config: list[dict]) -> dict[str, dict]:
    """Per brand: its Rules line by line with notes, its Rule Prompts, and a sample run."""
    views = {}
    for entry in config:
        for key, brand in entry.items():
            catalog = catalog_for(key) or {}
            values = {"words_range": word_range(catalog.get("word_count")),
                      "word_count": catalog.get("word_count"),
                      "video_length": catalog.get("video_length"),
                      "brand_name": brand.get("brand_name"),
                      "used_entries": "{unique} different entries (Admin → Used entries)".format(
                          **used_entries.summary(key, used_entries.for_brand(key)))}
            view = explain.brand_rules(brand, sc.STEP_COLUMNS,
                                       {k: v for k, v in values.items() if v})
            if view:
                views[key] = view
    return views


_CHECK_WORDS = ("VALIDATION", "CHECK", "SATISFACTION", "REVIEW")


def _example_rows(text: str) -> dict:
    """One example from keys.RULE_EXAMPLES, laid out line by line with notes."""
    program = lang.parse(text, sc.STEP_COLUMNS)
    names = program.prompt_names()
    # Example names only: ones that read like checks are shown as checks.
    prompts = {n: "STATUS - READY" if any(w in n for w in _CHECK_WORDS) else "Fix it."
               for n in names}
    return {"guidance": program.guidance,
            "lines": [{**line, "segments": explain.segments(line["code"], names)}
                      for line in explain.lines(program, prompts)]}


@router.get("/scripts/rules-guide", response_class=HTMLResponse,
            dependencies=[Depends(require_admin_page)])
def rules_guide(request: Request):
    examples = [{"about": about, **_example_rows(text)} for about, text in keys.RULE_EXAMPLES]
    featured = next((e for e in examples if "TITLE_SATISFACTION" in str(e["lines"])), examples[0])
    numbers = {"DEFAULT_MAX_LOOP": str(keys.DEFAULT_MAX_LOOP),
               "HARD_MAX_LOOP": str(keys.HARD_MAX_LOOP)}
    statements = [(key, re.sub("|".join(numbers), lambda m: numbers[m.group()], about))
                  for key, about in keys.RULE_KEYWORDS.items()]
    return templates.TemplateResponse(
        request, "admin/rules_guide.html",
        {"active": "guide", "keys": keys, "featured": featured, "statements": statements,
         "examples": [e for e in examples if e is not featured]},
    )


# ---------------------------------------------------------------- used entries

class EntriesSave(BaseModel):
    brand: str = Field(..., pattern=r"^[a-z0-9_]{1,80}$")
    text: str = Field("", max_length=used_entries.MAX_TEXT)
    source: str | None = Field(None, max_length=500)


def _brands() -> list[dict]:
    """Config brands, with whether their prompts use {{ used_entries }}."""
    out = []
    for entry in sc.load_config() or []:
        for key, brand in entry.items():
            texts = [*(brand.get("prompts") or {}).values(),
                     *(brand.get("rule_prompts") or {}).values()]
            uses = any("used_entries" in keys.PLACEHOLDER.findall(t or "") for t in texts)
            out.append({"key": key, "name": brand.get("brand_name") or key, "uses": uses})
    return out


@router.get("/scripts/entries", response_class=HTMLResponse,
            dependencies=[Depends(require_admin_page)])
def entries_page(request: Request, brand: str | None = None):
    try:
        brands = _brands()
    except (OSError, ValueError):
        brands = []
    stored = used_entries.load()["brands"]
    for b in brands:
        b["summary"] = used_entries.summary(b["key"], stored.get(b["key"]))
    keys_ = [b["key"] for b in brands]
    if brand not in keys_:
        brand = next((b["key"] for b in brands if b["uses"] or b["summary"]["entries"]),
                     keys_[0] if keys_ else None)
    current = used_entries.for_brand(brand) if brand else None
    groups = (current or {}).get("groups") or []
    return templates.TemplateResponse(
        request, "admin/entries.html",
        {"active": "entries", "brands": brands, "brand": brand, "groups": groups,
         "text": used_entries.to_text(groups), "source": (current or {}).get("source") or "",
         "summary": used_entries.summary(brand, current) if brand else None,
         "file": str(used_entries.path()), "value": used_entries.render(groups)},
    )


@router.post("/scripts/entries", dependencies=[Depends(require_admin_api)])
def entries_save(req: EntriesSave, dry_run: bool = Query(False)):
    if req.brand not in {b["key"] for b in _brands()}:
        raise HTTPException(404, f"No brand {req.brand!r} in the prompts config.")
    groups = used_entries.parse(req.text)
    if req.text.strip() and not groups:
        raise HTTPException(422, "No entries found in that text.")
    if not dry_run:
        try:
            used_entries.save(req.brand, groups, source=(req.source or "").strip() or None)
        except OSError as exc:
            raise HTTPException(500, f"Couldn't write {used_entries.path()}: {exc}") from exc
    stored = used_entries.for_brand(req.brand) if not dry_run else {"groups": groups}
    return {"saved": not dry_run, "groups": groups, "summary": used_entries.summary(req.brand, stored),
            "value": used_entries.render(groups)}


@router.get("/scripts/entries.json", dependencies=[Depends(require_admin_api)])
def entries_download():
    path = used_entries.path()
    if not path.is_file():
        raise HTTPException(404, "No used entries saved yet.")
    return FileResponse(path, media_type="application/json", filename=used_entries.FILENAME)


@router.get("/scripts/prompts/edit", response_class=HTMLResponse,
            dependencies=[Depends(require_admin_page)])
def edit_form(request: Request):
    return templates.TemplateResponse(
        request, "admin/edit.html",
        {"s": sc.status(), "columns": sc.REQUIRED_COLUMNS, "active": "edit",
         "tsv_name": sc.TSV_FILENAME, "config_name": sc.CONFIG_FILENAME},
    )


@router.post("/scripts/prompts/edit", dependencies=[Depends(require_admin_api)])
async def upload_tsv(
    file: UploadFile = File(...),
    dry_run: bool = Query(False, description="Only validate and preview; don't save."),
):
    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED_SUFFIXES):
        raise HTTPException(400, "Please upload a .tsv file (tab-separated).")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The file is larger than 10 MB.")
    if not raw.strip():
        raise HTTPException(400, "The file is empty.")

    try:
        result = sc.parse_tsv(raw)
    except sc.TsvError as exc:
        raise HTTPException(422, str(exc)) from exc

    if not dry_run:
        try:
            sc.save(raw, result)
        except OSError as exc:
            raise HTTPException(500, f"Couldn't write the files: {exc}") from exc

    return JSONResponse({
        "saved": not dry_run,
        "brands": result.brands,
        "warnings": result.warnings,
        "tsv_path": str(sc.tsv_path()),
        "config_path": str(sc.config_path()),
        "config": result.config,
    })


@router.get("/scripts/prompts.json", dependencies=[Depends(require_admin_api)])
def download_config():
    path = sc.config_path()
    if not path.is_file():
        raise HTTPException(404, "No config yet. Upload a TSV first.")
    return FileResponse(path, media_type="application/json", filename=sc.CONFIG_FILENAME)

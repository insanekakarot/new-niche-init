from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.admin import scripts_config as sc
from app.admin.auth import (
    COOKIE_NAME,
    key_matches,
    require_admin_api,
    require_admin_page,
    safe_next,
    session_token,
)
from app.config import settings
from app.web.pages import templates

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
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"s": sc.status(), "step_names": list(sc.STEP_COLUMNS), "active": "dashboard"},
    )


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

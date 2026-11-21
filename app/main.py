import uvicorn
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.admin import routes as admin_routes
from app.admin.auth import LoginRequired, login_url
from app.api.routes import health
from app.config import settings
from app.web import pages, scripting_api

app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.exception_handler(LoginRequired)
async def _to_login(request: Request, exc: LoginRequired):
    return RedirectResponse(login_url(exc.next_path), status_code=303)


_ERRORS = {
    401: ("Not allowed", "This page needs the admin key."),
    403: ("Not allowed", "You can't open this page."),
    404: ("Page not found",
          ("There's nothing at this address. Go back, or start again from the home page.")),
}
_NOT_PAGES = ("/api/", "/docs", "/redoc", "/openapi.json", "/health")


def _wants_page(request: Request) -> bool:
    """A browser / desktop window opening an address (not a fetch from the page or the API)."""
    return (request.method == "GET" and "text/html" in request.headers.get("accept", "")
            and not request.url.path.startswith(_NOT_PAGES))


@app.exception_handler(StarletteHTTPException)
async def _error_page(request: Request, exc: StarletteHTTPException):
    if exc.status_code < 400 or not _wants_page(request):
        return await http_exception_handler(request, exc)
    title, message = _ERRORS.get(exc.status_code, ("Something went wrong", ""))
    if isinstance(exc.detail, str) and exc.detail and exc.status_code != 404:
        message = exc.detail
    return pages.templates.TemplateResponse(
        request, "error.html",
        {"status": exc.status_code, "title": title, "message": message,
         "path": request.url.path},
        status_code=exc.status_code, headers=getattr(exc, "headers", None))


app.include_router(health.router)
app.include_router(admin_routes.router)
app.include_router(scripting_api.router)
# Pages router last: its "/{slug}" catch-all must not shadow other routes.
app.include_router(pages.router)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=True)

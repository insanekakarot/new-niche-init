import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.admin import routes as admin_routes
from app.admin.auth import LoginRequired, login_url
from app.api.routes import health
from app.config import settings
from app.web import pages, scripting_api

app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.exception_handler(LoginRequired)
async def _to_login(request: Request, exc: LoginRequired):
    return RedirectResponse(login_url(exc.next_path), status_code=303)


app.include_router(health.router)
app.include_router(admin_routes.router)
app.include_router(scripting_api.router)
# Pages router last: its "/{slug}" catch-all must not shadow other routes.
app.include_router(pages.router)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=True)

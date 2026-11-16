"""API-key protection for /admin.

Send the key as an `X-API-Key` header (scripts, curl), or sign in once at /admin/login,
which sets an HttpOnly cookie. The cookie holds an HMAC of the key, never the key itself.
"""

import hashlib
import hmac
from urllib.parse import quote

from fastapi import HTTPException, Request, status

from app.config import settings

COOKIE_NAME = "admin_session"
HEADER_NAME = "X-API-Key"


class LoginRequired(Exception):
    """Raised by page routes; main.py turns it into a redirect to the login page."""

    def __init__(self, next_path: str):
        self.next_path = next_path


def _configured_key() -> str:
    key = settings.admin_api_key
    if not key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Admin is locked: set ADMIN_API_KEY in .env and restart the server.",
        )
    return key


def session_token(key: str) -> str:
    return hmac.new(key.encode(), b"admin-session-v1", hashlib.sha256).hexdigest()


def key_matches(candidate: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), _configured_key().encode())


def is_authenticated(request: Request) -> bool:
    key = _configured_key()
    header = request.headers.get(HEADER_NAME, "")
    if header and hmac.compare_digest(header.encode(), key.encode()):
        return True
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and hmac.compare_digest(cookie, session_token(key))


def require_admin_page(request: Request) -> None:
    """For HTML pages: send unauthenticated visitors to the login form."""
    if not is_authenticated(request):
        raise LoginRequired(request.url.path)


def require_admin_api(request: Request) -> None:
    """For API/upload endpoints: answer 401 instead of redirecting."""
    if not is_authenticated(request):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Missing or wrong API key. Send it as the {HEADER_NAME} header or sign in.",
        )


def safe_next(path: str | None) -> str:
    """Only redirect back inside /admin, never to another site."""
    if path and path.startswith("/admin") and not path.startswith("//"):
        return path
    return "/admin/scripts/dashboard"


def login_url(next_path: str) -> str:
    return f"/admin/login?next={quote(safe_next(next_path))}"

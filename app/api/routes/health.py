from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    # "app" lets the desktop window tell this server apart from anything else on the port.
    return {"status": "ok", "app": settings.app_name}

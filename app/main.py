import uvicorn
from fastapi import FastAPI

from app.api.routes import health
from app.config import settings

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(health.router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": f"Welcome to {settings.app_name}"}


if __name__ == "__main__":

    uvicorn.run(app, host="0.0.0.0", port=settings.port, reload=True)
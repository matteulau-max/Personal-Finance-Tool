"""Health-check endpoints.

A health check is a trivial endpoint that answers "are you alive?". Hosting
platforms (Railway, Render) call it every few seconds to decide whether to
restart your service or route traffic to it. Building it first also gives us
something real to test end-to-end before any complexity exists.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

# A router is a group of related endpoints that we later attach to the app.
# Splitting routes into routers keeps main.py small as the project grows.
router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Declaring the response shape gives us validation AND free API docs."""

    status: str
    app_name: str
    environment: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        app_name=settings.APP_NAME,
        environment=settings.ENVIRONMENT,
    )

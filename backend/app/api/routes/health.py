"""Health-check endpoints.

A health check is a trivial endpoint that answers "are you alive?". Hosting
platforms (Railway, Render) call it every few seconds to decide whether to
restart your service or route traffic to it.

There are two kinds, and the distinction matters in production:

  /health     LIVENESS  -- is the process running? Must not touch the database.
                          If this fails, the platform restarts the container.
  /health/db  READINESS -- can it actually serve requests? Checks dependencies.
                          If this fails, the platform stops sending traffic but
                          does NOT restart -- restarting won't fix a database
                          that is down, it will just cause a crash loop.
"""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db

# A router is a group of related endpoints that we later attach to the app.
# Splitting routes into routers keeps main.py small as the project grows.
router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Declaring the response shape gives us validation AND free API docs."""

    status: str
    app_name: str
    environment: str


class DatabaseHealthResponse(BaseModel):
    status: str
    database: str
    migration_revision: str | None = None
    detail: str | None = None


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        app_name=settings.APP_NAME,
        environment=settings.ENVIRONMENT,
    )


@router.get("/health/db", response_model=DatabaseHealthResponse)
def database_health(db: Session = Depends(get_db)):
    """Verify the database is reachable and report the schema version.

    Reporting the Alembic revision is genuinely useful in production: the most
    common cause of "the API is throwing 500s right after a deploy" is code
    that expects a migration which has not been applied. This endpoint answers
    that question in one request instead of an hour of guessing.

    Note the error path returns 503 Service Unavailable, not 500. 503 tells a
    load balancer "try another instance, this one isn't ready" -- which is
    exactly right when a database connection is down.
    """
    try:
        db.execute(text("SELECT 1"))
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=DatabaseHealthResponse(
                status="error",
                database="unreachable",
                # str(exc) can contain the connection string, including the
                # password. Never return a raw driver error to a client.
                detail=type(exc).__name__,
            ).model_dump(),
        )

    return DatabaseHealthResponse(
        status="ok",
        database="connected",
        migration_revision=revision,
    )

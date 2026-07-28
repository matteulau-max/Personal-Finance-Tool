"""FastAPI application entrypoint.

This file does one job: assemble the app. It creates the FastAPI instance,
applies middleware (cross-cutting rules that run on every request), and
attaches the routers. Business logic never lives here -- that keeps the
entrypoint readable no matter how large the project gets.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    accounts,
    analytics,
    health,
    me,
    plaid,
    taxonomy,
    transactions,
)
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    # Interactive API documentation, auto-generated from your type hints.
    docs_url="/docs",
)

# CORS = Cross-Origin Resource Sharing. Browsers block a page served from
# localhost:3000 from calling an API on localhost:8000 unless the API
# explicitly allows that origin. We allow exactly one origin -- never "*" --
# because credentials (auth cookies/tokens) will flow across this boundary.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(me.router)
app.include_router(accounts.router)
app.include_router(plaid.router)
app.include_router(transactions.router)
app.include_router(taxonomy.router)
app.include_router(analytics.router)

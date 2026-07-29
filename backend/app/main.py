"""FastAPI application entrypoint.

This file does one job: assemble the app. It creates the FastAPI instance,
applies middleware (cross-cutting rules that run on every request), and
attaches the routers. Business logic never lives here -- that keeps the
entrypoint readable no matter how large the project gets.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import (
    accounts,
    analytics,
    health,
    insights,
    me,
    plaid,
    taxonomy,
    transactions,
)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.job_worker import start_worker, stop_worker

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the background webhook worker with the application.

    The `finally` matters more than it looks. Without a clean stop, a deploy
    kills the process mid-sync and leaves a job stuck in RUNNING until the
    reclaim pass notices half an hour later. With it, the worker finishes the
    job it is on and exits.
    """
    configure_logging(settings.LOG_LEVEL)
    start_worker()
    try:
        yield
    finally:
        stop_worker()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    lifespan=lifespan,
    # Interactive API documentation, auto-generated from your type hints.
    #
    # Off in production. The schema protects nothing by being secret -- every
    # endpoint still requires a valid token -- but publishing a complete,
    # browsable map of the API to anonymous visitors helps nobody except
    # somebody looking for a way in.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
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

# Reject requests whose Host header names a site we do not serve.
#
# This is what stops host-header poisoning: an attacker sends `Host:
# evil.example`, and anything that builds a URL from the request host -- a
# redirect, a link in an email, a cached response -- now points at their
# server. Production only, where the host list is known; locally you reach
# the API over any of localhost, 127.0.0.1 and a LAN address.
if settings.is_production and settings.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add the response headers a browser needs in order to defend itself.

    This API serves JSON to a separate frontend, so what matters is the small
    set of non-negotiable headers rather than a content policy for markup we
    never send:

    * `X-Content-Type-Options: nosniff` -- stop a browser deciding that a
      JSON response is really HTML and executing it. That guess is the
      mechanism behind a whole family of otherwise-harmless-looking bugs.
    * `Referrer-Policy: no-referrer` -- API URLs contain resource ids, and
      there is no reason for one to travel to a third party in a Referer.
    * `X-Frame-Options: DENY` -- nothing here should ever render in a frame.
    * `Cache-Control: no-store` -- every response is somebody's financial
      data, and an intermediary is entitled to cache anything not told
      otherwise.
    * `Strict-Transport-Security` -- production only. Sent from a development
      server it teaches the browser to refuse plain HTTP to localhost, which
      it then remembers for months.
    """
    response = await call_next(request)

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")

    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    return response


app.include_router(health.router)
app.include_router(me.router)
app.include_router(accounts.router)
app.include_router(plaid.router)
app.include_router(transactions.router)
app.include_router(taxonomy.router)
app.include_router(analytics.router)
app.include_router(insights.router)

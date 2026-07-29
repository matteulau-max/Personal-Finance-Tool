"""Shared FastAPI dependencies.

A "dependency" in FastAPI is a function that runs before your endpoint and
hands it a value. Declaring `current_user: CurrentUser` on an endpoint is
what makes that endpoint require authentication -- there is no separate step
to remember, and no way to receive a user without having verified one.

That property is the whole design: **authentication is a type, not a
convention.** You cannot accidentally read `current_user` in an endpoint that
did not authenticate, because the parameter would not exist.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import AuthError, TokenClaims, verify_token
from app.db import rls
from app.db.session import get_db
from app.models import User
from app.services.users import get_or_create_user

logger = logging.getLogger(__name__)

# `auto_error=False` so we can produce our own 401 with the correct
# `WWW-Authenticate` header rather than FastAPI's default 403.
bearer_scheme = HTTPBearer(auto_error=False, description="Clerk session token")

DbSession = Annotated[Session, Depends(get_db)]


def _unauthorized(log_detail: str) -> HTTPException:
    """Build a 401 that tells the client nothing useful for forging a token.

    The client always sees the same generic message. The specific reason goes
    to our logs, where it helps us debug and helps nobody attack us.
    """
    logger.warning("Authentication failed: %s", log_detail)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        # Required by the HTTP spec on a 401, and it tells clients which
        # scheme to use.
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_token_claims(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> TokenClaims:
    """Extract and verify the bearer token. No database access."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("no bearer token supplied")

    try:
        return verify_token(credentials.credentials)
    except AuthError as exc:
        raise _unauthorized(exc.log_detail) from exc


def get_current_user(
    db: DbSession,
    claims: Annotated[TokenClaims, Depends(get_token_claims)],
) -> User:
    """The verified, provisioned user behind this request."""
    user = get_or_create_user(db, claims)

    # Everything from here on runs under Row-Level Security as this user.
    #
    # This line, and not the endpoint, is where it belongs. Provisioning has
    # to run first -- looking a user up by their Clerk id, or creating them,
    # is a query that by definition cannot be filtered to a user we have not
    # identified yet. Once we know who they are, the database is told, and
    # every query the request makes afterwards is filtered whether or not the
    # code that wrote it remembered to scope it.
    #
    # It also means the protection follows the same rule as authentication:
    # an endpoint declaring `CurrentUser` gets it, and an endpoint that does
    # not declare it never receives a user to leak data to.
    rls.activate(db, user.id)

    if not user.is_active:
        # Deactivated accounts get 403, not 401: the credentials were valid,
        # the account simply is not permitted. Re-authenticating would not
        # help, and 401 would send the client into a pointless login loop.
        logger.warning("Rejected request from deactivated user %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not active",
        )

    return user


# The annotated types endpoints actually use. `CurrentUser` is the one that
# matters: writing it on an endpoint is what makes that endpoint protected.
CurrentUser = Annotated[User, Depends(get_current_user)]

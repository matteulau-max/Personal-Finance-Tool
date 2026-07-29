"""Turning a verified token into a local user row.

Clerk owns identity (passwords, MFA, social login). We own everything about
the user's *money*. This module is the bridge: given verified token claims,
find or create the matching row in our `users` table.

This is the anti-corruption layer described in `models/user.py`. Clerk's ID
enters the system in exactly one place -- here -- and everything downstream
works with our own UUID.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import TokenClaims
from app.models import User

logger = logging.getLogger(__name__)


def get_or_create_user(db: Session, claims: TokenClaims) -> User:
    """Return the local user for these claims, provisioning on first sight.

    This is "just-in-time provisioning": rather than syncing every Clerk
    signup via webhooks, we create the row the first time an authenticated
    request arrives. Fewer moving parts, and no window where someone can sign
    up but not use the app because a webhook was dropped.

    Note the retry on IntegrityError. Two requests from a brand-new user can
    arrive simultaneously -- a page load firing three parallel API calls does
    exactly this. Both find no row, both insert, and one loses. The unique
    constraint on `clerk_user_id` is what makes the loser detectable, and
    re-reading is what makes it harmless. Same principle as transaction
    deduplication: let the database arbitrate, then handle the rejection.
    """
    user = db.execute(
        select(User).where(User.clerk_user_id == claims.subject)
    ).scalar_one_or_none()

    if user is not None:
        _touch_last_seen(db, user)
        return user

    user = User(
        clerk_user_id=claims.subject,
        # Email may be absent if the Clerk JWT template does not include it.
        # A placeholder keeps the NOT NULL constraint satisfiable; the real
        # address is filled in as soon as a token carries it.
        email=claims.email or f"{claims.subject}@placeholder.invalid",
        full_name=claims.full_name,
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(user)

    try:
        # `flush` sends the INSERT now, so a conflict surfaces here where we
        # can handle it, rather than at commit time somewhere up the stack.
        db.flush()
        # ...and `commit` makes it permanent.
        #
        # This commit is easy to leave out, and leaving it out is invisible:
        # `flush` makes the row queryable within this transaction, so the
        # request still succeeds and returns a perfectly good user object.
        # But `get_db` closes the session without committing, so the INSERT is
        # rolled back and the user is silently re-created on every single
        # request -- with a new id each time. Any row that later references
        # them would point at a user that no longer exists.
        #
        # Committing here is safe because this runs in the authentication
        # dependency, before any endpoint logic, so there is nothing else
        # pending in the session that we might commit prematurely.
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(User).where(User.clerk_user_id == claims.subject)
        ).scalar_one_or_none()
        if existing is None:
            # The conflict was not the race we anticipated -- surface it.
            raise
        logger.info("Lost provisioning race for %s; using existing row", claims.subject)
        return existing

    logger.info("Provisioned new user for Clerk subject %s", claims.subject)
    return user


def _touch_last_seen(db: Session, user: User) -> None:
    """Record activity, but not on every single request.

    Writing on each request would turn every read into a write and put a row
    lock on the busiest table in the system. Once an hour is plenty for
    "when was this account last used?".
    """
    now = datetime.now(timezone.utc)
    if user.last_seen_at is None or (now - user.last_seen_at).total_seconds() > 3600:
        user.last_seen_at = now
        db.commit()

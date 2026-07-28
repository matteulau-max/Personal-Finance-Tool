"""User scoping -- the single most important security control in this app.

===========================================================================
The bug this exists to prevent
===========================================================================

In any multi-user application, the catastrophic failure is not "an attacker
broke the encryption". It is one missing WHERE clause:

    # WRONG -- returns every user's accounts to whoever asks
    db.execute(select(Account))

    # Right
    db.execute(select(Account).where(Account.user_id == current_user.id))

The second is easy to write and equally easy to forget, and nothing about
forgetting it looks wrong in code review. It fails silently: the endpoint
returns data, the tests pass (you only ever have one user in your test), and
the leak is invisible until someone else signs up.

===========================================================================
The approach: make the safe way the only convenient way
===========================================================================

`scoped_select()` cannot be called without a user. There is no default, no
optional parameter, no "None means everyone". If you want data out of a
user-owned table, you must say whose data it is.

Combined with the route guard test in `tests/test_authorization.py`, which
fails if any endpoint lacks an authentication dependency, this gives two
independent layers of protection.

**This is not the last word.** Defense in depth means not relying on
developers remembering things. Milestone 8 adds PostgreSQL Row-Level
Security, so the database itself refuses to return another user's rows even
if application code asks for them. That is the real belt-and-braces answer;
this is the belt.
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from sqlalchemy import Select, select

from app.models import (
    Account,
    AccountBalance,
    AuditLog,
    Category,
    Merchant,
    MerchantAlias,
    PlaidItem,
    Rule,
    Tag,
    Transaction,
    User,
)

# Every model that holds data belonging to one specific user.
#
# Keeping this list explicit is deliberate: adding a new user-owned table
# forces a developer to come here and think about scoping, instead of the
# question never arising.
USER_OWNED_MODELS = (
    Account,
    AuditLog,
    PlaidItem,
    Rule,
    Tag,
    Transaction,
)

# Models where `user_id IS NULL` means "shared by everyone" and a non-NULL
# value means "this user's private version". Scoping these means "mine OR
# global", not "mine only".
SHARED_OR_OWNED_MODELS = (
    Category,
    Merchant,
    MerchantAlias,
)

T = TypeVar("T")


class NotScopeable(TypeError):
    """Raised when scoping is attempted on a model that has no owner."""


def scoped_select(model: type[T], user: User) -> Select[tuple[T]]:
    """Build a SELECT that can only ever return this user's rows.

    Use this instead of `select(Model)` for anything user-owned:

        accounts = db.execute(scoped_select(Account, current_user)).scalars().all()

    For models that support shared rows (categories, merchants), this returns
    the user's own rows plus the global ones.
    """
    if model in USER_OWNED_MODELS:
        return select(model).where(model.user_id == user.id)

    if model in SHARED_OR_OWNED_MODELS:
        return select(model).where(
            (model.user_id == user.id) | (model.user_id.is_(None))
        )

    raise NotScopeable(
        f"{model.__name__} is not a user-owned model. Add it to USER_OWNED_MODELS "
        f"or SHARED_OR_OWNED_MODELS in app/db/scoping.py after deciding how it "
        f"should be scoped."
    )


def scoped_get(db, model: type[T], entity_id: uuid.UUID, user: User) -> T | None:
    """Fetch one row by id, but only if this user owns it.

    Note what this does NOT do: it never fetches the row and then checks
    ownership afterwards. The ownership test is part of the query, so a row
    belonging to someone else is indistinguishable from a row that does not
    exist.

    That distinction matters. An endpoint that returns 403 for "exists but is
    not yours" and 404 for "does not exist" leaks information: an attacker can
    enumerate valid IDs and learn how many accounts other users have. Always
    return 404 for both.
    """
    return db.execute(
        scoped_select(model, user).where(model.id == entity_id)
    ).scalar_one_or_none()

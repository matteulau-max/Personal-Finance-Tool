"""Row-Level Security -- the braces to `scoping.py`'s belt.

===========================================================================
Why this exists when we already scope every query
===========================================================================

`scoping.py` makes the safe query the convenient one. It works, and it is
still the first line of defence. But it shares a weakness with every control
of its kind: it only protects the queries that use it. A developer in a hurry
writes `select(Transaction)` instead of `scoped_select(Transaction, user)`,
the code reviews fine, the tests pass because the test has one user in it, and
the leak ships.

Row-Level Security moves the check somewhere a developer cannot forget it:
inside PostgreSQL. Once a policy is attached to a table, the database rewrites
*every* query against it -- ours, an ORM's, a psql session's, a SQL injection
payload's -- to include the ownership predicate. `SELECT * FROM transactions`
stops being a data breach and starts being a query that returns your own rows.

===========================================================================
How the user identity reaches the database
===========================================================================

Policies compare `user_id` against `app_current_user_id()`, a SQL function
that reads the `app.current_user_id` session variable. Each request sets it
once, immediately after authentication:

    request -> verify token -> find/create user -> activate(db, user.id)
                                                       |
                              SET ROLE finance_app     |
                              SET app.current_user_id  |
                                                       v
                                       every later query is filtered

If the variable is unset, the function returns NULL, `user_id = NULL` is
NULL, and NULL is not TRUE -- so the row is not visible. **The failure mode
is an empty result set, not somebody else's data.** That is the property
worth having: forgetting to set the context breaks the feature loudly instead
of leaking quietly.

===========================================================================
Why `SET ROLE` is part of it
===========================================================================

PostgreSQL exempts two kinds of role from RLS: superusers (and any role with
BYPASSRLS), and the table's owner. Our migrations run as the owner -- they
have to, they create the tables -- so simply attaching policies would protect
nothing if the application connects as that same role.

So the application switches: `SET ROLE finance_app` makes `current_user` a
plain role that owns nothing and bypasses nothing, and policies apply from
that statement onward.

Be precise about what this buys. `SET ROLE` is reversible by whoever issued
it, so this is **not** a defence against an attacker who can already run
arbitrary SQL as our connection role -- at that point they can `RESET ROLE`.
It is a defence against *our own code being wrong*, which is the failure that
actually happens. In production you close the remaining gap by connecting as
a login role that is a member of `finance_app` and owns nothing; then there
is no owner privilege to reset back to. `docs/milestone-08-hardening.md`
spells that out.

===========================================================================
Why `SET`, not `SET LOCAL`
===========================================================================

`SET LOCAL` scopes a setting to the current transaction, which sounds exactly
right and is a trap here: endpoints call `db.commit()`, and the commit ends
the transaction. Every statement after the first commit would run with no
user context -- and, because we fail closed, would return nothing. The symptom
is a page that renders correctly until you save something.

So the setting is session-scoped, which means it outlives the transaction --
and would outlive the *request* too, riding a pooled connection into whoever
gets it next. That is a cross-user leak with extra steps, so it is closed
twice over: `get_db` calls `deactivate()` in a `finally`, and the connection
pool clears the state again on check-in (see `session.py`). Neither alone is
something to bet a financial record on.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

# --- Table classification -------------------------------------------------
#
# These sets describe how each table is protected. They are duplicated as
# literal SQL inside the migration -- deliberately, because a migration must
# keep doing what it did the day it was written, not follow this file as it
# changes. `tests/test_rls.py` compares all three against the live database
# and against `Base.metadata`, so the duplication cannot drift unnoticed.

# `user_id` names the owner outright.
OWNED_TABLES = frozenset(
    {
        "accounts",
        "audit_log",
        "budgets",
        "plaid_items",
        "rules",
        "tags",
        "transactions",
    }
)

# `user_id IS NULL` means "shared with everyone". Readable by all, writable
# by nobody through the application -- new global rows are seeded by
# migrations, which run as the owner and are not subject to policies.
SHARED_TABLES = frozenset({"categories", "merchants", "merchant_aliases"})

# No `user_id` of their own; ownership is inherited through a parent row.
# Maps table -> (foreign key column, parent table, parent key column).
DERIVED_TABLES: dict[str, tuple[str, str, str]] = {
    "account_balances": ("account_id", "accounts", "id"),
    "sync_history": ("plaid_item_id", "plaid_items", "id"),
    "transaction_tags": ("transaction_id", "transactions", "id"),
    # Queued webhook work. The worker itself runs as the owning role and is
    # not subject to policies -- it has no user logged in, by definition --
    # but nothing an authenticated request can reach sees another user's jobs.
    "webhook_jobs": ("plaid_item_id", "plaid_items", "id"),
}

# The users table is its own case: the owning user *is* the row.
SELF_TABLES = frozenset({"users"})

# Tables with no user data in them at all. This is an allowlist, and the test
# suite fails if a table appears that is on none of these lists -- so a new
# table is a decision someone makes, not an omission nobody notices.
EXEMPT_TABLES = frozenset(
    {
        # Bank names, logos and Plaid institution ids. Shared reference data;
        # every user linking Chase writes the same row. Enabling RLS here
        # would mean either a policy that permits everything (theatre) or a
        # per-user copy of the same fifty institutions.
        "institutions",
        # Alembic's bookkeeping. The application role is granted SELECT only.
        "alembic_version",
    }
)

PROTECTED_TABLES = (
    OWNED_TABLES | SHARED_TABLES | SELF_TABLES | frozenset(DERIVED_TABLES)
)

# The GUC the policies read. "app." prefix because PostgreSQL requires custom
# settings to be namespaced.
USER_ID_SETTING = "app.current_user_id"

_SAFE_ROLE = re.compile(r"^[a-z_][a-z0-9_]*$")


class RLSConfigurationError(RuntimeError):
    """The configured application role is unusable."""


def _role() -> str:
    """The role to switch into, validated as an identifier.

    `SET ROLE` takes an identifier, and identifiers cannot be passed as bind
    parameters -- the name has to be interpolated into the statement. So it is
    checked against a strict pattern first. The value comes from our own
    configuration rather than from a request, but "it isn't attacker
    controlled today" is a property that quietly stops being true.
    """
    from app.core.config import get_settings

    role = get_settings().DB_APP_ROLE
    if not _SAFE_ROLE.match(role):
        raise RLSConfigurationError(
            f"DB_APP_ROLE must be a plain lowercase identifier, got {role!r}"
        )
    return role


def activate(db: Session, user_id: uuid.UUID) -> None:
    """Put this session under Row-Level Security for one user.

    Called once per request, from `get_current_user`. Everything the request
    does afterwards -- ORM queries, raw SQL, analytics aggregates, the AI
    insight tools -- is filtered by the database.
    """
    db.execute(text(f'SET ROLE "{_role()}"'))
    # `set_config` rather than `SET`, because SET cannot take a bind
    # parameter and a user id must never be string-formatted into SQL.
    # Third argument false = session scope, not transaction scope.
    db.execute(
        text("SELECT set_config(:name, :value, false)"),
        {"name": USER_ID_SETTING, "value": str(user_id)},
    )


def deactivate(db: Session) -> None:
    """Clear the context before the connection returns to the pool."""
    db.execute(
        text("SELECT set_config(:name, '', false)"), {"name": USER_ID_SETTING}
    )
    db.execute(text("RESET ROLE"))


def current_user_id(db: Session) -> uuid.UUID | None:
    """What the database currently thinks this session's user is.

    Exists for tests and for debugging a "why is this list empty?" report,
    which is the question RLS turns a leak into.
    """
    value = db.execute(
        text("SELECT NULLIF(current_setting(:name, true), '')"),
        {"name": USER_ID_SETTING},
    ).scalar()
    return uuid.UUID(value) if value else None

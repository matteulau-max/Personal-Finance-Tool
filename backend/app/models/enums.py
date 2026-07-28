"""Enumerations used across the schema.

A design decision worth explaining: every `Enum` column in this project is
declared with `native_enum=False`.

PostgreSQL has a real ENUM type. It looks appealing -- the database rejects
invalid values. But adding a value to a native enum requires `ALTER TYPE`,
which historically could not run inside a transaction, and *removing* one is
close to impossible without rewriting the type and every table that uses it.

`native_enum=False` stores the value as VARCHAR with a CHECK constraint. You
get the same validation, and changing the allowed set is a trivial migration.
For a schema that will keep growing (new account types, new sync outcomes),
that flexibility is worth far more than the marginal storage saving.

There is one sharp edge worth knowing about, because it bites almost everyone:
by default SQLAlchemy persists an enum's **name** (`POSTED`), not its
**value** (`posted`). So `TransactionStatus.POSTED` with value `"posted"`
lands in the database as the string `"POSTED"`. That mismatch shows up later
as uppercase strings leaking into API responses and as raw SQL queries that
mysteriously match nothing.

`enum_column()` below fixes it once, centrally, with `values_callable`. Use it
for every enum column in this project rather than constructing `Enum(...)` by
hand.
"""

import enum

from sqlalchemy import Enum as SAEnum


def enum_column(enum_class: type[enum.Enum], length: int = 32) -> SAEnum:
    """Build the Enum column type this project uses everywhere.

    - `native_enum=False`      -> VARCHAR instead of a PostgreSQL ENUM type,
                                  so the allowed set is easy to change.
    - `create_constraint=True` -> actually emit the CHECK constraint. This
                                  defaults to FALSE in SQLAlchemy 2.0, which
                                  means the obvious spelling of this column
                                  gives you a plain VARCHAR with NO validation
                                  at all -- the database would happily store
                                  "banana" in a status column.
    - `values_callable`        -> persist the lowercase VALUE, not the NAME.
    """
    return SAEnum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        # Constraints need explicit names to satisfy the project's naming
        # convention, which is what makes them alterable in later migrations.
        name=f"ck_{enum_class.__name__.lower()}",
        length=length,
        values_callable=lambda members: [member.value for member in members],
    )


class AccountType(str, enum.Enum):
    """Top-level account classification.

    Mirrors Plaid's `type` field. `str` mixin means the value serializes to
    JSON as "depository" rather than "AccountType.DEPOSITORY".
    """

    DEPOSITORY = "depository"  # checking, savings
    CREDIT = "credit"  # credit cards
    LOAN = "loan"  # mortgage, student, auto
    INVESTMENT = "investment"  # brokerage, 401k
    OTHER = "other"  # Venmo and anything unmapped


class TransactionStatus(str, enum.Enum):
    """Lifecycle of a transaction.

    PENDING -> POSTED is the normal path. REMOVED means the source told us the
    transaction no longer exists (a declined authorization, a reversed charge).
    We never delete the row -- see the note on soft deletion in
    `models/transaction.py`.
    """

    PENDING = "pending"
    POSTED = "posted"
    REMOVED = "removed"


class TransactionSource(str, enum.Enum):
    """Where a transaction came from. Determines who is allowed to edit it."""

    PLAID = "plaid"
    MANUAL = "manual"
    CSV_IMPORT = "csv_import"


class CategorySource(str, enum.Enum):
    """How a transaction's automatic category was decided.

    Recording this lets us answer "why is this categorized as Groceries?" and
    lets a better categorizer overwrite a worse one's guess without ever
    overwriting a human's explicit choice.
    """

    NONE = "none"
    PLAID = "plaid"  # Plaid's own suggestion
    RULE = "rule"  # one of the user's rules matched
    HEURISTIC = "heuristic"  # merchant-name matching
    MODEL = "model"  # ML / LLM classification (later)


class SyncStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class SyncTrigger(str, enum.Enum):
    """What caused a sync to run. Useful for debugging and rate limiting."""

    WEBHOOK = "webhook"
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    INITIAL = "initial"


class PlaidItemStatus(str, enum.Enum):
    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"  # user must re-authenticate
    ERROR = "error"
    DISCONNECTED = "disconnected"  # user removed it


class AuditAction(str, enum.Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"


class AuditActor(str, enum.Enum):
    """Who made a change. The whole point of the audit log is telling a bank
    correction apart from a user correction apart from an automated rule."""

    USER = "user"
    SYNC = "sync"
    RULE = "rule"
    SYSTEM = "system"

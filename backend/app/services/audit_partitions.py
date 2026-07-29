"""Keeping the audit log's monthly partitions ahead of the calendar.

===========================================================================
Why this needs a maintenance job at all
===========================================================================

A range-partitioned table has no partition for a month nobody created. An
INSERT for a date with no home fails -- and every audit write happens inside
the transaction that made the change it records, so a missing partition on
the 1st of the month does not merely lose an audit entry: it fails the user's
edit.

That is an unacceptable way to find out you forgot a cron job, so there are
two defences:

  1. **A DEFAULT partition.** Anything with nowhere else to go lands there,
     and the write succeeds. It is insurance, not a plan.
  2. **This module**, which creates partitions ahead of time and is called by
     the background worker.

===========================================================================
The catch with the default partition, stated plainly
===========================================================================

While rows for July sit in the default partition, PostgreSQL will refuse to
create the July partition -- it would have to move them, and `CREATE TABLE
... PARTITION OF` will not. Recovering means detaching the default, moving
the rows by hand, and reattaching.

So the default partition is a safety net whose whole purpose is to stay
empty. `default_partition_row_count()` exists to be alerted on: a non-zero
value means the maintenance job has not run and somebody has a few days to
notice before it becomes a manual data migration.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TABLE = "audit_log"
DEFAULT_PARTITION = f"{TABLE}_default"

# How far ahead to keep partitions. Three months means the maintenance job
# can be broken for a full quarter before anything lands in the default
# partition -- comfortably longer than it takes anyone to notice an alert.
MONTHS_AHEAD = 3

_PARTITION_NAME = re.compile(rf"^{TABLE}_\d{{4}}_\d{{2}}$")


def _month_start(when: date) -> date:
    return when.replace(day=1)


def _next_month(when: date) -> date:
    return date(when.year + (when.month == 12), (when.month % 12) + 1, 1)


def partition_name(month: date) -> str:
    return f"{TABLE}_{month.year:04d}_{month.month:02d}"


def _revoke_direct_access(db: Session, partition: str) -> None:
    """Make a new partition reachable only through its parent.

    Easy to miss, and it matters. Row-Level Security policies belong to the
    parent table and are applied to queries that go through it -- a partition
    is a table of its own, with its own privileges and no policies. The
    schema's default privileges hand the application role DML on every new
    table, and a partition is a new table, so without this
    `SELECT * FROM audit_log_2026_08` would return every user's audit trail
    while neatly stepping around the policy on `audit_log`.

    Nothing in the application names a partition, and access through the
    parent is checked against the parent, so revoking costs nothing.
    """
    from app.core.config import get_settings

    role = get_settings().DB_APP_ROLE
    if not re.match(r"^[a-z_][a-z0-9_]*$", role):  # pragma: no cover - defensive
        raise ValueError(f"refusing to use {role!r} as a role name")

    db.execute(text(f"REVOKE ALL ON {partition} FROM {role}"))


def ensure_partitions(
    db: Session, *, today: date | None = None, months_ahead: int = MONTHS_AHEAD
) -> list[str]:
    """Create any missing partitions for this month and the next few.

    Returns the names actually created, which is empty on almost every call --
    this is designed to be run often and cheaply rather than scheduled
    precisely.

    `IF NOT EXISTS` on the CREATE as well as the check above it: two workers
    running this at the same moment is normal, and "check then create" has a
    race between its two halves.
    """
    today = today or datetime.now(timezone.utc).date()
    present = set(existing_partitions(db))
    month = _month_start(today)
    created: list[str] = []

    for _ in range(months_ahead + 1):
        upper = _next_month(month)
        name = partition_name(month)

        # Interpolated, not bound: a table name cannot be a bind parameter.
        # Every value here is derived from a `date` rather than from anything
        # a request supplied, and the name is checked against a pattern
        # before it reaches the database.
        if not _PARTITION_NAME.match(name):  # pragma: no cover - defensive
            raise ValueError(f"refusing to create partition named {name!r}")

        if name not in present:
            db.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {TABLE} "
                    f"FOR VALUES FROM ('{month.isoformat()}') TO ('{upper.isoformat()}')"
                )
            )
            _revoke_direct_access(db, name)
            created.append(name)

        month = upper

    db.commit()
    if created:
        logger.info("Created audit partitions: %s", ", ".join(created))
    return created


def existing_partitions(db: Session) -> list[str]:
    """Every partition currently attached, oldest first."""
    rows = db.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_class parent ON parent.oid = i.inhparent "
            "WHERE parent.relname = :parent ORDER BY c.relname"
        ),
        {"parent": TABLE},
    )
    return [row[0] for row in rows]


def default_partition_row_count(db: Session) -> int:
    """How many rows have landed in the safety net.

    Alert on this. Zero is the expected value at all times; anything else
    means partition maintenance stopped running, and the longer it stays
    non-zero the more manual the recovery becomes.
    """
    return db.execute(text(f"SELECT count(*) FROM ONLY {DEFAULT_PARTITION}")).scalar_one()


def drop_partitions_before(db: Session, cutoff: date) -> list[str]:
    """Detach and drop whole months older than `cutoff`.

    This is the payoff for partitioning at all. Applying a retention policy
    to an unpartitioned audit log means `DELETE FROM audit_log WHERE
    created_at < ...`, which on a large table rewrites it, holds locks, and
    leaves the disk space to autovacuum. Dropping a partition is a catalogue
    update and returns the space immediately.

    Nothing calls this automatically, and that is deliberate: a retention
    period is a policy decision -- possibly a regulatory one -- and not
    something a maintenance job should quietly enact.
    """
    dropped = []

    for name in existing_partitions(db):
        if not _PARTITION_NAME.match(name):
            continue  # never the default partition
        year, month = int(name[-7:-3]), int(name[-2:])
        if date(year, month, 1) < _month_start(cutoff):
            db.execute(text(f"DROP TABLE {name}"))
            dropped.append(name)

    db.commit()
    if dropped:
        logger.info("Dropped audit partitions: %s", ", ".join(dropped))
    return dropped

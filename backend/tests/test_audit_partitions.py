"""Audit log partitioning tests.

Partitioning is invisible when it works and expensive when it does not, so
what is worth testing is the failure surface: a month with no partition, a
partition reachable around the security policy, and the retention operation
that partitioning exists to make cheap.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.db import rls
from app.models import AuditAction, AuditActor, AuditLog, User
from app.services import audit_partitions


def entry(user: User, *, created_at: datetime | None = None) -> AuditLog:
    row = AuditLog(
        user_id=user.id,
        entity_type="transaction",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        actor=AuditActor.USER,
        reason="test",
    )
    if created_at is not None:
        row.created_at = created_at
    return row


# ---------------------------------------------------------------------------
# The table is actually partitioned
# ---------------------------------------------------------------------------


def test_the_audit_log_is_a_partitioned_table(db: Session):
    relkind = db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = 'audit_log'")
    ).scalar_one()

    assert relkind == "p"


def test_a_row_lands_in_the_partition_for_its_month(db: Session, user: User):
    """Nothing in the application knows partitions exist. That is the test:
    an ordinary INSERT through the ORM ends up in the right place."""
    when = datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)
    row = entry(user, created_at=when)
    db.add(row)
    db.flush()

    located = db.execute(
        text("SELECT tableoid::regclass::text FROM audit_log WHERE id = :id"),
        {"id": row.id},
    ).scalar_one()

    assert located == "audit_log_2026_07"


def test_reading_back_does_not_care_which_partition(db: Session, user: User):
    """A query against the parent spans every partition, so code written
    before this change keeps working -- which is the whole bargain."""
    db.add(entry(user, created_at=datetime(2026, 6, 2, tzinfo=timezone.utc)))
    db.add(entry(user, created_at=datetime(2026, 7, 2, tzinfo=timezone.utc)))
    db.flush()

    rows = db.execute(select(AuditLog).where(AuditLog.user_id == user.id)).scalars().all()

    assert len(rows) == 2


# ---------------------------------------------------------------------------
# The security hole partitioning introduces
# ---------------------------------------------------------------------------


def test_a_partition_cannot_be_read_directly(db: Session, user: User):
    """The trap in partitioning a table that has RLS on it.

    Policies belong to the parent. A partition is a table in its own right,
    with its own privileges and no policies -- and the schema's default
    privileges hand the application role DML on every new table. Left alone,
    `SELECT * FROM audit_log_2026_07` returns every user's audit trail,
    having stepped politely around the policy on `audit_log`.

    So direct access is revoked when a partition is created. This test is the
    only thing standing between that reasoning and a silent regression the
    next time someone adds a partition by hand.
    """
    db.add(entry(user, created_at=datetime(2026, 7, 15, tzinfo=timezone.utc)))
    db.flush()
    rls.activate(db, user.id)

    with pytest.raises(ProgrammingError) as caught:
        db.execute(text("SELECT count(*) FROM audit_log_2026_07"))

    assert "permission denied" in str(caught.value).lower()
    db.rollback()


def test_the_parent_table_still_filters_by_user(db: Session, user: User, other_user):
    """The policy survived the table being rebuilt -- easy to lose in a
    migration that drops and recreates a table."""
    other, _ = other_user
    db.add(entry(user, created_at=datetime(2026, 7, 15, tzinfo=timezone.utc)))
    db.add(entry(other, created_at=datetime(2026, 7, 15, tzinfo=timezone.utc)))
    db.flush()

    rls.activate(db, user.id)

    rows = db.execute(select(AuditLog)).scalars().all()
    assert {row.user_id for row in rows} == {user.id}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def test_partitions_are_created_ahead_of_the_calendar(db: Session):
    """The point of running ahead: a month with no partition is not a
    logging problem, it is a failed user edit, because the audit write shares
    a transaction with the change it records."""
    far_future = date(2030, 1, 15)

    created = audit_partitions.ensure_partitions(db, today=far_future, months_ahead=3)

    assert created == [
        "audit_log_2030_01",
        "audit_log_2030_02",
        "audit_log_2030_03",
        "audit_log_2030_04",
    ]


def test_maintenance_is_safe_to_run_repeatedly(db: Session):
    """It runs hourly, forever. The second call must be a no-op rather than
    an error, or the worker log fills with failures nobody should act on."""
    audit_partitions.ensure_partitions(db, today=date(2031, 3, 1))

    assert audit_partitions.ensure_partitions(db, today=date(2031, 3, 1)) == []


def test_the_year_boundary_is_handled(db: Session):
    """December's partition runs to January of the next year. Getting this
    wrong produces a partition covering month 13, which PostgreSQL rejects --
    on New Year's Eve."""
    created = audit_partitions.ensure_partitions(
        db, today=date(2032, 12, 4), months_ahead=1
    )

    assert created == ["audit_log_2032_12", "audit_log_2033_01"]


def test_a_new_partition_is_locked_down_like_the_others(db: Session):
    audit_partitions.ensure_partitions(db, today=date(2033, 5, 1), months_ahead=0)

    granted = db.execute(
        text(
            "SELECT has_table_privilege('finance_app', 'audit_log_2033_05', 'SELECT')"
        )
    ).scalar_one()

    assert granted is False


def test_the_default_partition_is_empty_and_watched(db: Session, user: User):
    """It is a safety net that is supposed to stay empty.

    While rows for a month sit in the default partition, PostgreSQL refuses
    to create that month's partition -- so a non-zero count here is a problem
    that gets more manual the longer it is ignored. Hence a function whose
    only purpose is to be alerted on.
    """
    assert audit_partitions.default_partition_row_count(db) == 0


def test_retention_drops_whole_months(db: Session, user: User):
    """The payoff.

    Applying a retention policy to an unpartitioned audit log means a DELETE
    that rewrites the table and leaves the space to autovacuum. Here it is a
    catalogue update, and the disk comes back immediately.
    """
    audit_partitions.ensure_partitions(db, today=date(2029, 1, 1), months_ahead=1)
    before = set(audit_partitions.existing_partitions(db))
    assert "audit_log_2029_01" in before

    dropped = audit_partitions.drop_partitions_before(db, date(2029, 2, 1))

    assert "audit_log_2029_01" in dropped
    assert "audit_log_2029_01" not in audit_partitions.existing_partitions(db)


def test_retention_never_drops_the_default_partition(db: Session):
    """Dropping it would turn every future gap in the partition calendar from
    a warning into a failed write."""
    dropped = audit_partitions.drop_partitions_before(db, date(2099, 1, 1))

    assert audit_partitions.DEFAULT_PARTITION not in dropped
    assert (
        audit_partitions.DEFAULT_PARTITION
        in audit_partitions.existing_partitions(db)
    )


def test_recent_partitions_are_kept(db: Session):
    audit_partitions.ensure_partitions(db, today=date(2028, 6, 1), months_ahead=1)

    dropped = audit_partitions.drop_partitions_before(db, date(2028, 6, 1))

    assert "audit_log_2028_06" not in dropped
    assert "audit_log_2028_07" not in dropped

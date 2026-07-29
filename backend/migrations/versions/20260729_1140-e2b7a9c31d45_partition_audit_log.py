"""Partition the audit log by month

The audit log grows faster than any other table and nothing ever deletes from
it. Converting it to a range-partitioned table now -- while it holds a
handful of rows -- turns a future all-night migration into a five-minute one
today.

===========================================================================
How you convert a live table to a partitioned one
===========================================================================

PostgreSQL cannot turn an ordinary table into a partitioned table in place.
The shape of the operation is always:

    rename the old table out of the way
    create the partitioned table under the original name
    create partitions covering the data that exists
    copy the rows in
    drop the old table

This is safe here because it runs inside one transaction (PostgreSQL takes
DDL transactionally, which not every database does) -- either the whole swap
happens or none of it does. On a table with tens of millions of rows the copy
would need to be done in batches with the application still writing, which is
exactly the migration this change exists to avoid ever having to run.

===========================================================================
The primary key has to change
===========================================================================

A unique constraint on a partitioned table must contain the partition key,
because PostgreSQL enforces uniqueness within a partition and will not scan
every partition to check. So the key becomes `(created_at, id)` -- partition
key first, matching the model. `id` remains a UUID and remains unique in
practice; the database just no longer promises it across partitions, which
means `session.get(AuditLog, some_id)` needs both halves and code that had
one should query by `id` instead.

Revision ID: e2b7a9c31d45
Revises: c4d8b1f60e73
Create Date: 2026-07-29 11:40:00

"""

from alembic import op

revision = "e2b7a9c31d45"
down_revision = "c4d8b1f60e73"
branch_labels = None
depends_on = None

COLUMNS = (
    "id, user_id, entity_type, entity_id, action, actor, before_values, "
    "after_values, reason, ip_address, user_agent, created_at, updated_at"
)


def _lock_down(partition: str) -> None:
    """Make a partition unreachable except through its parent.

    This closes a hole that is easy to miss. RLS policies live on the parent
    and are applied to queries that go through it -- but a partition is also
    a table in its own right, with its own privileges and no policies of its
    own. `ALTER DEFAULT PRIVILEGES` (set when RLS was introduced) grants the
    application role DML on every new table in the schema, and a partition is
    a new table. So without this line, `SELECT * FROM audit_log_2026_07`
    would return every user's audit entries, having neatly stepped around the
    policy on `audit_log`.

    Revoking direct access is the fix rather than duplicating the policy onto
    each partition: nothing in the application has any reason to name a
    partition, and permissions for queries through the parent are checked on
    the parent.
    """
    op.execute(f"REVOKE ALL ON {partition} FROM finance_app")


def upgrade() -> None:
    connection = op.get_bind()

    op.execute("ALTER TABLE audit_log RENAME TO audit_log_unpartitioned")
    # Indexes and constraints follow the rename and would collide with the
    # new table's, so they are renamed out of the way too.
    op.execute("ALTER INDEX pk_audit_log RENAME TO pk_audit_log_unpartitioned")
    op.execute(
        "ALTER INDEX ix_audit_log_entity_type_entity_id "
        "RENAME TO ix_audit_log_unpart_entity"
    )
    op.execute(
        "ALTER INDEX ix_audit_log_user_id_created_at RENAME TO ix_audit_log_unpart_user"
    )

    op.execute(
        """
        CREATE TABLE audit_log (
            id uuid NOT NULL,
            user_id uuid,
            entity_type varchar(64) NOT NULL,
            entity_id uuid NOT NULL,
            action varchar(32) NOT NULL,
            actor varchar(32) NOT NULL,
            before_values jsonb,
            after_values jsonb,
            reason text,
            ip_address varchar(45),
            user_agent varchar(512),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_audit_log PRIMARY KEY (created_at, id),
            CONSTRAINT ck_audit_log_ck_auditaction
                CHECK (action IN ('create', 'update', 'delete', 'restore')),
            CONSTRAINT ck_audit_log_ck_auditactor
                CHECK (actor IN ('user', 'sync', 'rule', 'system')),
            CONSTRAINT fk_audit_log_user_id_users
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        ) PARTITION BY RANGE (created_at)
        """
    )

    # The safety net. Any row whose month has no partition lands here instead
    # of failing the INSERT -- and an audit INSERT failing would fail the
    # user's edit along with it, since they share a transaction.
    #
    # It is meant to stay empty: see app/services/audit_partitions.py for why
    # a non-empty default partition is a problem that grows.
    op.execute("CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT")
    _lock_down("audit_log_default")

    # Partitions for every month that already has rows, plus a year forward
    # from today, so a fresh deployment does not depend on the maintenance
    # job having run even once.
    months = connection.exec_driver_sql(
        """
        SELECT DISTINCT date_trunc('month', created_at)::date
        FROM audit_log_unpartitioned
        UNION
        SELECT generate_series(
            date_trunc('month', now()),
            date_trunc('month', now()) + interval '12 months',
            interval '1 month'
        )::date
        ORDER BY 1
        """
    ).scalars().all()

    for month in months:
        upper = (
            month.replace(year=month.year + 1, month=1, day=1)
            if month.month == 12
            else month.replace(month=month.month + 1, day=1)
        )
        op.execute(
            f"CREATE TABLE audit_log_{month.year:04d}_{month.month:02d} "
            f"PARTITION OF audit_log "
            f"FOR VALUES FROM ('{month.isoformat()}') TO ('{upper.isoformat()}')"
        )
        _lock_down(f"audit_log_{month.year:04d}_{month.month:02d}")

    # Columns named explicitly. `SELECT *` copies by position, and the old
    # table's column order is whatever Alembic emitted in 2026 -- which is not
    # the order written above. It fails loudly on a type mismatch, and would
    # fail silently on two columns of the same type.
    op.execute(f"INSERT INTO audit_log ({COLUMNS}) SELECT {COLUMNS} FROM audit_log_unpartitioned")
    op.execute("DROP TABLE audit_log_unpartitioned")

    # Creating an index on the parent creates it on every partition, now and
    # in future -- which is what makes adding a partition a one-line
    # operation rather than a checklist.
    op.execute(
        "CREATE INDEX ix_audit_log_entity_type_entity_id "
        "ON audit_log (entity_type, entity_id)"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_user_id_created_at ON audit_log (user_id, created_at)"
    )

    # RLS has to be re-established: the policy went with the old table.
    # Declared on the parent, where PostgreSQL applies it to every partition
    # reached through it.
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_log_owner ON audit_log
            USING (user_id = app_current_user_id())
            WITH CHECK (user_id = app_current_user_id())
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON audit_log TO finance_app"
    )


def downgrade() -> None:
    """Collapse the partitions back into a single table.

    Worth having even though nobody plans to run it: a downgrade that does
    not exist is a change nobody can back out of at 3am.
    """
    op.execute("ALTER TABLE audit_log RENAME TO audit_log_partitioned")

    # Renaming a table does not rename its indexes, and index names share one
    # namespace across the schema -- so `pk_audit_log` is still taken and
    # creating the replacement table below fails with "relation
    # \"pk_audit_log\" already exists".
    #
    # `upgrade()` does the same three renames for the same reason. Getting it
    # wrong here rather than there is worse: an upgrade fails in staging,
    # while a downgrade fails at the moment somebody is trying to back out of
    # a bad deploy.
    op.execute("ALTER INDEX pk_audit_log RENAME TO pk_audit_log_partitioned")
    op.execute(
        "ALTER INDEX ix_audit_log_entity_type_entity_id "
        "RENAME TO ix_audit_log_part_entity"
    )
    op.execute(
        "ALTER INDEX ix_audit_log_user_id_created_at RENAME TO ix_audit_log_part_user"
    )

    op.execute(
        """
        CREATE TABLE audit_log (
            id uuid NOT NULL,
            user_id uuid,
            entity_type varchar(64) NOT NULL,
            entity_id uuid NOT NULL,
            action varchar(32) NOT NULL,
            actor varchar(32) NOT NULL,
            before_values jsonb,
            after_values jsonb,
            reason text,
            ip_address varchar(45),
            user_agent varchar(512),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_audit_log PRIMARY KEY (id),
            CONSTRAINT ck_audit_log_ck_auditaction
                CHECK (action IN ('create', 'update', 'delete', 'restore')),
            CONSTRAINT ck_audit_log_ck_auditactor
                CHECK (actor IN ('user', 'sync', 'rule', 'system')),
            CONSTRAINT fk_audit_log_user_id_users
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(f"INSERT INTO audit_log ({COLUMNS}) SELECT {COLUMNS} FROM audit_log_partitioned")
    op.execute("DROP TABLE audit_log_partitioned CASCADE")

    op.execute(
        "CREATE INDEX ix_audit_log_entity_type_entity_id "
        "ON audit_log (entity_type, entity_id)"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_user_id_created_at ON audit_log (user_id, created_at)"
    )
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_log_owner ON audit_log
            USING (user_id = app_current_user_id())
            WITH CHECK (user_id = app_current_user_id())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON audit_log TO finance_app")

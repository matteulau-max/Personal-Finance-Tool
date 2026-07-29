"""Add the webhook job queue

Moves Plaid webhook processing off the request thread and into a durable
queue. See `app/services/job_queue.py` for why this is a table rather than
Celery or a FastAPI background task.

Revision ID: c4d8b1f60e73
Revises: a7c1e9d4b2f0
Create Date: 2026-07-29 10:15:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4d8b1f60e73"
down_revision = "a7c1e9d4b2f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plaid_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("webhook_type", sa.String(length=64), nullable=False),
        sa.Column("webhook_code", sa.String(length=64), nullable=False),
        sa.Column("plaid_item_external_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                name="ck_jobstatus",
                native_enum=False,
                length=32,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["plaid_item_id"],
            ["plaid_items.id"],
            name=op.f("fk_webhook_jobs_plaid_item_id_plaid_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_jobs")),
    )

    op.create_index(
        "ix_webhook_jobs_status_next_attempt_at",
        "webhook_jobs",
        ["status", "next_attempt_at"],
    )

    # Partial unique index: at most one PENDING job per item per webhook code.
    # This is what collapses Plaid's retries -- and any burst of identical
    # webhooks -- into a single sync.
    op.create_index(
        "uq_webhook_jobs_pending_item_code",
        "webhook_jobs",
        ["plaid_item_id", "webhook_code"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Row-Level Security, on the same terms as every other table that can be
    # traced to a user. The worker connects as the owning role and is not
    # subject to policies -- which it has to be, since it runs with no user
    # logged in -- but nothing reachable from an authenticated request can
    # read another user's queued work.
    op.execute("ALTER TABLE webhook_jobs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY webhook_jobs_via_parent ON webhook_jobs
            USING (EXISTS (
                SELECT 1 FROM plaid_items p
                WHERE p.id = webhook_jobs.plaid_item_id
                  AND p.user_id = app_current_user_id()
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM plaid_items p
                WHERE p.id = webhook_jobs.plaid_item_id
                  AND p.user_id = app_current_user_id()
            ))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS webhook_jobs_via_parent ON webhook_jobs")
    op.drop_index("uq_webhook_jobs_pending_item_code", table_name="webhook_jobs")
    op.drop_index("ix_webhook_jobs_status_next_attempt_at", table_name="webhook_jobs")
    op.drop_table("webhook_jobs")

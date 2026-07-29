"""Queued webhook work.

===========================================================================
Why a table and not a background task
===========================================================================

The obvious way to stop a webhook handler blocking is FastAPI's
`BackgroundTasks`, which runs the work after the response is sent. It is one
line, and it loses the job the moment the process restarts -- which happens
on every deploy. For a webhook saying "this bank has new transactions", a
lost job means a user's data is silently stale until something else triggers
a sync.

The other obvious answer is Celery or RQ, which means running Redis, a
broker, a worker fleet and their monitoring. That is the right call at scale
and considerable machinery for an application whose queue depth is measured
in single digits.

A table in the database we already run sits between the two. Jobs survive a
restart because they are rows; `FOR UPDATE SKIP LOCKED` lets several workers
share the queue without stepping on each other; retries and backoff are
columns rather than infrastructure. It is a real queue with real durability,
and when it stops being enough, the interface to replace is `enqueue` and
`claim_due_jobs`.

===========================================================================
What is stored, and what is deliberately not
===========================================================================

The webhook's type, code and item id -- enough to do the work. Not the raw
payload: it is not needed after parsing, and a table of third-party JSON
blobs is a place sensitive fields accumulate without anyone deciding they
should. If a webhook shape changes, the sync itself is the source of truth.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import JobStatus, enum_column


class WebhookJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_jobs"
    __table_args__ = (
        # The worker's only query: due, pending, oldest first.
        Index(
            "ix_webhook_jobs_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        # Collapse a retry storm into one job.
        #
        # Plaid resends a webhook when we do not answer quickly enough, and a
        # bank that has just synced can emit several DEFAULT_UPDATE events in
        # a row. Without this, each becomes a separate sync of the same item.
        # The sync engine is idempotent so nothing would corrupt -- it would
        # just do the same expensive work several times over.
        #
        # Partial index: only PENDING rows are constrained, so the history of
        # completed jobs is not affected.
        Index(
            "uq_webhook_jobs_pending_item_code",
            "plaid_item_id",
            "webhook_code",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # Nullable: a webhook can arrive for an item we do not have (already
    # disconnected, or never ours). Those are recorded and discarded rather
    # than dropped at the door, because "we received it and ignored it" is a
    # different diagnosis from "it never arrived".
    plaid_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("plaid_items.id", ondelete="CASCADE"),
        nullable=True,
    )

    webhook_type: Mapped[str] = mapped_column(String(64), nullable=False)
    webhook_code: Mapped[str] = mapped_column(String(64), nullable=False)
    plaid_item_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), nullable=False, default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)

    # When this job may next be tried. Set to now on creation, and pushed
    # into the future by each failure -- so "retry with backoff" is a column
    # comparison rather than a sleeping thread.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The last failure, kept so a dead job can be explained without digging
    # through logs from a week ago.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<WebhookJob {self.webhook_type}/{self.webhook_code} "
            f"{self.status} attempts={self.attempts}>"
        )

"""The audit log -- an append-only record of every change to financial data.

This table is never updated and never deleted from. Rows only get added.

What it buys us:

  * "Why does this say Groceries?" -> because a rule set it on the 3rd, then
    you changed it on the 9th. Both events are here, with timestamps.
  * Recovery from a bad deployment: the `before_values` column holds the old
    state of every field that changed, so a bad bulk update can be reversed.
  * Telling a bank correction apart from a user correction. If your bank
    restates an amount, that shows up as actor=SYNC; if you edited it, it is
    actor=USER. Without this distinction "the data changed" is unexplainable.

`before_values` and `after_values` store only the fields that actually
changed, as JSONB -- not full row copies. A full copy of every version would
grow without bound; a diff stays small and is what you actually want to read.

===========================================================================
Partitioning
===========================================================================

This table grows faster than any other: several rows per synced transaction,
forever, and nothing ever deletes from it. Left as one heap it eventually
becomes the table that makes `VACUUM` take all night and a retention policy
impossible to apply without a delete that locks the table for hours.

So it is partitioned by month on `created_at`. Two things follow from that,
both visible below:

  * **The primary key is `(id, created_at)`.** PostgreSQL requires the
    partition key to be part of every unique constraint -- it cannot enforce
    uniqueness across partitions it would have to scan all of. `id` is still
    a UUID and still unique in practice; the database simply guarantees it
    per-partition.
  * **Dropping a year of history becomes `DROP TABLE audit_log_2025_01`** --
    instant, and it reclaims the disk immediately, rather than a `DELETE`
    that rewrites the table and leaves the space to autovacuum.

`app/services/audit_partitions.py` creates next month's partition before it
is needed.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AuditAction, AuditActor, enum_column

if TYPE_CHECKING:
    from app.models.user import User


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_log"

    # Part of the primary key, because it is the partition key. Redeclared
    # here rather than inherited from TimestampMixin purely to add
    # `primary_key=True`; everything else about it is unchanged.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        primary_key=True,
    )

    __table_args__ = (
        # "Show me the history of this one transaction" -- the common lookup.
        Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),
        # "What did this user change recently?"
        Index("ix_audit_log_user_id_created_at", "user_id", "created_at"),
    )

    # Nullable because system-initiated changes have no user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        # SET NULL, not CASCADE: deleting a user must NOT erase the audit
        # trail. That is the entire point of an audit trail.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # A generic pointer to any row in any table. We deliberately do NOT use a
    # foreign key here: the audited row may later be deleted, and the log entry
    # must survive that. Losing referential integrity is the correct trade for
    # a record that outlives what it describes.
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    action: Mapped[AuditAction] = mapped_column(
        enum_column(AuditAction), nullable=False
    )
    actor: Mapped[AuditActor] = mapped_column(
        enum_column(AuditActor), nullable=False
    )

    # Only the fields that changed, not the whole row.
    before_values: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_values: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Free-text context: "applied rule 'Coffee'", "bulk recategorize".
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Request metadata, for security investigations ("was this change made
    # from a device I recognize?").
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped["User | None"] = relationship()

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type}:{self.entity_id}>"

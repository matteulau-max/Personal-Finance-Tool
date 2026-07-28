"""A record of every sync attempt.

Why log this at all? Because "my transactions are missing" is the support
question you will get most often, and without this table the only honest
answer is a shrug.

With it you can say precisely: the last sync ran at 04:12, took 1.8 seconds,
added 6 transactions, and failed on the third page with ITEM_LOGIN_REQUIRED.

It also gives us the cursor before and after each run. If a sync corrupts
something, the previous cursor lets us replay from a known-good point --
this table is the undo button for the sync engine.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import SyncStatus, SyncTrigger, enum_column

if TYPE_CHECKING:
    from app.models.plaid_item import PlaidItem


class SyncHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sync_history"
    __table_args__ = (
        Index("ix_sync_history_plaid_item_id_started_at", "plaid_item_id", "started_at"),
    )

    plaid_item_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("plaid_items.id", ondelete="CASCADE"),
        nullable=False,
    )

    trigger: Mapped[SyncTrigger] = mapped_column(
        enum_column(SyncTrigger), nullable=False
    )
    status: Mapped[SyncStatus] = mapped_column(
        enum_column(SyncStatus),
        default=SyncStatus.RUNNING,
        nullable=False,
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Cursor state, for replay and debugging.
    cursor_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_after: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- What actually happened ---
    transactions_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_modified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transactions_removed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Rows Plaid sent that we already had. A healthy number here proves
    # deduplication is working rather than silently doing nothing.
    transactions_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accounts_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    plaid_item: Mapped["PlaidItem"] = relationship(back_populates="sync_runs")

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def __repr__(self) -> str:
        return f"<SyncHistory {self.started_at} {self.status}>"

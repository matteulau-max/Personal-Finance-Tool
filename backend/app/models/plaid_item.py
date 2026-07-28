"""Plaid Items -- one per (user, institution) connection.

Plaid's vocabulary, which is easy to get confused by:

    Item     = one login at one institution. Connecting your Chase login
               creates one Item, even if that login exposes five accounts.
    Account  = one of those five (a checking account, a credit card, ...).

So: User 1---* PlaidItem 1---* Account.

This table also holds the **access token**, which is the single most sensitive
value in the entire system. It grants ongoing read access to real bank data.

Three rules for it, enforced from here on:
  1. It is never sent to the browser. Never in an API response, never in a log
     line, never in an error message.
  2. It is stored encrypted at rest (implemented in Milestone 4 -- the column
     is named `_encrypted` now so the intent is unambiguous and so no one
     accidentally writes a plaintext token into it).
  3. Access to this table is limited to the sync service.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PlaidItemStatus, enum_column

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.institution import Institution
    from app.models.sync_history import SyncHistory
    from app.models.user import User


class PlaidItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plaid_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # ondelete="CASCADE": if a user row is ever removed, the database
        # removes their items too, rather than leaving orphaned rows that
        # point at nothing. Referential integrity is the database's job --
        # never rely on application code to remember.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        # RESTRICT: refuse to delete an institution that still has items.
        ForeignKey("institutions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    plaid_item_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )

    # See the module docstring. Encrypted in Milestone 4.
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Incremental sync state ---
    # Plaid's /transactions/sync endpoint is a cursor-based feed: you send the
    # cursor you last saw, and Plaid returns only what changed since then.
    # Storing this cursor is what makes syncing cheap and, critically, what
    # stops us from re-downloading (and potentially re-inserting) history.
    transactions_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[PlaidItemStatus] = mapped_column(
        enum_column(PlaidItemStatus),
        default=PlaidItemStatus.HEALTHY,
        nullable=False,
    )

    # When an Item breaks (password changed, MFA required), Plaid tells us why.
    # We surface this to the user as "reconnect your bank".
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_successful_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Some regions (EU/UK open banking) require the user to re-consent
    # periodically. Warn them before access silently stops working.
    consent_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="plaid_items")
    institution: Mapped["Institution"] = relationship(back_populates="plaid_items")
    accounts: Mapped[list["Account"]] = relationship(
        back_populates="plaid_item", cascade="all, delete-orphan"
    )
    sync_runs: Mapped[list["SyncHistory"]] = relationship(
        back_populates="plaid_item", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<PlaidItem {self.plaid_item_id} status={self.status}>"

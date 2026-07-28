"""User accounts.

We store our own `users` row even though Clerk (Milestone 3) handles passwords
and login. Why not just use Clerk's ID everywhere?

Because every other table needs a foreign key to a user, and foreign keys must
point at a table *we* own. Keeping a local `users` table also means that if we
ever migrate off Clerk, exactly one column changes (`clerk_user_id`) instead of
every table in the schema. This is called an anti-corruption layer: never let a
third party's identifiers spread through your database.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.plaid_item import PlaidItem
    from app.models.transaction import Transaction


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # The external identity provider's ID. Unique and indexed because every
    # authenticated request looks a user up by this value.
    clerk_user_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Soft disable, e.g. for a cancelled subscription. We never hard-delete a
    # user: their financial history has to remain auditable.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ISO 4217 code used for display and for cross-currency reporting.
    default_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # IANA timezone, e.g. "America/New_York". Needed to decide which calendar
    # month a transaction belongs to -- a purchase at 11pm on the 31st is a
    # different month depending on the timezone you ask in.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    # Last authenticated request, updated at most hourly (see services/users).
    # Useful for dormant-account cleanup and for spotting a compromised
    # account that suddenly becomes active after months of silence.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    plaid_items: Mapped[list["PlaidItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    accounts: Mapped[list["Account"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"

"""Accounts and their balance history."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AccountType, enum_column

if TYPE_CHECKING:
    from app.models.plaid_item import PlaidItem
    from app.models.transaction import Transaction
    from app.models.user import User

# Money is ALWAYS Numeric/DECIMAL, never Float.
#
# Floats cannot represent 0.1 exactly. Add ten thousand of them and you drift
# by a fraction of a cent; sum a year of transactions and your dashboard
# disagrees with the bank. DECIMAL stores exact base-10 values.
#
# 18 total digits with 4 after the decimal point: enough for trillions, and
# 4 decimal places covers currencies with 3 (Kuwaiti dinar) plus interest
# calculations that need sub-cent precision.
MONEY = Numeric(18, 4)


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # An account from Plaid appears exactly once. If a sync tries to insert
        # the same Plaid account twice, the database rejects it. This is the
        # first of several places we push a correctness rule *into the
        # database* rather than trusting application code to be careful.
        UniqueConstraint("plaid_account_id", name="uq_accounts_plaid_account_id"),
        Index("ix_accounts_user_id_is_active", "user_id", "is_active"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Null for manually-created accounts (cash, or a bank Plaid doesn't cover).
    plaid_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("plaid_items.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plaid_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Display ---
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    official_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # What the user renamed it to. Same override pattern as transactions:
    # we keep what the bank said AND what the user prefers.
    user_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Last 4 digits only. Never store a full account number -- we have no
    # legitimate use for one, and storing it would put this database into a
    # far stricter regulatory category.
    mask: Mapped[str | None] = mapped_column(String(8), nullable=True)

    type: Mapped[AccountType] = mapped_column(
        enum_column(AccountType), nullable=False
    )
    # Plaid's finer classification: "checking", "savings", "credit card".
    # Free text because Plaid adds new subtypes regularly.
    subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)

    currency_code: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # --- Current snapshot (denormalized for speed) ---
    # These duplicate the newest row in account_balances. Denormalizing means
    # storing the same fact in two places -- normally something to avoid. It
    # is justified here because rendering the accounts list would otherwise
    # need a correlated "latest balance per account" subquery on every page
    # load. The balance history table remains the source of truth; these are a
    # cache, refreshed on every sync.
    current_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    available_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    credit_limit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    balance_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Hidden accounts still sync but are excluded from dashboard totals.
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Should this account count toward net worth? A business account you track
    # but don't own personally, for example.
    include_in_net_worth: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="accounts")
    plaid_item: Mapped["PlaidItem"] = relationship(back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    balances: Mapped[list["AccountBalance"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        return self.user_display_name or self.name

    @property
    def utilization(self) -> Decimal | None:
        """Credit utilization as a fraction (0.30 == 30%).

        Returns None when it is meaningless -- a checking account, or a card
        with no limit reported. Returning None rather than 0 matters: a
        dashboard showing "0% utilization" for a checking account is a bug,
        while showing nothing is correct.
        """
        if self.type != AccountType.CREDIT:
            return None
        if not self.credit_limit or self.credit_limit <= 0:
            return None
        if self.current_balance is None:
            return None
        return self.current_balance / self.credit_limit

    def __repr__(self) -> str:
        return f"<Account {self.display_name} ({self.type})>"


class AccountBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A point-in-time balance snapshot.

    Why keep history instead of just the current number? Because "net worth
    over time" is one of the headline dashboard features, and a balance you
    did not record is gone forever -- no bank API will tell you what your
    checking balance was last March.

    Recording a snapshot on every sync costs almost nothing and makes the
    entire historical charting feature possible later. This is the cheapest
    high-value decision in the schema.
    """

    __tablename__ = "account_balances"
    __table_args__ = (
        # One snapshot per account per instant. Re-running a sync is therefore
        # harmless -- it cannot double-write history.
        UniqueConstraint("account_id", "as_of", name="uq_account_balances_account_id_as_of"),
        # Descending index: history queries are "newest first", always.
        Index("ix_account_balances_account_id_as_of", "account_id", "as_of"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    current_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    available_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    credit_limit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency_code: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    account: Mapped["Account"] = relationship(back_populates="balances")

    def __repr__(self) -> str:
        return f"<AccountBalance {self.account_id} @ {self.as_of}>"

"""Transactions -- the heart of the schema.

Read this file carefully. Three decisions here determine whether the whole
application is trustworthy.

===========================================================================
DECISION 1: How we guarantee a transaction is never imported twice
===========================================================================

Deduplication is enforced by the *database*, not by application code. Two
unique constraints do the work:

    UNIQUE (account_id, plaid_transaction_id)   -- for synced transactions
    UNIQUE (account_id, fingerprint)            -- for manual / CSV imports

Why the database and not a "check if it exists first" in Python? Because that
check is a race condition. Two syncs running at the same moment -- a webhook
and a scheduled job, say -- can both look, both see nothing, and both insert.
A unique constraint cannot be raced: the second INSERT fails, always. We then
catch that failure and turn it into an UPDATE (an "upsert").

`fingerprint` covers sources that give us no stable ID. It is a hash of
(account, date, amount, normalized description). Two genuinely identical
coffees bought on the same day would collide, so the hash includes an
occurrence counter -- see Milestone 5, where the import pipeline is built.

===========================================================================
DECISION 2: How a correction never destroys the original
===========================================================================

Every fact about a transaction exists in up to three layers:

    raw_*    What the source said. Written ONLY by the sync engine.
             A user action never modifies these columns.
    auto_*   What our engine inferred (merchant matching, rules, ML).
             Overwritable by a better inference.
    user_*   What the human explicitly chose. Wins over everything.
             Nothing but another human action may overwrite it.

The value the app displays is `COALESCE(user_x, auto_x, raw_x)` -- the
`effective_*` properties below. So editing a category writes `user_category_id`
and leaves the bank's data completely intact. "Reset to original" is just
setting the user_ column back to NULL. Nothing is ever lost.

`raw_payload` additionally stores the entire original JSON from Plaid, so even
fields we never modeled remain recoverable years later.

===========================================================================
DECISION 3: Sign convention
===========================================================================

    POSITIVE amount = money LEAVING the account (spending, a card purchase)
    NEGATIVE amount = money ENTERING the account (income, a refund)

This matches Plaid's convention exactly. It reads backwards to most people --
but the alternative is flipping the sign on every ingest, and a sign flip
applied twice (or zero times) is a silent, catastrophic bug that no test
notices until your net worth chart is inverted. Matching the upstream source
means no transformation to get wrong. Tests below lock this down.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    CategorySource,
    TransactionSource,
    TransactionStatus,
    enum_column,
)

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.category import Category
    from app.models.merchant import Merchant
    from app.models.tag import Tag
    from app.models.user import User

MONEY = Numeric(18, 4)


class Transaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # --- The two deduplication guarantees (see Decision 1 above) ---
        UniqueConstraint(
            "account_id",
            "plaid_transaction_id",
            name="uq_transactions_account_id_plaid_transaction_id",
        ),
        UniqueConstraint(
            "account_id",
            "fingerprint",
            name="uq_transactions_account_id_fingerprint",
        ),
        # --- Indexes, chosen from the queries we know we will run ---
        # An index is a lookup structure that turns "scan every row" into
        # "jump straight there". The cost is slower writes and disk space, so
        # you add them for real query patterns, not speculatively.
        #
        # "This user's transactions, newest first" -- every list view.
        Index("ix_transactions_user_id_raw_date", "user_id", "raw_date"),
        # "This account's transactions in a date range" -- account detail.
        Index("ix_transactions_account_id_raw_date", "account_id", "raw_date"),
        # "Spending by category over time" -- the dashboards.
        Index("ix_transactions_user_id_user_category_id", "user_id", "user_category_id"),
        Index("ix_transactions_user_id_auto_category_id", "user_id", "auto_category_id"),
        # "Everything from this merchant" -- merchant analysis.
        Index("ix_transactions_user_id_auto_merchant_id", "user_id", "auto_merchant_id"),
        # Sync needs to find pending rows quickly to reconcile them.
        Index("ix_transactions_status", "status"),
        # Substring search ("show me anything with 'coffee' in it").
        #
        # A leading wildcard -- ILIKE '%coffee%' -- makes a normal B-tree
        # index useless, so PostgreSQL would read every row. A trigram GIN
        # index indexes three-character sequences instead, which makes
        # substring matching indexable. Requires the pg_trgm extension,
        # created by migration 5d94c4e6a26a.
        Index(
            "ix_transactions_raw_name_trgm",
            "raw_name",
            postgresql_using="gin",
            postgresql_ops={"raw_name": "gin_trgm_ops"},
        ),
    )

    # --- Ownership -------------------------------------------------------
    # user_id is technically redundant (it is reachable via account_id), but
    # every single query filters by user, and getting that filter wrong means
    # showing one person another person's money. Storing it directly makes the
    # security filter impossible to forget and avoids a join on the hottest
    # path in the app. This is a deliberate, documented denormalization.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- Identity / deduplication ---------------------------------------
    plaid_transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[TransactionSource] = mapped_column(
        enum_column(TransactionSource),
        default=TransactionSource.PLAID,
        nullable=False,
    )

    # --- Lifecycle -------------------------------------------------------
    status: Mapped[TransactionStatus] = mapped_column(
        enum_column(TransactionStatus),
        default=TransactionStatus.POSTED,
        nullable=False,
    )
    # Soft deletion. When Plaid says a transaction was removed we set status
    # to REMOVED and stamp this column -- we never DELETE the row.
    #
    # Why: a hard delete destroys evidence. If a sync bug removes 400
    # transactions, a soft delete is one UPDATE away from full recovery, while
    # a hard delete means they are gone. Soft deletion is the default for any
    # financial record.
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When a pending charge posts, Plaid issues a NEW transaction id and tells
    # us which pending one it replaces. Storing that link is what stops the
    # same coffee appearing twice -- once pending, once posted.
    replaces_plaid_transaction_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    # ====================================================================
    # LAYER 1 -- raw_*: what the source told us. Sync writes these; a user
    # action never does.
    # ====================================================================
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_merchant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    raw_currency_code: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    raw_date: Mapped[date] = mapped_column(Date, nullable=False)
    raw_authorized_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw_category: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The complete original API response. JSONB is PostgreSQL's binary,
    # indexable JSON type. Keeping it means a field we did not think to model
    # today is still available in two years without a re-sync -- and it may be
    # impossible to re-fetch by then.
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ====================================================================
    # LAYER 2 -- auto_*: what our engine inferred. Overwritable by a better
    # inference, never by a worse one, and never over a user's choice.
    # ====================================================================
    auto_merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True
    )
    auto_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    auto_category_source: Mapped[CategorySource] = mapped_column(
        enum_column(CategorySource),
        default=CategorySource.NONE,
        nullable=False,
    )
    # Which rule produced this, so the UI can say "categorized by your rule
    # 'Starbucks → Coffee'" and so a deleted rule's effects are traceable.
    auto_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rules.id", ondelete="SET NULL"), nullable=True
    )

    # ====================================================================
    # LAYER 3 -- user_*: explicit human choices. Always win. NULL means
    # "the user has not overridden this", not "the user chose nothing".
    # ====================================================================
    user_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True
    )
    user_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Correcting an amount or date is only meaningful for manual/CSV records,
    # but the columns exist for all rows so the override mechanism is uniform.
    user_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    user_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    user_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- User-set flags --------------------------------------------------
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Manual override of the transfer detection, for when we get it wrong.
    is_transfer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Relationships ---------------------------------------------------
    user: Mapped["User"] = relationship(back_populates="transactions")
    account: Mapped["Account"] = relationship(back_populates="transactions")

    # `foreign_keys` is required because this table points at `merchants` and
    # `categories` twice each -- SQLAlchemy cannot guess which column a given
    # relationship should follow.
    auto_merchant: Mapped["Merchant | None"] = relationship(foreign_keys=[auto_merchant_id])
    user_merchant: Mapped["Merchant | None"] = relationship(foreign_keys=[user_merchant_id])
    auto_category: Mapped["Category | None"] = relationship(foreign_keys=[auto_category_id])
    user_category: Mapped["Category | None"] = relationship(foreign_keys=[user_category_id])

    tags: Mapped[list["Tag"]] = relationship(
        secondary="transaction_tags", back_populates="transactions"
    )

    # ====================================================================
    # Effective values -- what the application actually displays.
    #
    # `hybrid_property` is a SQLAlchemy feature that defines a value twice:
    # once in Python (for a loaded object) and once as SQL (so it can be used
    # in a WHERE or GROUP BY). Without the SQL half you could not write
    # "group spending by effective category" in the database, and would have
    # to load every row into Python to do it. That does not scale.
    # ====================================================================

    @hybrid_property
    def effective_amount(self) -> Decimal:
        return self.user_amount if self.user_amount is not None else self.raw_amount

    @effective_amount.inplace.expression
    @classmethod
    def _effective_amount_expr(cls):
        return func.coalesce(cls.user_amount, cls.raw_amount)

    @hybrid_property
    def effective_date(self) -> date:
        return self.user_date if self.user_date is not None else self.raw_date

    @effective_date.inplace.expression
    @classmethod
    def _effective_date_expr(cls):
        return func.coalesce(cls.user_date, cls.raw_date)

    @hybrid_property
    def effective_category_id(self) -> uuid.UUID | None:
        return self.user_category_id if self.user_category_id is not None else self.auto_category_id

    @effective_category_id.inplace.expression
    @classmethod
    def _effective_category_id_expr(cls):
        return func.coalesce(cls.user_category_id, cls.auto_category_id)

    @hybrid_property
    def effective_merchant_id(self) -> uuid.UUID | None:
        return self.user_merchant_id if self.user_merchant_id is not None else self.auto_merchant_id

    @effective_merchant_id.inplace.expression
    @classmethod
    def _effective_merchant_id_expr(cls):
        return func.coalesce(cls.user_merchant_id, cls.auto_merchant_id)

    @hybrid_property
    def effective_description(self) -> str:
        if self.user_description:
            return self.user_description
        return self.raw_merchant_name or self.raw_name

    @effective_description.inplace.expression
    @classmethod
    def _effective_description_expr(cls):
        return func.coalesce(cls.user_description, cls.raw_merchant_name, cls.raw_name)

    # --- Convenience -----------------------------------------------------

    @property
    def is_outflow(self) -> bool:
        """True when money left the account. See Decision 3 on signs."""
        return self.effective_amount > 0

    @property
    def is_user_modified(self) -> bool:
        """Has a human touched this? Drives the 'edited' badge in the UI."""
        return any(
            value is not None
            for value in (
                self.user_description,
                self.user_merchant_id,
                self.user_category_id,
                self.user_amount,
                self.user_date,
            )
        )

    def __repr__(self) -> str:
        return f"<Transaction {self.raw_date} {self.raw_name!r} {self.raw_amount}>"

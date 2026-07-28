"""Merchants, and the normalization that makes them useful.

The problem this table solves. Your bank sends these three strings:

    WHOLEFDS MKT #10259
    Whole Foods #125
    WHOLE FOODS MARKET

They are one merchant. Without normalization, "how much do I spend at Whole
Foods?" returns three unrelated answers, and merchant analytics are worthless.

The approach:

  1. `normalized_name` is a machine-generated key derived from the raw string:
     lowercase, strip store numbers, strip payment-processor noise ("SQ *",
     "TST*", "PAYPAL *"), collapse whitespace. All three examples above reduce
     to "whole foods". This column is what we match on.
  2. `display_name` is the pretty name a human sees: "Whole Foods".
  3. A merchant with `user_id = NULL` is a global, shared merchant. A row with
     a `user_id` is that user's private correction, which wins for them alone.

This two-tier design means improving the global list helps everyone, while a
user who insists a merchant is called something else is never overruled.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.category import Category


class Merchant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "merchants"
    __table_args__ = (
        # A user cannot have two merchants with the same normalized name, and
        # the global list cannot contain duplicates either.
        #
        # A subtlety: in SQL, NULL is never equal to NULL, so a plain UNIQUE
        # constraint would NOT prevent two global rows (user_id IS NULL) with
        # the same name. PostgreSQL 15+ fixes this with NULLS NOT DISTINCT,
        # which we use here.
        UniqueConstraint(
            "user_id",
            "normalized_name",
            name="uq_merchants_user_id_normalized_name",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_merchants_normalized_name", "normalized_name"),
    )

    # NULL = global merchant shared by all users.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Enrichment, mostly from Plaid.
    logo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # When set, transactions matched to this merchant get this category
    # automatically. That single field powers most auto-categorization:
    # "Whole Foods -> Groceries" only has to be learned once.
    default_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Netflix, Spotify, gym memberships. Feeds the "subscriptions" dashboard
    # and the "what can I cancel?" AI question.
    is_subscription: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    default_category: Mapped["Category"] = relationship(foreign_keys=[default_category_id])

    def __repr__(self) -> str:
        return f"<Merchant {self.display_name}>"


class MerchantAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Learned mappings from a raw bank string to a merchant.

    When the automatic normalizer fails -- and it will, because bank
    descriptors are chaotic -- the user corrects one transaction, and we write
    an alias here. Every future transaction with that raw pattern is then
    matched instantly and for free.

    This is how the system gets smarter through use rather than through us
    guessing better regular expressions.
    """

    __tablename__ = "merchant_aliases"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "raw_pattern",
            name="uq_merchant_aliases_user_id_raw_pattern",
            postgresql_nulls_not_distinct=True,
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The normalized form of the raw descriptor we matched on.
    raw_pattern: Mapped[str] = mapped_column(String(512), nullable=False)

    merchant: Mapped["Merchant"] = relationship()

    def __repr__(self) -> str:
        return f"<MerchantAlias {self.raw_pattern!r} -> {self.merchant_id}>"

"""Budgets: a planned amount per category per period.

===========================================================================
The modelling decision: one row per period, not one row with a date range
===========================================================================

A budget could be stored as "Groceries, $600/month, effective from March".
That is compact, but it makes the two most common operations awkward:

  * changing next month's budget without rewriting history;
  * answering "what was my budget in March 2025?" after three changes.

Storing one row per (category, month) instead means a budget is simply a
fact about a month. Changing April's amount cannot alter March's, and "budget
vs actual" for any past month is a single lookup rather than a reconstruction
from a chain of effective-dated rows.

The cost is more rows -- twelve per category per year, which is nothing -- and
a small helper to roll budgets forward. That is a good trade: storage is
cheap, and losing the ability to answer questions about the past is not.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.category import Category

MONEY = Numeric(18, 4)


class Budget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "budgets"
    __table_args__ = (
        # One budget per category per month. Two would make "budget vs actual"
        # ambiguous, and the ambiguity would surface as a number that changes
        # depending on which row the database happened to return first.
        UniqueConstraint(
            "user_id",
            "category_id",
            "period_start",
            name="uq_budgets_user_id_category_id_period_start",
        ),
        Index("ix_budgets_user_id_period_start", "user_id", "period_start"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # CASCADE: a budget for a deleted category is meaningless. Unlike a
        # transaction, it carries no historical record of money that moved.
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Always the FIRST day of the month. Storing a normalized date rather than
    # a (year, month) pair keeps date arithmetic and range queries in SQL
    # rather than in application code.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    category: Mapped["Category"] = relationship()

    def __repr__(self) -> str:
        return f"<Budget {self.period_start} {self.amount}>"

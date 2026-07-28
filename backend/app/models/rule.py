"""User-defined categorization rules.

A rule is "if a transaction looks like X, do Y" -- for example, *if the
description contains "SQ *BLUE BOTTLE" then set category to Coffee and tag it
Business*.

The design question: how do you store conditions that users invent, when you
cannot know in advance what they will want to match on?

We store them as **JSONB** -- structured JSON that PostgreSQL can index and
query. A rule's conditions look like:

    {
      "operator": "AND",
      "conditions": [
        {"field": "raw_name", "op": "contains", "value": "BLUE BOTTLE"},
        {"field": "amount",   "op": "gt",       "value": 5.00}
      ]
    }

and its actions like:

    {"set_category_id": "...", "add_tags": ["Business"]}

Why not a `rule_conditions` table with one row per condition? Because
conditions nest (AND of ORs), and modelling a tree in relational rows makes
every read a recursive query. JSONB is the right tool when the *shape* of the
data is variable but it is always read as a whole.

The trade-off, stated honestly: the database can no longer validate what is
inside that JSON. So validation moves to the application, using Pydantic
schemas -- built in Milestone 5, along with the engine that evaluates these.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class Rule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "rules"
    __table_args__ = (
        # Rules are evaluated in priority order for one user at a time.
        Index("ix_rules_user_id_priority", "user_id", "priority"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Lower number = evaluated earlier. Explicit ordering matters because two
    # rules can match the same transaction and disagree; without a defined
    # order the result depends on row order, which is not stable in SQL.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # When true, no lower-priority rule runs after this one matches. Gives the
    # user "this is the final word" without needing to disable other rules.
    stop_processing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actions: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # --- Observability ---
    # Knowing a rule has matched 0 transactions in six months is how a user
    # discovers it was written wrong. Cheap to maintain, genuinely useful.
    match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_matched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship()

    def __repr__(self) -> str:
        return f"<Rule {self.name!r} priority={self.priority}>"

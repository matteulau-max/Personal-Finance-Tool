"""Tags, and the join table that connects them to transactions.

Categories and tags answer different questions, which is why both exist:

    Category = what KIND of spending this is. Exactly one per transaction.
               ("Groceries")
    Tag      = any other label you care about. Unlimited per transaction.
               ("Vacation", "Tax Deductible", "Reimbursable")

One dinner can be Restaurants (category) AND tagged Business AND Tax
Deductible AND Reimbursable simultaneously. Forcing that into a single
category field is exactly the limitation that makes most budgeting apps
frustrating.

This is a **many-to-many** relationship: a transaction has many tags, and a
tag has many transactions. SQL cannot express that with a single foreign key
in either table, so you create a third table whose rows are the pairings.
That table is `transaction_tags` below -- a "join table".
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.transaction import Transaction


class Tag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (
        # Tags belong to one user, and that user cannot have two tags with the
        # same name. Note we store `name` as typed but uniqueness should be
        # case-insensitive -- the service layer lowercases before comparing,
        # and Milestone 5 adds a functional index to enforce it in the
        # database as well.
        UniqueConstraint("user_id", "name", name="uq_tags_user_id_name"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    transactions: Mapped[list["Transaction"]] = relationship(
        secondary="transaction_tags", back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag {self.name}>"


class TransactionTag(TimestampMixin, Base):
    """The join table linking transactions to tags.

    Note there is no `id` column. The primary key is the *pair*
    (transaction_id, tag_id) -- a composite primary key. This is better than
    adding a surrogate id because it makes applying the same tag twice
    structurally impossible rather than merely discouraged.
    """

    __tablename__ = "transaction_tags"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<TransactionTag {self.transaction_id} +{self.tag_id}>"

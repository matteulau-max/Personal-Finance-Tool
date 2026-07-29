"""Spending categories, arranged as a tree.

`parent_id` points at another row in this same table -- a *self-referential*
foreign key. That is how you store a hierarchy of arbitrary depth in one table:

    Food & Drink            (parent_id = NULL)
      ├─ Groceries          (parent_id = Food & Drink)
      └─ Restaurants        (parent_id = Food & Drink)
           └─ Coffee Shops  (parent_id = Restaurants)

The alternative -- separate `categories` and `subcategories` tables -- locks
you into exactly two levels forever. This costs nothing extra and never
needs redesigning.

As with merchants: `user_id = NULL` means a built-in category everyone gets;
a row with a `user_id` is that user's own creation.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    pass


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "parent_id",
            "name",
            name="uq_categories_user_id_parent_id_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        # A parent cannot be deleted while children reference it. Forcing the
        # caller to deal with the children explicitly is safer than silently
        # orphaning or cascading away a chunk of someone's history.
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # A stable machine-readable key for built-in categories, e.g.
    # "food_and_drink.groceries". Names get renamed; slugs don't, so code and
    # seed data reference the slug.
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)

    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Income categories are excluded from "spending" totals and drive the
    # income tracking and savings-rate widgets.
    is_income: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Transfers between your own accounts are neither income nor spending.
    # Counting a credit card payment as "spending" double-counts every
    # purchase on that card -- this flag is what prevents that.
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Built-in categories cannot be deleted by users, only hidden.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # `remote_side` tells SQLAlchemy which end of this self-join is the "one"
    # side -- without it, it cannot tell parents from children.
    parent: Mapped["Category | None"] = relationship(
        back_populates="children", remote_side="Category.id"
    )
    children: Mapped[list["Category"]] = relationship(back_populates="parent")

    @property
    def full_name(self) -> str:
        """'Food & Drink › Groceries', for display in lists and reports."""
        if self.parent is None:
            return self.name
        return f"{self.parent.full_name} › {self.name}"

    def __repr__(self) -> str:
        return f"<Category {self.name}>"

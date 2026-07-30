"""Financial institutions (Chase, Amex, Venmo, ...).

This table is *shared across all users* -- there is one row for Chase, not one
per customer. That is what normalization means in practice: a fact about the
world ("Chase's logo is this image") is stored once, and everything that needs
it points at that single row.

The alternative -- copying the institution name and logo onto every account --
means a rebrand requires updating millions of rows, and guarantees that some of
them end up inconsistent.
"""

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.plaid_item import PlaidItem


class Institution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "institutions"

    # Plaid's identifier, e.g. "ins_56". Unique: we upsert on this value so a
    # second user connecting to Chase reuses the existing row.
    plaid_institution_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Branding, used to make the accounts list recognizable at a glance.
    #
    # Text, not String(n). Plaid does not return a link to a logo -- it returns
    # the image itself, base64-encoded, which the gateway wraps as a `data:`
    # URI. American Express's is around 3.5 KB, so a bounded column rejects it
    # outright: PostgreSQL refuses to truncate rather than silently trimming,
    # which failed the INSERT and rolled back the entire bank connection.
    #
    # A larger bound would only move the ceiling. The size here is decided by
    # whatever image an institution happens to publish, so there is no length
    # this column could pick that some bank could not exceed.
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    plaid_items: Mapped[list["PlaidItem"]] = relationship(back_populates="institution")

    def __repr__(self) -> str:
        return f"<Institution {self.name}>"

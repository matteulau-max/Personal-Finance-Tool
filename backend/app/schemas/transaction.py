"""Transaction request and response shapes."""

from __future__ import annotations

import uuid
import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import CategorySource, TransactionSource, TransactionStatus


class TagSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    color: str | None = None


class MerchantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    logo_url: str | None = None
    is_subscription: bool = False


class CategorySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str | None = None
    icon: str | None = None
    color: str | None = None
    is_income: bool = False
    is_transfer: bool = False


class TransactionResponse(BaseModel):
    """What the client sees.

    Note the shape: `amount`, `date`, and `description` are the EFFECTIVE
    values, so the UI never has to know about the three-layer model. The raw
    values are exposed separately and read-only, which is what powers the
    "reset to original" affordance and the "edited" badge.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    status: TransactionStatus
    source: TransactionSource

    # Effective values -- what to display.
    amount: Decimal
    # `dt.date`, not `date`. A field literally named `date` would shadow the
    # imported type inside the class body -- and in the update schema below,
    # `date: date | None = None` assigns `date = None`, so the annotation then
    # evaluates as `None | None` and the model fails to build. Aliasing the
    # module sidesteps the collision entirely.
    date: dt.date
    description: str

    currency_code: str
    category: CategorySummary | None = None
    merchant: MerchantSummary | None = None
    tags: list[TagSummary] = Field(default_factory=list)

    notes: str | None = None
    is_hidden: bool = False
    is_reviewed: bool = False

    # Provenance, so the UI can explain itself.
    category_source: CategorySource = CategorySource.NONE
    is_user_modified: bool = False

    # The bank's original values, always read-only.
    raw_name: str
    raw_amount: Decimal
    raw_date: dt.date

    created_at: dt.datetime

    # Deliberately NOT exposed: user_id, plaid_transaction_id, raw_payload,
    # fingerprint. raw_payload in particular can contain fields Plaid returns
    # that we have no business republishing.


class TransactionUpdateRequest(BaseModel):
    """A user correction.

    Every field here writes a `user_*` column. None of them touch `raw_*` --
    the bank's version stays intact, which is what makes every edit
    reversible.

    Setting a field to `null` explicitly CLEARS the override and restores the
    original. That is why `model_dump(exclude_unset=True)` is essential in the
    endpoint: it distinguishes "the client said null" (reset) from "the client
    did not mention this field" (leave alone).
    """

    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID | None = None
    merchant_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    amount: Decimal | None = None
    date: dt.date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    is_hidden: bool | None = None
    is_reviewed: bool | None = None
    tag_ids: list[uuid.UUID] | None = None

    # When true, remember this correction and apply it to matching
    # transactions. See the note on explicit vs implicit learning in
    # docs/milestone-05-categorization.md.
    apply_to_similar: bool = False


class BulkUpdateRequest(BaseModel):
    """Apply the same change to many transactions at once."""

    model_config = ConfigDict(extra="forbid")

    # Bounded: an unbounded bulk endpoint is a denial-of-service waiting to
    # happen, and 500 is far more than any real UI selection.
    transaction_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)

    category_id: uuid.UUID | None = None
    add_tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    remove_tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    is_reviewed: bool | None = None
    is_hidden: bool | None = None

    @model_validator(mode="after")
    def _must_do_something(self) -> "BulkUpdateRequest":
        if not any(
            [
                self.category_id,
                self.add_tag_ids,
                self.remove_tag_ids,
                self.is_reviewed is not None,
                self.is_hidden is not None,
            ]
        ):
            raise ValueError("specify at least one change")
        return self


class BulkUpdateResponse(BaseModel):
    updated: int
    skipped: int


class TransactionPage(BaseModel):
    """A page of results.

    `total` is returned so the UI can show "1-50 of 3,214". It costs a second
    COUNT query, which is worth it here: without a total, pagination controls
    cannot tell the user how much there is.
    """

    items: list[TransactionResponse]
    total: int
    limit: int
    offset: int

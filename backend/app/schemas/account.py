"""API response shapes for accounts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import AccountType


class AccountResponse(BaseModel):
    # `from_attributes` reads values off the SQLAlchemy object by attribute
    # name. That includes plain Python `@property` values, not just columns --
    # which is how `display_name` and `utilization` below get populated from
    # the properties defined on the Account model. One definition of the
    # calculation, used by both the API and any future analytics code.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    display_name: str
    mask: str | None
    type: AccountType
    subtype: str | None
    currency_code: str

    current_balance: Decimal | None
    available_balance: Decimal | None
    credit_limit: Decimal | None
    balance_updated_at: datetime | None

    # Derived on the model; None when it would be meaningless (see
    # Account.utilization).
    utilization: Decimal | None

    is_active: bool
    is_hidden: bool
    include_in_net_worth: bool

    # Deliberately NOT exposed: user_id (the client already knows who it is,
    # and echoing internal ids back invites them to be used as inputs),
    # plaid_account_id and plaid_item_id (internal identifiers that are only
    # meaningful alongside an access token).

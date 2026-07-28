"""Account endpoints.

There is nothing to populate these with until Plaid arrives in Milestone 4.
They exist now because they are the first endpoints that read *owned* data,
and so they are where the scoping rules get established -- and tested --
before there is enough code for a mistake to hide in.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.db.scoping import scoped_get, scoped_select
from app.models import Account
from app.schemas.account import AccountResponse

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
def list_accounts(
    current_user: CurrentUser,
    db: DbSession,
    include_hidden: bool = False,
) -> list[AccountResponse]:
    """List the signed-in user's accounts.

    `scoped_select` rather than `select`: the query is scoped to this user by
    construction, so there is no WHERE clause to forget.
    """
    statement = scoped_select(Account, current_user).where(Account.is_active.is_(True))

    if not include_hidden:
        statement = statement.where(Account.is_hidden.is_(False))

    accounts = db.execute(statement.order_by(Account.name)).scalars().all()

    return [AccountResponse.model_validate(account) for account in accounts]


@router.get("/{account_id}", response_model=AccountResponse)
def read_account(
    account_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> AccountResponse:
    """Fetch one account by id.

    Returns 404 when the account belongs to someone else -- deliberately the
    same response as when it does not exist at all.

    Returning 403 for "exists but not yours" would confirm the id is real,
    letting an attacker enumerate valid ids and infer how many accounts other
    users have. Never let an authorization failure be distinguishable from a
    missing record.
    """
    account = scoped_get(db, Account, account_id, current_user)

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    return AccountResponse.model_validate(account)

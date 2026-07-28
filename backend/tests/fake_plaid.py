"""A fake Plaid, so the sync engine can be tested exhaustively.

===========================================================================
Why not test against Plaid's sandbox?
===========================================================================

The sandbox is genuinely useful for a one-off manual check, and Milestone 4's
guide walks you through doing exactly that. It is a poor foundation for an
automated suite:

  * You cannot make it produce the scenarios that matter. "A pending charge
    posts with a different amount", "the same page arrives twice", "the
    connection expires halfway through page 3" -- these are the cases where
    sync engines break, and you cannot ask a sandbox for them on demand.
  * It needs credentials, so CI cannot run without secrets.
  * It is slow and occasionally flaky, which trains you to ignore red builds.

This fake implements the same `PlaidGateway` protocol and is *scripted*: each
test says exactly what Plaid returns, including failures, and asserts what the
database looks like afterwards. Every line of the real sync engine runs.

The one thing it cannot prove is that our understanding of Plaid's API is
correct. That is what the manual sandbox check in the guide is for -- the two
kinds of verification are complements, not substitutes.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from app.services.plaid_gateway import (
    PlaidAccount,
    PlaidApiError,
    PlaidInstitution,
    PlaidItemInfo,
    PlaidTransaction,
    SyncPage,
)


def make_account(
    account_id: str = "plaid_acct_1",
    *,
    name: str = "Plaid Checking",
    type: str = "depository",
    subtype: str | None = "checking",
    current_balance: str | None = "1200.50",
    available_balance: str | None = "1100.00",
    credit_limit: str | None = None,
) -> PlaidAccount:
    return PlaidAccount(
        account_id=account_id,
        name=name,
        official_name=f"{name} Account",
        mask="0000",
        type=type,
        subtype=subtype,
        currency_code="USD",
        current_balance=Decimal(current_balance) if current_balance else None,
        available_balance=Decimal(available_balance) if available_balance else None,
        credit_limit=Decimal(credit_limit) if credit_limit else None,
    )


def make_txn(
    transaction_id: str,
    *,
    account_id: str = "plaid_acct_1",
    amount: str = "12.34",
    date: dt.date | None = None,
    name: str = "STARBUCKS STORE 12345",
    pending: bool = False,
    merchant_name: str | None = "Starbucks",
    pending_transaction_id: str | None = None,
    category: str | None = "FOOD_AND_DRINK",
) -> PlaidTransaction:
    return PlaidTransaction(
        transaction_id=transaction_id,
        account_id=account_id,
        amount=Decimal(amount),
        currency_code="USD",
        date=date or dt.date(2026, 5, 12),
        name=name,
        pending=pending,
        merchant_name=merchant_name,
        authorized_date=None,
        pending_transaction_id=pending_transaction_id,
        category=category,
        raw={"transaction_id": transaction_id, "name": name},
    )


class FakePlaidGateway:
    """Scripted Plaid. Satisfies `PlaidGateway` structurally."""

    def __init__(
        self,
        *,
        pages: list[SyncPage] | None = None,
        accounts: list[PlaidAccount] | None = None,
        institution: PlaidInstitution | None = None,
    ) -> None:
        self.pages = pages or []
        self.accounts = accounts if accounts is not None else [make_account()]
        self.institution = institution or PlaidInstitution(
            institution_id="ins_fake", name="Fake Bank"
        )

        # Recorded so tests can assert on HOW we called Plaid, not just on the
        # resulting rows. The cursor sequence in particular is what proves
        # incremental sync is working rather than re-fetching all history.
        self.cursors_seen: list[str | None] = []
        self.sync_call_count = 0
        self.removed_items: list[str] = []
        self.link_tokens_created = 0
        self._items_created = 0

        # Failure injection.
        self.raise_on_sync: PlaidApiError | None = None
        self.raise_on_sync_after_pages: int | None = None

    # -- link / item -----------------------------------------------------

    def create_link_token(self, *, user_id: str) -> str:
        self.link_tokens_created += 1
        return f"link-sandbox-{user_id}"

    def exchange_public_token(self, *, public_token: str) -> tuple[str, str]:
        if public_token == "invalid":
            raise PlaidApiError("INVALID_PUBLIC_TOKEN", "public token is invalid")
        # A fresh item id per exchange, as real Plaid does. Returning a
        # constant would let a test pass that should not: two users linking
        # the same bank must produce two Items.
        self._items_created += 1
        return ("access-sandbox-secret-value", f"item_fake_{self._items_created}")

    def get_item(self, *, access_token: str) -> PlaidItemInfo:
        return PlaidItemInfo(
            item_id=f"item_fake_{self._items_created}", institution_id="ins_fake"
        )

    def get_institution(self, *, institution_id: str) -> PlaidInstitution | None:
        return self.institution

    def get_accounts(self, *, access_token: str) -> list[PlaidAccount]:
        return self.accounts

    def remove_item(self, *, access_token: str) -> None:
        self.removed_items.append(access_token)

    # -- data ------------------------------------------------------------

    def sync_transactions(self, *, access_token: str, cursor: str | None) -> SyncPage:
        self.cursors_seen.append(cursor)

        if self.raise_on_sync is not None:
            raise self.raise_on_sync

        if (
            self.raise_on_sync_after_pages is not None
            and self.sync_call_count >= self.raise_on_sync_after_pages
        ):
            raise PlaidApiError("ITEM_LOGIN_REQUIRED", "the user must reconnect")

        if self.sync_call_count >= len(self.pages):
            # Nothing further: an empty page that ends the loop.
            return SyncPage(
                added=[],
                modified=[],
                removed_ids=[],
                accounts=[],
                next_cursor=cursor or "cursor-empty",
                has_more=False,
            )

        page = self.pages[self.sync_call_count]
        self.sync_call_count += 1
        return page

    def get_webhook_verification_key(self, *, key_id: str) -> dict[str, Any]:
        raise PlaidApiError("KEY_NOT_FOUND", "no such key")


def page(
    *,
    added: list[PlaidTransaction] | None = None,
    modified: list[PlaidTransaction] | None = None,
    removed_ids: list[str] | None = None,
    accounts: list[PlaidAccount] | None = None,
    next_cursor: str = "cursor-1",
    has_more: bool = False,
) -> SyncPage:
    return SyncPage(
        added=added or [],
        modified=modified or [],
        removed_ids=removed_ids or [],
        accounts=accounts if accounts is not None else [make_account()],
        next_cursor=next_cursor,
        has_more=has_more,
    )

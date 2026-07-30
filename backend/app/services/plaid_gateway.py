"""The boundary between our code and Plaid.

===========================================================================
Why a gateway instead of calling the SDK directly
===========================================================================

Everything that talks to Plaid goes through the `PlaidGateway` protocol
below. Nothing else in the application imports the `plaid` package.

Three concrete benefits:

1. **The sync engine becomes testable.** Tests supply a fake gateway and
   drive every scenario that matters -- a transaction posting, a duplicate
   page, an expired login -- deterministically, offline, in milliseconds.
   Reproducing those against the real sandbox would be slow, flaky, and in
   some cases impossible.

2. **Plaid's SDK types stay out of our domain.** The SDK returns objects
   whose shape changes between major versions. We convert once, here, into
   plain dataclasses. When Plaid v43 renames a field, one file changes.

3. **One place to enforce the rules.** Retries, timeouts, error mapping, and
   the rule that an access token is never logged all live in a single file
   rather than being repeated at every call site.

This is the same pattern used for Clerk in Milestone 3: substitute the
boundary, exercise everything inside it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Our own data types. Deliberately plain -- no Plaid classes leak past here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaidAccount:
    account_id: str
    name: str
    official_name: str | None
    mask: str | None
    type: str
    subtype: str | None
    currency_code: str
    current_balance: Decimal | None
    available_balance: Decimal | None
    credit_limit: Decimal | None


@dataclass(frozen=True)
class PlaidTransaction:
    transaction_id: str
    account_id: str
    amount: Decimal
    currency_code: str
    date: dt.date
    name: str
    pending: bool
    merchant_name: str | None = None
    authorized_date: dt.date | None = None
    # Set on a POSTED transaction, naming the pending one it replaces. This
    # single field is what stops one coffee appearing on your statement twice.
    pending_transaction_id: str | None = None
    category: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncPage:
    """One page of `/transactions/sync` output."""

    added: list[PlaidTransaction]
    modified: list[PlaidTransaction]
    # Plaid sends only the ids of removed transactions, not the whole record.
    removed_ids: list[str]
    accounts: list[PlaidAccount]
    next_cursor: str
    has_more: bool


@dataclass(frozen=True)
class PlaidItemInfo:
    item_id: str
    institution_id: str | None


@dataclass(frozen=True)
class PlaidInstitution:
    institution_id: str
    name: str
    logo_url: str | None = None
    primary_color: str | None = None
    website_url: str | None = None


class PlaidApiError(Exception):
    """A structured Plaid failure.

    `error_code` is the part that matters: it decides whether the sync engine
    should retry (`RATE_LIMIT_EXCEEDED`), ask the user to reconnect
    (`ITEM_LOGIN_REQUIRED`), or give up.
    """

    def __init__(self, error_code: str, message: str, status_code: int = 400) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code
        self.message = message
        self.status_code = status_code

    @property
    def requires_user_reauth(self) -> bool:
        return self.error_code in {
            "ITEM_LOGIN_REQUIRED",
            "PENDING_EXPIRATION",
            "ITEM_LOCKED",
        }

    @property
    def token_already_invalid(self) -> bool:
        """Plaid will not accept this token, and no retry will change that.

        The distinction that matters is between "the call failed" and "there
        is nothing behind this token to act on". Both of these mean the
        latter, so an operation whose goal is *removal* has already got what
        it wanted:

        - ITEM_NOT_FOUND     -- the item is gone from Plaid's side already.
        - INVALID_ACCESS_TOKEN -- the token is not one this Plaid environment
          recognises. The way to reach it is switching PLAID_ENV: a token
          minted in sandbox is meaningless to production and vice versa, so
          every connection made before the switch is left holding one.

        Treating these as failures makes such a connection permanently
        undeletable -- the revoke is attempted first by design, so it fails
        before the row is ever cleaned up, and it will fail identically every
        time. That leaves the user with a dead connection they cannot remove
        and a stored token we promised to stop keeping. Neither is safer than
        proceeding; both are worse.
        """
        return self.error_code in {"ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"}

    @property
    def is_transient(self) -> bool:
        """Worth retrying later; not worth surfacing to the user."""
        return self.error_code in {
            "RATE_LIMIT_EXCEEDED",
            "INTERNAL_SERVER_ERROR",
            "PLANNED_MAINTENANCE",
            "INSTITUTION_DOWN",
            "INSTITUTION_NOT_RESPONDING",
        }


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class PlaidGateway(Protocol):
    """What the rest of the application is allowed to ask Plaid for.

    A `Protocol` is Python's structural interface: any object with these
    methods satisfies it, with no inheritance required. The test fake does not
    import this class at all -- it simply has the same methods.
    """

    def create_link_token(self, *, user_id: str) -> str: ...

    def exchange_public_token(self, *, public_token: str) -> tuple[str, str]: ...

    def get_item(self, *, access_token: str) -> PlaidItemInfo: ...

    def get_institution(self, *, institution_id: str) -> PlaidInstitution | None: ...

    def get_accounts(self, *, access_token: str) -> list[PlaidAccount]: ...

    def sync_transactions(
        self, *, access_token: str, cursor: str | None
    ) -> SyncPage: ...

    def remove_item(self, *, access_token: str) -> None: ...

    def get_webhook_verification_key(self, *, key_id: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# The real implementation
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal | None:
    """Convert Plaid's float amounts to Decimal.

    Plaid's JSON gives us floats, which cannot represent 0.1 exactly. Going
    via `str()` is what makes this exact: `Decimal(0.1)` is
    0.1000000000000000055511151231257827, while `Decimal(str(0.1))` is
    exactly 0.1. Converting at the boundary means no float ever reaches the
    database or an arithmetic operation.
    """
    if value is None:
        return None
    return Decimal(str(value))


class LivePlaidGateway:
    """Talks to the real Plaid API."""

    def __init__(self) -> None:
        # Imported lazily so the application starts without the SDK configured
        # and so tests never touch it.
        import plaid
        from plaid.api import plaid_api

        settings = get_settings()

        if not settings.plaid_configured:
            raise PlaidApiError(
                "PLAID_NOT_CONFIGURED",
                "PLAID_CLIENT_ID and PLAID_SECRET must be set",
                status_code=503,
            )

        configuration = plaid.Configuration(
            host=settings.plaid_host,
            api_key={
                "clientId": settings.PLAID_CLIENT_ID,
                "secret": settings.PLAID_SECRET,
            },
        )
        self._client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
        self._settings = settings

    # -- helpers ---------------------------------------------------------

    def _call(self, operation: str, func, *args):
        """Run an SDK call and turn its failures into `PlaidApiError`.

        Note what is NOT logged: the access token, and the raw request body.
        Plaid's exceptions can include the request that caused them, and that
        request contains the token.
        """
        from plaid.exceptions import ApiException

        try:
            return func(*args)
        except ApiException as exc:
            error_code, message = self._parse_error(exc)
            logger.warning("Plaid %s failed: %s", operation, error_code)
            raise PlaidApiError(error_code, message, status_code=exc.status) from exc

    @staticmethod
    def _parse_error(exc) -> tuple[str, str]:
        import json

        try:
            body = json.loads(exc.body)
            return (
                body.get("error_code", "UNKNOWN"),
                body.get("error_message", "Unknown Plaid error"),
            )
        except (ValueError, TypeError, AttributeError):
            return ("UNKNOWN", "Unparseable Plaid error response")

    # -- link / item -----------------------------------------------------

    def create_link_token(self, *, user_id: str) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import (
            LinkTokenCreateRequestUser,
        )
        from plaid.model.products import Products

        kwargs: dict[str, Any] = {
            "client_name": self._settings.PLAID_CLIENT_NAME,
            "language": "en",
            "country_codes": [
                CountryCode(code) for code in self._settings.PLAID_COUNTRY_CODES
            ],
            "products": [Products(p) for p in self._settings.PLAID_PRODUCTS],
            # Our own user id, NOT an email or anything else identifying.
            # Plaid stores this, and there is no reason to hand a third party
            # personal data they do not need.
            "user": LinkTokenCreateRequestUser(client_user_id=user_id),
        }
        if self._settings.PLAID_WEBHOOK_URL:
            kwargs["webhook"] = self._settings.PLAID_WEBHOOK_URL

        request = LinkTokenCreateRequest(**kwargs)
        response = self._call(
            "link_token_create", self._client.link_token_create, request
        )
        return response["link_token"]

    def exchange_public_token(self, *, public_token: str) -> tuple[str, str]:
        """Swap the short-lived public token for a long-lived access token.

        This exchange happens SERVER-side, always. The browser receives only
        the public token, which is useless on its own and expires in minutes.
        The access token it becomes is permanent and must never be sent to a
        client.
        """
        from plaid.model.item_public_token_exchange_request import (
            ItemPublicTokenExchangeRequest,
        )

        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        response = self._call(
            "item_public_token_exchange",
            self._client.item_public_token_exchange,
            request,
        )
        return response["access_token"], response["item_id"]

    def get_item(self, *, access_token: str) -> PlaidItemInfo:
        from plaid.model.item_get_request import ItemGetRequest

        response = self._call(
            "item_get", self._client.item_get, ItemGetRequest(access_token=access_token)
        )
        item = response["item"]
        return PlaidItemInfo(
            item_id=item["item_id"],
            institution_id=item.get("institution_id"),
        )

    def get_institution(self, *, institution_id: str) -> PlaidInstitution | None:
        from plaid.model.country_code import CountryCode
        from plaid.model.institutions_get_by_id_request import (
            InstitutionsGetByIdRequest,
        )
        from plaid.model.institutions_get_by_id_request_options import (
            InstitutionsGetByIdRequestOptions,
        )

        request = InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[
                CountryCode(code) for code in self._settings.PLAID_COUNTRY_CODES
            ],
            options=InstitutionsGetByIdRequestOptions(
                include_optional_metadata=True
            ),
        )

        try:
            response = self._call(
                "institutions_get_by_id",
                self._client.institutions_get_by_id,
                request,
            )
        except PlaidApiError:
            # Branding is cosmetic. Never fail a bank connection because a
            # logo lookup did not work.
            logger.info("Could not fetch institution metadata for %s", institution_id)
            return None

        institution = response["institution"]
        return PlaidInstitution(
            institution_id=institution["institution_id"],
            name=institution["name"],
            logo_url=institution.get("logo"),
            primary_color=institution.get("primary_color"),
            website_url=institution.get("url"),
        )

    def remove_item(self, *, access_token: str) -> None:
        """Tell Plaid to forget this connection.

        Called when a user disconnects a bank. Important for two reasons:
        Plaid bills per active Item, and leaving a live credential in place
        after the user asked you to remove it is a genuine privacy failure.
        """
        from plaid.model.item_remove_request import ItemRemoveRequest

        self._call(
            "item_remove",
            self._client.item_remove,
            ItemRemoveRequest(access_token=access_token),
        )

    # -- data ------------------------------------------------------------

    def get_accounts(self, *, access_token: str) -> list[PlaidAccount]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        response = self._call(
            "accounts_get",
            self._client.accounts_get,
            AccountsGetRequest(access_token=access_token),
        )
        return [self._convert_account(a) for a in response["accounts"]]

    def sync_transactions(self, *, access_token: str, cursor: str | None) -> SyncPage:
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        kwargs: dict[str, Any] = {"access_token": access_token, "count": 500}
        # Omitting the cursor entirely means "start from the beginning of
        # history". Passing an empty string is an error, which is an easy
        # mistake to make on the very first sync.
        if cursor:
            kwargs["cursor"] = cursor

        response = self._call(
            "transactions_sync",
            self._client.transactions_sync,
            TransactionsSyncRequest(**kwargs),
        )

        return SyncPage(
            added=[self._convert_transaction(t) for t in response["added"]],
            modified=[self._convert_transaction(t) for t in response["modified"]],
            removed_ids=[r["transaction_id"] for r in response["removed"]],
            accounts=[self._convert_account(a) for a in response.get("accounts", [])],
            next_cursor=response["next_cursor"],
            has_more=response["has_more"],
        )

    def get_webhook_verification_key(self, *, key_id: str) -> dict[str, Any]:
        from plaid.model.webhook_verification_key_get_request import (
            WebhookVerificationKeyGetRequest,
        )

        response = self._call(
            "webhook_verification_key_get",
            self._client.webhook_verification_key_get,
            WebhookVerificationKeyGetRequest(key_id=key_id),
        )
        return response["key"].to_dict()

    # -- conversion ------------------------------------------------------

    @staticmethod
    def _convert_account(account: Any) -> PlaidAccount:
        balances = account["balances"]
        return PlaidAccount(
            account_id=account["account_id"],
            name=account["name"],
            official_name=account.get("official_name"),
            mask=account.get("mask"),
            type=str(account["type"]),
            subtype=str(account["subtype"]) if account.get("subtype") else None,
            currency_code=balances.get("iso_currency_code") or "USD",
            current_balance=_to_decimal(balances.get("current")),
            available_balance=_to_decimal(balances.get("available")),
            credit_limit=_to_decimal(balances.get("limit")),
        )

    @staticmethod
    def _convert_transaction(txn: Any) -> PlaidTransaction:
        return PlaidTransaction(
            transaction_id=txn["transaction_id"],
            account_id=txn["account_id"],
            amount=_to_decimal(txn["amount"]),
            currency_code=txn.get("iso_currency_code") or "USD",
            date=txn["date"],
            name=txn["name"],
            pending=bool(txn["pending"]),
            merchant_name=txn.get("merchant_name"),
            authorized_date=txn.get("authorized_date"),
            pending_transaction_id=txn.get("pending_transaction_id"),
            category=(
                txn["personal_finance_category"]["primary"]
                if txn.get("personal_finance_category")
                else None
            ),
            # The whole original record, kept for forensics. See the note on
            # raw_payload in models/transaction.py.
            raw=txn.to_dict() if hasattr(txn, "to_dict") else dict(txn),
        )


def get_plaid_gateway() -> PlaidGateway:
    """FastAPI dependency. Overridden in tests with a fake."""
    return LivePlaidGateway()

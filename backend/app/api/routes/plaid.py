"""Plaid endpoints: linking, syncing, disconnecting, and the webhook.

===========================================================================
The token exchange, and why it is shaped this way
===========================================================================

    browser                 our backend                Plaid
      |                          |                       |
      |-- GET /link-token ------>|                       |
      |                          |-- link_token/create ->|
      |<------ link_token -------|                       |
      |                                                  |
      |----------- user picks their bank, logs in ------>|
      |<---------------- public_token -------------------|
      |                          |                       |
      |-- POST /exchange ------->|                       |
      |   {public_token}         |-- exchange ---------->|
      |                          |<---- ACCESS TOKEN ----|
      |                          |   (encrypt, store)    |
      |<------- 201 Created -----|                       |

The browser only ever holds a `public_token`: single-use, expires in minutes,
and worthless without our Plaid secret. The `access_token` -- which grants
ongoing read access to real bank accounts -- is created server-side,
encrypted immediately, and never appears in any API response.

The user's bank credentials never touch our servers at all. They are entered
inside Plaid's iframe, which is the entire point of using Plaid.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.db.scoping import scoped_get, scoped_select
from app.models import PlaidItem, PlaidItemStatus, SyncHistory, SyncTrigger
from app.schemas.plaid import (
    ExchangePublicTokenRequest,
    LinkTokenResponse,
    PlaidItemResponse,
    SyncRunResponse,
    WebhookAck,
)
from app.services.plaid_gateway import (
    PlaidApiError,
    PlaidGateway,
    get_plaid_gateway,
)
from app.services.plaid_webhooks import (
    SYNC_TRIGGERING_CODES,
    WebhookVerificationError,
    parse_webhook,
    verify_webhook,
)
from app.services.sync import SyncEngine, link_institution

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plaid", tags=["plaid"])

Gateway = Annotated[PlaidGateway, Depends(get_plaid_gateway)]


def _plaid_error_to_http(exc: PlaidApiError) -> HTTPException:
    """Translate a Plaid failure into an honest HTTP status.

    502 for a broken upstream, 503 for a temporary one -- never 500, which
    would claim the bug is ours and send an on-call engineer hunting through
    our own code.
    """
    if exc.error_code == "PLAID_NOT_CONFIGURED":
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plaid is not configured on this server.",
        )
    if exc.is_transient:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plaid is temporarily unavailable. Please try again.",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Plaid rejected the request ({exc.error_code}).",
    )


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------


@router.post("/link-token", response_model=LinkTokenResponse)
def create_link_token(current_user: CurrentUser, gateway: Gateway) -> LinkTokenResponse:
    """Mint a Link token to open Plaid's bank-picker in the browser.

    We pass OUR user id as Plaid's `client_user_id` -- never an email or a
    name. Plaid retains it, and there is no reason to give a third party
    personal data they do not need to do their job.
    """
    try:
        link_token = gateway.create_link_token(user_id=str(current_user.id))
    except PlaidApiError as exc:
        raise _plaid_error_to_http(exc) from exc

    return LinkTokenResponse(link_token=link_token)


@router.post(
    "/exchange",
    response_model=PlaidItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def exchange_public_token(
    payload: ExchangePublicTokenRequest,
    current_user: CurrentUser,
    db: DbSession,
    gateway: Gateway,
) -> PlaidItemResponse:
    """Finish linking: exchange the public token and pull in the accounts."""
    try:
        item = link_institution(
            db,
            gateway,
            user_id=current_user.id,
            public_token=payload.public_token,
        )
    except PlaidApiError as exc:
        db.rollback()
        raise _plaid_error_to_http(exc) from exc

    # Reload with the institution attached so the response can include it
    # without triggering a lazy load after the response model is built.
    item = db.execute(
        select(PlaidItem)
        .options(selectinload(PlaidItem.institution))
        .where(PlaidItem.id == item.id)
    ).scalar_one()

    return PlaidItemResponse.model_validate(item)


# ---------------------------------------------------------------------------
# Reading and managing connections
# ---------------------------------------------------------------------------


@router.get("/items", response_model=list[PlaidItemResponse])
def list_items(current_user: CurrentUser, db: DbSession) -> list[PlaidItemResponse]:
    items = (
        db.execute(
            scoped_select(PlaidItem, current_user)
            .options(selectinload(PlaidItem.institution))
            .where(PlaidItem.status != PlaidItemStatus.DISCONNECTED)
            .order_by(PlaidItem.created_at)
        )
        .scalars()
        .all()
    )
    return [PlaidItemResponse.model_validate(item) for item in items]


@router.post("/items/{item_id}/sync", response_model=SyncRunResponse)
def sync_item(
    item_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    gateway: Gateway,
) -> SyncRunResponse:
    """Sync one connection on demand.

    `scoped_get` means a user cannot trigger a sync on somebody else's item;
    a foreign id is indistinguishable from a missing one.
    """
    item = scoped_get(db, PlaidItem, item_id, current_user)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.MANUAL)
    return SyncRunResponse.model_validate(run)


@router.get("/items/{item_id}/syncs", response_model=list[SyncRunResponse])
def list_sync_history(
    item_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    limit: int = 20,
) -> list[SyncRunResponse]:
    """Recent sync attempts, newest first.

    This is what turns "my transactions are missing" from a shrug into an
    answer: the last run, when it happened, what it did, and how it failed.
    """
    item = scoped_get(db, PlaidItem, item_id, current_user)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )

    runs = (
        db.execute(
            select(SyncHistory)
            .where(SyncHistory.plaid_item_id == item.id)
            .order_by(SyncHistory.started_at.desc())
            .limit(min(limit, 100))
        )
        .scalars()
        .all()
    )
    return [SyncRunResponse.model_validate(run) for run in runs]


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # `response_model=None` is required, not optional.
    #
    # This module uses `from __future__ import annotations`, so the `-> None`
    # return annotation is resolved to the *class* `NoneType` -- which is
    # truthy. FastAPI therefore treats it as a real response model and asserts
    # that a 204 must not have a body. Stating it explicitly resolves the
    # ambiguity.
    response_model=None,
)
def disconnect_item(
    item_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    gateway: Gateway,
) -> None:
    """Disconnect a bank.

    Two things happen, in this order:

    1. Plaid is told to invalidate the access token. This must come first --
       if we only marked our row DISCONNECTED, a live credential to the user's
       bank would keep existing at Plaid after they asked us to remove it.
    2. Our record is marked DISCONNECTED and the stored token is cleared.

    The accounts and transactions are NOT deleted. The user asked to stop
    syncing, not to erase their financial history; destroying years of records
    because someone clicked "disconnect" would be its own kind of data loss.
    Full erasure is a separate, explicit action.
    """
    item = scoped_get(db, PlaidItem, item_id, current_user)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )

    try:
        gateway.remove_item(access_token=decrypt(item.access_token_encrypted))
    except PlaidApiError as exc:
        # Already invalid at Plaid's end is a success for our purposes.
        if not exc.requires_user_reauth and exc.error_code != "ITEM_NOT_FOUND":
            raise _plaid_error_to_http(exc) from exc
        logger.info("Plaid item already invalid on removal: %s", exc.error_code)

    item.status = PlaidItemStatus.DISCONNECTED
    # Overwritten rather than left in place: there is no reason to keep a
    # credential we have promised to stop using.
    item.access_token_encrypted = ""
    item.transactions_cursor = None
    db.commit()


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@router.post("/webhook", response_model=WebhookAck, include_in_schema=False)
async def plaid_webhook(
    request: Request,
    db: DbSession,
    gateway: Gateway,
    plaid_verification: Annotated[str | None, Header(alias="Plaid-Verification")] = None,
) -> WebhookAck:
    """Receive a webhook from Plaid.

    This endpoint is PUBLIC -- Plaid cannot log in. That is why the signature
    check below is the only thing standing between the internet and this code,
    and why it runs before anything else touches the payload.

    It is listed in the authorization guard test's PUBLIC_PATHS allowlist,
    which is a deliberate, reviewable exception rather than an oversight.
    """
    raw_body = await request.body()

    try:
        verify_webhook(
            gateway=gateway,
            verification_header=plaid_verification,
            raw_body=raw_body,
        )
    except WebhookVerificationError as exc:
        # 401, and nothing about why. An attacker probing this endpoint learns
        # only that it refused them.
        logger.warning("Rejected unverified Plaid webhook: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unverified webhook"
        ) from exc

    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload"
        ) from exc

    event = parse_webhook(payload)
    logger.info("Plaid webhook %s/%s", event.webhook_type, event.webhook_code)

    if event.item_id:
        _handle_item_event(db, gateway, event)

    # Always 200 once verified. Plaid retries on a non-2xx, so returning an
    # error for an event we simply do not handle would cause it to be
    # redelivered forever.
    return WebhookAck()


def _handle_item_event(db: Session, gateway: PlaidGateway, event) -> None:
    item = db.execute(
        select(PlaidItem).where(PlaidItem.plaid_item_id == event.item_id)
    ).scalar_one_or_none()

    if item is None:
        # Not ours, or already disconnected. Nothing to do, and nothing to
        # complain about -- Plaid can legitimately send a trailing webhook
        # after removal.
        logger.info("Webhook for unknown item %s", event.item_id)
        return

    if event.webhook_type == "ITEM" and event.error_code:
        item.status = (
            PlaidItemStatus.LOGIN_REQUIRED
            if event.error_code == "ITEM_LOGIN_REQUIRED"
            else PlaidItemStatus.ERROR
        )
        item.error_code = event.error_code
        db.commit()
        return

    if event.webhook_code in SYNC_TRIGGERING_CODES:
        # Synchronous for now, which is fine at one user. Milestone 8 moves
        # this to a background queue: a webhook handler that does slow work
        # inline will eventually time out, and Plaid will retry, and you get
        # two syncs running at once. Idempotency is what makes that survivable
        # in the meantime.
        SyncEngine(db, gateway).sync_item(item, SyncTrigger.WEBHOOK)

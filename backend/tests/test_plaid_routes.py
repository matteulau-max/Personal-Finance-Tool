"""Plaid endpoint tests.

Covers the API surface: linking, listing, syncing, disconnecting, and the
webhook -- plus the authorization rules from Milestone 3 applied to the new
routes.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import encrypt
from app.models import Account, PlaidItem, PlaidItemStatus, Transaction
from app.services.plaid_gateway import PlaidApiError, get_plaid_gateway
from tests.auth_helpers import auth_header
from tests.conftest import make_account
from tests.fake_plaid import FakePlaidGateway, make_txn, page


@pytest.fixture
def plaid_client(client: TestClient):
    """A client whose Plaid gateway is the fake.

    Returns both, so a test can drive the API and then assert on how Plaid was
    called -- which cursor was sent, whether remove_item happened.
    """
    from app.main import app

    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_1")])])
    app.dependency_overrides[get_plaid_gateway] = lambda: gateway

    yield client, gateway

    app.dependency_overrides.pop(get_plaid_gateway, None)


def existing_item(db: Session, user, *, plaid_item_id: str | None = None) -> PlaidItem:
    item = PlaidItem(
        user_id=user.id,
        plaid_item_id=plaid_item_id or f"item_{uuid.uuid4().hex[:12]}",
        access_token_encrypted=encrypt("access-sandbox-existing"),
    )
    db.add(item)
    db.flush()
    return item


# ---------------------------------------------------------------------------
# Link token
# ---------------------------------------------------------------------------


def test_link_token_requires_authentication(plaid_client):
    client, _ = plaid_client
    assert client.post("/api/plaid/link-token").status_code == 401


def test_link_token_is_created_for_the_signed_in_user(plaid_client, authenticated_user):
    client, gateway = plaid_client
    user, token = authenticated_user

    response = client.post("/api/plaid/link-token", headers=auth_header(token))

    assert response.status_code == 200
    assert response.json()["link_token"].startswith("link-sandbox-")
    assert gateway.link_tokens_created == 1


def test_link_token_identifies_the_user_by_uuid_not_email(
    plaid_client, authenticated_user
):
    """Plaid retains `client_user_id`. There is no reason to hand a third
    party an email address they do not need."""
    client, gateway = plaid_client
    user, token = authenticated_user

    body = client.post("/api/plaid/link-token", headers=auth_header(token)).json()

    assert str(user.id) in body["link_token"]
    assert user.email not in body["link_token"]


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------


def test_exchange_creates_an_item_and_imports_accounts(
    plaid_client, db: Session, authenticated_user
):
    client, _ = plaid_client
    user, token = authenticated_user

    response = client.post(
        "/api/plaid/exchange",
        headers=auth_header(token),
        json={"public_token": "public-sandbox-abc"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "healthy"
    assert body["institution"]["name"] == "Fake Bank"

    item = db.execute(
        select(PlaidItem).where(PlaidItem.user_id == user.id)
    ).scalar_one()
    assert item.access_token_encrypted != ""


def test_exchange_never_returns_the_access_token(plaid_client, authenticated_user):
    """The single most important assertion about this endpoint.

    An access token in an API response would be readable in the browser's
    network tab, in any proxy log, and in the client's own error reporting.
    """
    client, _ = plaid_client
    _, token = authenticated_user

    response = client.post(
        "/api/plaid/exchange",
        headers=auth_header(token),
        json={"public_token": "public-sandbox-abc"},
    )

    assert "access-sandbox" not in response.text
    assert "access_token" not in response.json()
    assert "transactions_cursor" not in response.json()


def test_exchange_rejects_an_empty_public_token(plaid_client, authenticated_user):
    client, _ = plaid_client
    _, token = authenticated_user

    response = client.post(
        "/api/plaid/exchange", headers=auth_header(token), json={"public_token": ""}
    )

    # Caught by schema validation, before it ever reaches Plaid.
    assert response.status_code == 422


def test_a_plaid_rejection_becomes_502_not_500(plaid_client, authenticated_user):
    """An upstream failure is not our bug.

    Returning 500 would send an on-call engineer hunting through our code for
    a problem that is entirely at Plaid's end.
    """
    client, _ = plaid_client
    _, token = authenticated_user

    response = client.post(
        "/api/plaid/exchange",
        headers=auth_header(token),
        json={"public_token": "invalid"},
    )

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# Listing and isolation
# ---------------------------------------------------------------------------


def test_items_are_scoped_to_the_owner(
    plaid_client, db: Session, authenticated_user, other_user
):
    client, _ = plaid_client
    user, token = authenticated_user
    stranger, _ = other_user

    existing_item(db, user)
    existing_item(db, stranger)
    db.flush()

    response = client.get("/api/plaid/items", headers=auth_header(token))

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_syncing_another_users_item_returns_404(
    plaid_client, db: Session, authenticated_user, other_user
):
    """Not 403 -- the id must not be confirmed as real."""
    client, _ = plaid_client
    _, token = authenticated_user
    stranger, _ = other_user

    theirs = existing_item(db, stranger)
    db.flush()

    response = client.post(
        f"/api/plaid/items/{theirs.id}/sync", headers=auth_header(token)
    )

    assert response.status_code == 404


def test_item_response_hides_the_stored_token_and_cursor(
    plaid_client, db: Session, authenticated_user
):
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    item.transactions_cursor = "secret-cursor"
    db.flush()

    [body] = client.get("/api/plaid/items", headers=auth_header(token)).json()

    assert "access_token_encrypted" not in body
    assert "transactions_cursor" not in body
    assert "secret-cursor" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Manual sync
# ---------------------------------------------------------------------------


def test_manual_sync_imports_transactions(
    plaid_client, db: Session, authenticated_user
):
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    db.flush()

    response = client.post(
        f"/api/plaid/items/{item.id}/sync", headers=auth_header(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["transactions_added"] == 1
    assert body["trigger"] == "manual"

    count = len(
        db.execute(select(Transaction).where(Transaction.user_id == user.id))
        .scalars()
        .all()
    )
    assert count == 1


def test_sync_history_is_readable(plaid_client, db: Session, authenticated_user):
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    db.flush()

    client.post(f"/api/plaid/items/{item.id}/sync", headers=auth_header(token))

    runs = client.get(
        f"/api/plaid/items/{item.id}/syncs", headers=auth_header(token)
    ).json()

    assert len(runs) == 1
    assert runs[0]["status"] == "success"


# ---------------------------------------------------------------------------
# Disconnecting
# ---------------------------------------------------------------------------


def test_disconnect_revokes_at_plaid_and_clears_the_token(
    plaid_client, db: Session, authenticated_user
):
    """Revoking at Plaid must actually happen.

    Marking our row disconnected while leaving a live credential at Plaid
    would mean the user's bank access continues after they asked us to stop.
    """
    client, gateway = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    db.flush()

    response = client.delete(
        f"/api/plaid/items/{item.id}", headers=auth_header(token)
    )

    assert response.status_code == 204
    assert gateway.removed_items == ["access-sandbox-existing"]

    db.expire_all()
    db.refresh(item)
    assert item.status == PlaidItemStatus.DISCONNECTED
    assert item.access_token_encrypted == ""


def test_disconnect_keeps_the_transaction_history(
    plaid_client, db: Session, authenticated_user
):
    """Disconnecting means "stop syncing", not "erase my financial history".

    Deleting years of records because someone clicked a button would be its
    own kind of data loss. Erasure is a separate, explicit action.
    """
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    db.flush()

    client.post(f"/api/plaid/items/{item.id}/sync", headers=auth_header(token))
    client.delete(f"/api/plaid/items/{item.id}", headers=auth_header(token))

    remaining = (
        db.execute(select(Transaction).where(Transaction.user_id == user.id))
        .scalars()
        .all()
    )
    assert len(remaining) == 1


def test_disconnect_retires_the_accounts(
    plaid_client, db: Session, authenticated_user
):
    """A disconnected bank's accounts must stop being listed as live.

    `is_active` is what the accounts page and every balance query filter on.
    Left true, the accounts keep appearing with whatever balance they held at
    the moment of disconnection, and keep counting toward net worth -- frozen
    figures presented as current ones, which is worse than showing nothing.
    """
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    account = make_account(db, user)
    account.plaid_item_id = item.id
    db.flush()

    client.delete(f"/api/plaid/items/{item.id}", headers=auth_header(token))

    db.expire_all()
    db.refresh(account)
    assert account.is_active is False

    listed = client.get("/api/accounts", headers=auth_header(token)).json()
    assert listed == []


def test_disconnect_does_not_erase_the_accounts(
    plaid_client, db: Session, authenticated_user
):
    """Retired, not deleted.

    The account rows are what every historical transaction points at. Deleting
    them to tidy the list would take the history with them, which is the
    opposite of what disconnecting promises.
    """
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    account = make_account(db, user)
    account.plaid_item_id = item.id
    db.flush()
    account_id = account.id

    client.delete(f"/api/plaid/items/{item.id}", headers=auth_header(token))

    db.expire_all()
    assert db.get(Account, account_id) is not None


def test_disconnected_items_are_hidden_from_the_list(
    plaid_client, db: Session, authenticated_user
):
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    db.flush()

    client.delete(f"/api/plaid/items/{item.id}", headers=auth_header(token))

    assert client.get("/api/plaid/items", headers=auth_header(token)).json() == []


@pytest.mark.parametrize("error_code", ["ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"])
def test_disconnect_succeeds_when_the_token_is_already_invalid(
    plaid_client, db: Session, authenticated_user, error_code
):
    """A token Plaid rejects must not make a connection undeletable.

    INVALID_ACCESS_TOKEN is the one that actually happens: switching PLAID_ENV
    from sandbox to production invalidates every token minted before the
    switch. Since revocation is attempted before the row is cleaned up, an
    unforgiven error here fails identically on every attempt, stranding the
    user with a dead connection they cannot remove.
    """
    client, gateway = plaid_client
    user, token = authenticated_user
    gateway.raise_on_remove = PlaidApiError(error_code, "no such token")
    item = existing_item(db, user)
    db.flush()

    response = client.delete(
        f"/api/plaid/items/{item.id}", headers=auth_header(token)
    )

    assert response.status_code == 204

    db.expire_all()
    db.refresh(item)
    assert item.status == PlaidItemStatus.DISCONNECTED
    # Still cleared: the token is worthless, and keeping it serves nothing.
    assert item.access_token_encrypted == ""


def test_disconnect_fails_loudly_when_revocation_genuinely_fails(
    plaid_client, db: Session, authenticated_user
):
    """The tolerance above must not become "ignore every error".

    If Plaid is down, the credential is still live. Reporting success would
    tell the user their bank access was revoked when it was not, and clearing
    our token would destroy the only means of ever revoking it.
    """
    client, gateway = plaid_client
    user, token = authenticated_user
    gateway.raise_on_remove = PlaidApiError("INTERNAL_SERVER_ERROR", "Plaid is down")
    item = existing_item(db, user)
    db.flush()

    response = client.delete(
        f"/api/plaid/items/{item.id}", headers=auth_header(token)
    )

    assert response.status_code >= 400

    db.expire_all()
    db.refresh(item)
    assert item.status != PlaidItemStatus.DISCONNECTED
    assert item.access_token_encrypted != ""


def test_disconnecting_an_already_disconnected_item_does_not_error(
    plaid_client, db: Session, authenticated_user
):
    """The second click must not produce a 500.

    A disconnected item holds an empty token, and decrypting that raises
    rather than returning an empty string -- so the guard has to come before
    the call, not inside it.
    """
    client, _ = plaid_client
    user, token = authenticated_user
    item = existing_item(db, user)
    db.flush()

    client.delete(f"/api/plaid/items/{item.id}", headers=auth_header(token))
    response = client.delete(
        f"/api/plaid/items/{item.id}", headers=auth_header(token)
    )

    assert response.status_code == 204


def test_disconnecting_another_users_item_returns_404(
    plaid_client, db: Session, authenticated_user, other_user
):
    client, gateway = plaid_client
    _, token = authenticated_user
    stranger, _ = other_user
    theirs = existing_item(db, stranger)
    db.flush()

    response = client.delete(
        f"/api/plaid/items/{theirs.id}", headers=auth_header(token)
    )

    assert response.status_code == 404
    # And crucially, nothing was revoked at Plaid.
    assert gateway.removed_items == []


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_webhook_without_a_signature_is_rejected(plaid_client):
    """This endpoint is public, so the signature IS the security boundary."""
    client, _ = plaid_client

    response = client.post(
        "/api/plaid/webhook",
        json={"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"},
    )

    assert response.status_code == 401


def test_webhook_with_a_bogus_signature_is_rejected(plaid_client):
    client, _ = plaid_client

    response = client.post(
        "/api/plaid/webhook",
        headers={"Plaid-Verification": "not-a-real-jwt"},
        json={"webhook_type": "TRANSACTIONS"},
    )

    assert response.status_code == 401
    # Nothing about WHY it failed.
    assert response.json() == {"detail": "Unverified webhook"}

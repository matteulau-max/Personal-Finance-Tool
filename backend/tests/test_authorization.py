"""Authorization tests.

Authentication asks "who are you?". Authorization asks "what may you see?".
This file is about the second, and it is the more dangerous of the two --
authentication bugs are loud (nobody can log in), authorization bugs are
silent (everybody sees everything, and the app looks fine).

Two layers are tested here:

  1. A structural guard that fails if ANY endpoint is added without
     authentication. It needs no knowledge of the new endpoint.
  2. Behavioural tests proving one user cannot reach another's data.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.scoping import NotScopeable, scoped_get, scoped_select
from app.main import app
from app.models import Account, Institution
from tests.auth_helpers import auth_header
from tests.conftest import make_account

# Endpoints that are intentionally public. Anything not on this list must
# require authentication.
#
# Keeping the allowlist here, in the test, is the point: adding a public
# endpoint becomes a deliberate edit to a security test rather than something
# that slips by unnoticed in a feature branch.
PUBLIC_PATHS = {
    "/health",
    "/health/db",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _requires_dependency(dependant, target) -> bool:
    """Walk the dependency tree looking for a specific function."""
    if dependant.call is target:
        return True
    return any(_requires_dependency(sub, target) for sub in dependant.dependencies)


def test_every_endpoint_requires_authentication():
    """The guard test.

    It inspects FastAPI's route table rather than making requests, so it
    covers endpoints that do not exist yet. Add an unprotected route in
    Milestone 6 and this fails immediately, naming the path.

    This is the single highest-value test in the project: it protects against
    a mistake nobody makes on purpose and everybody makes eventually.
    """
    unprotected: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in PUBLIC_PATHS:
            continue
        if not _requires_dependency(route.dependant, get_current_user):
            unprotected.append(f"{sorted(route.methods)} {route.path}")

    assert not unprotected, (
        "These endpoints do not require authentication:\n  "
        + "\n  ".join(unprotected)
        + "\n\nAdd `current_user: CurrentUser` to the endpoint, or add the path "
        "to PUBLIC_PATHS if it is genuinely public."
    )


def test_health_endpoints_stay_public(client: TestClient):
    """Load balancers cannot authenticate. If these ever require a token, the
    platform will conclude the service is down and restart it forever."""
    assert client.get("/health").status_code == 200
    assert client.get("/health/db").status_code == 200


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


def test_account_list_only_returns_your_own_accounts(
    client: TestClient, db: Session, authenticated_user, other_user
):
    """The test that catches a missing WHERE clause.

    With only one user in the database, an unscoped query returns exactly the
    right answer and this test would pass. The second user is what makes the
    bug detectable.
    """
    user, token = authenticated_user
    stranger, _ = other_user

    make_account(db, user, name="My Checking")
    make_account(db, stranger, name="Their Checking")
    db.flush()

    response = client.get("/api/accounts", headers=auth_header(token))

    assert response.status_code == 200
    names = [account["name"] for account in response.json()]
    assert names == ["My Checking"]


def test_fetching_another_users_account_returns_404_not_403(
    client: TestClient, db: Session, authenticated_user, other_user
):
    """404, deliberately -- not 403.

    A 403 would confirm the id exists. An attacker could then enumerate ids
    and learn how many accounts other people have, and when they were
    created. "Not yours" and "not there" must be indistinguishable.
    """
    _, token = authenticated_user
    stranger, _ = other_user

    their_account = make_account(db, stranger, name="Their Savings")
    db.flush()

    response = client.get(
        f"/api/accounts/{their_account.id}", headers=auth_header(token)
    )

    assert response.status_code == 404
    # And the response body must not hint that the record exists.
    assert "Their Savings" not in response.text


def test_fetching_a_nonexistent_account_returns_the_same_404(
    client: TestClient, authenticated_user
):
    """Proves the two cases really are indistinguishable."""
    _, token = authenticated_user

    response = client.get(
        f"/api/accounts/{uuid.uuid4()}", headers=auth_header(token)
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Account not found"}


def test_each_user_sees_their_own_profile(
    client: TestClient, authenticated_user, other_user
):
    user, token = authenticated_user
    stranger, stranger_token = other_user

    mine = client.get("/api/me", headers=auth_header(token)).json()
    theirs = client.get("/api/me", headers=auth_header(stranger_token)).json()

    assert mine["email"] == user.email
    assert theirs["email"] == stranger.email
    assert mine["id"] != theirs["id"]


def test_hidden_accounts_are_excluded_unless_requested(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user

    make_account(db, user, name="Visible")
    hidden = make_account(db, user, name="Hidden")
    hidden.is_hidden = True
    db.flush()

    default = client.get("/api/accounts", headers=auth_header(token)).json()
    with_hidden = client.get(
        "/api/accounts?include_hidden=true", headers=auth_header(token)
    ).json()

    assert [a["name"] for a in default] == ["Visible"]
    assert {a["name"] for a in with_hidden} == {"Visible", "Hidden"}


def test_account_response_hides_internal_identifiers(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    make_account(db, user)
    db.flush()

    [account] = client.get("/api/accounts", headers=auth_header(token)).json()

    assert "user_id" not in account
    assert "plaid_account_id" not in account
    assert "plaid_item_id" not in account


# ---------------------------------------------------------------------------
# The scoping helper itself
# ---------------------------------------------------------------------------


def test_scoped_select_filters_to_the_owner(
    db: Session, authenticated_user, other_user
):
    user, _ = authenticated_user
    stranger, _ = other_user

    mine = make_account(db, user, name="Mine")
    make_account(db, stranger, name="Theirs")
    db.flush()

    results = db.execute(scoped_select(Account, user)).scalars().all()

    assert [account.id for account in results] == [mine.id]


def test_scoped_get_refuses_another_users_row(
    db: Session, authenticated_user, other_user
):
    user, _ = authenticated_user
    stranger, _ = other_user

    theirs = make_account(db, stranger)
    db.flush()

    assert scoped_get(db, Account, theirs.id, user) is None


def test_scoping_an_unowned_model_raises_rather_than_returning_everything(
    db: Session, authenticated_user
):
    """Fail loudly on an unknown model.

    The dangerous alternative would be returning an unfiltered query for
    anything not in the list -- which would silently leak the moment someone
    adds a table and forgets to classify it. Institutions are genuinely
    shared, so they must be queried deliberately, not through the scoping
    helper.
    """
    user, _ = authenticated_user

    with pytest.raises(NotScopeable):
        scoped_select(Institution, user)

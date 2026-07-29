"""Authentication tests.

Each test here makes exactly one thing wrong with a token and proves the API
rejects it. That structure matters: a single "bad token is rejected" test
passes even if only one of the five checks is working, and you would never
know the other four had been silently disabled.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from tests.auth_helpers import (
    KeyPair,
    auth_header,
    generate_key_pair,
    make_token,
    make_unsigned_token,
)

# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_valid_token_is_accepted(client: TestClient, authenticated_user):
    user, token = authenticated_user

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 200
    assert response.json()["email"] == user.email


def test_response_never_exposes_the_clerk_id(client: TestClient, authenticated_user):
    """Response schemas are an allowlist. Internal identifiers stay internal."""
    _, token = authenticated_user

    body = client.get("/api/me", headers=auth_header(token)).json()

    assert "clerk_user_id" not in body
    assert "is_active" not in body
    assert "last_seen_at" not in body


# ---------------------------------------------------------------------------
# Rejection: one broken thing at a time
# ---------------------------------------------------------------------------


def test_request_without_a_token_is_rejected(client: TestClient):
    response = client.get("/api/me")

    assert response.status_code == 401
    # Required by the HTTP spec on a 401, and clients rely on it.
    assert response.headers["www-authenticate"] == "Bearer"


def test_garbage_token_is_rejected(client: TestClient):
    response = client.get("/api/me", headers=auth_header("not-a-jwt"))
    assert response.status_code == 401


def test_expired_token_is_rejected(client: TestClient, key_pair: KeyPair):
    """A leaked token must stop working. Clerk's are short-lived for exactly
    this reason."""
    token = make_token(key_pair, expires_in=-60, issued_ago=3600)

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 401


def test_token_from_a_different_issuer_is_rejected(client: TestClient, key_pair: KeyPair):
    """Stops a token minted by some other Clerk instance being replayed here."""
    token = make_token(key_pair, issuer="https://evil.clerk.accounts.dev")

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 401


def test_token_signed_with_the_wrong_key_is_rejected(client: TestClient):
    """The central guarantee.

    An attacker who knows the exact claim structure still cannot forge a
    token, because they do not hold Clerk's private key. This test signs a
    perfectly-formed token with a different key and proves it is refused.
    """
    attacker_key = generate_key_pair()
    token = make_token(attacker_key, subject="user_attacker")

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 401


def test_alg_none_token_is_rejected(client: TestClient):
    """The classic JWT vulnerability: declare `alg: none`, omit the signature.

    Libraries that trusted the header's algorithm accepted these. We pin the
    allowed algorithms to RS256, so it cannot work here.
    """
    response = client.get("/api/me", headers=auth_header(make_unsigned_token()))

    assert response.status_code == 401


def test_token_with_wrong_authorized_party_is_rejected(
    client: TestClient, key_pair: KeyPair
):
    """`azp` names the origin the token was minted for.

    Without this check, a token issued for a different application on the
    same Clerk instance could be replayed against this API.
    """
    token = make_token(key_pair, azp="https://some-other-app.example.com")

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 401


def test_token_without_a_subject_is_rejected(client: TestClient, key_pair: KeyPair):
    token = make_token(key_pair, omit_claims=("sub",))

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 401


def test_tampered_payload_is_rejected(client: TestClient, key_pair: KeyPair):
    """Editing the claims invalidates the signature.

    This is what "integrity, not secrecy" means in practice: anyone can READ
    the payload, but nobody can CHANGE it without the private key.
    """
    token = make_token(key_pair, subject="user_alice")
    header, payload, signature = token.split(".")

    # Swap in a different (valid base64) payload, keeping the old signature.
    forged_payload = make_token(key_pair, subject="user_bob").split(".")[1]
    forged = f"{header}.{forged_payload}.{signature}"

    response = client.get("/api/me", headers=auth_header(forged))

    assert response.status_code == 401


def test_error_response_does_not_leak_why_the_token_failed(
    client: TestClient, key_pair: KeyPair
):
    """Every failure looks identical to the client.

    Telling an attacker "expired" vs "bad signature" vs "wrong issuer" turns
    your API into a debugger for forging tokens. The specific reason is
    logged server-side, where it helps us and nobody else.
    """
    expired = client.get(
        "/api/me", headers=auth_header(make_token(key_pair, expires_in=-60))
    )
    wrong_key = client.get(
        "/api/me", headers=auth_header(make_token(generate_key_pair()))
    )

    assert expired.json() == wrong_key.json() == {"detail": "Not authenticated"}


# ---------------------------------------------------------------------------
# Just-in-time provisioning
# ---------------------------------------------------------------------------


def test_first_request_creates_the_local_user(
    client: TestClient, db: Session, key_pair: KeyPair
):
    """A user who exists in Clerk but not yet in our database is created on
    their first authenticated request -- no webhook required."""
    subject = f"user_{uuid.uuid4().hex[:16]}"
    token = make_token(
        key_pair, subject=subject, email="newcomer@example.com", first_name="New"
    )

    assert db.execute(
        select(User).where(User.clerk_user_id == subject)
    ).scalar_one_or_none() is None

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 200
    created = db.execute(
        select(User).where(User.clerk_user_id == subject)
    ).scalar_one()
    assert created.email == "newcomer@example.com"


def test_repeated_requests_do_not_create_duplicate_users(
    client: TestClient, db: Session, key_pair: KeyPair
):
    subject = f"user_{uuid.uuid4().hex[:16]}"
    token = make_token(key_pair, subject=subject, email="repeat@example.com")

    for _ in range(3):
        assert client.get("/api/me", headers=auth_header(token)).status_code == 200

    count = len(
        db.execute(select(User).where(User.clerk_user_id == subject)).scalars().all()
    )
    assert count == 1


def test_provisioning_without_an_email_claim_still_works(
    client: TestClient, db: Session, key_pair: KeyPair
):
    """Clerk's default JWT template does not include an email address.

    Users must still be able to sign in; we fill a placeholder and pick up
    the real address as soon as a token carries one.
    """
    subject = f"user_{uuid.uuid4().hex[:16]}"
    token = make_token(key_pair, subject=subject, email=None)

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 200
    created = db.execute(select(User).where(User.clerk_user_id == subject)).scalar_one()
    assert created.email.endswith("@placeholder.invalid")


def test_deactivated_user_is_forbidden_not_unauthorized(
    client: TestClient, db: Session, authenticated_user
):
    """403, not 401.

    The credentials were valid -- the account is simply not permitted. A 401
    would send the client into a pointless re-login loop that can never
    succeed.
    """
    user, token = authenticated_user
    user.is_active = False
    db.flush()

    response = client.get("/api/me", headers=auth_header(token))

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Updating your own profile
# ---------------------------------------------------------------------------


def test_user_can_update_their_own_preferences(client: TestClient, authenticated_user):
    _, token = authenticated_user

    response = client.patch(
        "/api/me",
        headers=auth_header(token),
        json={"timezone": "America/New_York"},
    )

    assert response.status_code == 200
    assert response.json()["timezone"] == "America/New_York"


def test_patch_does_not_wipe_unmentioned_fields(client: TestClient, authenticated_user):
    """The most common PATCH bug.

    Without `exclude_unset=True`, a request containing only `timezone` would
    null out `full_name`, because the model defaults every absent field to
    None.
    """
    _, token = authenticated_user

    response = client.patch(
        "/api/me", headers=auth_header(token), json={"timezone": "Europe/London"}
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Primary User"


@pytest.mark.parametrize("forbidden_field", ["email", "is_active", "clerk_user_id"])
def test_protected_fields_cannot_be_changed_via_the_api(
    client: TestClient, db: Session, authenticated_user, forbidden_field: str
):
    """Fields absent from the request schema are ignored, not applied.

    This is mass assignment protection. If the endpoint bound the request
    body straight onto the model, a user could POST `{"is_active": true}` to
    reactivate a suspended account -- or overwrite their Clerk id and
    hijack someone else's row.
    """
    user, token = authenticated_user
    original_email = user.email

    response = client.patch(
        "/api/me",
        headers=auth_header(token),
        json={forbidden_field: "attacker-controlled-value"},
    )

    assert response.status_code == 200
    db.refresh(user)
    assert user.email == original_email
    assert user.is_active is True
    assert user.clerk_user_id != "attacker-controlled-value"


def test_provisioned_user_is_actually_persisted(
    committing_client: TestClient, engine, key_pair: KeyPair
):
    """Provisioning must COMMIT, not merely flush.

    This test exists because the bug it catches is invisible to every other
    test in this file. `flush()` makes the new row visible inside the current
    transaction, so the request succeeds and returns a perfectly good user --
    but `get_db` closes the session without committing and the INSERT is
    rolled back. The user is then silently re-created, with a different id,
    on every single request.

    Catching it requires looking from OUTSIDE the request's transaction, which
    is what the separate session below does.
    """
    subject = f"persist_test_{uuid.uuid4().hex[:12]}"
    token = make_token(key_pair, subject=subject, email="persisted@example.com")

    response = committing_client.get("/api/me", headers=auth_header(token))
    assert response.status_code == 200
    returned_id = response.json()["id"]

    # A brand-new session on a different connection: it can only see data that
    # was genuinely committed.
    with Session(bind=engine) as independent_session:
        stored = independent_session.execute(
            select(User).where(User.clerk_user_id == subject)
        ).scalar_one_or_none()

    assert stored is not None, "user was returned by the API but never committed"
    assert str(stored.id) == returned_id
    assert stored.last_seen_at is not None


def test_repeat_requests_keep_the_same_user_id(
    committing_client: TestClient, key_pair: KeyPair
):
    """A stable id across requests is what makes foreign keys safe.

    If provisioning did not persist, every request would mint a new id and any
    account or transaction created against the previous one would be orphaned.
    """
    subject = f"persist_test_{uuid.uuid4().hex[:12]}"
    token = make_token(key_pair, subject=subject, email="stable@example.com")

    first = committing_client.get("/api/me", headers=auth_header(token)).json()
    second = committing_client.get("/api/me", headers=auth_header(token)).json()

    assert first["id"] == second["id"]

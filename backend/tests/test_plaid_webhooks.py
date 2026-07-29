"""Webhook verification tests.

The webhook endpoint is the only public, unauthenticated route that causes
this server to do work. Its signature check is the entire security boundary,
so every way of getting it wrong gets its own test.

As with the Clerk tests, we generate an EC key pair in-process and stub only
the key lookup. The signature verification, body hash comparison, and replay
window all execute for real.
"""

from __future__ import annotations

import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.services.plaid_gateway import PlaidApiError
from app.services.plaid_webhooks import (
    WebhookVerificationError,
    parse_webhook,
    verify_webhook,
)

TEST_KID = "plaid-webhook-key-1"


class _KeyServingGateway:
    """A gateway that serves one EC public key as a JWK."""

    def __init__(self, public_numbers, *, kid: str = TEST_KID) -> None:
        self._numbers = public_numbers
        self._kid = kid
        self.keys_requested: list[str] = []

    def get_webhook_verification_key(self, *, key_id: str) -> dict:
        self.keys_requested.append(key_id)
        if key_id != self._kid:
            raise PlaidApiError("KEY_NOT_FOUND", f"no key {key_id}")

        def b64(value: int) -> str:
            import base64

            raw = value.to_bytes(32, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        return {
            "kty": "EC",
            "crv": "P-256",
            "kid": self._kid,
            "use": "sig",
            "alg": "ES256",
            "x": b64(self._numbers.x),
            "y": b64(self._numbers.y),
        }


@pytest.fixture
def webhook_keys():
    private_key = ec.generate_private_key(ec.SECP256R1())
    gateway = _KeyServingGateway(private_key.public_key().public_numbers())
    return private_key, gateway


def sign(private_key, body: bytes, *, iat: int | None = None, kid: str = TEST_KID,
         body_hash: str | None = None) -> str:
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    claims = {
        "iat": iat if iat is not None else int(time.time()),
        "request_body_sha256": body_hash or hashlib.sha256(body).hexdigest(),
    }
    return jwt.encode(claims, pem, algorithm="ES256", headers={"kid": kid})


BODY = json.dumps({"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"}).encode()


# ---------------------------------------------------------------------------


def test_a_genuine_webhook_is_accepted(webhook_keys):
    private_key, gateway = webhook_keys

    verify_webhook(
        gateway=gateway,
        verification_header=sign(private_key, BODY),
        raw_body=BODY,
    )

    assert gateway.keys_requested == [TEST_KID]


def test_a_missing_header_is_rejected(webhook_keys):
    _, gateway = webhook_keys

    with pytest.raises(WebhookVerificationError, match="missing"):
        verify_webhook(gateway=gateway, verification_header=None, raw_body=BODY)


def test_a_forged_signature_is_rejected(webhook_keys):
    """Signed with a different key. This is the core guarantee."""
    _, gateway = webhook_keys
    attacker_key = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            gateway=gateway,
            verification_header=sign(attacker_key, BODY),
            raw_body=BODY,
        )


def test_a_tampered_body_is_rejected(webhook_keys):
    """A valid signature attached to a DIFFERENT body.

    Without the `request_body_sha256` check, an attacker who captured one
    legitimate webhook could reuse its signature with a payload of their
    choosing -- naming any item id they liked.
    """
    private_key, gateway = webhook_keys
    header = sign(private_key, BODY)

    tampered = json.dumps(
        {"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "someone-elses"}
    ).encode()

    with pytest.raises(WebhookVerificationError, match="body does not match"):
        verify_webhook(
            gateway=gateway, verification_header=header, raw_body=tampered
        )


def test_an_old_webhook_is_rejected_as_a_replay(webhook_keys):
    """Six minutes old, against a five-minute window.

    Without this, a captured webhook could be replayed indefinitely to force
    unlimited syncs and burn through the Plaid rate limit.
    """
    private_key, gateway = webhook_keys
    stale = sign(private_key, BODY, iat=int(time.time()) - 360)

    with pytest.raises(WebhookVerificationError, match="rejecting replay"):
        verify_webhook(gateway=gateway, verification_header=stale, raw_body=BODY)


def test_a_recent_webhook_is_accepted(webhook_keys):
    private_key, gateway = webhook_keys
    recent = sign(private_key, BODY, iat=int(time.time()) - 60)

    verify_webhook(gateway=gateway, verification_header=recent, raw_body=BODY)


def test_an_unknown_key_id_is_rejected(webhook_keys):
    private_key, gateway = webhook_keys

    with pytest.raises(WebhookVerificationError, match="could not fetch key"):
        verify_webhook(
            gateway=gateway,
            verification_header=sign(private_key, BODY, kid="unknown-key"),
            raw_body=BODY,
        )


def test_a_non_es256_algorithm_is_rejected(webhook_keys):
    """The same `alg` trap as in Milestone 3, in a different place.

    An HS256 token signed with the PUBLIC key would verify, if we let the
    token choose its own algorithm -- and the public key is, by definition,
    public.
    """
    _, gateway = webhook_keys
    forged = jwt.encode(
        {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(BODY).hexdigest()},
        "any-shared-secret",
        algorithm="HS256",
        headers={"kid": TEST_KID},
    )

    with pytest.raises(WebhookVerificationError, match="unexpected algorithm"):
        verify_webhook(gateway=gateway, verification_header=forged, raw_body=BODY)


def test_garbage_in_the_header_is_rejected(webhook_keys):
    _, gateway = webhook_keys

    with pytest.raises(WebhookVerificationError, match="malformed"):
        verify_webhook(
            gateway=gateway, verification_header="not-a-jwt", raw_body=BODY
        )


def test_whitespace_changes_to_the_body_are_detected(webhook_keys):
    """Why the endpoint must hash the RAW bytes.

    Parsing JSON and re-serializing it changes whitespace and key order. The
    hash would never match, and every webhook would be rejected -- a bug that
    looks like a signing problem and is actually a plumbing one.
    """
    private_key, gateway = webhook_keys
    header = sign(private_key, BODY)

    reserialized = json.dumps(json.loads(BODY), indent=2).encode()

    with pytest.raises(WebhookVerificationError, match="body does not match"):
        verify_webhook(
            gateway=gateway, verification_header=header, raw_body=reserialized
        )


# ---------------------------------------------------------------------------


def test_parse_webhook_extracts_the_fields_we_act_on():
    event = parse_webhook(
        {
            "webhook_type": "ITEM",
            "webhook_code": "ERROR",
            "item_id": "item_123",
            "error": {"error_code": "ITEM_LOGIN_REQUIRED"},
        }
    )

    assert event.webhook_type == "ITEM"
    assert event.item_id == "item_123"
    assert event.error_code == "ITEM_LOGIN_REQUIRED"


def test_parse_webhook_tolerates_a_missing_error_block():
    event = parse_webhook({"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"})
    assert event.error_code is None

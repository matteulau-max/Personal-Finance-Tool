"""Verifying that a webhook really came from Plaid.

===========================================================================
Why this is not optional
===========================================================================

A webhook endpoint is a URL on the public internet that causes your server to
do work. Anyone can POST to it. Without verification, a stranger can:

  * force unlimited syncs and exhaust your Plaid rate limit or bill;
  * feed you fabricated events;
  * replay a captured legitimate webhook indefinitely.

Plaid signs every webhook with an ES256 JWT in the `Plaid-Verification`
header. Verifying it is five steps, and skipping any one leaves a hole:

  1. Confirm the algorithm is ES256. (Never trust the token's own `alg` --
     the same "alg: none" trap as in Milestone 3.)
  2. Read `kid` and fetch the matching public key from
     /webhook_verification_key/get.
  3. Verify the JWT signature with that key.
  4. Compute SHA-256 of the RAW request body and compare it with the
     `request_body_sha256` claim. Without this, a valid signature could be
     attached to a body of the attacker's choosing.
  5. Reject anything whose `iat` is more than five minutes old, which is what
     stops a captured webhook being replayed forever.

Source: https://plaid.com/docs/api/webhooks/webhook-verification/
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import jwt
from jwt.algorithms import ECAlgorithm

from app.core.config import get_settings
from app.services.plaid_gateway import PlaidApiError, PlaidGateway

logger = logging.getLogger(__name__)

PLAID_WEBHOOK_ALGORITHM = "ES256"


class WebhookVerificationError(Exception):
    """Raised when a webhook cannot be proven to have come from Plaid."""


@dataclass(frozen=True)
class WebhookEvent:
    webhook_type: str
    webhook_code: str
    item_id: str | None
    error_code: str | None
    payload: dict


def verify_webhook(
    *,
    gateway: PlaidGateway,
    verification_header: str | None,
    raw_body: bytes,
) -> None:
    """Raise `WebhookVerificationError` unless this webhook is genuinely Plaid's.

    Takes the RAW body bytes, not parsed JSON. That matters: the hash must be
    computed over exactly the bytes Plaid signed. Re-serializing parsed JSON
    changes key order and whitespace, and the hash would never match.
    """
    if not verification_header:
        raise WebhookVerificationError("missing Plaid-Verification header")

    # --- Step 1: pin the algorithm -----------------------------------
    try:
        header = jwt.get_unverified_header(verification_header)
    except jwt.InvalidTokenError as exc:
        raise WebhookVerificationError(f"malformed verification JWT: {exc!r}") from exc

    if header.get("alg") != PLAID_WEBHOOK_ALGORITHM:
        raise WebhookVerificationError(f"unexpected algorithm {header.get('alg')!r}")

    key_id = header.get("kid")
    if not key_id:
        raise WebhookVerificationError("verification JWT has no key id")

    # --- Step 2: fetch the public key --------------------------------
    try:
        jwk = gateway.get_webhook_verification_key(key_id=key_id)
    except PlaidApiError as exc:
        raise WebhookVerificationError(f"could not fetch key {key_id}: {exc}") from exc

    try:
        public_key = ECAlgorithm.from_jwk(json.dumps(jwk))
    except Exception as exc:  # noqa: BLE001 - any malformed key is fatal
        raise WebhookVerificationError(f"unusable verification key: {exc!r}") from exc

    # --- Step 3: verify the signature --------------------------------
    try:
        claims = jwt.decode(
            verification_header,
            public_key,
            algorithms=[PLAID_WEBHOOK_ALGORITHM],
            options={
                "require": ["iat"],
                "verify_signature": True,
                # Plaid's verification JWT has no exp or aud claims.
                "verify_exp": False,
                "verify_aud": False,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise WebhookVerificationError(f"invalid verification JWT: {exc!r}") from exc

    # --- Step 5 (checked before the hash: it is cheaper) --------------
    settings = get_settings()
    age = time.time() - float(claims["iat"])
    if age > settings.PLAID_WEBHOOK_MAX_AGE_SECONDS:
        raise WebhookVerificationError(f"webhook is {int(age)}s old; rejecting replay")

    # --- Step 4: the body really is the body that was signed ---------
    expected = claims.get("request_body_sha256")
    if not expected:
        raise WebhookVerificationError("verification JWT has no body hash")

    actual = hashlib.sha256(raw_body).hexdigest()

    # `compare_digest` rather than `==`. A normal string comparison returns as
    # soon as two characters differ, so how long it takes leaks how much of
    # the prefix was correct. Over many attempts that is enough to reconstruct
    # the value. Constant-time comparison is the habit for every secret.
    if not hmac.compare_digest(actual, expected):
        raise WebhookVerificationError("request body does not match signed hash")


def parse_webhook(payload: dict) -> WebhookEvent:
    """Pull out the fields we act on."""
    return WebhookEvent(
        webhook_type=payload.get("webhook_type", ""),
        webhook_code=payload.get("webhook_code", ""),
        item_id=payload.get("item_id"),
        error_code=(payload.get("error") or {}).get("error_code"),
        payload=payload,
    )


# Webhook codes that mean "there is new transaction data waiting".
#
# SYNC_UPDATES_AVAILABLE is the modern one and the only one needed when using
# /transactions/sync. The others are legacy codes still sent to some
# integrations; treating them as sync triggers is harmless because syncing is
# idempotent -- an unnecessary sync costs one API call and changes nothing.
SYNC_TRIGGERING_CODES = {
    "SYNC_UPDATES_AVAILABLE",
    "DEFAULT_UPDATE",
    "INITIAL_UPDATE",
    "HISTORICAL_UPDATE",
    "TRANSACTIONS_REMOVED",
}

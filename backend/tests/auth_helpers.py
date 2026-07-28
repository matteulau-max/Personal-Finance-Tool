"""Helpers for minting test tokens.

===========================================================================
Why we generate our own signing key instead of calling Clerk
===========================================================================

A test suite must never depend on a third-party service. If it does:

  * it fails when their API is slow or down, and you learn to ignore red
    builds -- which is how real failures get missed;
  * it cannot run offline, or in CI without secrets;
  * it is slow, because every test makes network calls.

So we generate an RSA key pair inside the test process, sign tokens with the
private half, and stub the JWKS lookup to return the public half. Every
verification rule in `app.core.security` then runs for real -- signature,
expiry, issuer, algorithm, authorized party. The *only* thing replaced is
where the public key came from.

That is the general shape of good integration testing: substitute the
boundary, exercise everything inside it.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_ISSUER = "https://test-instance.clerk.accounts.dev"
TEST_AUTHORIZED_PARTY = "http://localhost:3000"
TEST_KID = "test-key-1"


@dataclass
class KeyPair:
    private_pem: str
    public_key: object


def generate_key_pair() -> KeyPair:
    """A 2048-bit RSA key pair, matching what Clerk uses for RS256."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    return KeyPair(private_pem=private_pem, public_key=private_key.public_key())


def make_token(
    key_pair: KeyPair,
    *,
    subject: str = "user_test_subject",
    issuer: str = TEST_ISSUER,
    expires_in: int = 3600,
    issued_ago: int = 0,
    azp: str | None = TEST_AUTHORIZED_PARTY,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    session_id: str | None = "sess_test",
    omit_claims: tuple[str, ...] = (),
) -> str:
    """Mint a token shaped like a real Clerk session token.

    Every parameter exists so a test can make exactly one thing wrong and
    prove that verification rejects it.
    """
    now = int(time.time())

    claims: dict = {
        "sub": subject,
        "iss": issuer,
        "iat": now - issued_ago,
        "nbf": now - issued_ago - 5,
        "exp": now + expires_in,
        "jti": "test-jti",
    }

    if azp is not None:
        claims["azp"] = azp
    if session_id is not None:
        claims["sid"] = session_id
    # Only present when the Clerk JWT template adds them.
    if email is not None:
        claims["email"] = email
    if first_name is not None:
        claims["first_name"] = first_name
    if last_name is not None:
        claims["last_name"] = last_name

    for claim in omit_claims:
        claims.pop(claim, None)

    return jwt.encode(
        claims,
        key_pair.private_pem,
        algorithm="RS256",
        headers={"kid": TEST_KID},
    )


def make_unsigned_token(subject: str = "user_attacker") -> str:
    """Forge an `alg: none` token -- the classic JWT attack.

    Early JWT libraries trusted the header's algorithm field. Set it to
    "none", drop the signature, and the library would happily accept whatever
    claims you wrote. Our verifier pins the allowed algorithms to RS256, so
    this must be rejected.

    Built by hand because PyJWT deliberately makes minting one awkward.
    """
    header = {"alg": "none", "typ": "JWT", "kid": TEST_KID}
    payload = {
        "sub": subject,
        "iss": TEST_ISSUER,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }

    def _b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    # Third segment (the signature) is intentionally empty.
    return f"{_b64(header)}.{_b64(payload)}."


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

"""Token verification.

===========================================================================
What a JWT actually is
===========================================================================

A JSON Web Token is three base64 chunks joined by dots:

    eyJhbGciOiJSUzI1NiIsImtpZCI6Imluc18xIn0 . eyJzdWIiOiJ1c2VyXzEyMyJ9 . MEUCIQ...
    └────────── header ──────────┘   └──── payload ────┘   └ signature ┘

The header says which algorithm and which key. The payload holds the claims
(who the user is, when the token expires). The signature is what makes it
trustworthy.

**The payload is not encrypted.** Anyone holding the token can read it --
paste one into jwt.io and you will see the claims in plain text. So a token
must never contain anything secret.

What a JWT gives you is *integrity*, not secrecy: Clerk signs the token with a
private key that only Clerk holds, and we verify it with the matching public
key. Change one byte of the payload and the signature no longer matches.

That is why this is worth doing: we can trust "this request is from user
X" without a database lookup or a call to Clerk on every request.

===========================================================================
The five things that must be checked
===========================================================================

Skipping any one of these turns authentication into decoration:

1. **Signature** -- against Clerk's public key. Without this anyone can mint
   any token they like.
2. **Algorithm** -- pinned to RS256. If you accept whatever the header asks
   for, an attacker sets `"alg": "none"` and supplies no signature at all.
   This was a real, widespread vulnerability in early JWT libraries. PyJWT
   requires an explicit algorithm list, which is why we pass one.
3. **Expiry** (`exp`) and not-before (`nbf`) -- a leaked token must stop
   working. Clerk session tokens are short-lived (about a minute) precisely
   to limit that window.
4. **Issuer** (`iss`) -- the token must come from *our* Clerk instance, not
   from some other instance an attacker controls.
5. **Authorized party** (`azp`) -- the origin the token was minted for. Stops
   a token issued for a different application on the same Clerk instance
   being replayed against this API.

Sources:
  https://clerk.com/docs/guides/sessions/manual-jwt-verification
  https://clerk.com/docs/guides/sessions/session-tokens
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Clerk signs session tokens with RS256. Pinning this list is security-
# critical -- see point 2 in the module docstring.
ALLOWED_ALGORITHMS = ["RS256"]


class AuthError(Exception):
    """Raised when a token cannot be trusted.

    Deliberately carries a *generic* message for the client and a detailed one
    for our logs. Telling an attacker whether a token was malformed, expired,
    or signed by the wrong key hands them a debugging tool for forging one.
    """

    def __init__(self, log_detail: str) -> None:
        super().__init__(log_detail)
        self.log_detail = log_detail


@dataclass(frozen=True)
class TokenClaims:
    """The parts of a verified Clerk session token that we actually use."""

    subject: str  # `sub` -- Clerk's stable user id, e.g. "user_2abc..."
    session_id: str | None  # `sid`
    email: str | None
    first_name: str | None
    last_name: str | None

    @property
    def full_name(self) -> str | None:
        parts = [part for part in (self.first_name, self.last_name) if part]
        return " ".join(parts) if parts else None


@lru_cache
def _jwks_client() -> PyJWKClient:
    """Fetches and caches Clerk's public keys.

    `PyJWKClient` caches keys in memory, so we are not making an HTTPS request
    on every single API call. It re-fetches when it sees an unknown key id,
    which is what makes Clerk's key rotation transparent to us.

    `lru_cache` keeps one client per process rather than rebuilding (and
    re-fetching) on every request.
    """
    settings = get_settings()
    return PyJWKClient(settings.jwks_url, cache_keys=True)


def verify_token(token: str) -> TokenClaims:
    """Verify a Clerk session token and return its claims.

    Raises `AuthError` if the token cannot be trusted, for any reason.
    """
    settings = get_settings()

    if not settings.auth_configured:
        # Refuse rather than wave the request through. A misconfigured
        # deployment must fail closed.
        raise AuthError("authentication is not configured (CLERK_ISSUER unset)")

    try:
        # Reads the `kid` from the token header and finds the matching public
        # key, fetching the JWKS if this key id has not been seen before.
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
    except Exception as exc:  # network failure, unknown key id, malformed token
        raise AuthError(f"could not resolve signing key: {exc!r}") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALLOWED_ALGORITHMS,
            issuer=settings.CLERK_ISSUER,
            leeway=settings.JWT_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                # Clerk session tokens carry no `aud` claim by default, so
                # audience checking is off. The `azp` check below is the
                # equivalent control, and it is the one Clerk documents.
                "verify_aud": False,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("token issuer does not match CLERK_ISSUER") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {exc!r}") from exc

    _verify_authorized_party(claims)

    subject = claims.get("sub")
    if not subject:
        raise AuthError("token has no subject claim")

    return TokenClaims(
        subject=subject,
        session_id=claims.get("sid"),
        # These are only present if you add them to Clerk's JWT template.
        # We treat them as optional so the app works with the default one.
        email=claims.get("email"),
        first_name=claims.get("first_name"),
        last_name=claims.get("last_name"),
    )


def _verify_authorized_party(claims: dict) -> None:
    """Check the `azp` claim against our allowlist.

    Clerk omits `azp` in some configurations (notably custom JWT templates),
    so an absent claim is not itself a failure -- but a *present* claim that
    does not match is, and loudly.
    """
    settings = get_settings()
    if not settings.CLERK_AUTHORIZED_PARTIES:
        return

    azp = claims.get("azp")
    if azp is None:
        return

    if azp not in settings.CLERK_AUTHORIZED_PARTIES:
        raise AuthError(f"unauthorized party: {azp!r}")

"""Deployment hardening tests.

Small checks on things that are invisible when they work: response headers,
and a configuration validator whose entire job is to stop a bad deploy.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptography.fernet import Fernet

from app.core.config import Settings


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------


def test_every_response_carries_the_security_headers(client: TestClient):
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_responses_are_not_cacheable(client: TestClient):
    """Every response here is somebody's financial data, and an intermediary
    is entitled to cache anything not told otherwise."""
    assert client.get("/health").headers["Cache-Control"] == "no-store"


def test_headers_are_present_on_errors_too(client: TestClient):
    """Middleware that only runs on the happy path is middleware that is
    absent exactly when a response is unusual."""
    response = client.get("/api/accounts")

    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_hsts_is_not_sent_locally(client: TestClient):
    """A development server sending HSTS teaches the browser to refuse plain
    HTTP to localhost -- for months, across every project on that port."""
    assert "Strict-Transport-Security" not in client.get("/health").headers


# ---------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------


def production(**overrides) -> dict:
    """A production configuration that is otherwise valid."""
    base = dict(
        ENVIRONMENT="production",
        DEBUG=False,
        CLERK_ISSUER="https://clerk.example.com",
        CLERK_AUTHORIZED_PARTIES=["https://app.example.com"],
        # A real one. A plausible-looking string would now be rejected --
        # which is the point of the validator this fixture would otherwise
        # be quietly working around.
        ENCRYPTION_KEYS=[Fernet.generate_key().decode()],
        PLAID_ENV="production",
        PLAID_WEBHOOK_URL="https://api.example.com/api/plaid/webhook",
        ALLOWED_HOSTS=["api.example.com"],
        FRONTEND_ORIGIN="https://app.example.com",
    )
    base.update(overrides)
    return base


def test_a_valid_production_configuration_is_accepted():
    """Guards the tests below: if this failed, every one of them would pass
    for the wrong reason."""
    assert Settings(**production()).is_production


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"ALLOWED_HOSTS": []}, "ALLOWED_HOSTS"),
        ({"FRONTEND_ORIGIN": "http://app.example.com"}, "FRONTEND_ORIGIN"),
        ({"DEBUG": True}, "DEBUG"),
        ({"ENCRYPTION_KEYS": []}, "ENCRYPTION_KEYS"),
        ({"CLERK_ISSUER": ""}, "CLERK_ISSUER"),
        ({"PLAID_ENV": "sandbox"}, "PLAID_ENV"),
    ],
)
def test_production_refuses_to_start_when_misconfigured(override, expected):
    """Fail closed.

    An application that starts happily with a security control missing is one
    bad deploy from serving financial data to the internet. Refusing to boot
    turns that into an outage, which somebody notices immediately.
    """
    with pytest.raises(ValueError, match=expected):
        Settings(**production(**override))


def test_a_placeholder_encryption_key_is_refused_everywhere():
    """The one that following the setup instructions produced.

    `cp .env.example .env` leaves this literal string in place. Every check we
    had passed it -- the list is non-empty -- and the failure surfaced only
    when a Plaid access token was first encrypted, which is the moment after
    somebody enters their bank credentials.
    """
    with pytest.raises(ValueError, match="not a usable Fernet key"):
        Settings(ENCRYPTION_KEYS=["replace-with-a-generated-fernet-key"])


def test_no_encryption_key_at_all_is_still_allowed_locally():
    """Off is a legitimate state -- CI, and a first look at the app. Only a
    key that is set and wrong is always a mistake."""
    assert Settings(ENCRYPTION_KEYS=[]).ENCRYPTION_KEYS == []


def test_local_development_needs_none_of_it():
    """The strictness applies to production only. Requiring HTTPS and a host
    allowlist to run the app on a laptop is how people end up developing with
    ENVIRONMENT unset and discovering the checks at deploy time."""
    assert Settings(ENVIRONMENT="local").is_production is False

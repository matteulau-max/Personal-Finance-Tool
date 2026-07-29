"""Rate limiting tests.

The bucket arithmetic is tested with a fake clock rather than by sleeping.
A test suite that sleeps to prove a refill works is a test suite people stop
running -- and "slow tests get skipped" is how a limiter ends up untested.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.rate_limit import RateLimiter
from tests.auth_helpers import auth_header


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------------------


def test_requests_are_allowed_up_to_the_capacity():
    limiter = RateLimiter(3, 60, clock=FakeClock())

    assert [limiter.consume("k").allowed for _ in range(4)] == [True, True, True, False]


def test_each_key_gets_its_own_allowance():
    """One user exhausting theirs must not affect anybody else -- which is
    the failure mode of a limiter keyed too broadly."""
    limiter = RateLimiter(1, 60, clock=FakeClock())

    assert limiter.consume("alice").allowed is True
    assert limiter.consume("alice").allowed is False
    assert limiter.consume("bob").allowed is True


def test_tokens_refill_gradually_rather_than_all_at_once():
    """The reason this is a token bucket and not a counter.

    A counter reset hourly lets someone spend the whole allowance at 10:59
    and the whole of the next one at 11:00 -- double the limit, back to back.
    Continuous refill has no such edge.
    """
    clock = FakeClock()
    limiter = RateLimiter(60, 60, clock=clock)  # one token per second

    for _ in range(60):
        limiter.consume("k")
    assert limiter.consume("k").allowed is False

    clock.advance(10)

    assert [limiter.consume("k").allowed for _ in range(11)] == [True] * 10 + [False]


def test_the_bucket_never_fills_past_capacity():
    """Otherwise an idle week would bank a week's worth of requests, and the
    limit would mean nothing on the day it mattered."""
    clock = FakeClock()
    limiter = RateLimiter(5, 60, clock=clock)

    clock.advance(86_400)

    assert [limiter.consume("k").allowed for _ in range(6)] == [True] * 5 + [False]


def test_retry_after_is_never_zero():
    """A client told to retry in zero seconds retries immediately, gets
    another 429, and the loop the limit exists to stop continues."""
    limiter = RateLimiter(1, 3600, clock=FakeClock())
    limiter.consume("k")

    decision = limiter.consume("k")

    assert decision.allowed is False
    assert decision.retry_after >= 1


def test_concurrent_consumers_cannot_overspend():
    """The read-modify-write in `consume` runs under a lock.

    Uvicorn hands sync endpoints to a thread pool, so simultaneous requests
    are real. Without the lock the update is lost and the limit leaks under
    exactly the burst it exists to control -- and, being a race, it would pass
    a single-threaded test every time.
    """
    limiter = RateLimiter(50, 3600)
    allowed: list[bool] = []
    guard = threading.Lock()

    def worker():
        for _ in range(20):
            decision = limiter.consume("shared")
            with guard:
                allowed.append(decision.allowed)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(allowed) == 50


def test_full_buckets_are_forgotten_once_the_map_is_large():
    """An IP-keyed limiter is a dictionary an attacker can grow. Buckets that
    have refilled completely are indistinguishable from ones that never
    existed, so dropping them is free."""
    clock = FakeClock()
    limiter = RateLimiter(5, 60, clock=clock)

    for index in range(1100):
        limiter.consume(f"key-{index}")
    assert len(limiter._buckets) > 1000

    clock.advance(120)
    limiter.consume("trigger-the-prune")

    assert len(limiter._buckets) == 1


# ---------------------------------------------------------------------------
# Wired into the API
# ---------------------------------------------------------------------------


def test_the_insights_endpoint_stops_answering_once_the_budget_is_spent(
    client: TestClient, authenticated_user
):
    """The endpoint this module was written for.

    Every call here costs money at Anthropic, so the limit has to reject the
    request *before* the model is invoked -- which is why it is a dependency
    and not a check inside the handler.
    """
    _, token = authenticated_user
    rate_limit.INSIGHTS.reset()

    statuses = [
        client.post(
            "/api/insights", json={"question": "What did I spend?"}, headers=auth_header(token)
        ).status_code
        for _ in range(rate_limit.INSIGHTS.capacity + 1)
    ]

    # 503 because the AI key is not configured in tests -- which is the point:
    # the request never reached a provider either way. What matters is that
    # the last one was refused for a different reason.
    assert statuses[-1] == 429
    assert set(statuses[:-1]) == {503}


def test_a_429_tells_the_client_when_to_come_back(client: TestClient, authenticated_user):
    _, token = authenticated_user

    for _ in range(rate_limit.INSIGHTS.capacity):
        client.post("/api/insights", json={"question": "hi"}, headers=auth_header(token))
    response = client.post(
        "/api/insights", json={"question": "hi"}, headers=auth_header(token)
    )

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_one_users_limit_does_not_affect_another(
    client: TestClient, authenticated_user, other_user
):
    """Keyed by user id, not by IP.

    In the test client both users share an address, so a limiter keyed by IP
    would fail this -- and in production it would mean one person's runaway
    script locking out everyone in their office.
    """
    _, token = authenticated_user
    _, other_token = other_user

    for _ in range(rate_limit.INSIGHTS.capacity):
        client.post("/api/insights", json={"question": "hi"}, headers=auth_header(token))

    response = client.post(
        "/api/insights", json={"question": "hi"}, headers=auth_header(other_token)
    )

    assert response.status_code != 429


def test_the_public_webhook_is_limited_by_address(client: TestClient, monkeypatch):
    """The one endpoint with no user to key on.

    Unverified webhooks are rejected with a 401 before any work happens, but
    a signature check is not free, and this endpoint is reachable by anyone
    who knows the URL.
    """
    from app.main import app
    from app.services.plaid_gateway import get_plaid_gateway
    from tests.fake_plaid import FakePlaidGateway

    app.dependency_overrides[get_plaid_gateway] = lambda: FakePlaidGateway()

    # The real limit is 240/minute -- four tokens a second, so issuing 241
    # requests over real time never empties the bucket. Shrinking the
    # allowance for the duration of the test keeps it about the wiring (is
    # this endpoint metered, and is it keyed by address?) rather than about
    # how fast the test machine is.
    monkeypatch.setattr(rate_limit.WEBHOOK, "capacity", 3)
    monkeypatch.setattr(rate_limit.WEBHOOK, "per_seconds", 3600.0)
    monkeypatch.setattr(rate_limit.WEBHOOK, "_refill_rate", 3 / 3600)
    rate_limit.WEBHOOK.reset()

    try:
        statuses = [
            client.post("/api/plaid/webhook", json={"webhook_type": "ITEM"}).status_code
            for _ in range(4)
        ]
    finally:
        app.dependency_overrides.pop(get_plaid_gateway, None)

    assert statuses[-1] == 429
    assert set(statuses[:-1]) == {401}

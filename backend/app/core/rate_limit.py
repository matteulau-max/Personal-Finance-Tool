"""Rate limiting.

===========================================================================
What this protects against, and what it does not
===========================================================================

Three different problems get called "rate limiting", and conflating them
produces a limiter that solves none of them:

  1. **Cost.** `/api/insights` calls Claude. A loop in someone's script --
     theirs or an attacker's -- turns into a bill. This is the one endpoint
     in the application where a request spends real money, and it is the
     reason this module exists.
  2. **Load.** A sync pulls transactions from Plaid and writes to the
     database. Fifty concurrent syncs from one user is a denial of service
     against every other user.
  3. **Abuse of the unauthenticated surface.** The Plaid webhook has no
     login, so it needs a limit keyed by something other than a user.

Volumetric network attacks are *not* on that list. A limiter running inside
the application still accepts the connection, parses the request, and
allocates memory for it. Stopping a flood is the job of the layer in front --
Cloudflare, an ALB, nginx. This module stops one client from being expensive,
which is a different and more tractable problem.

===========================================================================
The honest limitation
===========================================================================

State lives in this process's memory. Run four uvicorn workers and each keeps
its own counters, so a limit of 20/hour is really up to 80/hour depending on
which worker answers. That is a real weakness and it is written down here
rather than discovered in a bill.

It is still worth having: 80/hour is bounded, and unbounded is the actual
danger. The upgrade path is a shared counter in Redis, and the interface
below is deliberately narrow enough that swapping the storage does not touch
any endpoint -- `consume()` is the only method anything calls.

===========================================================================
Why a token bucket
===========================================================================

The obvious implementation is a counter reset every hour. It has a nasty
edge: a user who spends their whole allowance at 10:59 gets a fresh one at
11:00, so the real peak is twice the limit, back to back.

A token bucket refills continuously -- a 60/hour limit hands back one token a
minute -- so the long-run average is the limit and short bursts are still
allowed up to the bucket's capacity. It is also cheap: two floats per key and
no background timer.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import CurrentUser
from app.models import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A token bucket per key, held in memory.

    `capacity` is how much can be spent in one burst; `per_seconds` is how
    long a full bucket takes to refill from empty. A limit of "20 per hour" is
    `RateLimiter(20, 3600)`.
    """

    def __init__(
        self,
        capacity: int,
        per_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self.per_seconds = per_seconds
        self._refill_rate = capacity / per_seconds
        # monotonic, not wall clock: an NTP correction that steps the system
        # clock backwards would otherwise hand out free tokens, and one that
        # steps it forwards would lock people out.
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        # Uvicorn runs sync endpoints in a thread pool, so two requests really
        # can land here at once. Without the lock the read-modify-write below
        # loses updates, and the limit leaks under exactly the concurrency it
        # exists to control.
        self._lock = threading.Lock()

    def consume(self, key: str, cost: float = 1.0) -> Decision:
        now = self._clock()

        with self._lock:
            self._prune(now)
            bucket = self._buckets.get(key)

            if bucket is None:
                bucket = _Bucket(tokens=float(self.capacity), updated=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated)
                bucket.tokens = min(
                    float(self.capacity), bucket.tokens + elapsed * self._refill_rate
                )
                bucket.updated = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return Decision(True, int(bucket.tokens), 0)

            shortfall = cost - bucket.tokens
            # Round up: telling a client to retry in 0 seconds sends it
            # straight back into another rejection.
            retry_after = max(1, int(shortfall / self._refill_rate) + 1)
            return Decision(False, 0, retry_after)

    def _prune(self, now: float) -> None:
        """Forget buckets that have refilled completely.

        Without this the dictionary grows one entry per key forever, which for
        an IP-keyed limiter is a memory leak an attacker controls. A bucket at
        full capacity is indistinguishable from one that never existed, so
        dropping it changes no behaviour.

        Only worth doing when the map is large enough for the scan to be worth
        the lock time.
        """
        if len(self._buckets) < 1024:
            return

        full_after = self.per_seconds
        self._buckets = {
            key: bucket
            for key, bucket in self._buckets.items()
            if now - bucket.updated < full_after
        }

    def reset(self) -> None:
        """Drop all state. For tests, and for nothing else."""
        with self._lock:
            self._buckets.clear()


# ---------------------------------------------------------------------------
# The configured limits
#
# Each is a named module-level limiter so that its state survives between
# requests -- a limiter constructed inside a dependency would be brand new
# every time, which is a rate limiter that permits everything. That mistake
# is easy to make and produces no error, so the limiters live here where
# there is one of each.
# ---------------------------------------------------------------------------

# Insights cost money per call. Generous enough that a person exploring their
# spending never notices; tight enough that a runaway loop costs cents.
INSIGHTS = RateLimiter(capacity=20, per_seconds=3600)

# Syncing hits Plaid and writes to the database. Plaid data updates a few
# times a day, so a manual sync more than once a minute cannot return
# anything new -- this limit costs the user nothing real.
SYNC = RateLimiter(capacity=10, per_seconds=600)

# Link tokens are cheap but not free, and a loop creating them is either a
# bug or someone else's problem being made ours.
PLAID_LINK = RateLimiter(capacity=30, per_seconds=3600)

# The webhook is public, so this one is keyed by IP rather than by user.
# Plaid's real traffic is nowhere near this; the number is chosen to be
# invisible to them and finite to everyone else.
WEBHOOK = RateLimiter(capacity=240, per_seconds=60)

ALL_LIMITERS = (INSIGHTS, SYNC, PLAID_LINK, WEBHOOK)


def _reject(name: str, decision: Decision, key: str) -> HTTPException:
    logger.warning("Rate limit %s exceeded for %s", name, key)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please slow down.",
        # `Retry-After` is the part clients can act on. A 429 without it
        # invites the naive retry loop that caused the 429.
        headers={"Retry-After": str(decision.retry_after)},
    )


def limit_by_user(name: str, limiter: RateLimiter) -> Callable:
    """Build a dependency that limits by authenticated user.

    Keyed by user id, never by IP: users share IPs (offices, carriers, a
    household) and a per-IP limit would let one person's runaway script lock
    out their colleagues.
    """

    def dependency(current_user: CurrentUser) -> User:
        decision = limiter.consume(f"{name}:{current_user.id}")
        if not decision.allowed:
            raise _reject(name, decision, str(current_user.id))
        return current_user

    return dependency


def limit_by_ip(name: str, limiter: RateLimiter) -> Callable:
    """Build a dependency for endpoints with no user to key on.

    `request.client.host` is the peer address, which behind a load balancer is
    the load balancer. Getting the real client address means trusting a
    forwarded header, and a header can be forged by anyone who can reach the
    application directly -- so the proxy has to be configured to overwrite it,
    and the application has to be unreachable except through the proxy. Until
    both are true, trusting `X-Forwarded-For` would turn this limiter into a
    way to lock out any address of the attacker's choosing.

    So: peer address, and a note in the deployment guide.
    """

    def dependency(request: Request) -> None:
        key = f"{name}:{request.client.host if request.client else 'unknown'}"
        decision = limiter.consume(key)
        if not decision.allowed:
            raise _reject(name, decision, key)

    return dependency


# Ready-made dependencies. `RateLimitedUser` doubles as the authentication
# dependency, so an endpoint swaps `CurrentUser` for it and gets both --
# rather than declaring two things and being one edit away from having the
# limit silently drop off.
RateLimitedInsightsUser = Annotated[
    User, Depends(limit_by_user("insights", INSIGHTS))
]
RateLimitedSyncUser = Annotated[User, Depends(limit_by_user("sync", SYNC))]
RateLimitedLinkUser = Annotated[User, Depends(limit_by_user("plaid_link", PLAID_LINK))]
WebhookRateLimit = Depends(limit_by_ip("webhook", WEBHOOK))

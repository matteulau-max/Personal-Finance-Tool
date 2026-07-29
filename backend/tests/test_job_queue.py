"""Webhook queue tests.

The queue exists so that a slow sync cannot make Plaid's webhook time out.
These tests drive it directly -- claim, run, fail, retry -- because the
interesting behaviour is what happens when something goes wrong, and a
polling worker running alongside would make those assertions racy.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Institution,
    JobStatus,
    PlaidItem,
    PlaidItemStatus,
    Transaction,
    User,
    WebhookJob,
)
from app.core.crypto import encrypt
from app.services import job_queue
from app.services.plaid_webhooks import WebhookEvent
from tests.fake_plaid import FakePlaidGateway, make_txn, page

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def item(db: Session, user: User) -> PlaidItem:
    institution = Institution(
        plaid_institution_id=f"ins_{uuid.uuid4().hex[:8]}", name="Test Bank"
    )
    db.add(institution)
    db.flush()

    item = PlaidItem(
        user_id=user.id,
        institution_id=institution.id,
        plaid_item_id=f"item_{uuid.uuid4().hex[:12]}",
        access_token_encrypted=encrypt("access-sandbox-fake"),
    )
    db.add(item)
    db.flush()
    return item


def event(item: PlaidItem, *, code: str = "DEFAULT_UPDATE", error: str | None = None):
    return WebhookEvent(
        webhook_type="TRANSACTIONS" if error is None else "ITEM",
        webhook_code=code,
        item_id=item.plaid_item_id,
        error_code=error,
        payload={},
    )


# ---------------------------------------------------------------------------
# Enqueueing
# ---------------------------------------------------------------------------


def test_a_webhook_becomes_a_pending_job(db: Session, item: PlaidItem):
    job = job_queue.enqueue_webhook(db, event(item), now=NOW)

    assert job is not None
    assert job.status == JobStatus.PENDING
    assert job.plaid_item_id == item.id
    assert job.attempts == 0


def test_a_repeated_webhook_does_not_queue_a_second_job(db: Session, item: PlaidItem):
    """Plaid resends what it thinks we missed, and a bank posting a batch can
    emit several identical updates. Each becoming its own sync would be the
    same expensive work several times over."""
    first = job_queue.enqueue_webhook(db, event(item), now=NOW)
    second = job_queue.enqueue_webhook(db, event(item), now=NOW)

    assert first is not None
    assert second is None
    assert db.execute(select(WebhookJob)).scalars().all() == [first]


def test_a_new_job_is_queued_once_the_previous_one_finished(db: Session, item: PlaidItem):
    """The collapse must not be permanent.

    Only PENDING jobs are deduplicated. If a completed job still blocked new
    ones, the second day's transactions would never sync -- a bug that would
    look like the feature working, once.
    """
    first = job_queue.enqueue_webhook(db, event(item), now=NOW)
    first.status = JobStatus.SUCCEEDED
    db.flush()

    second = job_queue.enqueue_webhook(db, event(item), now=NOW)

    assert second is not None
    assert second.id != first.id


def test_a_webhook_for_an_unknown_item_is_recorded_rather_than_dropped(db: Session):
    """"We received it and ignored it" is a different diagnosis from "it never
    arrived", and only one of them is answerable at 2am."""
    unknown = WebhookEvent(
        webhook_type="TRANSACTIONS",
        webhook_code="DEFAULT_UPDATE",
        item_id="item_not_ours",
        error_code=None,
        payload={},
    )

    job = job_queue.enqueue_webhook(db, unknown, now=NOW)

    assert job is not None
    assert job.plaid_item_id is None
    assert job.plaid_item_external_id == "item_not_ours"


# ---------------------------------------------------------------------------
# Claiming and running
# ---------------------------------------------------------------------------


def test_claiming_marks_the_job_running_and_counts_the_attempt(db: Session, item: PlaidItem):
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()

    claimed = job_queue.claim_due_job(db, now=NOW)

    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.started_at == NOW


def test_a_job_scheduled_for_later_is_not_claimed(db: Session, item: PlaidItem):
    job = job_queue.enqueue_webhook(db, event(item), now=NOW)
    job.next_attempt_at = NOW + timedelta(minutes=5)
    db.commit()

    assert job_queue.claim_due_job(db, now=NOW) is None
    assert job_queue.claim_due_job(db, now=NOW + timedelta(minutes=6)) is not None


def test_running_a_transactions_job_actually_syncs(db: Session, item: PlaidItem):
    """The end-to-end point of the queue: the same work as before, later."""
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_queued")])])
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()

    claimed = job_queue.claim_due_job(db, now=NOW)
    assert job_queue.run_job(db, gateway, claimed, now=NOW) is True

    assert claimed.status == JobStatus.SUCCEEDED
    synced = db.execute(
        select(Transaction).where(Transaction.plaid_transaction_id == "txn_queued")
    ).scalar_one_or_none()
    assert synced is not None


def test_an_item_error_webhook_updates_the_connection(db: Session, item: PlaidItem):
    gateway = FakePlaidGateway()
    job_queue.enqueue_webhook(db, event(item, code="ERROR", error="ITEM_LOGIN_REQUIRED"), now=NOW)
    db.commit()

    job_queue.run_job(db, gateway, job_queue.claim_due_job(db, now=NOW), now=NOW)

    db.refresh(item)
    assert item.status == PlaidItemStatus.LOGIN_REQUIRED
    assert item.error_code == "ITEM_LOGIN_REQUIRED"


def test_a_job_for_a_disconnected_item_succeeds_without_syncing(db: Session, item: PlaidItem):
    """Plaid can send a trailing webhook after a user disconnects. Treating
    that as a failure would retry it five times and then log an error about
    something the user asked for."""
    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_late")])])
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    item.status = PlaidItemStatus.DISCONNECTED
    db.commit()

    claimed = job_queue.claim_due_job(db, now=NOW)

    assert job_queue.run_job(db, gateway, claimed, now=NOW) is True
    assert gateway.sync_call_count == 0


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


class ExplodingGateway(FakePlaidGateway):
    """A Plaid that is having an outage.

    Note it fails inside `sync_transactions` rather than by making
    `sync_item` raise: the sync engine catches everything and records the
    failure instead of propagating it, so this is what a real outage looks
    like from the queue's side.
    """

    def sync_transactions(self, *args, **kwargs):
        raise RuntimeError("Plaid is having a day")


def test_a_failure_reschedules_with_backoff_rather_than_dying(db: Session, item: PlaidItem):
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()
    claimed = job_queue.claim_due_job(db, now=NOW)

    assert job_queue.run_job(db, ExplodingGateway(), claimed, now=NOW) is False

    assert claimed.status == JobStatus.PENDING
    assert claimed.next_attempt_at == NOW + job_queue.BACKOFF_BASE
    assert "sync failed" in claimed.last_error


def test_backoff_grows_with_each_attempt(db: Session, item: PlaidItem):
    """A queue that retries a broken item every second is a queue that never
    gets to anyone else's work."""
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()

    delays = []
    at = NOW
    for _ in range(3):
        claimed = job_queue.claim_due_job(db, now=at)
        job_queue.run_job(db, ExplodingGateway(), claimed, now=at)
        delays.append(claimed.next_attempt_at - at)
        at = claimed.next_attempt_at

    assert delays == [
        job_queue.BACKOFF_BASE,
        job_queue.BACKOFF_BASE * 2,
        job_queue.BACKOFF_BASE * 4,
    ]


def test_a_job_gives_up_eventually(db: Session, item: PlaidItem):
    """Retrying forever turns one broken connection into a queue that never
    drains for anybody."""
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()

    at = NOW
    for _ in range(job_queue.MAX_ATTEMPTS):
        claimed = job_queue.claim_due_job(db, now=at)
        assert claimed is not None
        job_queue.run_job(db, ExplodingGateway(), claimed, now=at)
        at = claimed.next_attempt_at + timedelta(seconds=1)

    assert claimed.status == JobStatus.FAILED
    assert job_queue.claim_due_job(db, now=at + timedelta(days=1)) is None


def test_a_failing_job_does_not_block_the_next_one(db: Session, item: PlaidItem, user: User):
    """Containment. One item's outage must not stop everyone else's sync."""
    other = PlaidItem(
        user_id=user.id,
        plaid_item_id=f"item_{uuid.uuid4().hex[:12]}",
        access_token_encrypted=encrypt("access-sandbox-fake"),
    )
    db.add(other)
    db.flush()

    job_queue.enqueue_webhook(db, event(item), now=NOW)
    job_queue.enqueue_webhook(db, event(other), now=NOW)
    db.commit()

    succeeded, failed = job_queue.drain(db, ExplodingGateway(), now=NOW)

    assert (succeeded, failed) == (0, 2)


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def test_a_job_abandoned_by_a_dead_worker_is_reclaimed(db: Session, item: PlaidItem):
    """The deploy-during-a-sync case.

    Without this the row sits in RUNNING forever: no worker will touch it,
    and the user's transactions simply never arrive. There is no error
    anywhere -- which is what makes it worth a test.
    """
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()
    claimed = job_queue.claim_due_job(db, now=NOW)

    later = NOW + job_queue.STUCK_AFTER + timedelta(minutes=1)
    assert job_queue.reclaim_stuck_jobs(db, now=later) == 1

    db.refresh(claimed)
    assert claimed.status == JobStatus.PENDING
    # The attempt still counts: a job that reliably kills its worker must
    # exhaust its retries rather than loop forever.
    assert claimed.attempts == 1


def test_a_job_still_running_is_left_alone(db: Session, item: PlaidItem):
    """Stealing a job from a live worker would run the same sync twice."""
    job_queue.enqueue_webhook(db, event(item), now=NOW)
    db.commit()
    job_queue.claim_due_job(db, now=NOW)

    assert job_queue.reclaim_stuck_jobs(db, now=NOW + timedelta(minutes=5)) == 0


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_the_webhook_endpoint_queues_instead_of_syncing(
    client, db: Session, item: PlaidItem, monkeypatch
):
    """What the endpoint must NOT do any more.

    Before the queue, this request ran a full sync inline -- inside Plaid's
    timeout. The assertion that matters is that the gateway was never asked
    to sync while the request was open.

    Signature verification is stubbed out here and only here; it has its own
    file of tests, and repeating a signed-JWT setup would make this test about
    cryptography rather than about what the handler does with a webhook it has
    already accepted.
    """
    from app.main import app
    from app.services.plaid_gateway import get_plaid_gateway
    from app.api.routes import plaid as plaid_routes

    gateway = FakePlaidGateway(pages=[page(added=[make_txn("txn_not_yet")])])
    app.dependency_overrides[get_plaid_gateway] = lambda: gateway
    monkeypatch.setattr(plaid_routes, "verify_webhook", lambda **kwargs: None)

    try:
        response = client.post(
            "/api/plaid/webhook",
            json={
                "webhook_type": "TRANSACTIONS",
                "webhook_code": "DEFAULT_UPDATE",
                "item_id": item.plaid_item_id,
            },
            headers={"Plaid-Verification": "stub"},
        )
    finally:
        app.dependency_overrides.pop(get_plaid_gateway, None)

    assert response.status_code == 200
    assert gateway.sync_call_count == 0

    queued = db.execute(select(WebhookJob)).scalars().all()
    assert len(queued) == 1
    assert queued[0].status == JobStatus.PENDING
    assert queued[0].plaid_item_id == item.id

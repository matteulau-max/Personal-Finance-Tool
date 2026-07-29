"""The webhook job queue: enqueue, claim, run, retry.

===========================================================================
The problem this solves
===========================================================================

Until now the Plaid webhook did the sync inline. That works with one user and
fails in a specific, unpleasant way as soon as it does not:

    Plaid POSTs the webhook  ->  we sync 90 days of transactions  ->  30s
                             ->  Plaid times out and retries
                             ->  we start a *second* sync of the same item
                             ->  both are still running when the third arrives

Nothing corrupts -- the sync engine is idempotent, which is what has made
this survivable so far -- but the database is doing the same expensive work
three times while Plaid concludes our endpoint is broken.

The fix is to make the webhook handler do the least possible work: write down
what happened, commit, return 200. Plaid is satisfied in milliseconds. The
sync happens afterwards, on a worker, where taking thirty seconds is fine.

===========================================================================
Claiming without collisions
===========================================================================

Two workers polling the same table will both find the same oldest job.
`SELECT ... FOR UPDATE SKIP LOCKED` is PostgreSQL's answer: the first
transaction locks the row, and the second *skips past it* rather than
blocking, and takes the next one. No leader election, no distributed lock, no
duplicated work.

`SKIP LOCKED` is the whole reason a database table is a credible queue rather
than a naive one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    JobStatus,
    PlaidItem,
    PlaidItemStatus,
    SyncStatus,
    SyncTrigger,
    WebhookJob,
)
from app.services.plaid_gateway import PlaidGateway
from app.services.plaid_webhooks import SYNC_TRIGGERING_CODES, WebhookEvent
from app.services.sync import SyncEngine

logger = logging.getLogger(__name__)


class SyncFailed(RuntimeError):
    """A sync reported failure. Raised so the job retries with backoff."""


# After this many attempts a job is left FAILED and stops consuming workers.
#
# Retrying forever is the failure mode that turns one broken item into a
# queue that never drains. Five attempts with the backoff below spans a bit
# over an hour, which covers a Plaid outage; past that the problem is not
# going to fix itself and wants a human.
MAX_ATTEMPTS = 5

# Exponential: 1, 2, 4, 8, 16 minutes.
BACKOFF_BASE = timedelta(minutes=1)

# A job RUNNING for longer than this is assumed to belong to a worker that
# died -- a deploy in the middle of a sync, an OOM kill. Long enough that a
# genuinely slow sync is never stolen from a live worker.
STUCK_AFTER = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Producer side
# ---------------------------------------------------------------------------


def enqueue_webhook(
    db: Session, event: WebhookEvent, *, now: datetime | None = None
) -> WebhookJob | None:
    """Record a webhook for later processing.

    Returns None when an identical job is already waiting. That is not an
    error: Plaid resends webhooks it thinks we missed, and a bank that has
    just posted a batch can emit several DEFAULT_UPDATEs in a row. Collapsing
    them means one sync instead of five, and the partial unique index makes
    that a property of the table rather than of this function remembering to
    check.

    The caller commits. The webhook endpoint's entire job is this insert plus
    that commit, which is what keeps its response time in milliseconds.
    """
    now = now or _now()

    item_id = None
    if event.item_id:
        item_id = db.execute(
            select(PlaidItem.id).where(PlaidItem.plaid_item_id == event.item_id)
        ).scalar_one_or_none()

    statement = (
        insert(WebhookJob)
        .values(
            plaid_item_id=item_id,
            plaid_item_external_id=event.item_id,
            webhook_type=event.webhook_type,
            webhook_code=event.webhook_code,
            error_code=event.error_code,
            status=JobStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
        )
        # ON CONFLICT rather than "check then insert": between the check and
        # the insert, another request can slip in. The database is the only
        # place that race can be settled.
        # `index_where` has to repeat the partial index's predicate, because
        # PostgreSQL infers which index to use from the columns *and* the
        # condition. Omit it and the statement fails with "no unique or
        # exclusion constraint matching the ON CONFLICT specification" --
        # loudly, which is the good kind of wrong.
        .on_conflict_do_nothing(
            index_elements=["plaid_item_id", "webhook_code"],
            index_where=text("status = 'pending'"),
        )
        .returning(WebhookJob.id)
    )

    job_id = db.execute(statement).scalar_one_or_none()
    if job_id is None:
        logger.info(
            "Webhook %s/%s already queued for item %s",
            event.webhook_type,
            event.webhook_code,
            event.item_id,
        )
        return None

    return db.get(WebhookJob, job_id)


# ---------------------------------------------------------------------------
# Consumer side
# ---------------------------------------------------------------------------


def reclaim_stuck_jobs(db: Session, *, now: datetime | None = None) -> int:
    """Return jobs abandoned by a dead worker to the queue.

    Without this, a deploy during a sync leaves a row stuck in RUNNING
    forever: no worker will touch it, and the user's transactions never
    arrive. The row is put back as PENDING with its attempt already counted,
    so a job that reliably kills its worker still exhausts its retries rather
    than looping.
    """
    now = now or _now()
    cutoff = now - STUCK_AFTER

    stuck = (
        db.execute(
            select(WebhookJob)
            .where(WebhookJob.status == JobStatus.RUNNING)
            .where(WebhookJob.started_at < cutoff)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    for job in stuck:
        logger.warning("Reclaiming stuck webhook job %s", job.id)
        _reschedule(job, now, "worker did not finish; reclaimed")

    db.commit()
    return len(stuck)


def claim_due_job(db: Session, *, now: datetime | None = None) -> WebhookJob | None:
    """Take exactly one job, or return None.

    `SKIP LOCKED` is what makes several workers safe: a row another worker is
    already holding is invisible to this query rather than something to wait
    behind.

    The claim is committed immediately, before any work starts. If this
    process dies mid-sync the row says RUNNING, which `reclaim_stuck_jobs`
    can act on -- whereas an uncommitted claim would roll back and let a
    second worker start the same job while the first is still going.
    """
    now = now or _now()

    job = (
        db.execute(
            select(WebhookJob)
            .where(WebhookJob.status == JobStatus.PENDING)
            .where(WebhookJob.next_attempt_at <= now)
            .order_by(WebhookJob.next_attempt_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .first()
    )

    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.started_at = now
    job.attempts += 1
    db.commit()
    return job


def run_job(
    db: Session, gateway: PlaidGateway, job: WebhookJob, *, now: datetime | None = None
) -> bool:
    """Do the work a claimed job describes. Returns whether it succeeded.

    Every failure is contained here. A worker that raises is a worker that
    stops, and a queue with no worker is an outage -- so the exception becomes
    a retry with backoff, or a FAILED row once the attempts run out.
    """
    now = now or _now()

    try:
        _dispatch(db, gateway, job)
    except Exception as exc:  # noqa: BLE001 - containment is the point
        db.rollback()
        logger.exception("Webhook job %s failed", job.id)
        # The exception *type and message*, not the traceback: this column is
        # read in an admin view, and a Plaid error can carry request details.
        _reschedule(job, now, f"{type(exc).__name__}: {exc}"[:500])
        db.commit()
        return False

    job.status = JobStatus.SUCCEEDED
    job.finished_at = now
    job.last_error = None
    db.commit()
    return True


def _dispatch(db: Session, gateway: PlaidGateway, job: WebhookJob) -> None:
    """Apply one webhook.

    This is the body that used to run inline in the endpoint, unchanged in
    what it does -- only in when.
    """
    if job.plaid_item_id is None:
        # Not ours, or disconnected before we got to it. Recorded, not an
        # error: Plaid can legitimately send a trailing webhook after removal.
        logger.info("Webhook job %s refers to an unknown item", job.id)
        return

    item = db.get(PlaidItem, job.plaid_item_id)
    if item is None or item.status == PlaidItemStatus.DISCONNECTED:
        logger.info("Webhook job %s refers to a disconnected item", job.id)
        return

    if job.webhook_type == "ITEM" and job.error_code:
        item.status = (
            PlaidItemStatus.LOGIN_REQUIRED
            if job.error_code == "ITEM_LOGIN_REQUIRED"
            else PlaidItemStatus.ERROR
        )
        item.error_code = job.error_code
        return

    if job.webhook_code not in SYNC_TRIGGERING_CODES:
        return

    run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.WEBHOOK)

    if run.status != SyncStatus.FAILED:
        return

    # The sync engine never raises -- it records every failure as a
    # SyncHistory row and returns, deliberately, so that a partial sync keeps
    # the pages it already committed. That is right for the engine and means
    # the queue has to read the *outcome* rather than wait for an exception.
    # Without this the job would be marked SUCCEEDED on a sync that fetched
    # nothing, and the retry logic below would never run.
    if item.status in (PlaidItemStatus.LOGIN_REQUIRED, PlaidItemStatus.ERROR):
        # Terminal. The user has to reconnect their bank, or the institution
        # has told us something a retry cannot change. Five more attempts
        # would produce five more identical failures and an alert nobody can
        # act on -- the item's own status is where this surfaces.
        logger.info(
            "Webhook job %s: sync failed permanently (%s)", job.id, run.error_code
        )
        return

    raise SyncFailed(f"sync failed: {run.error_code}")


def _reschedule(job: WebhookJob, now: datetime, error: str) -> None:
    job.last_error = error

    if job.attempts >= MAX_ATTEMPTS:
        job.status = JobStatus.FAILED
        job.finished_at = now
        logger.error(
            "Webhook job %s failed permanently after %s attempts", job.id, job.attempts
        )
        return

    job.status = JobStatus.PENDING
    job.started_at = None
    job.next_attempt_at = now + BACKOFF_BASE * (2 ** (job.attempts - 1))


def drain(
    db: Session,
    gateway: PlaidGateway,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Run up to `limit` due jobs. Returns (succeeded, failed).

    Bounded on purpose. An unbounded drain holds a database session for as
    long as the backlog takes and gives the process no chance to notice a
    shutdown signal between jobs.
    """
    succeeded = failed = 0

    for _ in range(limit):
        job = claim_due_job(db, now=now)
        if job is None:
            break
        if run_job(db, gateway, job, now=now):
            succeeded += 1
        else:
            failed += 1

    return succeeded, failed

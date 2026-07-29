"""The worker that drains the webhook queue.

===========================================================================
Where this runs, and why that is a choice
===========================================================================

By default the worker runs inside the API process, on a background thread
started by the application's lifespan. That is the right default for this project:
one process to deploy, one thing to watch, and a queue whose depth is
measured in single digits.

It has two real costs, and they are the reason `scripts/run_worker.py` exists
alongside it:

  * **Shared fate.** A sync that pins the CPU makes the API slow. Nothing
    isolates them, because they are the same process.
  * **Multiplied polling.** Four uvicorn workers means four pollers. They do
    not collide -- `SKIP LOCKED` sees to that -- but they are four times the
    queries for the same empty queue.

Set `WEBHOOK_WORKER_ENABLED=false` on the API and run `scripts/run_worker.py`
as its own process to get the separation. The queue does not care which it
is; that is the point of putting the state in the database.

===========================================================================
Polling, not listening
===========================================================================

PostgreSQL has LISTEN/NOTIFY, which would wake the worker the instant a job
arrives instead of up to `poll_seconds` later. It is deliberately not used
here: NOTIFY is delivered at most once and only to sessions connected at that
moment, so a worker restarting during the notification simply misses it, and
the job sits until something else happens to poll. Building the reliable
version means polling *anyway* as a backstop, at which point the notification
is an optimisation on a latency nobody is measuring.

A few seconds of delay on a bank sync is not a problem worth that.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services import audit_partitions, job_queue
from app.services.plaid_gateway import PlaidApiError, get_plaid_gateway

logger = logging.getLogger(__name__)

# How often the worker checks that the audit log has partitions for the
# months ahead. Hourly: the answer changes once a month.
PARTITION_MAINTENANCE_SECONDS = 3600.0


def run_once(*, limit: int = 10) -> tuple[int, int]:
    """One pass: reclaim what was abandoned, then run what is due.

    Opens and closes its own session. A long-lived session held across an
    idle queue is a database connection doing nothing and a transaction
    snapshot getting older, which is how a poller quietly blocks autovacuum.

    Note what is *not* here: any RLS user context. The worker acts on behalf
    of no one -- it cannot, since nobody is logged in -- so it connects as the
    owning role and is not subject to policies. That makes it trusted code,
    and it is why `job_queue` reaches rows only through the item id recorded
    on the job rather than through anything a request supplied.
    """
    try:
        gateway = get_plaid_gateway()
    except PlaidApiError as exc:
        # Plaid not configured -- the normal state in local development.
        # There is nothing to sync, so this is quiet, not an error.
        logger.debug("Webhook worker idle: %s", exc.error_code)
        return (0, 0)

    db: Session = SessionLocal()
    try:
        job_queue.reclaim_stuck_jobs(db)
        return job_queue.drain(db, gateway, limit=limit)
    finally:
        db.close()


class WebhookWorker:
    """Polls the queue on a background thread until told to stop.

    A thread rather than an asyncio task because the work underneath is
    entirely synchronous -- SQLAlchemy sessions and a blocking Plaid client.
    Running that on the event loop would block every request in the process
    for the duration of a sync, which is precisely the problem the queue was
    introduced to solve.
    """

    def __init__(self, *, poll_seconds: float, batch_size: int = 10) -> None:
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Partition maintenance is hourly, not per-pass. Next month's
        # partition does not appear any faster for being checked every five
        # seconds, and the check is a catalogue query.
        self._next_maintenance = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="webhook-worker", daemon=True
        )
        self._thread.start()
        logger.info("Webhook worker started (poll every %ss)", self.poll_seconds)

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the worker to finish its current job and exit.

        `Event.wait` rather than `sleep` in the loop is what makes this
        prompt: a shutdown does not have to wait out the poll interval. A
        deploy that waits five seconds per worker to stop is a deploy people
        start killing by hand.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Webhook worker stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._maintain_partitions()
                succeeded, failed = run_once(limit=self.batch_size)
                if succeeded or failed:
                    logger.info(
                        "Webhook worker ran %s job(s), %s failed", succeeded + failed, failed
                    )
            except Exception:  # noqa: BLE001
                # The loop must outlive any single failure. A worker that
                # exits on an unexpected exception leaves a queue nobody
                # drains, and the symptom -- transactions quietly not
                # arriving -- looks nothing like a crash.
                logger.exception("Webhook worker pass failed")

            self._stop.wait(self.poll_seconds)

    def _maintain_partitions(self) -> None:
        """Keep the audit log's monthly partitions ahead of the calendar.

        Runs here rather than as a cron entry so that deploying the
        application is enough -- a partitioned table whose maintenance job
        lives in someone's crontab is a table that stops working the first
        time it moves to a new machine.
        """
        if time.monotonic() < self._next_maintenance:
            return
        self._next_maintenance = time.monotonic() + PARTITION_MAINTENANCE_SECONDS

        db = SessionLocal()
        try:
            audit_partitions.ensure_partitions(db)
            stranded = audit_partitions.default_partition_row_count(db)
            if stranded:
                # Alert on this. Rows in the default partition mean the
                # month they belong to has no partition, and the longer that
                # lasts the more manual the fix becomes -- creating the
                # missing partition is refused while rows for it sit in the
                # default.
                logger.error(
                    "%s audit rows are in the default partition; "
                    "partition maintenance is behind",
                    stranded,
                )
        finally:
            db.close()


_worker: WebhookWorker | None = None


def start_worker() -> WebhookWorker | None:
    """Start the in-process worker if configuration asks for one."""
    global _worker

    settings = get_settings()
    if not settings.WEBHOOK_WORKER_ENABLED:
        logger.info(
            "In-process webhook worker disabled; run scripts/run_worker.py separately"
        )
        return None

    _worker = WebhookWorker(poll_seconds=settings.WEBHOOK_WORKER_POLL_SECONDS)
    _worker.start()
    return _worker


def stop_worker() -> None:
    global _worker

    if _worker is not None:
        _worker.stop()
        _worker = None


async def run_forever(poll_seconds: float) -> None:
    """Entry point for the standalone worker process."""
    worker = WebhookWorker(poll_seconds=poll_seconds)
    worker.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        worker.stop()

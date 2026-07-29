"""Run the webhook worker as its own process.

    python scripts/run_worker.py

Use this when you want sync load isolated from the request path -- set
`WEBHOOK_WORKER_ENABLED=false` on the API processes and run one of these.
The queue lives in the database, so neither side needs to know the other
exists.

It is also the way to drain a backlog by hand after an incident: stop it with
Ctrl-C at any point and no job is lost, because a job in progress is a row
that says RUNNING and gets reclaimed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.services.job_worker import run_forever  # noqa: E402


def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    logging.getLogger(__name__).info(
        "Webhook worker polling every %ss", settings.WEBHOOK_WORKER_POLL_SECONDS
    )

    try:
        asyncio.run(run_forever(settings.WEBHOOK_WORKER_POLL_SECONDS))
    except KeyboardInterrupt:
        # Ctrl-C is how this is stopped, so it is a clean exit and not a
        # traceback. `run_forever` stops the worker in its `finally`, which
        # lets the job in flight finish rather than being reclaimed later.
        pass


if __name__ == "__main__":
    main()

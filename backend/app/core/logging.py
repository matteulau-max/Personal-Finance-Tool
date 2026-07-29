"""Logging configuration.

This exists because of something that only shows up when you run the
application rather than the tests.

Every module in this project logs carefully -- the webhook worker logs an
error when audit partitions fall behind, `deps.py` logs why a token was
rejected, the rate limiter logs who was throttled. Under `uvicorn`, none of
it appeared. Uvicorn configures *its own* loggers and leaves the root logger
alone, so records from `app.*` propagate to a root logger with no handler and
are discarded.

The result is an application that looks quiet and healthy while telling you
nothing. Alerting on a log line that is never emitted is worse than having no
alert, because it reads as reassurance.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Attach a handler to the root logger, once.

    Idempotent: uvicorn's reloader imports the application more than once,
    and a handler added on each import means every line printed several
    times -- which people then work around by not reading the logs.
    """
    root = logging.getLogger()

    if any(getattr(handler, "_finance_app", False) for handler in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler._finance_app = True  # type: ignore[attr-defined]

    root.addHandler(handler)
    root.setLevel(level.upper())

    # SQLAlchemy's engine logger is deliberately left alone. Turning it up
    # here would put every statement -- including the values bound into them,
    # which are somebody's transactions -- into the application log. If you
    # want that while debugging, set `echo=True` on the engine yourself, and
    # turn it off before deploying.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

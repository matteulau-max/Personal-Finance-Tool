"""Database engine and session management.

A *session* is one conversation with the database. It holds a transaction
open, tracks the objects you've loaded, and flushes changes when you commit.

The critical rule: **one session per request, always closed afterwards.**
Sessions that leak hold database connections open, and a connection pool that
runs dry takes the entire application down. FastAPI's dependency system gives
us that guarantee for free -- see `get_db` below.
"""

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db import rls

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_engine(
    settings.DATABASE_URL,
    # Verify a pooled connection is still alive before handing it out.
    # Hosted databases (Railway, Supabase) drop idle connections; without
    # this you get random "server closed the connection unexpectedly" errors.
    pool_pre_ping=True,
    # Log every SQL statement when debugging. Extremely useful for learning
    # what the ORM actually does -- set DEBUG=true and watch your terminal.
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    # Keep attributes readable after commit(); otherwise SQLAlchemy expires
    # them and touching one triggers a surprise query (or fails outright).
    expire_on_commit=False,
)


def clear_session_state(dbapi_connection, connection_record, reset_state) -> None:
    """Scrub session state before a connection goes back to the pool.

    Row-Level Security identifies the current user through a *session*
    setting, and a pooled connection outlives the request that set it. Left
    alone, the next request to borrow that connection would inherit the
    previous user's identity -- which is precisely the cross-user leak RLS
    exists to prevent, arriving through the plumbing instead of the query.

    `get_db` already clears it explicitly. This is the second line: it also
    covers connections returned by a path that never ran that code, such as a
    session abandoned to garbage collection.

    Three details matter, and the first cost me a debugging session:

    * `RESET ALL` does **not** reset the current role. It clears every custom
      setting -- including `app.current_user_id` -- which makes it look like
      it worked, while the connection stays switched into `finance_app`. Both
      statements are needed, and the test for this is what found it.
    * The reset has to be committed, because in PostgreSQL a `SET` is
      transactional; the rollback that follows would otherwise undo it.
    * If the scrub fails for any reason the connection is invalidated rather
      than reused. A connection we cannot prove is clean is not one to hand
      to the next user.
    """
    try:
        dbapi_connection.rollback()
        with dbapi_connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("RESET ALL")
        dbapi_connection.commit()
    except Exception:
        logger.exception("Could not clear session state; discarding connection")
        connection_record.invalidate()
        raise


# Registered as a function rather than with the decorator so the test suite
# can attach the same hook to its own engine and prove it works, instead of
# asserting that a line of setup code exists.
event.listen(engine, "reset", clear_session_state)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it.

    Usage in an endpoint:

        @router.get("/accounts")
        def list_accounts(db: Session = Depends(get_db)):
            ...

    The `finally` block runs even if the endpoint raises, so the connection
    always returns to the pool -- and, since Milestone 8, returns without the
    RLS identity the request may have set on it.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            # Discard anything the request left uncommitted, clear the
            # identity, then commit -- a `SET` is transactional in
            # PostgreSQL, so without the commit the rollback inside `close()`
            # would put the identity straight back.
            db.rollback()
            rls.deactivate(db)
            db.commit()
        except Exception:  # pragma: no cover - the pool hook is the backstop
            logger.exception("Could not deactivate RLS context")
        db.close()

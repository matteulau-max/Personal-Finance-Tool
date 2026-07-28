"""Database engine and session management.

A *session* is one conversation with the database. It holds a transaction
open, tracks the objects you've loaded, and flushes changes when you commit.

The critical rule: **one session per request, always closed afterwards.**
Sessions that leak hold database connections open, and a connection pool that
runs dry takes the entire application down. FastAPI's dependency system gives
us that guarantee for free -- see `get_db` below.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

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


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it.

    Usage in an endpoint:

        @router.get("/accounts")
        def list_accounts(db: Session = Depends(get_db)):
            ...

    The `finally` block runs even if the endpoint raises, so the connection
    always returns to the pool.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

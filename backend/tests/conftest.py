"""Shared pytest fixtures.

`conftest.py` is a magic filename: pytest imports it automatically and makes
everything in it available to every test file in the directory, with no
imports needed.

Two ideas here are worth understanding, because they are what make a test
suite fast AND trustworthy.

---------------------------------------------------------------------------
1. Tests run against a REAL PostgreSQL database, not SQLite
---------------------------------------------------------------------------
It is tempting to test against in-memory SQLite because it is faster and needs
no setup. Don't. Our schema uses JSONB, PostgreSQL-specific UUID columns, and
`NULLS NOT DISTINCT` -- none of which SQLite has. A suite that passes on
SQLite would prove nothing about what happens in production. Test against the
database you deploy on.

We create a separate `<yourdb>_test` database so running tests can never
touch your development data.

---------------------------------------------------------------------------
2. Every test is isolated by a transaction rollback
---------------------------------------------------------------------------
Recreating the schema for each test would be slow. Instead each test runs
inside a transaction that is *rolled back* afterwards, so its writes vanish.
Tests can therefore run in any order, and a failing test cannot poison the
next one. `join_transaction_mode="create_savepoint"` lets a test call
`session.commit()` normally -- the commit becomes a savepoint release inside
our outer transaction, which is still discarded at the end.
"""

import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import crypto, security
from app.core.config import get_settings
from app.models import (
    Account,
    AccountType,
    Institution,
    PlaidItem,
    Transaction,
    User,
)
from tests.auth_helpers import (
    TEST_AUTHORIZED_PARTY,
    TEST_ISSUER,
    KeyPair,
    generate_key_pair,
    make_token,
)


def _test_database_url() -> str:
    """Derive `<database>_test` from the configured DATABASE_URL."""
    url = make_url(get_settings().DATABASE_URL)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def engine():
    """Create the test database, migrate it, and tear it down afterwards.

    `scope="session"` means this runs once for the whole test run, not once
    per test -- migrations are far too slow to repeat.

    Note we apply real Alembic migrations rather than `Base.metadata
    .create_all()`. That is deliberate: it means the test suite also proves
    the migrations work. A schema that only exists in the models but has no
    working migration is useless -- you could never deploy it.
    """
    test_url = _test_database_url()
    db_name = make_url(test_url).database
    admin_url = make_url(test_url).set(database="postgres")

    # CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    alembic_cfg = Config("alembic.ini")
    # `attributes` is the supported way to hand a URL to env.py from code.
    # Setting the main option instead would not work: env.py deliberately
    # overwrites it from application settings.
    alembic_cfg.attributes["sqlalchemy_url"] = test_url
    command.upgrade(alembic_cfg, "head")

    test_engine = create_engine(test_url)
    yield test_engine
    test_engine.dispose()

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin_engine.dispose()


@pytest.fixture
def db(engine):
    """A session whose writes are discarded when the test finishes."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    session.close()
    transaction.rollback()
    connection.close()


# ---------------------------------------------------------------------------
# Factories.
#
# Building a Transaction by hand needs a user, an account, a Plaid item and an
# institution -- four objects of setup noise before the one line you actually
# want to test. These helpers hide that, so each test reads as the thing it is
# checking rather than a wall of scaffolding.
# ---------------------------------------------------------------------------


@pytest.fixture
def user(db: Session) -> User:
    user = User(email=f"test-{uuid.uuid4()}@example.com", full_name="Test User")
    db.add(user)
    db.flush()  # assigns defaults without ending the transaction
    return user


@pytest.fixture
def institution(db: Session) -> Institution:
    institution = Institution(
        plaid_institution_id=f"ins_{uuid.uuid4().hex[:8]}", name="Test Bank"
    )
    db.add(institution)
    db.flush()
    return institution


@pytest.fixture
def plaid_item(db: Session, user: User, institution: Institution) -> PlaidItem:
    item = PlaidItem(
        user_id=user.id,
        institution_id=institution.id,
        plaid_item_id=f"item_{uuid.uuid4().hex[:12]}",
        access_token_encrypted="encrypted-placeholder",
    )
    db.add(item)
    db.flush()
    return item


@pytest.fixture
def account(db: Session, user: User, plaid_item: PlaidItem) -> Account:
    account = Account(
        user_id=user.id,
        plaid_item_id=plaid_item.id,
        plaid_account_id=f"acct_{uuid.uuid4().hex[:12]}",
        name="Test Checking",
        type=AccountType.DEPOSITORY,
        subtype="checking",
        current_balance=Decimal("1000.00"),
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def credit_account(db: Session, user: User, plaid_item: PlaidItem) -> Account:
    account = Account(
        user_id=user.id,
        plaid_item_id=plaid_item.id,
        plaid_account_id=f"acct_{uuid.uuid4().hex[:12]}",
        name="Test Card",
        type=AccountType.CREDIT,
        subtype="credit card",
        current_balance=Decimal("1500.00"),
        credit_limit=Decimal("5000.00"),
    )
    db.add(account)
    db.flush()
    return account


def make_transaction(
    db: Session,
    account: Account,
    *,
    plaid_transaction_id: str | None = None,
    fingerprint: str | None = None,
    raw_name: str = "WHOLEFDS MKT #10259",
    raw_amount: str = "52.30",
    raw_date: date | None = None,
    flush: bool = True,
    **kwargs,
) -> Transaction:
    """Create a transaction with sensible defaults; override what matters.

    Pass `flush=False` when the test expects the INSERT to be rejected. The
    row is then only written when the test itself calls `db.flush()`, so the
    IntegrityError is raised inside the test's `pytest.raises` block rather
    than in here -- where it would escape as an error instead of a pass.
    """
    transaction = Transaction(
        user_id=account.user_id,
        account_id=account.id,
        plaid_transaction_id=plaid_transaction_id or f"txn_{uuid.uuid4().hex[:16]}",
        fingerprint=fingerprint,
        raw_name=raw_name,
        raw_amount=Decimal(raw_amount),
        raw_date=raw_date or date(2026, 3, 15),
        **kwargs,
    )
    db.add(transaction)
    if flush:
        db.flush()
    return transaction


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Authentication fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def key_pair() -> KeyPair:
    """One RSA key pair for the whole run -- generating them is slow."""
    return generate_key_pair()


@pytest.fixture(scope="session", autouse=True)
def _configure_auth_settings():
    """Point the app at our fake Clerk instance.

    `get_settings` is `lru_cache`d, so the cache must be cleared after
    changing the environment or the old values persist for the whole run.
    Clearing it again on teardown stops these test values leaking into
    anything else in the same process.
    """
    os.environ["CLERK_ISSUER"] = TEST_ISSUER
    os.environ["CLERK_AUTHORIZED_PARTIES"] = f'["{TEST_AUTHORIZED_PARTY}"]'
    # A throwaway encryption key. Generated per run rather than hard-coded, so
    # there is no chance of a key committed "just for tests" being reused in
    # an environment that matters.
    os.environ["ENCRYPTION_KEYS"] = json.dumps([Fernet.generate_key().decode()])
    # No background worker during tests. The queue is driven explicitly in
    # `tests/test_job_queue.py`, which is the only way to assert on what a
    # worker did; a poller running alongside would race those assertions and
    # fail them occasionally, which is worse than not testing it at all.
    os.environ["WEBHOOK_WORKER_ENABLED"] = "false"
    get_settings.cache_clear()
    crypto._cipher.cache_clear()

    yield

    os.environ.pop("CLERK_ISSUER", None)
    os.environ.pop("CLERK_AUTHORIZED_PARTIES", None)
    os.environ.pop("ENCRYPTION_KEYS", None)
    os.environ.pop("WEBHOOK_WORKER_ENABLED", None)
    get_settings.cache_clear()
    crypto._cipher.cache_clear()


@pytest.fixture(autouse=True)
def _stub_jwks(monkeypatch, key_pair: KeyPair):
    """Serve our test public key instead of fetching Clerk's JWKS.

    This is the ONLY thing stubbed. Signature checking, expiry, issuer,
    algorithm pinning and the azp check all execute for real against tokens
    we minted -- so these tests exercise the actual verification logic rather
    than a mock of it.
    """

    class _StubKey:
        def __init__(self, key):
            self.key = key

    class _StubJWKSClient:
        def get_signing_key_from_jwt(self, token: str):
            return _StubKey(key_pair.public_key)

    monkeypatch.setattr(security, "_jwks_client", lambda: _StubJWKSClient())


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Give every test a full bucket.

    The limiters are module-level singletons, which is what makes them work
    across requests -- and what would otherwise let one test's traffic fail
    the next test with a 429. Clearing between tests keeps them independent;
    `tests/test_rate_limit.py` deliberately exhausts a bucket and relies on
    this to clean up after itself.
    """
    from app.core.rate_limit import ALL_LIMITERS

    for limiter in ALL_LIMITERS:
        limiter.reset()
    yield
    for limiter in ALL_LIMITERS:
        limiter.reset()


@pytest.fixture
def client(db: Session) -> TestClient:
    """A TestClient wired to the transactional test session.

    Without the override, endpoints would open their own session against the
    development database -- so test writes would escape the rollback and
    leak into your real data.
    """
    from app.db import rls
    from app.db.session import get_db
    from app.main import app

    def _shared_session():
        """Yield the test's session, and clear RLS state on the way out.

        The override replaces `get_db` wholesale, so the real one's cleanup
        never runs. Without this, a request would leave the connection stuck
        as `finance_app` with a user identity set, and the *test's own* asserts
        afterwards would silently run under Row-Level Security -- turning
        "the fixture data is missing" into a mystery.
        """
        try:
            yield db
        finally:
            rls.deactivate(db)

    app.dependency_overrides[get_db] = _shared_session
    test_client = TestClient(app)

    yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def committing_client(engine):
    """A client whose writes REALLY commit, plus cleanup afterwards.

    The normal `client` fixture shares one rolled-back transaction with the
    test, which is fast and isolated -- but it cannot distinguish "flushed"
    from "committed", because both are visible inside the same transaction.
    That blind spot hides a whole class of bug where data appears to save and
    then silently vanishes.

    This fixture gives each request its own real session so a test can open a
    *separate* connection afterwards and check what actually survived. Use it
    sparingly: it is slower, and rows must be cleaned up by hand.
    """
    from app.db.session import get_db
    from app.main import app

    def _real_session():
        from app.db import rls

        session = Session(bind=engine)
        try:
            yield session
        finally:
            session.rollback()
            rls.deactivate(session)
            session.commit()
            session.close()

    app.dependency_overrides[get_db] = _real_session
    test_client = TestClient(app)

    yield test_client

    app.dependency_overrides.clear()

    with Session(bind=engine) as cleanup:
        cleanup.execute(
            delete(User).where(User.clerk_user_id.like("persist_test_%"))
        )
        cleanup.commit()


@pytest.fixture
def authenticated_user(db: Session, key_pair: KeyPair) -> tuple[User, str]:
    """An existing user plus a valid token for them.

    Returns the pair because almost every authorization test needs both: the
    token to make the request, and the row to attach fixture data to.
    """
    subject = f"user_{uuid.uuid4().hex[:16]}"
    user = User(
        clerk_user_id=subject,
        email=f"{subject}@example.com",
        full_name="Primary User",
    )
    db.add(user)
    db.flush()

    return user, make_token(key_pair, subject=subject, email=user.email)


@pytest.fixture
def other_user(db: Session, key_pair: KeyPair) -> tuple[User, str]:
    """A SECOND user, for proving one cannot see the other's data.

    Most cross-tenant leaks survive because the test suite only ever has one
    user in it -- with a single user, a missing WHERE clause returns exactly
    the right answer. This fixture is what makes that bug detectable.
    """
    subject = f"user_{uuid.uuid4().hex[:16]}"
    user = User(
        clerk_user_id=subject,
        email=f"{subject}@example.com",
        full_name="Other User",
    )
    db.add(user)
    db.flush()

    return user, make_token(key_pair, subject=subject, email=user.email)


def make_account(db: Session, user: User, name: str = "Checking") -> Account:
    account = Account(
        user_id=user.id,
        plaid_account_id=f"acct_{uuid.uuid4().hex[:12]}",
        name=name,
        type=AccountType.DEPOSITORY,
        subtype="checking",
        current_balance=Decimal("500.00"),
    )
    db.add(account)
    db.flush()
    return account

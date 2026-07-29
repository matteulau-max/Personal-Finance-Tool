"""Row-Level Security tests.

Every test in this file deliberately writes the query a tired developer would
write -- `select(Transaction)` with no WHERE clause -- and asserts that the
*database* refuses to leak. If RLS were quietly disabled, or a policy were
missing from one table, these would fail; nothing else in the suite would.

That is the point. `tests/test_authorization.py` proves the application scopes
its queries. This file proves it does not have to.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.db import rls
from app.db.session import clear_session_state
from app.models import (
    Account,
    AccountBalance,
    Base,
    Category,
    Merchant,
    Transaction,
    User,
)
from tests.conftest import make_account, make_transaction


@pytest.fixture
def two_users_with_data(db: Session):
    """Two users, each with an account and a transaction.

    Cross-tenant tests are worthless with one user in the database: a missing
    filter returns exactly the right answer. Everything here needs both.
    """
    alice = User(email=f"alice-{uuid.uuid4()}@example.com", full_name="Alice")
    bob = User(email=f"bob-{uuid.uuid4()}@example.com", full_name="Bob")
    db.add_all([alice, bob])
    db.flush()

    alice_account = make_account(db, alice, "Alice Checking")
    bob_account = make_account(db, bob, "Bob Checking")

    alice_txn = make_transaction(db, alice_account, raw_name="ALICE COFFEE")
    bob_txn = make_transaction(db, bob_account, raw_name="BOB COFFEE")

    return {
        "alice": alice,
        "bob": bob,
        "alice_account": alice_account,
        "bob_account": bob_account,
        "alice_txn": alice_txn,
        "bob_txn": bob_txn,
    }


# ---------------------------------------------------------------------------
# The headline behaviour
# ---------------------------------------------------------------------------


def test_an_unscoped_select_returns_only_the_current_users_rows(db, two_users_with_data):
    """The bug `scoping.py` exists to prevent, committed on purpose.

    `select(Transaction)` is the query that leaks every user's financial
    history in an application protected only by convention. Under RLS it is
    merely a query that returns your own transactions.
    """
    data = two_users_with_data
    rls.activate(db, data["alice"].id)

    rows = db.execute(select(Transaction)).scalars().all()

    assert [row.id for row in rows] == [data["alice_txn"].id]


def test_another_users_row_is_invisible_even_when_asked_for_by_id(db, two_users_with_data):
    """No 403-vs-404 distinction to leak: the row simply is not there."""
    data = two_users_with_data
    rls.activate(db, data["alice"].id)

    found = db.execute(
        select(Transaction).where(Transaction.id == data["bob_txn"].id)
    ).scalar_one_or_none()

    assert found is None


def test_accounts_are_filtered_too(db, two_users_with_data):
    data = two_users_with_data
    rls.activate(db, data["bob"].id)

    names = db.execute(select(Account.name)).scalars().all()

    assert names == ["Bob Checking"]


def test_a_user_sees_only_their_own_row_in_the_users_table(db, two_users_with_data):
    data = two_users_with_data
    rls.activate(db, data["alice"].id)

    ids = db.execute(select(User.id)).scalars().all()

    assert ids == [data["alice"].id]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_a_row_cannot_be_written_on_another_users_behalf(db, two_users_with_data):
    """`WITH CHECK` is the half people forget.

    A `USING` clause alone stops you reading someone else's rows but happily
    lets you create one -- planting a transaction in another user's history,
    which for a financial application is arguably worse than reading it.
    """
    data = two_users_with_data
    rls.activate(db, data["alice"].id)

    with pytest.raises(ProgrammingError) as caught:
        make_transaction(db, data["bob_account"], raw_name="PLANTED BY ALICE")

    assert "row-level security" in str(caught.value).lower()
    db.rollback()


def test_ownership_cannot_be_reassigned_by_update(db, two_users_with_data):
    """Giving a row away is the same hole as planting one, in reverse."""
    data = two_users_with_data
    rls.activate(db, data["alice"].id)

    with pytest.raises(ProgrammingError):
        db.execute(
            text("UPDATE transactions SET user_id = :bob WHERE id = :id"),
            {"bob": data["bob"].id, "id": data["alice_txn"].id},
        )

    db.rollback()


def test_an_update_cannot_reach_another_users_row(db, two_users_with_data):
    """Not an error -- an UPDATE that matches nothing. The row is invisible,
    so there is nothing to update, which is exactly a SELECT's behaviour."""
    data = two_users_with_data
    rls.activate(db, data["alice"].id)

    result = db.execute(
        text("UPDATE transactions SET raw_name = 'HACKED' WHERE id = :id"),
        {"id": data["bob_txn"].id},
    )

    assert result.rowcount == 0


# ---------------------------------------------------------------------------
# Shared rows
# ---------------------------------------------------------------------------


def test_global_categories_stay_visible(db, two_users_with_data):
    """RLS must not break the shared taxonomy.

    Every user needs the seeded categories. A policy that filtered them out
    would be secure and useless.
    """
    rls.activate(db, two_users_with_data["alice"].id)

    globals_visible = db.execute(
        select(Category).where(Category.user_id.is_(None)).limit(1)
    ).scalar_one_or_none()

    assert globals_visible is not None


def test_a_global_merchant_cannot_be_edited(db, two_users_with_data):
    """The database enforces copy-on-write.

    A global merchant is visible to everybody, so an UPDATE on one would
    change what every user sees. The application already copies instead of
    editing -- this makes that the only option, rather than a rule that holds
    until someone writes a convenient bulk-fix script.
    """
    data = two_users_with_data
    merchant = Merchant(
        user_id=None, normalized_name=f"shared-{uuid.uuid4().hex[:8]}", display_name="Shared"
    )
    db.add(merchant)
    db.flush()

    rls.activate(db, data["alice"].id)

    result = db.execute(
        text("UPDATE merchants SET display_name = 'Renamed' WHERE id = :id"),
        {"id": merchant.id},
    )

    assert result.rowcount == 0


def test_another_users_private_merchant_is_invisible(db, two_users_with_data):
    data = two_users_with_data
    private = Merchant(
        user_id=data["bob"].id,
        normalized_name=f"bobs-{uuid.uuid4().hex[:8]}",
        display_name="Bob's Corner Shop",
    )
    db.add(private)
    db.flush()

    rls.activate(db, data["alice"].id)

    found = db.execute(
        select(Merchant).where(Merchant.id == private.id)
    ).scalar_one_or_none()

    assert found is None


# ---------------------------------------------------------------------------
# Tables with no user_id of their own
# ---------------------------------------------------------------------------


def test_balances_inherit_their_accounts_owner(db, two_users_with_data):
    """`account_balances` has no user_id, which is exactly the kind of table
    that gets forgotten -- there is no column to write a WHERE clause on."""
    data = two_users_with_data
    db.add(
        AccountBalance(
            account_id=data["bob_account"].id,
            current_balance=Decimal("42.00"),
            as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    )
    db.flush()

    rls.activate(db, data["alice"].id)

    assert db.execute(select(AccountBalance)).scalars().all() == []


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_no_identity_means_no_rows_rather_than_all_rows(db, two_users_with_data):
    """The most important test here.

    Every access-control system fails eventually; what matters is which way.
    With the role switched on but no user set, the policy compares against
    NULL, and NULL is not TRUE. So a request that somehow skipped
    authentication sees an empty database -- a visible, reportable bug --
    instead of everybody's.
    """
    db.execute(text('SET ROLE "finance_app"'))

    assert db.execute(select(Transaction)).scalars().all() == []
    assert db.execute(select(Account)).scalars().all() == []

    db.execute(text("RESET ROLE"))


def test_the_identity_survives_a_commit(db, two_users_with_data):
    """`SET LOCAL` would fail this, and would have been the obvious choice.

    A transaction-scoped setting is cleared by the first `db.commit()` an
    endpoint makes. Because we fail closed, the symptom would be a page that
    works until you save something and then shows an empty list -- a bug that
    is unpleasant to diagnose and trivial to avoid.
    """
    data = two_users_with_data
    rls.activate(db, data["alice"].id)
    db.commit()

    assert rls.current_user_id(db) == data["alice"].id
    assert db.execute(select(Transaction)).scalars().all() != []


def test_deactivate_restores_the_owning_role(db, two_users_with_data):
    data = two_users_with_data
    rls.activate(db, data["alice"].id)
    assert db.execute(text("SELECT current_user")).scalar() == "finance_app"

    rls.deactivate(db)

    assert db.execute(text("SELECT current_user")).scalar() != "finance_app"
    assert rls.current_user_id(db) is None


def test_a_pooled_connection_is_scrubbed_before_reuse(engine, two_users_with_data):
    """The leak that would arrive through the plumbing.

    RLS identity lives in a *session* setting, and a pooled connection
    outlives the request. If it were not cleared on return, the next request
    to borrow that connection would inherit the previous user's identity --
    and every query would be scoped, correctly and confidently, to the wrong
    person.

    This builds a real pool of exactly one connection so the second session is
    guaranteed to be the first one coming back.
    """
    pooled = create_engine(engine.url, pool_size=1, max_overflow=0)
    event.listen(pooled, "reset", clear_session_state)

    try:
        with Session(bind=pooled) as first:
            rls.activate(first, two_users_with_data["alice"].id)
            first.commit()

        with Session(bind=pooled) as second:
            assert second.execute(text("SELECT current_user")).scalar() != "finance_app"
            assert rls.current_user_id(second) is None
    finally:
        pooled.dispose()


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def test_every_table_is_either_protected_or_deliberately_exempt():
    """The guard test, and the reason the policy lists are worth maintaining.

    It reads the model registry rather than a hand-written list, so adding a
    table to the application is enough to fail it. Someone then has to decide
    whether the new table holds user data -- which is the decision that gets
    skipped when nothing asks.
    """
    known = rls.PROTECTED_TABLES | rls.EXEMPT_TABLES
    unclassified = sorted(set(Base.metadata.tables) - known)

    assert not unclassified, (
        "These tables are neither RLS-protected nor exempt:\n  "
        + "\n  ".join(unclassified)
        + "\nAdd each one to app/db/rls.py, and write the policy in a migration."
    )


def test_every_protected_table_actually_has_row_security_enabled(db):
    """Classification is a claim; this checks the database agrees.

    A table listed as protected but never altered in a migration would pass
    the test above and leak in production.
    """
    rows = db.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, count(p.polname) "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
            # 'r' is an ordinary table, 'p' a partitioned one. The audit log
            # is partitioned, and omitting 'p' would silently exclude the
            # table with the most sensitive history in the schema.
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "GROUP BY c.relname, c.relrowsecurity"
        )
    ).all()
    state = {name: (enabled, policies) for name, enabled, policies in rows}

    missing = [
        table
        for table in sorted(rls.PROTECTED_TABLES)
        if state.get(table, (False, 0))[0] is not True or state.get(table, (False, 0))[1] == 0
    ]

    assert not missing, f"RLS is not actually enabled on: {missing}"


def test_exempt_tables_are_left_alone(db):
    """The exemption list is not a place to quietly park a table with user
    data in it, so check the exempt tables really do have no owner column."""
    for table in rls.EXEMPT_TABLES:
        columns = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).scalars().all()
        assert "user_id" not in columns, f"{table} has a user_id but is exempt from RLS"


def test_the_application_role_cannot_bypass_policies(db):
    """A role with BYPASSRLS or table ownership makes every policy above
    decorative. Both are easy to grant by accident while debugging."""
    row = db.execute(
        text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'finance_app'")
    ).one()

    assert row.rolbypassrls is False
    assert row.rolsuper is False

    owned = db.execute(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE r.rolname = 'finance_app'"
        )
    ).scalar()
    assert owned == 0

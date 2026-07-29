"""Tests for the "never duplicate a transaction" guarantee.

These are the most important tests in the project. If deduplication breaks,
every number the application shows is wrong -- and wrong in a way that looks
plausible, which is the worst kind of bug.

Note what is being tested: not that our Python code is careful, but that the
*database* refuses the duplicate. Application code can be bypassed by a
script, a migration, or a second process. A constraint cannot.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, Transaction, TransactionStatus
from tests.conftest import make_transaction


def test_same_plaid_transaction_id_in_same_account_is_rejected(
    db: Session, account: Account
):
    """The core guarantee: Plaid sending the same transaction twice cannot
    create two rows."""
    make_transaction(db, account, plaid_transaction_id="txn_duplicate_me")

    make_transaction(db, account, plaid_transaction_id="txn_duplicate_me", flush=False)

    with pytest.raises(IntegrityError) as exc_info:
        db.flush()

    assert "uq_transactions_account_id_plaid_transaction_id" in str(exc_info.value)


def test_same_plaid_transaction_id_in_different_accounts_is_allowed(
    db: Session, account: Account, credit_account: Account
):
    """The constraint is scoped to (account, transaction_id), not the id alone.

    This matters because Plaid only guarantees transaction ids are unique
    within an Item. Making the id globally unique would eventually reject a
    legitimate transaction -- a bug that would be extremely hard to diagnose.
    """
    make_transaction(db, account, plaid_transaction_id="txn_shared_id")
    make_transaction(db, credit_account, plaid_transaction_id="txn_shared_id")

    db.flush()  # must not raise

    assert db.query(Transaction).filter_by(plaid_transaction_id="txn_shared_id").count() == 2


def test_duplicate_fingerprint_in_same_account_is_rejected(db: Session, account: Account):
    """Manual and CSV imports have no Plaid id, so they dedupe on a hash."""
    make_transaction(db, account, plaid_transaction_id=None, fingerprint="abc123")

    make_transaction(db, account, plaid_transaction_id=None, fingerprint="abc123", flush=False)

    with pytest.raises(IntegrityError) as exc_info:
        db.flush()

    assert "uq_transactions_account_id_fingerprint" in str(exc_info.value)


def test_multiple_transactions_without_ids_are_allowed(db: Session, account: Account):
    """A subtle but critical SQL behaviour: NULL is never equal to NULL.

    So rows with a NULL fingerprint do not collide with each other. Without
    this, a user could only ever have one transaction lacking a fingerprint.
    We rely on it deliberately -- but you must know it is happening, because
    it also means a UNIQUE constraint will NOT stop duplicate NULLs.
    """
    for _ in range(3):
        make_transaction(db, account, plaid_transaction_id=None, fingerprint=None)

    db.flush()

    assert db.query(Transaction).filter_by(account_id=account.id).count() == 3


def test_pending_to_posted_link_is_recorded(db: Session, account: Account):
    """When a pending charge posts, Plaid issues a NEW id and tells us which
    pending transaction it replaced.

    Storing that link is what lets the sync engine remove the pending row
    instead of leaving the same coffee on your statement twice.
    """
    pending = make_transaction(
        db,
        account,
        plaid_transaction_id="txn_pending_1",
        status=TransactionStatus.PENDING,
        raw_amount="4.50",
    )

    posted = make_transaction(
        db,
        account,
        plaid_transaction_id="txn_posted_1",
        status=TransactionStatus.POSTED,
        raw_amount="4.75",  # tips often change the amount at posting
        replaces_plaid_transaction_id="txn_pending_1",
    )
    db.flush()

    assert posted.replaces_plaid_transaction_id == pending.plaid_transaction_id
    # Both rows exist right now; reconciling them is the sync engine's job
    # (Milestone 4). The schema's job is only to make the link recordable.
    assert db.query(Transaction).filter_by(account_id=account.id).count() == 2


def test_removed_transactions_are_soft_deleted(db: Session, account: Account, utc_now):
    """A removed transaction keeps its row. Recovery from a bad sync is then
    an UPDATE, not a restore-from-backup."""
    transaction = make_transaction(db, account, plaid_transaction_id="txn_will_be_removed")
    transaction_id = transaction.id

    transaction.status = TransactionStatus.REMOVED
    transaction.removed_at = utc_now
    db.flush()

    still_there = db.get(Transaction, transaction_id)
    assert still_there is not None
    assert still_there.status == TransactionStatus.REMOVED
    assert still_there.removed_at is not None
    # And the original data survives intact for forensics.
    assert still_there.raw_amount == Decimal("52.30")


def test_transaction_cannot_reference_a_missing_account(db: Session, account: Account):
    """Foreign keys stop orphaned rows at the database level."""
    orphan = Transaction(
        user_id=account.user_id,
        account_id=uuid.uuid4(),  # no such account
        plaid_transaction_id="txn_orphan",
        raw_name="Nowhere",
        raw_amount=Decimal("1.00"),
        raw_date=date(2026, 1, 1),
    )
    db.add(orphan)

    with pytest.raises(IntegrityError):
        db.flush()

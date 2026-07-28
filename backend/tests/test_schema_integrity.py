"""Tests for the rest of the schema's integrity rules.

Money precision, cascade behaviour, the audit trail surviving deletion, the
category tree, tags, and credit utilization.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountBalance,
    AccountType,
    AuditAction,
    AuditActor,
    AuditLog,
    Category,
    Merchant,
    Tag,
    Transaction,
    TransactionTag,
    User,
)
from tests.conftest import make_transaction

# ---------------------------------------------------------------------------
# Money precision -- the bug that silently corrupts financial software
# ---------------------------------------------------------------------------


def test_money_survives_a_round_trip_exactly(db: Session, account: Account):
    """DECIMAL stores exact base-10 values. A float would not."""
    transaction = make_transaction(db, account, raw_amount="0.10")
    db.flush()
    db.expire_all()

    reloaded = db.get(Transaction, transaction.id)
    assert reloaded.raw_amount == Decimal("0.10")
    assert isinstance(reloaded.raw_amount, Decimal)


def test_summing_many_small_amounts_stays_exact(db: Session, account: Account):
    """Add 0.10 a thousand times.

    In floating point this drifts away from 100.00 -- the classic bug that
    makes a dashboard disagree with a bank statement by a few cents and
    destroys a user's trust in the entire product. With DECIMAL the sum is
    exact, and this test proves it end to end through PostgreSQL.
    """
    for _ in range(1000):
        make_transaction(db, account, raw_amount="0.10")
    db.flush()

    total = db.execute(
        select(func.sum(Transaction.raw_amount)).where(Transaction.account_id == account.id)
    ).scalar_one()

    assert total == Decimal("100.00")


def test_large_amounts_are_supported(db: Session, account: Account):
    """Numeric(18, 4) comfortably holds a mortgage or a portfolio balance."""
    transaction = make_transaction(db, account, raw_amount="1234567890.1234")
    db.flush()
    db.expire_all()

    assert db.get(Transaction, transaction.id).raw_amount == Decimal("1234567890.1234")


# ---------------------------------------------------------------------------
# Cascades and the audit trail
# ---------------------------------------------------------------------------


def test_deleting_a_user_removes_their_financial_data(db: Session, user: User, account: Account):
    """Account deletion must actually delete the data -- both for privacy law
    (GDPR/CCPA erasure requests) and because orphaned financial rows are a
    liability."""
    make_transaction(db, account)
    db.flush()

    db.delete(user)
    db.flush()

    remaining = db.execute(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id)
    ).scalar_one()
    assert remaining == 0


def test_audit_log_survives_the_deletion_of_its_user(db: Session, user: User):
    """The audit trail outlives what it describes.

    The foreign key is ON DELETE SET NULL, not CASCADE. An audit log that
    disappears along with the thing being audited would be worthless -- the
    single most important case to investigate is precisely the one where
    something was deleted.
    """
    entry = AuditLog(
        user_id=user.id,
        entity_type="transaction",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        actor=AuditActor.USER,
        before_values={"user_category_id": None},
        after_values={"user_category_id": str(uuid.uuid4())},
        reason="manual recategorization",
    )
    db.add(entry)
    db.flush()
    entry_id = entry.id

    db.delete(user)
    db.flush()
    db.expire_all()

    surviving = db.get(AuditLog, entry_id)
    assert surviving is not None
    assert surviving.user_id is None  # the link is cleared, the record remains
    assert surviving.reason == "manual recategorization"


def test_audit_log_records_a_before_and_after_diff(db: Session, user: User):
    """JSONB round-trips as a normal Python dict."""
    entry = AuditLog(
        user_id=user.id,
        entity_type="transaction",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        actor=AuditActor.SYNC,
        before_values={"raw_amount": "4.50", "status": "pending"},
        after_values={"raw_amount": "4.75", "status": "posted"},
    )
    db.add(entry)
    db.flush()
    db.expire_all()

    reloaded = db.get(AuditLog, entry.id)
    assert reloaded.before_values["raw_amount"] == "4.50"
    assert reloaded.after_values["status"] == "posted"


# ---------------------------------------------------------------------------
# Accounts and balances
# ---------------------------------------------------------------------------


def test_credit_utilization_is_calculated(db: Session, credit_account: Account):
    # 1500 of a 5000 limit
    assert credit_account.utilization == Decimal("0.3")


def test_utilization_is_none_for_non_credit_accounts(db: Session, account: Account):
    """Returning None rather than 0 matters: a checking account showing
    '0% utilization' on a dashboard is a bug, showing nothing is correct."""
    assert account.utilization is None


def test_utilization_is_none_without_a_known_limit(db: Session, credit_account: Account):
    credit_account.credit_limit = None
    assert credit_account.utilization is None


def test_balance_history_is_unique_per_instant(db: Session, account: Account, utc_now):
    """Re-running a sync cannot double-write history."""
    db.add(AccountBalance(account_id=account.id, as_of=utc_now, current_balance=Decimal("100")))
    db.flush()

    db.add(AccountBalance(account_id=account.id, as_of=utc_now, current_balance=Decimal("100")))
    with pytest.raises(IntegrityError) as exc_info:
        db.flush()

    assert "uq_account_balances_account_id_as_of" in str(exc_info.value)


def test_balance_history_accumulates_over_time(db: Session, account: Account, utc_now):
    """This is what makes a net-worth-over-time chart possible. A balance you
    did not record is gone forever -- no bank API will tell you what your
    balance was last March."""
    for days_ago in range(5):
        db.add(
            AccountBalance(
                account_id=account.id,
                as_of=utc_now - timedelta(days=days_ago),
                current_balance=Decimal("1000") + Decimal(days_ago * 10),
            )
        )
    db.flush()

    history = db.execute(
        select(AccountBalance)
        .where(AccountBalance.account_id == account.id)
        .order_by(AccountBalance.as_of.desc())
    ).scalars().all()

    assert len(history) == 5
    assert history[0].current_balance == Decimal("1000.0000")


def test_duplicate_plaid_account_is_rejected(db: Session, user: User, account: Account):
    duplicate = Account(
        user_id=user.id,
        plaid_account_id=account.plaid_account_id,
        name="Duplicate",
        type=AccountType.DEPOSITORY,
    )
    db.add(duplicate)

    with pytest.raises(IntegrityError) as exc_info:
        db.flush()

    assert "uq_accounts_plaid_account_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def test_system_categories_are_seeded_by_the_migration(db: Session):
    """Proves the data migration ran. Every environment gets the same tree."""
    count = db.execute(
        select(func.count()).select_from(Category).where(Category.is_system.is_(True))
    ).scalar_one()
    assert count > 50

    uncategorized = db.execute(
        select(Category).where(Category.slug == "uncategorized")
    ).scalar_one()
    assert uncategorized.is_system is True


def test_income_and_transfer_categories_are_flagged(db: Session):
    """These flags are what stop a credit card payment being counted as
    spending on top of the purchases the card already recorded."""
    salary = db.execute(select(Category).where(Category.slug == "income.salary")).scalar_one()
    assert salary.is_income is True
    assert salary.is_transfer is False

    card_payment = db.execute(
        select(Category).where(Category.slug == "transfer.credit_card_payment")
    ).scalar_one()
    assert card_payment.is_transfer is True
    assert card_payment.is_income is False


def test_category_tree_builds_a_readable_full_name(db: Session):
    groceries = db.execute(
        select(Category).where(Category.slug == "food_and_drink.groceries")
    ).scalar_one()

    assert groceries.parent is not None
    assert groceries.full_name == "Food & Drink › Groceries"


def test_a_user_can_create_their_own_category_named_like_a_system_one(
    db: Session, user: User
):
    """user_id distinguishes them, so this must not collide."""
    db.add(Category(user_id=user.id, name="Groceries", is_system=False))
    db.flush()  # must not raise


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


def test_duplicate_global_merchants_are_rejected(db: Session):
    """Relies on PostgreSQL 15+ NULLS NOT DISTINCT.

    Without it, `user_id IS NULL` rows would never compare equal and the
    global merchant list would silently fill with duplicates -- exactly the
    problem the merchants table exists to solve.
    """
    db.add(Merchant(normalized_name="whole foods", display_name="Whole Foods"))
    db.flush()

    db.add(Merchant(normalized_name="whole foods", display_name="Whole Foods Market"))
    with pytest.raises(IntegrityError) as exc_info:
        db.flush()

    assert "uq_merchants_user_id_normalized_name" in str(exc_info.value)


def test_a_user_may_override_a_global_merchant_name(db: Session, user: User):
    db.add(Merchant(normalized_name="whole foods", display_name="Whole Foods"))
    db.add(
        Merchant(user_id=user.id, normalized_name="whole foods", display_name="WF (my name)")
    )
    db.flush()  # must not raise -- different user_id, so not a duplicate

    assert db.execute(
        select(func.count()).select_from(Merchant).where(Merchant.normalized_name == "whole foods")
    ).scalar_one() == 2


# ---------------------------------------------------------------------------
# Tags (many-to-many)
# ---------------------------------------------------------------------------


def test_a_transaction_can_carry_many_tags(db: Session, user: User, account: Account):
    """One dinner can be Business AND Tax Deductible AND Reimbursable at once
    -- which a single category field could never express."""
    transaction = make_transaction(db, account)
    for name in ("Business", "Tax Deductible", "Reimbursable"):
        tag = Tag(user_id=user.id, name=name)
        db.add(tag)
        db.flush()
        db.add(TransactionTag(transaction_id=transaction.id, tag_id=tag.id))
    db.flush()
    db.expire_all()

    reloaded = db.get(Transaction, transaction.id)
    assert {tag.name for tag in reloaded.tags} == {
        "Business",
        "Tax Deductible",
        "Reimbursable",
    }


def test_the_same_tag_cannot_be_applied_twice(db: Session, user: User, account: Account):
    """The composite primary key makes this structurally impossible."""
    transaction = make_transaction(db, account)
    tag = Tag(user_id=user.id, name="Vacation")
    db.add(tag)
    db.flush()

    db.add(TransactionTag(transaction_id=transaction.id, tag_id=tag.id))
    db.flush()
    db.add(TransactionTag(transaction_id=transaction.id, tag_id=tag.id))

    with pytest.raises(IntegrityError):
        db.flush()


def test_duplicate_tag_names_per_user_are_rejected(db: Session, user: User):
    """Uniqueness is CASE-INSENSITIVE as of migration 5d94c4e6a26a.

    "Vacation" and "vacation" collide, because a user who ends up with both
    has two tags they believe are one -- and their vacation report silently
    shows half their spending.
    """
    db.add(Tag(user_id=user.id, name="Vacation"))
    db.flush()

    db.add(Tag(user_id=user.id, name="vacation"))
    with pytest.raises(IntegrityError) as exc_info:
        db.flush()

    assert "uq_tags_user_id_lower_name" in str(exc_info.value)


def test_deleting_a_tag_leaves_its_transactions_intact(
    db: Session, user: User, account: Account
):
    """Removing a label must never remove the money."""
    transaction = make_transaction(db, account)
    tag = Tag(user_id=user.id, name="Vacation")
    db.add(tag)
    db.flush()
    db.add(TransactionTag(transaction_id=transaction.id, tag_id=tag.id))
    db.flush()

    db.delete(tag)
    db.flush()

    assert db.get(Transaction, transaction.id) is not None


# ---------------------------------------------------------------------------
# Enum storage
# ---------------------------------------------------------------------------


def test_enums_are_stored_as_lowercase_values_not_names(db: Session, account: Account):
    """SQLAlchemy's default is to persist an enum's NAME ("POSTED"), not its
    VALUE ("posted").

    That default would leak uppercase strings into API responses and break any
    hand-written SQL that filters on the documented lowercase value. Our
    `enum_column()` helper overrides it, and this test makes sure nobody
    quietly reverts that by constructing `Enum(...)` directly.
    """
    make_transaction(db, account)
    db.flush()

    raw_value = db.execute(
        text("SELECT status FROM transactions WHERE account_id = :account_id LIMIT 1"),
        {"account_id": account.id},
    ).scalar_one()

    assert raw_value == "posted"


def test_an_invalid_enum_value_is_rejected_by_the_database(db: Session, account: Account):
    """The CHECK constraint must actually exist.

    `Enum(..., native_enum=False)` looks like it validates, but in SQLAlchemy
    2.0 `create_constraint` defaults to False -- giving you a plain VARCHAR
    that will store any string at all. We pass `create_constraint=True`; this
    test fails loudly if that is ever dropped.
    """
    transaction = make_transaction(db, account)
    db.flush()

    with pytest.raises(IntegrityError) as exc_info:
        db.execute(
            text("UPDATE transactions SET status = 'banana' WHERE id = :id"),
            {"id": transaction.id},
        )

    assert "ck_transactions_ck_transactionstatus" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Sign convention -- locked down so it can never drift
# ---------------------------------------------------------------------------


def test_positive_amount_means_money_left_the_account(db: Session, account: Account):
    """Plaid's convention. Documented in models/transaction.py, Decision 3.

    This test exists so that if anyone ever "fixes" the sign, the suite fails
    loudly rather than silently inverting every chart in the application.
    """
    purchase = make_transaction(db, account, raw_amount="52.30")
    refund = make_transaction(db, account, raw_amount="-52.30")

    assert purchase.is_outflow is True
    assert refund.is_outflow is False


def test_spending_total_uses_positive_amounts(db: Session, account: Account):
    make_transaction(db, account, raw_amount="100.00")  # spent
    make_transaction(db, account, raw_amount="40.00")  # spent
    make_transaction(db, account, raw_amount="-2000.00", raw_name="PAYROLL")  # income
    db.flush()

    spending = db.execute(
        select(func.sum(Transaction.effective_amount)).where(
            Transaction.account_id == account.id,
            Transaction.effective_amount > 0,
        )
    ).scalar_one()

    assert spending == Decimal("140.00")


def test_transactions_are_ordered_newest_first_by_effective_date(
    db: Session, account: Account
):
    make_transaction(db, account, raw_date=date(2026, 1, 1))
    make_transaction(db, account, raw_date=date(2026, 3, 1))
    # A user-corrected date must sort by the corrected value, not the original.
    make_transaction(db, account, raw_date=date(2026, 2, 1), user_date=date(2026, 6, 1))
    db.flush()

    dates = db.execute(
        select(Transaction.effective_date)
        .where(Transaction.account_id == account.id)
        .order_by(Transaction.effective_date.desc())
    ).scalars().all()

    assert dates == [date(2026, 6, 1), date(2026, 3, 1), date(2026, 1, 1)]

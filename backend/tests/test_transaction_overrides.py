"""Tests for the "corrections never destroy the original" guarantee.

The three-layer model: raw_* (source) -> auto_* (inferred) -> user_* (human).
The displayed value is the first non-NULL, reading right to left.

Every test here exists to prove one sentence: a user edit is always
reversible, because the original was never touched.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Category, CategorySource, Merchant, Transaction
from tests.conftest import make_transaction


def _category(db: Session, name: str) -> Category:
    category = Category(name=name, is_system=False)
    db.add(category)
    db.flush()
    return category


def _merchant(db: Session, normalized: str, display: str) -> Merchant:
    merchant = Merchant(normalized_name=normalized, display_name=display)
    db.add(merchant)
    db.flush()
    return merchant


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_raw_value_is_used_when_nothing_overrides_it(db: Session, account: Account):
    transaction = make_transaction(db, account, raw_amount="52.30")

    assert transaction.effective_amount == Decimal("52.30")
    assert transaction.effective_category_id is None


def test_auto_category_is_used_when_the_user_has_not_chosen(db: Session, account: Account):
    groceries = _category(db, "Groceries")
    transaction = make_transaction(db, account)

    transaction.auto_category_id = groceries.id
    transaction.auto_category_source = CategorySource.HEURISTIC
    db.flush()

    assert transaction.effective_category_id == groceries.id


def test_user_category_beats_auto_category(db: Session, account: Account):
    """The whole point: an automated system may never overrule a human."""
    groceries = _category(db, "Groceries")
    dining = _category(db, "Restaurants")

    transaction = make_transaction(db, account)
    transaction.auto_category_id = groceries.id
    transaction.user_category_id = dining.id
    db.flush()

    assert transaction.effective_category_id == dining.id
    # The engine's guess is retained, not overwritten -- so we can later
    # measure how often our categorizer disagrees with the user.
    assert transaction.auto_category_id == groceries.id


def test_user_merchant_beats_auto_merchant(db: Session, account: Account):
    guessed = _merchant(db, "whole foods", "Whole Foods")
    corrected = _merchant(db, "whole foods market", "Whole Foods Market")

    transaction = make_transaction(db, account)
    transaction.auto_merchant_id = guessed.id
    transaction.user_merchant_id = corrected.id
    db.flush()

    assert transaction.effective_merchant_id == corrected.id
    assert transaction.auto_merchant_id == guessed.id


# ---------------------------------------------------------------------------
# The original is never destroyed
# ---------------------------------------------------------------------------


def test_editing_a_transaction_leaves_every_raw_field_untouched(
    db: Session, account: Account
):
    transaction = make_transaction(
        db,
        account,
        raw_name="WHOLEFDS MKT #10259",
        raw_amount="52.30",
        raw_date=date(2026, 3, 15),
    )

    # The user corrects essentially everything.
    transaction.user_description = "Weekly grocery run"
    transaction.user_amount = Decimal("48.00")
    transaction.user_date = date(2026, 3, 14)
    transaction.user_notes = "Split with roommate"
    db.flush()
    db.expire_all()  # force a real re-read from the database

    reloaded = db.get(Transaction, transaction.id)
    assert reloaded.raw_name == "WHOLEFDS MKT #10259"
    assert reloaded.raw_amount == Decimal("52.30")
    assert reloaded.raw_date == date(2026, 3, 15)

    # ...while the app displays the corrections.
    assert reloaded.effective_amount == Decimal("48.00")
    assert reloaded.effective_date == date(2026, 3, 14)
    assert reloaded.effective_description == "Weekly grocery run"


def test_clearing_an_override_restores_the_original(db: Session, account: Account):
    """'Reset to original' is just setting the user_ column back to NULL.

    Because the raw value was never modified, this is guaranteed to work --
    there is no backup to restore and nothing that can have gone stale.
    """
    transaction = make_transaction(db, account, raw_amount="52.30")
    transaction.user_amount = Decimal("48.00")
    db.flush()
    assert transaction.effective_amount == Decimal("48.00")

    transaction.user_amount = None
    db.flush()

    assert transaction.effective_amount == Decimal("52.30")


def test_is_user_modified_flag(db: Session, account: Account):
    transaction = make_transaction(db, account)
    assert transaction.is_user_modified is False

    transaction.user_notes = "a note"  # notes alone are not a data correction
    assert transaction.is_user_modified is False

    transaction.user_category_id = _category(db, "Groceries").id
    assert transaction.is_user_modified is True


# ---------------------------------------------------------------------------
# The effective_* values must work in SQL, not just in Python
# ---------------------------------------------------------------------------


def test_effective_amount_is_usable_in_a_database_query(db: Session, account: Account):
    """This is why `effective_amount` is a hybrid_property.

    If it only existed in Python, "sum my spending" would have to load every
    transaction into memory first. With the SQL half, PostgreSQL does the work
    -- which is the difference between a dashboard that loads in 50ms and one
    that falls over at 50,000 transactions.
    """
    make_transaction(db, account, raw_amount="100.00")
    make_transaction(db, account, raw_amount="200.00", user_amount=Decimal("20.00"))
    db.flush()

    rows = db.execute(
        select(Transaction.effective_amount)
        .where(Transaction.account_id == account.id)
        .order_by(Transaction.effective_amount)
    ).scalars().all()

    assert rows == [Decimal("20.00"), Decimal("100.00")]


def test_effective_category_filtering_in_sql(db: Session, account: Account):
    """Filtering by effective category must find rows whose category came
    from EITHER layer -- that is what COALESCE in the expression buys us."""
    groceries = _category(db, "Groceries")

    auto_only = make_transaction(db, account, auto_category_id=groceries.id)
    user_only = make_transaction(db, account, user_category_id=groceries.id)
    make_transaction(db, account)  # uncategorized
    db.flush()

    found = db.execute(
        select(Transaction.id).where(
            Transaction.account_id == account.id,
            Transaction.effective_category_id == groceries.id,
        )
    ).scalars().all()

    assert set(found) == {auto_only.id, user_only.id}


def test_effective_description_falls_back_through_all_layers(
    db: Session, account: Account
):
    """raw_name is the last resort, raw_merchant_name is better, and the
    user's own wording wins."""
    transaction = make_transaction(
        db, account, raw_name="WHOLEFDS MKT #10259", raw_merchant_name=None
    )
    assert transaction.effective_description == "WHOLEFDS MKT #10259"

    transaction.raw_merchant_name = "Whole Foods"
    assert transaction.effective_description == "Whole Foods"

    transaction.user_description = "Groceries for the week"
    assert transaction.effective_description == "Groceries for the week"

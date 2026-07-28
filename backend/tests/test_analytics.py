"""Analytics tests.

Aggregation bugs are quiet: the page renders, the number looks plausible, and
it is wrong. Each test below pins one rule that a plausible-looking query
would break.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountBalance,
    AccountType,
    Budget,
    Category,
    Merchant,
    Transaction,
    TransactionStatus,
    User,
)
from app.services import analytics
from tests.conftest import make_transaction

MAY = dt.date(2026, 5, 1)
MAY_END = dt.date(2026, 5, 31)
APRIL = dt.date(2026, 4, 1)
APRIL_END = dt.date(2026, 4, 30)


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


def spend(
    db: Session,
    account: Account,
    amount: str,
    *,
    day: dt.date = dt.date(2026, 5, 15),
    category_slug: str | None = None,
    name: str = "SHOP",
    merchant: Merchant | None = None,
) -> Transaction:
    txn = make_transaction(
        db,
        account,
        plaid_transaction_id=f"a_{uuid.uuid4().hex[:12]}",
        raw_name=name,
        raw_amount=amount,
        raw_date=day,
    )
    if category_slug:
        txn.auto_category_id = category(db, category_slug).id
    if merchant:
        txn.auto_merchant_id = merchant.id
    db.flush()
    return txn


# ---------------------------------------------------------------------------
# The three rules every query obeys
# ---------------------------------------------------------------------------


def test_transfers_are_excluded_from_spending(
    db: Session, user: User, account: Account
):
    """THE most consequential exclusion.

    Paying a credit card moves money between your own accounts. Counting it
    as spending double-counts every purchase that card already recorded --
    silently inflating every total on the dashboard.
    """
    spend(db, account, "100.00", category_slug="food_and_drink.groceries")
    spend(db, account, "2000.00", category_slug="transfer.credit_card_payment")

    summaries = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert len(summaries) == 1
    assert summaries[0].spending == Decimal("100.00")


def test_income_is_separated_not_netted(db: Session, user: User, account: Account):
    """Summing everything together nets salary against groceries and produces
    a number that means nothing."""
    spend(db, account, "100.00", category_slug="food_and_drink.groceries")
    spend(db, account, "-3000.00", category_slug="income.salary")

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.spending == Decimal("100.00")
    assert summary.income == Decimal("3000.00")
    assert summary.net == Decimal("2900.00")


def test_analytics_respect_user_corrections(
    db: Session, user: User, account: Account
):
    """A dashboard that ignores corrections is confidently wrong, and the
    user can see it."""
    txn = spend(db, account, "500.00", category_slug="food_and_drink.groceries")
    txn.user_amount = Decimal("50.00")
    db.flush()

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.spending == Decimal("50.00")


def test_a_recategorization_moves_the_money(db: Session, user: User, account: Account):
    txn = spend(db, account, "100.00", category_slug="food_and_drink.groceries")
    txn.user_category_id = category(db, "travel").id
    db.flush()

    totals = {
        c.category_name: c.total
        for c in analytics.spending_by_category(db, user, start=MAY, end=MAY_END)
    }

    assert totals == {"Travel": Decimal("100.00")}


def test_removed_and_hidden_transactions_are_excluded(
    db: Session, user: User, account: Account
):
    spend(db, account, "100.00")
    removed = spend(db, account, "999.00")
    removed.status = TransactionStatus.REMOVED
    hidden = spend(db, account, "888.00")
    hidden.is_hidden = True
    db.flush()

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.spending == Decimal("100.00")


def test_uncategorized_spending_still_counts(
    db: Session, user: User, account: Account
):
    """Hiding uncategorized money until someone labels it would make every
    total quietly understate reality."""
    spend(db, account, "75.00")  # no category

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)
    categories = analytics.spending_by_category(db, user, start=MAY, end=MAY_END)

    assert summary.spending == Decimal("75.00")
    assert categories[0].category_name == "Uncategorized"


def test_another_users_data_is_never_included(
    db: Session, user: User, account: Account, other_user
):
    """The single test that would catch a missing user filter in aggregation.

    With one user in the database, an unscoped SUM returns the right answer.
    """
    stranger, _ = other_user
    from tests.conftest import make_account

    spend(db, account, "100.00")
    spend(db, make_account(db, stranger), "50000.00")

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.spending == Decimal("100.00")


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_months_are_grouped_separately(db: Session, user: User, account: Account):
    spend(db, account, "100.00", day=dt.date(2026, 4, 10))
    spend(db, account, "200.00", day=dt.date(2026, 5, 10))
    spend(db, account, "300.00", day=dt.date(2026, 5, 20))

    summaries = analytics.monthly_summary(db, user, start=APRIL, end=MAY_END)

    assert [(s.period_start, s.spending) for s in summaries] == [
        (APRIL, Decimal("100.00")),
        (MAY, Decimal("500.00")),
    ]


def test_month_boundaries_are_inclusive(db: Session, user: User, account: Account):
    """The first and last day of the month belong to that month.

    Off-by-one on a range boundary is the classic aggregation bug, and it
    hides: the total is only wrong on two days out of thirty.
    """
    spend(db, account, "10.00", day=MAY)
    spend(db, account, "20.00", day=MAY_END)

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.spending == Decimal("30.00")


def test_savings_rate(db: Session, user: User, account: Account):
    spend(db, account, "-1000.00", category_slug="income.salary")
    spend(db, account, "250.00", category_slug="food_and_drink.groceries")

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.savings_rate == Decimal("0.75")


def test_savings_rate_is_none_without_income(
    db: Session, user: User, account: Account
):
    """None, not zero. A month with no income has an undefined savings rate;
    "0%" would suggest they spent everything they earned."""
    spend(db, account, "250.00")

    [summary] = analytics.monthly_summary(db, user, start=MAY, end=MAY_END)

    assert summary.savings_rate is None


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


def test_merchant_totals_and_averages(db: Session, user: User, account: Account):
    merchant = Merchant(
        user_id=None, normalized_name="starbucks", display_name="Starbucks"
    )
    db.add(merchant)
    db.flush()

    for amount in ("4.00", "6.00", "8.00"):
        spend(db, account, amount, merchant=merchant)

    [top] = analytics.spending_by_merchant(db, user, start=MAY, end=MAY_END)

    assert top.merchant_name == "Starbucks"
    assert top.total == Decimal("18.00")
    assert top.transaction_count == 3
    assert top.average == Decimal("6.00")


def test_category_change_finds_the_biggest_increase(
    db: Session, user: User, account: Account
):
    """The query behind "why did I spend more this month?"."""
    spend(db, account, "100.00", day=APRIL + dt.timedelta(days=5), category_slug="travel")
    spend(db, account, "500.00", day=MAY + dt.timedelta(days=5), category_slug="travel")
    spend(
        db,
        account,
        "200.00",
        day=APRIL + dt.timedelta(days=6),
        category_slug="food_and_drink.groceries",
    )
    spend(
        db,
        account,
        "150.00",
        day=MAY + dt.timedelta(days=6),
        category_slug="food_and_drink.groceries",
    )

    changes = analytics.category_change(
        db,
        user,
        current_start=MAY,
        current_end=MAY_END,
        previous_start=APRIL,
        previous_end=APRIL_END,
    )

    biggest, delta = changes[0]
    assert biggest.category_name == "Travel"
    assert delta == Decimal("400.00")

    smallest, delta = changes[-1]
    assert smallest.category_name == "Groceries"
    assert delta == Decimal("-50.00")


def test_a_category_new_this_month_appears_as_an_increase(
    db: Session, user: User, account: Account
):
    """Present in one period and absent from the other -- the case a Python
    join of two separate queries usually gets wrong."""
    spend(db, account, "300.00", day=MAY + dt.timedelta(days=5), category_slug="travel")

    changes = analytics.category_change(
        db,
        user,
        current_start=MAY,
        current_end=MAY_END,
        previous_start=APRIL,
        previous_end=APRIL_END,
    )

    biggest, delta = changes[0]
    assert biggest.category_name == "Travel"
    assert delta == Decimal("300.00")


def test_largest_transactions(db: Session, user: User, account: Account):
    for amount in ("10.00", "500.00", "50.00"):
        spend(db, account, amount)

    rows = analytics.largest_transactions(db, user, start=MAY, end=MAY_END, limit=2)

    assert [r.effective_amount for r in rows] == [Decimal("500.00"), Decimal("50.00")]


# ---------------------------------------------------------------------------
# Net worth
# ---------------------------------------------------------------------------


def test_liabilities_are_subtracted(db: Session, user: User, utc_now):
    """Getting this backwards inflates net worth by twice the card balance."""
    checking = Account(
        user_id=user.id, name="Checking", type=AccountType.DEPOSITORY,
        plaid_account_id=f"c_{uuid.uuid4().hex[:8]}",
    )
    card = Account(
        user_id=user.id, name="Card", type=AccountType.CREDIT,
        plaid_account_id=f"k_{uuid.uuid4().hex[:8]}",
    )
    db.add_all([checking, card])
    db.flush()

    db.add_all(
        [
            AccountBalance(
                account_id=checking.id, as_of=utc_now, current_balance=Decimal("5000")
            ),
            AccountBalance(
                account_id=card.id, as_of=utc_now, current_balance=Decimal("1500")
            ),
        ]
    )
    db.flush()

    [point] = analytics.net_worth_series(
        db, user, start=utc_now.date() - dt.timedelta(days=1), end=utc_now.date()
    )

    assert point.assets == Decimal("5000.0000")
    assert point.liabilities == Decimal("1500.0000")
    assert point.net_worth == Decimal("3500.0000")


def test_multiple_snapshots_in_one_day_are_not_double_counted(
    db: Session, user: User, utc_now
):
    """Syncs do not run on a tidy schedule.

    Three syncs on Monday would triple-count Monday if the query summed every
    snapshot. DISTINCT ON takes the last per account per day.
    """
    checking = Account(
        user_id=user.id, name="Checking", type=AccountType.DEPOSITORY,
        plaid_account_id=f"c_{uuid.uuid4().hex[:8]}",
    )
    db.add(checking)
    db.flush()

    for hours, balance in ((3, "1000"), (2, "1100"), (1, "1234")):
        db.add(
            AccountBalance(
                account_id=checking.id,
                as_of=utc_now - dt.timedelta(hours=hours),
                current_balance=Decimal(balance),
            )
        )
    db.flush()

    points = analytics.net_worth_series(
        db, user, start=utc_now.date() - dt.timedelta(days=1), end=utc_now.date()
    )

    assert len(points) == 1
    # The LAST snapshot of the day, not the sum and not the first.
    assert points[0].assets == Decimal("1234.0000")


def test_accounts_excluded_from_net_worth_are_ignored(
    db: Session, user: User, utc_now
):
    excluded = Account(
        user_id=user.id,
        name="Business",
        type=AccountType.DEPOSITORY,
        include_in_net_worth=False,
        plaid_account_id=f"b_{uuid.uuid4().hex[:8]}",
    )
    db.add(excluded)
    db.flush()
    db.add(
        AccountBalance(
            account_id=excluded.id, as_of=utc_now, current_balance=Decimal("99999")
        )
    )
    db.flush()

    points = analytics.net_worth_series(
        db, user, start=utc_now.date() - dt.timedelta(days=1), end=utc_now.date()
    )

    assert points == []


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_budget_vs_actual(db: Session, user: User, account: Account):
    groceries = category(db, "food_and_drink.groceries")
    db.add(
        Budget(
            user_id=user.id,
            category_id=groceries.id,
            period_start=MAY,
            amount=Decimal("600"),
        )
    )
    spend(db, account, "250.00", category_slug="food_and_drink.groceries")

    [comparison] = analytics.budget_vs_actual(db, user, period_start=MAY)

    assert comparison.budgeted == Decimal("600.0000")
    assert comparison.actual == Decimal("250.00")
    assert comparison.remaining == Decimal("350.0000")
    assert comparison.used_fraction.quantize(Decimal("0.01")) == Decimal("0.42")


def test_a_budget_with_no_spending_still_appears(db: Session, user: User):
    """Showing $0 of $600 is the confirmation the user wants.

    A join the other way round would hide exactly the categories they are
    checking on.
    """
    groceries = category(db, "food_and_drink.groceries")
    db.add(
        Budget(
            user_id=user.id,
            category_id=groceries.id,
            period_start=MAY,
            amount=Decimal("600"),
        )
    )
    db.flush()

    [comparison] = analytics.budget_vs_actual(db, user, period_start=MAY)

    assert comparison.actual == Decimal("0")


def test_spending_in_another_month_does_not_count_against_this_budget(
    db: Session, user: User, account: Account
):
    groceries = category(db, "food_and_drink.groceries")
    db.add(
        Budget(
            user_id=user.id,
            category_id=groceries.id,
            period_start=MAY,
            amount=Decimal("600"),
        )
    )
    spend(
        db,
        account,
        "400.00",
        day=dt.date(2026, 4, 15),
        category_slug="food_and_drink.groceries",
    )

    [comparison] = analytics.budget_vs_actual(db, user, period_start=MAY)

    assert comparison.actual == Decimal("0")


# ---------------------------------------------------------------------------
# Recurring detection
# ---------------------------------------------------------------------------


def test_a_monthly_subscription_is_detected(db: Session, user: User, account: Account):
    netflix = Merchant(user_id=None, normalized_name="netflix", display_name="Netflix")
    db.add(netflix)
    db.flush()

    for month in range(1, 6):
        spend(
            db,
            account,
            "15.99",
            day=dt.date(2026, month, 5),
            merchant=netflix,
            name="NETFLIX.COM",
        )

    charges = analytics.recurring_charges(db, user, since=dt.date(2025, 1, 1))

    assert len(charges) == 1
    assert charges[0].merchant_name == "Netflix"
    assert charges[0].typical_amount == Decimal("15.99")
    assert charges[0].occurrences == 5
    assert 28 <= charges[0].average_gap_days <= 32


def test_a_variable_amount_is_not_treated_as_a_subscription(
    db: Session, user: User, account: Account
):
    """A coffee shop you visit monthly is not a subscription.

    The amount spread filter is what separates the two.
    """
    cafe = Merchant(user_id=None, normalized_name="cafe", display_name="Cafe")
    db.add(cafe)
    db.flush()

    for month, amount in enumerate(("4.00", "12.50", "7.25", "19.00", "5.50"), start=1):
        spend(db, account, amount, day=dt.date(2026, month, 5), merchant=cafe)

    assert analytics.recurring_charges(db, user, since=dt.date(2025, 1, 1)) == []


def test_too_few_occurrences_is_not_a_subscription(
    db: Session, user: User, account: Account
):
    """Two charges are not yet a pattern.

    A genuine limitation, stated in the code: a subscription paid twice so far
    will not be detected. Guessing from two points would produce more noise
    than signal.
    """
    gym = Merchant(user_id=None, normalized_name="gym", display_name="Gym")
    db.add(gym)
    db.flush()

    for month in (1, 2):
        spend(db, account, "40.00", day=dt.date(2026, month, 5), merchant=gym)

    assert analytics.recurring_charges(db, user, since=dt.date(2025, 1, 1)) == []


def test_daily_charges_are_not_subscriptions(
    db: Session, user: User, account: Account
):
    """A gap of one day is a habit, not a billing cycle."""
    cafe = Merchant(user_id=None, normalized_name="cafe", display_name="Cafe")
    db.add(cafe)
    db.flush()

    for day in range(1, 15):
        spend(db, account, "5.00", day=dt.date(2026, 5, day), merchant=cafe)

    assert analytics.recurring_charges(db, user, since=dt.date(2025, 1, 1)) == []


# ---------------------------------------------------------------------------
# Burn rate and runway
# ---------------------------------------------------------------------------


def test_runway_is_none_when_not_burning(db: Session, user: User, account: Account):
    """None, not a huge number. If income exceeds spending there is no
    runway to run out of, and a large number would be a lie dressed as
    precision."""
    today = dt.date.today()
    last_month = analytics._add_months(analytics._start_of_month(today), -1)

    spend(db, account, "-5000.00", day=last_month, category_slug="income.salary")
    spend(db, account, "100.00", day=last_month)

    assert analytics.cash_runway_months(db, user) is None


def test_burn_rate_excludes_the_current_month(
    db: Session, user: User, account: Account
):
    """On the 3rd, this month's spending is a fraction of a normal month.

    Including it would drag the average down and overstate the runway --
    exactly when an accurate number matters most.
    """
    today = dt.date.today()
    this_month = analytics._start_of_month(today)
    last_month = analytics._add_months(this_month, -1)

    spend(db, account, "1000.00", day=last_month)
    spend(db, account, "5.00", day=this_month)

    assert analytics.burn_rate(db, user, months=1) == Decimal("1000.00")


def test_liquid_balance_excludes_investments_and_credit(db: Session, user: User):
    """Treating investments as runway is how people conclude they have six
    months of cushion when they have six weeks."""
    db.add_all(
        [
            Account(
                user_id=user.id, name="Checking", type=AccountType.DEPOSITORY,
                current_balance=Decimal("3000"),
                plaid_account_id=f"c_{uuid.uuid4().hex[:8]}",
            ),
            Account(
                user_id=user.id, name="Brokerage", type=AccountType.INVESTMENT,
                current_balance=Decimal("50000"),
                plaid_account_id=f"i_{uuid.uuid4().hex[:8]}",
            ),
            Account(
                user_id=user.id, name="Card", type=AccountType.CREDIT,
                current_balance=Decimal("900"),
                plaid_account_id=f"k_{uuid.uuid4().hex[:8]}",
            ),
        ]
    )
    db.flush()

    assert analytics.liquid_balance(db, user) == Decimal("3000.0000")

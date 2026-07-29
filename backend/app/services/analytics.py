"""Analytics: aggregation done in SQL, not in Python.

===========================================================================
The decision this whole module rests on
===========================================================================

The obvious way to compute "spending by category last month" is to load the
transactions and add them up in Python:

    rows = db.execute(select(Transaction).where(...)).scalars().all()
    totals = defaultdict(Decimal)
    for row in rows:
        totals[row.effective_category_id] += row.effective_amount

This works. It is also the single most common reason financial dashboards
become unusable, for three compounding reasons:

  1. Every row crosses the network from PostgreSQL to Python.
  2. Every row becomes a Python object -- hundreds of bytes each, versus the
     handful of bytes PostgreSQL needs to add a number to a running total.
  3. Memory grows with your history rather than with the size of the answer.
     A year of data is fine. Five years is not.

Pushing the same work into SQL sends one query and receives one row per
category. The database adds the numbers where they already live.

===========================================================================
Measured, not assumed
===========================================================================

`scripts/benchmark_analytics.py` builds a scratch database and times both
implementations. On this machine, spending-by-category over three years:

    rows      SQL      Python     ratio
    10,000    8 ms     367 ms      45x
    25,000   18 ms     880 ms      49x
    100,000  76 ms   3,884 ms      51x

Both scale roughly linearly, so the RATIO is broadly constant -- around 50x.
What changes with size is the absolute number, and that is what decides
whether the product is usable: at 100,000 transactions the Python version
takes nearly four seconds to render one panel of one page, while SQL stays
under a tenth of a second.

Re-run it yourself rather than trusting these figures:

    python scripts/benchmark_analytics.py --rows 100000

===========================================================================
Three rules every query here obeys
===========================================================================

**1. Transfers are excluded from spending.**
   Paying your credit card moves money between your own accounts. Counting it
   as spending double-counts every purchase that card already recorded. This
   is what `Category.is_transfer` exists for.

**2. Income is separated, not negated.**
   Income has a negative amount under Plaid's convention. Summing everything
   together silently nets salary against groceries and produces a number that
   means nothing.

**3. Effective values, always.**
   `COALESCE(user_amount, raw_amount)`, never `raw_amount`. A dashboard that
   ignores the user's own corrections is worse than no dashboard: it is
   confidently wrong, and the user can see it.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, and_, func, literal, or_, select
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

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def _base_transactions(user: User) -> Select:
    """Every analytics query starts here.

    Centralizing the exclusions matters: if one query forgets to exclude
    removed transactions, its number disagrees with every other number on the
    page and the user has no way to tell which is right.
    """
    return select(Transaction).where(
        Transaction.user_id == user.id,
        Transaction.status != TransactionStatus.REMOVED,
        Transaction.is_hidden.is_(False),
    )


def _effective_category_join():
    """Join to the effective category.

    `effective_category_id` is COALESCE(user, auto), so the join has to be an
    outer join on that expression rather than on a plain column -- an inner
    join would silently drop every uncategorized transaction, and
    "uncategorized" is usually the number the user most wants to see.
    """
    return Category, Category.id == Transaction.effective_category_id


def _is_spending():
    """Money genuinely leaving the household.

    Excludes transfers between your own accounts and anything categorized as
    income. Uncategorized transactions with a positive amount DO count -- they
    are real money, and hiding them until they are categorized would make the
    dashboard quietly understate every total.
    """
    return and_(
        Transaction.effective_amount > 0,
        or_(Category.is_transfer.is_(False), Category.is_transfer.is_(None)),
        or_(Category.is_income.is_(False), Category.is_income.is_(None)),
    )


def _is_income():
    """Money arriving. Negative under Plaid's convention, hence the flip."""
    return and_(
        Category.is_income.is_(True),
        or_(Category.is_transfer.is_(False), Category.is_transfer.is_(None)),
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class PeriodSummary:
    period_start: dt.date
    spending: Decimal
    income: Decimal

    @property
    def net(self) -> Decimal:
        return self.income - self.spending

    @property
    def savings_rate(self) -> Decimal | None:
        """Fraction of income kept. None when there was no income.

        Returning None rather than 0 matters: a month with no income has an
        undefined savings rate, and showing "0%" would suggest the user spent
        everything they earned rather than that the question does not apply.
        """
        if self.income <= 0:
            return None
        return (self.income - self.spending) / self.income


@dataclass
class CategoryTotal:
    category_id: uuid.UUID | None
    category_name: str
    total: Decimal
    transaction_count: int


@dataclass
class MerchantTotal:
    merchant_id: uuid.UUID | None
    merchant_name: str
    total: Decimal
    transaction_count: int
    average: Decimal


@dataclass
class NetWorthPoint:
    as_of: dt.date
    assets: Decimal
    liabilities: Decimal

    @property
    def net_worth(self) -> Decimal:
        return self.assets - self.liabilities


@dataclass
class BudgetComparison:
    category_id: uuid.UUID
    category_name: str
    budgeted: Decimal
    actual: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.budgeted - self.actual

    @property
    def used_fraction(self) -> Decimal | None:
        if self.budgeted <= 0:
            return None
        return self.actual / self.budgeted


@dataclass
class RecurringCharge:
    merchant_id: uuid.UUID | None
    merchant_name: str
    typical_amount: Decimal
    occurrences: int
    average_gap_days: float
    last_seen: dt.date


# ---------------------------------------------------------------------------
# Monthly cash flow
# ---------------------------------------------------------------------------


def monthly_summary(
    db: Session, user: User, *, start: dt.date, end: dt.date
) -> list[PeriodSummary]:
    """Spending and income per calendar month.

    `date_trunc('month', ...)` groups in the database. The alternative --
    grouping in Python -- means transferring every row to compute a dozen
    numbers.

    Note both aggregates are computed in ONE pass using FILTER, rather than
    running two queries. PostgreSQL scans the rows once and maintains two
    running totals.
    """
    month = func.date_trunc("month", Transaction.effective_date).label("month")

    statement = (
        select(
            month,
            func.coalesce(
                func.sum(Transaction.effective_amount).filter(_is_spending()), ZERO
            ).label("spending"),
            # Income amounts are negative; negate so the number reads as
            # "money in" rather than requiring the caller to remember.
            func.coalesce(
                -func.sum(Transaction.effective_amount).filter(_is_income()), ZERO
            ).label("income"),
        )
        .select_from(Transaction)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date >= start,
            Transaction.effective_date <= end,
        )
        .group_by(month)
        .order_by(month)
    )

    return [
        PeriodSummary(
            period_start=row.month.date(),
            spending=row.spending or ZERO,
            income=row.income or ZERO,
        )
        for row in db.execute(statement)
    ]


def spending_between(
    db: Session, user: User, *, start: dt.date, end: dt.date
) -> Decimal:
    """Total spending in a window. Used for rolling 30/90-day figures."""
    statement = (
        select(func.coalesce(func.sum(Transaction.effective_amount), ZERO))
        .select_from(Transaction)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date >= start,
            Transaction.effective_date <= end,
            _is_spending(),
        )
    )
    return db.execute(statement).scalar_one() or ZERO


# ---------------------------------------------------------------------------
# Category and merchant breakdowns
# ---------------------------------------------------------------------------


def spending_by_category(
    db: Session, user: User, *, start: dt.date, end: dt.date, limit: int = 50
) -> list[CategoryTotal]:
    """Spending grouped by effective category, largest first."""
    statement = (
        select(
            Transaction.effective_category_id.label("category_id"),
            func.coalesce(func.max(Category.name), literal("Uncategorized")).label(
                "category_name"
            ),
            func.sum(Transaction.effective_amount).label("total"),
            func.count().label("transaction_count"),
        )
        .select_from(Transaction)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date >= start,
            Transaction.effective_date <= end,
            _is_spending(),
        )
        .group_by(Transaction.effective_category_id)
        .order_by(func.sum(Transaction.effective_amount).desc())
        .limit(limit)
    )

    return [
        CategoryTotal(
            category_id=row.category_id,
            category_name=row.category_name,
            total=row.total or ZERO,
            transaction_count=row.transaction_count,
        )
        for row in db.execute(statement)
    ]


def spending_by_merchant(
    db: Session, user: User, *, start: dt.date, end: dt.date, limit: int = 20
) -> list[MerchantTotal]:
    """Top merchants, with count and average transaction size.

    Average is computed by the database rather than as total/count in Python.
    Same result, but it keeps the whole calculation in one place and one pass.
    """
    merchant_id = Transaction.effective_merchant_id

    statement = (
        select(
            merchant_id.label("merchant_id"),
            func.coalesce(func.max(Merchant.display_name), literal("Unknown")).label(
                "merchant_name"
            ),
            func.sum(Transaction.effective_amount).label("total"),
            func.count().label("transaction_count"),
            func.avg(Transaction.effective_amount).label("average"),
        )
        .select_from(Transaction)
        .outerjoin(Merchant, Merchant.id == merchant_id)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date >= start,
            Transaction.effective_date <= end,
            _is_spending(),
        )
        .group_by(merchant_id)
        .order_by(func.sum(Transaction.effective_amount).desc())
        .limit(limit)
    )

    return [
        MerchantTotal(
            merchant_id=row.merchant_id,
            merchant_name=row.merchant_name,
            total=row.total or ZERO,
            transaction_count=row.transaction_count,
            average=Decimal(row.average or 0).quantize(Decimal("0.01")),
        )
        for row in db.execute(statement)
    ]


def category_change(
    db: Session,
    user: User,
    *,
    current_start: dt.date,
    current_end: dt.date,
    previous_start: dt.date,
    previous_end: dt.date,
) -> list[tuple[CategoryTotal, Decimal]]:
    """Category spending this period versus last, sorted by biggest increase.

    This is the query behind "why did I spend more this month?" -- and the one
    the AI milestone will lean on hardest.

    Two aggregates over different date ranges in a single pass, using FILTER.
    Running two queries and joining the results in Python would be slower and,
    more importantly, would have to handle categories present in one period
    and absent from the other -- a source of subtle off-by-one bugs that this
    approach avoids entirely.
    """
    current = func.coalesce(
        func.sum(Transaction.effective_amount).filter(
            Transaction.effective_date.between(current_start, current_end)
        ),
        ZERO,
    )
    previous = func.coalesce(
        func.sum(Transaction.effective_amount).filter(
            Transaction.effective_date.between(previous_start, previous_end)
        ),
        ZERO,
    )

    statement = (
        select(
            Transaction.effective_category_id.label("category_id"),
            func.coalesce(func.max(Category.name), literal("Uncategorized")).label(
                "category_name"
            ),
            current.label("current_total"),
            previous.label("previous_total"),
            func.count()
            .filter(Transaction.effective_date.between(current_start, current_end))
            .label("current_count"),
        )
        .select_from(Transaction)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date.between(previous_start, current_end),
            _is_spending(),
        )
        .group_by(Transaction.effective_category_id)
        .order_by((current - previous).desc())
    )

    return [
        (
            CategoryTotal(
                category_id=row.category_id,
                category_name=row.category_name,
                total=row.current_total or ZERO,
                transaction_count=row.current_count,
            ),
            (row.current_total or ZERO) - (row.previous_total or ZERO),
        )
        for row in db.execute(statement)
    ]


def largest_transactions(
    db: Session, user: User, *, start: dt.date, end: dt.date, limit: int = 10
) -> list[Transaction]:
    statement = (
        _base_transactions(user)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.effective_date >= start,
            Transaction.effective_date <= end,
            _is_spending(),
        )
        .order_by(Transaction.effective_amount.desc())
        .limit(limit)
    )
    return list(db.execute(statement).scalars().unique().all())


def search_transactions(
    db: Session,
    user: User,
    *,
    start: dt.date,
    end: dt.date,
    category_slug: str | None = None,
    merchant_name: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    limit: int = 25,
) -> list[Transaction]:
    """Filtered transaction lookup: "restaurants over $100 last month".

    Every filter is optional and every one is applied in SQL. The `limit` is
    not optional -- an unbounded result set is how a question about one month
    turns into thirty thousand rows.

    `category_slug` matches the category *or any of its children*, because a
    user who asks about "food and drink" means the whole subtree, not the
    handful of transactions that landed on the parent node itself.
    """
    statement = (
        _base_transactions(user)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.effective_date >= start,
            Transaction.effective_date <= end,
        )
    )

    if category_slug:
        statement = statement.where(
            or_(
                Category.slug == category_slug,
                Category.slug.startswith(f"{category_slug}."),
            )
        )

    if merchant_name:
        # ILIKE, not a regex: a user-supplied regular expression is a denial
        # of service waiting to happen (the same reasoning as `rules.py`).
        statement = statement.where(
            Transaction.effective_description.ilike(f"%{merchant_name}%")
        )

    if min_amount is not None:
        statement = statement.where(Transaction.effective_amount >= min_amount)

    if max_amount is not None:
        statement = statement.where(Transaction.effective_amount <= max_amount)

    statement = statement.order_by(Transaction.effective_date.desc()).limit(limit)

    return list(db.execute(statement).scalars().unique().all())


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


def forecast_next_month(
    db: Session, user: User, *, months: int = 3
) -> tuple[Decimal, int]:
    """Projected spending next month: the mean of recent COMPLETE months.

    Returns (projection, months_used) so the caller can say how much history
    the figure rests on -- a forecast from one month is a very different
    object from a forecast from six, and presenting them identically would be
    dishonest.

    ===================================================================
    What this is, and what it is not
    ===================================================================

    It is an average. It is not a model. It carries no trend, no seasonality,
    no awareness that December exists, and no confidence interval. A user who
    took a holiday last month will see that holiday projected into next month.

    That is a deliberate choice rather than a shortcut. The alternative --
    fitting a trend to a handful of noisy monthly totals -- produces a figure
    that *looks* far more authoritative while being barely more accurate, and
    on a personal finance dashboard, unearned authority is the more expensive
    error. A mean is something the user can verify in their head against the
    cash-flow chart sitting next to it.

    The current month is excluded for the same reason as `burn_rate`: on the
    3rd it is a fraction of a month and would drag the average down.
    """
    today = dt.date.today()
    end = _start_of_month(today) - dt.timedelta(days=1)
    start = _start_of_month(_add_months(end, -(months - 1)))

    summaries = monthly_summary(db, user, start=start, end=end)
    if not summaries:
        return ZERO, 0

    total = sum((summary.spending for summary in summaries), ZERO)
    return (total / len(summaries)).quantize(Decimal("0.01")), len(summaries)


# ---------------------------------------------------------------------------
# Net worth
# ---------------------------------------------------------------------------


def net_worth_series(
    db: Session, user: User, *, start: dt.date, end: dt.date
) -> list[NetWorthPoint]:
    """Net worth over time, from the balance snapshots collected on every sync.

    The subtlety: syncs do not happen on a tidy schedule, so an account may
    have three snapshots on Monday and none on Tuesday. Summing every snapshot
    per day would triple-count Monday.

    `DISTINCT ON (account, day)` with an ORDER BY takes the LAST snapshot per
    account per day, which is the one that reflects the day's closing state.
    This is a PostgreSQL-specific feature and a genuinely good reason to be
    using PostgreSQL: the portable equivalent is a window function plus a
    subquery, and it is considerably harder to read.
    """
    day = func.date(AccountBalance.as_of).label("day")

    latest_per_day = (
        select(
            day,
            AccountBalance.account_id,
            AccountBalance.current_balance,
            Account.type.label("account_type"),
        )
        .select_from(AccountBalance)
        .join(Account, Account.id == AccountBalance.account_id)
        .where(
            Account.user_id == user.id,
            Account.include_in_net_worth.is_(True),
            func.date(AccountBalance.as_of) >= start,
            func.date(AccountBalance.as_of) <= end,
        )
        .distinct(day, AccountBalance.account_id)
        .order_by(day, AccountBalance.account_id, AccountBalance.as_of.desc())
        .subquery()
    )

    # Credit and loan balances are money OWED. Getting this backwards inflates
    # net worth by twice the card balance -- a mistake that looks plausible
    # right up until someone reconciles it.
    is_liability = latest_per_day.c.account_type.in_(
        [AccountType.CREDIT.value, AccountType.LOAN.value]
    )

    statement = (
        select(
            latest_per_day.c.day,
            func.coalesce(
                func.sum(latest_per_day.c.current_balance).filter(~is_liability), ZERO
            ).label("assets"),
            func.coalesce(
                func.sum(latest_per_day.c.current_balance).filter(is_liability), ZERO
            ).label("liabilities"),
        )
        .group_by(latest_per_day.c.day)
        .order_by(latest_per_day.c.day)
    )

    return [
        NetWorthPoint(
            as_of=row.day,
            assets=row.assets or ZERO,
            liabilities=row.liabilities or ZERO,
        )
        for row in db.execute(statement)
    ]


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def budget_vs_actual(
    db: Session, user: User, *, period_start: dt.date
) -> list[BudgetComparison]:
    """Compare each budget to what was actually spent that month.

    A LEFT JOIN from budgets, so a category with a budget and no spending
    still appears -- showing $0 of $600 used. Joining the other way round
    would hide exactly the categories a user most wants confirmation about.
    """
    period_end = _end_of_month(period_start)

    actual = (
        select(
            Transaction.effective_category_id.label("category_id"),
            func.sum(Transaction.effective_amount).label("spent"),
        )
        .select_from(Transaction)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date >= period_start,
            Transaction.effective_date <= period_end,
            _is_spending(),
        )
        .group_by(Transaction.effective_category_id)
        .subquery()
    )

    statement = (
        select(
            Budget.category_id,
            Category.name.label("category_name"),
            Budget.amount.label("budgeted"),
            func.coalesce(actual.c.spent, ZERO).label("actual"),
        )
        .select_from(Budget)
        .join(Category, Category.id == Budget.category_id)
        .outerjoin(actual, actual.c.category_id == Budget.category_id)
        .where(Budget.user_id == user.id, Budget.period_start == period_start)
        .order_by(Category.name)
    )

    return [
        BudgetComparison(
            category_id=row.category_id,
            category_name=row.category_name,
            budgeted=row.budgeted,
            actual=row.actual or ZERO,
        )
        for row in db.execute(statement)
    ]


# ---------------------------------------------------------------------------
# Recurring charges
# ---------------------------------------------------------------------------


def recurring_charges(
    db: Session, user: User, *, since: dt.date, min_occurrences: int = 3
) -> list[RecurringCharge]:
    """Detect subscriptions from spending patterns.

    ===================================================================
    The heuristic, and its honest limits
    ===================================================================

    A charge looks recurring when the same merchant appears at least
    `min_occurrences` times, at a consistent amount, at roughly regular
    intervals. We compute three things per merchant in SQL -- count, amount
    spread, and average gap between charges -- and filter on them.

    What this catches: Netflix, Spotify, gym memberships, rent, insurance.

    What it does NOT catch, and cannot: a subscription you have paid twice so
    far, or one whose price changes every month. What it wrongly flags: a
    coffee shop you visit every Tuesday for the same amount.

    That is acceptable because of how the result is used -- a list the user
    reviews, not an action taken automatically. A heuristic that suggests is
    fine; the same heuristic silently cancelling things would not be.

    The `is_subscription` flag on Merchant lets a user confirm or reject each
    suggestion, which is how this improves over time rather than staying a
    guess forever.
    """
    merchant_id = Transaction.effective_merchant_id

    statement = (
        select(
            merchant_id.label("merchant_id"),
            func.max(Merchant.display_name).label("merchant_name"),
            func.count().label("occurrences"),
            func.avg(Transaction.effective_amount).label("typical_amount"),
            func.stddev_pop(Transaction.effective_amount).label("amount_spread"),
            func.min(Transaction.effective_date).label("first_seen"),
            func.max(Transaction.effective_date).label("last_seen"),
        )
        .select_from(Transaction)
        .join(Merchant, Merchant.id == merchant_id)
        .outerjoin(*_effective_category_join())
        .where(
            Transaction.user_id == user.id,
            Transaction.status != TransactionStatus.REMOVED,
            Transaction.is_hidden.is_(False),
            Transaction.effective_date >= since,
            _is_spending(),
        )
        .group_by(merchant_id)
        .having(func.count() >= min_occurrences)
    )

    results: list[RecurringCharge] = []

    for row in db.execute(statement):
        typical = Decimal(row.typical_amount or 0)
        spread = Decimal(row.amount_spread or 0)

        # A charge whose amount varies by more than 15% of its own size is
        # more likely a shop you visit often than a subscription.
        if typical <= 0 or spread / typical > Decimal("0.15"):
            continue

        span_days = (row.last_seen - row.first_seen).days
        if span_days <= 0:
            continue

        average_gap = span_days / max(row.occurrences - 1, 1)

        # Roughly weekly to roughly quarterly. Outside that range the
        # "interval" is more likely coincidence than a billing cycle.
        if not (5 <= average_gap <= 100):
            continue

        results.append(
            RecurringCharge(
                merchant_id=row.merchant_id,
                merchant_name=row.merchant_name or "Unknown",
                typical_amount=typical.quantize(Decimal("0.01")),
                occurrences=row.occurrences,
                average_gap_days=round(average_gap, 1),
                last_seen=row.last_seen,
            )
        )

    results.sort(key=lambda charge: charge.typical_amount, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Burn rate and runway
# ---------------------------------------------------------------------------


def burn_rate(db: Session, user: User, *, months: int = 3) -> Decimal:
    """Average monthly net outflow over recent COMPLETE months.

    The current month is deliberately excluded. On the 3rd, this month's
    spending is a third of a week's worth, and including it would drag the
    average down and overstate the runway -- exactly when an accurate number
    matters most.
    """
    today = dt.date.today()
    end = _start_of_month(today) - dt.timedelta(days=1)
    start = _start_of_month(_add_months(end, -(months - 1)))

    summaries = monthly_summary(db, user, start=start, end=end)
    if not summaries:
        return ZERO

    total_net = sum((summary.spending - summary.income for summary in summaries), ZERO)
    return (total_net / len(summaries)).quantize(Decimal("0.01"))


def liquid_balance(db: Session, user: User) -> Decimal:
    """Cash available right now: depository accounts only.

    Investments are excluded because they are not spendable without selling,
    and credit is excluded because available credit is not money you have.
    Treating either as runway is how people conclude they have six months of
    cushion when they have six weeks.
    """
    statement = select(
        func.coalesce(func.sum(Account.current_balance), ZERO)
    ).where(
        Account.user_id == user.id,
        Account.is_active.is_(True),
        Account.include_in_net_worth.is_(True),
        Account.type == AccountType.DEPOSITORY,
    )
    return db.execute(statement).scalar_one() or ZERO


def cash_runway_months(db: Session, user: User, *, months: int = 3) -> Decimal | None:
    """How long the cash lasts at the recent burn rate.

    None when burn is zero or negative -- you are not burning, so "runway" has
    no meaning. Returning a huge number instead would be a lie dressed as
    precision.
    """
    burn = burn_rate(db, user, months=months)
    if burn <= 0:
        return None

    return (liquid_balance(db, user) / burn).quantize(Decimal("0.1"))


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _start_of_month(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _end_of_month(value: dt.date) -> dt.date:
    return _add_months(_start_of_month(value), 1) - dt.timedelta(days=1)


def _add_months(value: dt.date, months: int) -> dt.date:
    """Month arithmetic without an extra dependency.

    `timedelta(days=30)` is NOT month arithmetic: applied to January 31 it
    lands in early March, and applied twelve times it drifts by five days.
    Normalizing to day 1 first sidesteps every end-of-month edge case,
    including February.
    """
    month_index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(month_index, 12)
    return dt.date(year, month + 1, 1)

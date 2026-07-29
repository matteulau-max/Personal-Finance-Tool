"""Analytics endpoints."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.db.scoping import scoped_get, scoped_select
from app.models import Account, AccountType, Budget, Category
from app.schemas.analytics import (
    BudgetComparisonResponse,
    BudgetResponse,
    BudgetUpsertRequest,
    CategoryChangeResponse,
    CategoryTotalResponse,
    LargestTransactionResponse,
    MerchantTotalResponse,
    NetWorthPointResponse,
    OverviewResponse,
    PeriodSummaryResponse,
    RecurringChargeResponse,
)
from app.services import analytics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

ZERO = Decimal("0")


def _period(summary: analytics.PeriodSummary) -> PeriodSummaryResponse:
    return PeriodSummaryResponse(
        period_start=summary.period_start,
        spending=summary.spending,
        income=summary.income,
        net=summary.net,
        savings_rate=summary.savings_rate,
    )


@router.get("/overview", response_model=OverviewResponse)
def overview(
    current_user: CurrentUser,
    db: DbSession,
    months: int = Query(default=12, ge=1, le=60),
) -> OverviewResponse:
    """Everything the executive dashboard needs, in one request."""
    today = dt.date.today()
    month_start = analytics._start_of_month(today)
    window_start = analytics._add_months(month_start, -(months - 1))

    previous_month_start = analytics._add_months(month_start, -1)
    previous_month_end = month_start - dt.timedelta(days=1)

    monthly = analytics.monthly_summary(db, current_user, start=window_start, end=today)

    this_month = next(
        (m for m in monthly if m.period_start == month_start),
        analytics.PeriodSummary(month_start, ZERO, ZERO),
    )

    changes = analytics.category_change(
        db,
        current_user,
        current_start=month_start,
        current_end=today,
        previous_start=previous_month_start,
        previous_end=previous_month_end,
    )

    net_worth_points = analytics.net_worth_series(
        db, current_user, start=window_start, end=today
    )

    accounts = (
        db.execute(scoped_select(Account, current_user).where(Account.is_active.is_(True)))
        .scalars()
        .all()
    )
    latest_net_worth = (
        net_worth_points[-1].net_worth if net_worth_points else _net_worth_now(accounts)
    )

    return OverviewResponse(
        as_of=today,
        net_worth=latest_net_worth,
        liquid_balance=analytics.liquid_balance(db, current_user),
        credit_utilization=_overall_utilization(accounts),
        month_to_date_spending=this_month.spending,
        month_to_date_income=this_month.income,
        savings_rate=this_month.savings_rate,
        rolling_30_day_spending=analytics.spending_between(
            db, current_user, start=today - dt.timedelta(days=30), end=today
        ),
        rolling_90_day_spending=analytics.spending_between(
            db, current_user, start=today - dt.timedelta(days=90), end=today
        ),
        burn_rate=analytics.burn_rate(db, current_user),
        cash_runway_months=analytics.cash_runway_months(db, current_user),
        monthly=[_period(m) for m in monthly],
        top_categories=[
            CategoryTotalResponse(**vars(c))
            for c in analytics.spending_by_category(
                db, current_user, start=month_start, end=today, limit=10
            )
        ],
        top_merchants=[
            MerchantTotalResponse(**vars(m))
            for m in analytics.spending_by_merchant(
                db, current_user, start=month_start, end=today, limit=10
            )
        ],
        largest_transactions=[
            LargestTransactionResponse(
                id=t.id,
                date=t.effective_date,
                description=t.effective_description,
                amount=t.effective_amount,
                category_name=(
                    (t.user_category or t.auto_category).name
                    if (t.user_category or t.auto_category)
                    else None
                ),
            )
            for t in analytics.largest_transactions(
                db, current_user, start=month_start, end=today, limit=5
            )
        ],
        biggest_increases=[
            CategoryChangeResponse(**vars(total), change=change)
            for total, change in changes[:5]
            if change > 0
        ],
        biggest_decreases=[
            CategoryChangeResponse(**vars(total), change=change)
            for total, change in reversed(changes[-5:])
            if change < 0
        ],
        net_worth_series=[
            NetWorthPointResponse(
                as_of=p.as_of,
                assets=p.assets,
                liabilities=p.liabilities,
                net_worth=p.net_worth,
            )
            for p in net_worth_points
        ],
        recurring=[
            RecurringChargeResponse(**vars(r))
            for r in analytics.recurring_charges(
                db, current_user, since=today - dt.timedelta(days=365)
            )
        ],
        budgets=[
            BudgetComparisonResponse(
                category_id=b.category_id,
                category_name=b.category_name,
                budgeted=b.budgeted,
                actual=b.actual,
                remaining=b.remaining,
                used_fraction=b.used_fraction,
            )
            for b in analytics.budget_vs_actual(
                db, current_user, period_start=month_start
            )
        ],
    )


@router.get("/monthly", response_model=list[PeriodSummaryResponse])
def monthly(
    current_user: CurrentUser,
    db: DbSession,
    months: int = Query(default=24, ge=1, le=120),
) -> list[PeriodSummaryResponse]:
    today = dt.date.today()
    start = analytics._add_months(analytics._start_of_month(today), -(months - 1))
    return [
        _period(m)
        for m in analytics.monthly_summary(db, current_user, start=start, end=today)
    ]


@router.get("/categories", response_model=list[CategoryTotalResponse])
def by_category(
    current_user: CurrentUser,
    db: DbSession,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[CategoryTotalResponse]:
    start, end = _resolve_range(start_date, end_date)
    return [
        CategoryTotalResponse(**vars(c))
        for c in analytics.spending_by_category(db, current_user, start=start, end=end)
    ]


@router.get("/merchants", response_model=list[MerchantTotalResponse])
def by_merchant(
    current_user: CurrentUser,
    db: DbSession,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[MerchantTotalResponse]:
    start, end = _resolve_range(start_date, end_date)
    return [
        MerchantTotalResponse(**vars(m))
        for m in analytics.spending_by_merchant(
            db, current_user, start=start, end=end, limit=limit
        )
    ]


@router.get("/net-worth", response_model=list[NetWorthPointResponse])
def net_worth(
    current_user: CurrentUser,
    db: DbSession,
    months: int = Query(default=12, ge=1, le=120),
) -> list[NetWorthPointResponse]:
    today = dt.date.today()
    start = analytics._add_months(analytics._start_of_month(today), -(months - 1))
    return [
        NetWorthPointResponse(
            as_of=p.as_of, assets=p.assets, liabilities=p.liabilities, net_worth=p.net_worth
        )
        for p in analytics.net_worth_series(db, current_user, start=start, end=today)
    ]


@router.get("/recurring", response_model=list[RecurringChargeResponse])
def recurring(
    current_user: CurrentUser,
    db: DbSession,
    days: int = Query(default=365, ge=60, le=1095),
) -> list[RecurringChargeResponse]:
    since = dt.date.today() - dt.timedelta(days=days)
    return [
        RecurringChargeResponse(**vars(r))
        for r in analytics.recurring_charges(db, current_user, since=since)
    ]


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@router.get("/budgets", response_model=list[BudgetComparisonResponse])
def budgets(
    current_user: CurrentUser,
    db: DbSession,
    period_start: dt.date | None = None,
) -> list[BudgetComparisonResponse]:
    month = analytics._start_of_month(period_start or dt.date.today())
    return [
        BudgetComparisonResponse(
            category_id=b.category_id,
            category_name=b.category_name,
            budgeted=b.budgeted,
            actual=b.actual,
            remaining=b.remaining,
            used_fraction=b.used_fraction,
        )
        for b in analytics.budget_vs_actual(db, current_user, period_start=month)
    ]


@router.put("/budgets", response_model=BudgetResponse)
def upsert_budget(
    payload: BudgetUpsertRequest, current_user: CurrentUser, db: DbSession
) -> BudgetResponse:
    """Set a budget. PUT because it is idempotent: same input, same result.

    The period is normalized to the first of the month server-side, so a
    client sending the 5th and another sending the 20th cannot create two
    budgets for the same month.
    """
    month = analytics._start_of_month(payload.period_start)

    visible = db.execute(
        select(Category.id).where(
            Category.id == payload.category_id,
            (Category.user_id == current_user.id) | (Category.user_id.is_(None)),
        )
    ).scalar_one_or_none()
    if visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")

    budget = db.execute(
        select(Budget).where(
            Budget.user_id == current_user.id,
            Budget.category_id == payload.category_id,
            Budget.period_start == month,
        )
    ).scalar_one_or_none()

    if budget is None:
        budget = Budget(
            user_id=current_user.id,
            category_id=payload.category_id,
            period_start=month,
            amount=payload.amount,
        )
        db.add(budget)
    else:
        budget.amount = payload.amount

    try:
        db.commit()
    except IntegrityError as exc:
        # The unique constraint is the real arbiter: two concurrent PUTs can
        # both find nothing and both insert.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That budget was just created elsewhere."
        ) from exc

    db.refresh(budget)
    return BudgetResponse.model_validate(budget)


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_budget(
    budget_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> None:
    budget = scoped_get(db, Budget, budget_id, current_user)
    if budget is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Budget not found")

    db.delete(budget)
    db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_range(
    start_date: dt.date | None, end_date: dt.date | None
) -> tuple[dt.date, dt.date]:
    """Default to the current month when no range is given."""
    today = dt.date.today()
    end = end_date or today
    start = start_date or analytics._start_of_month(end)
    return start, end


def _net_worth_now(accounts: list[Account]) -> Decimal:
    """Fallback when there are no balance snapshots yet.

    A brand-new connection has current balances but no history, and showing
    zero net worth on day one would look like a bug.
    """
    total = ZERO
    for account in accounts:
        if not account.include_in_net_worth or account.current_balance is None:
            continue
        if account.type in (AccountType.CREDIT, AccountType.LOAN):
            total -= account.current_balance
        else:
            total += account.current_balance
    return total


def _overall_utilization(accounts: list[Account]) -> Decimal | None:
    """Utilization across ALL cards, not the average of per-card figures.

    Averaging percentages weights a $500 card the same as a $50,000 one and
    produces a number that matches nothing a credit bureau would compute.
    Total balance over total limit is the figure that actually matters.
    """
    total_balance = ZERO
    total_limit = ZERO

    for account in accounts:
        if account.type != AccountType.CREDIT or not account.credit_limit:
            continue
        total_limit += account.credit_limit
        total_balance += account.current_balance or ZERO

    if total_limit <= 0:
        return None

    return (total_balance / total_limit).quantize(Decimal("0.0001"))

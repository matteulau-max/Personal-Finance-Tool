"""Analytics response shapes."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PeriodSummaryResponse(BaseModel):
    period_start: dt.date
    spending: Decimal
    income: Decimal
    net: Decimal
    # None when there was no income that month -- the question does not apply.
    savings_rate: Decimal | None


class CategoryTotalResponse(BaseModel):
    category_id: uuid.UUID | None
    category_name: str
    total: Decimal
    transaction_count: int


class CategoryChangeResponse(CategoryTotalResponse):
    change: Decimal


class MerchantTotalResponse(BaseModel):
    merchant_id: uuid.UUID | None
    merchant_name: str
    total: Decimal
    transaction_count: int
    average: Decimal


class NetWorthPointResponse(BaseModel):
    as_of: dt.date
    assets: Decimal
    liabilities: Decimal
    net_worth: Decimal


class BudgetComparisonResponse(BaseModel):
    category_id: uuid.UUID
    category_name: str
    budgeted: Decimal
    actual: Decimal
    remaining: Decimal
    used_fraction: Decimal | None


class RecurringChargeResponse(BaseModel):
    merchant_id: uuid.UUID | None
    merchant_name: str
    typical_amount: Decimal
    occurrences: int
    average_gap_days: float
    last_seen: dt.date


class ForecastResponse(BaseModel):
    """Projected spending, shown with the evidence it rests on.

    `basis` and `months_used` are part of the response rather than a detail
    the caller could look up separately, because the projection is a mean of
    those months and nothing more. A single number presented alone invites
    being read as a model output; returning the months it averaged lets the
    UI show its work, and lets a user check the arithmetic against the
    cash-flow figures on the next page.

    `recurring_committed` is the portion of the projection already spoken for
    by detected subscriptions. It is the difference between "you will probably
    spend this" and "you will spend this unless you cancel something".
    """

    projected_spending: Decimal
    months_used: int
    basis: list[PeriodSummaryResponse]
    recurring_committed: Decimal
    recurring: list[RecurringChargeResponse]


class LargestTransactionResponse(BaseModel):
    id: uuid.UUID
    date: dt.date
    description: str
    amount: Decimal
    category_name: str | None


class OverviewResponse(BaseModel):
    """Everything the executive dashboard needs, in one request.

    Deliberately one endpoint rather than eight. A dashboard that fires eight
    parallel requests pays eight round trips, eight authentications, and eight
    connection checkouts to render a single screen -- and any one of them
    failing leaves the page half-drawn with no clear error.
    """

    as_of: dt.date

    # Headline figures.
    net_worth: Decimal
    liquid_balance: Decimal
    credit_utilization: Decimal | None

    # This month so far.
    month_to_date_spending: Decimal
    month_to_date_income: Decimal
    savings_rate: Decimal | None

    # Rolling windows: less jumpy than calendar months, and comparable on any
    # day rather than only at month end.
    rolling_30_day_spending: Decimal
    rolling_90_day_spending: Decimal

    burn_rate: Decimal
    cash_runway_months: Decimal | None

    monthly: list[PeriodSummaryResponse] = Field(default_factory=list)
    top_categories: list[CategoryTotalResponse] = Field(default_factory=list)
    top_merchants: list[MerchantTotalResponse] = Field(default_factory=list)
    largest_transactions: list[LargestTransactionResponse] = Field(default_factory=list)
    biggest_increases: list[CategoryChangeResponse] = Field(default_factory=list)
    biggest_decreases: list[CategoryChangeResponse] = Field(default_factory=list)
    net_worth_series: list[NetWorthPointResponse] = Field(default_factory=list)
    recurring: list[RecurringChargeResponse] = Field(default_factory=list)
    budgets: list[BudgetComparisonResponse] = Field(default_factory=list)


class BudgetUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: uuid.UUID
    # Any date within the month; normalized to the 1st server-side so clients
    # cannot create two budgets for one month by sending different days.
    period_start: dt.date
    amount: Decimal = Field(ge=0)


class BudgetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID
    period_start: dt.date
    amount: Decimal

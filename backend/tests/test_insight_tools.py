"""Tool tests.

The tools are the only route from the model to the database, so what they
return and what they refuse are both worth pinning down.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Budget, Category, Merchant
from app.services import analytics
from app.services.insight_tools import MAX_LIMIT, InsightTools, ToolArgumentError
from tests.conftest import make_account, make_transaction


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


def spend(db, account, amount: str, *, day: dt.date, slug: str | None = None, name: str = "STORE"):
    txn = make_transaction(
        db,
        account,
        plaid_transaction_id=f"t_{uuid.uuid4().hex[:12]}",
        raw_name=name,
        raw_amount=amount,
        raw_date=day,
    )
    if slug:
        txn.auto_category_id = category(db, slug).id
    db.flush()
    return txn


@pytest.fixture
def tools(db: Session, authenticated_user) -> InsightTools:
    user, _ = authenticated_user
    return InsightTools(db, user, today=dt.date(2026, 7, 15))


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------


def test_a_natural_language_date_is_rejected_with_a_usable_message(tools):
    """The model will occasionally send "last month" instead of a date. The
    error has to say what a date looks like, because that message is the only
    feedback it gets before retrying."""
    with pytest.raises(ToolArgumentError, match="YYYY-MM-DD"):
        tools.run("spending_by_category", {"start": "last month", "end": "2026-07-15"})


def test_a_missing_required_date_is_rejected(tools):
    with pytest.raises(ToolArgumentError, match="required"):
        tools.run("spending_by_category", {"end": "2026-07-15"})


def test_an_oversized_limit_is_clamped_rather_than_rejected(db: Session, tools, authenticated_user):
    """A model asking for 5,000 rows means "lots". Failing the call teaches it
    nothing; capping gives it an answer and protects the response size."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "10.00", day=dt.date(2026, 7, 10))

    result = tools.run(
        "search_transactions",
        {"start": "2026-07-01", "end": "2026-07-31", "limit": 5000},
    )

    assert result["limit"] == MAX_LIMIT


def test_an_unknown_tool_name_raises(tools):
    with pytest.raises(ToolArgumentError, match="no tool named"):
        tools.run("delete_everything", {})


# ---------------------------------------------------------------------------
# Money crosses the boundary as strings
# ---------------------------------------------------------------------------


def test_amounts_are_strings_not_json_numbers(db: Session, tools, authenticated_user):
    """JSON numbers are IEEE doubles. Having used NUMERIC through the whole
    database, handing the model a float here would reintroduce exactly the
    imprecision that was avoided everywhere else."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "0.10", day=dt.date(2026, 7, 10), slug="food_and_drink.groceries")
    spend(db, account, "0.20", day=dt.date(2026, 7, 11), slug="food_and_drink.groceries")

    result = tools.run("spending_by_category", {"start": "2026-07-01", "end": "2026-07-31"})

    assert result["categories"][0]["total"] == "0.30"
    assert isinstance(result["categories"][0]["total"], str)


# ---------------------------------------------------------------------------
# Individual tools
# ---------------------------------------------------------------------------


def test_search_can_filter_by_amount(db: Session, tools, authenticated_user):
    """"Show me restaurants over $100" -- the filter runs in SQL, so the model
    never sees the rows that did not match."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "150.00", day=dt.date(2026, 7, 10), slug="food_and_drink.restaurants", name="NICE PLACE")
    spend(db, account, "20.00", day=dt.date(2026, 7, 11), slug="food_and_drink.restaurants", name="CHEAP PLACE")

    result = tools.run(
        "search_transactions",
        {
            "start": "2026-07-01",
            "end": "2026-07-31",
            "category_slug": "food_and_drink.restaurants",
            "min_amount": "100",
        },
    )

    assert [row["amount"] for row in result["transactions"]] == ["150.00"]


def test_a_category_filter_includes_child_categories(db: Session, tools, authenticated_user):
    """Asking about "food and drink" means the subtree. Matching only the
    parent node would return almost nothing and read as a missing
    transaction rather than a narrow query."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "30.00", day=dt.date(2026, 7, 10), slug="food_and_drink.groceries")
    spend(db, account, "40.00", day=dt.date(2026, 7, 11), slug="food_and_drink.restaurants")
    spend(db, account, "50.00", day=dt.date(2026, 7, 12), slug="travel")

    result = tools.run(
        "search_transactions",
        {"start": "2026-07-01", "end": "2026-07-31", "category_slug": "food_and_drink"},
    )

    assert sorted(row["amount"] for row in result["transactions"]) == ["30.00", "40.00"]


def test_search_says_when_the_result_may_be_truncated(db: Session, tools, authenticated_user):
    """Without this, "here are your restaurant charges" is a claim about
    completeness the data does not support."""
    user, _ = authenticated_user
    account = make_account(db, user)
    for day in range(1, 6):
        spend(db, account, "10.00", day=dt.date(2026, 7, day))

    result = tools.run(
        "search_transactions", {"start": "2026-07-01", "end": "2026-07-31", "limit": 3}
    )

    assert result["returned"] == result["limit"] == 3
    assert "complete list" in result["note"]


def test_compare_categories_computes_the_percentage_itself(
    db: Session, tools, authenticated_user
):
    """The percentage has to come from a query, not from the model. Prose
    arithmetic is arithmetic nobody checked -- and grounding would reject it
    anyway, so the answer would simply fail."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "100.00", day=dt.date(2026, 6, 10), slug="travel")
    spend(db, account, "150.00", day=dt.date(2026, 7, 10), slug="travel")

    result = tools.run("compare_categories", {})

    [travel] = [row for row in result["categories"] if row["category"] == "Travel"]
    assert travel["previous"] == "100.00"
    assert travel["current"] == "150.00"
    assert travel["change"] == "50.00"
    assert travel["change_percent"] == "50.00"


def test_a_new_category_reports_a_null_percentage_not_infinity(
    db: Session, tools, authenticated_user
):
    """Spending $80 on something you spent $0 on last month is not a "+8000%
    increase" -- the increase is undefined. Null makes the model say so."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "80.00", day=dt.date(2026, 7, 10), slug="travel")

    result = tools.run("compare_categories", {})

    [travel] = [row for row in result["categories"] if row["category"] == "Travel"]
    assert travel["change_percent"] is None


def test_recurring_charges_annualize_the_cost(db: Session, tools, authenticated_user):
    """"What can I cancel" is answered by the yearly number, not the monthly
    one. $12/month is easy to ignore; $146/year is a decision."""
    user, _ = authenticated_user
    account = make_account(db, user)
    netflix = Merchant(user_id=user.id, normalized_name="netflix", display_name="Netflix")
    db.add(netflix)
    db.flush()

    for month in (3, 4, 5, 6):
        txn = spend(db, account, "12.00", day=dt.date(2026, month, 5), name="NETFLIX.COM")
        txn.auto_merchant_id = netflix.id
    db.flush()

    result = tools.run("recurring_charges", {"months": 12})

    assert result["charges"], "expected the monthly charge to be detected"
    charge = result["charges"][0]
    assert charge["typical_amount"] == "12.00"
    assert Decimal(charge["annualized_cost"]) > Decimal("100")


def test_recurring_charges_carry_their_own_caveat(tools):
    """The note travels with the data. A limitation documented only in our
    source is a limitation the model cannot pass on to the user."""
    result = tools.run("recurring_charges", {})

    assert "heuristic" in result["note"]


def test_budget_status_reports_percentage_used(db: Session, tools, authenticated_user):
    user, _ = authenticated_user
    account = make_account(db, user)
    groceries = category(db, "food_and_drink.groceries")
    db.add(
        Budget(
            user_id=user.id,
            category_id=groceries.id,
            period_start=dt.date(2026, 7, 1),
            amount=Decimal("600"),
        )
    )
    spend(db, account, "150.00", day=dt.date(2026, 7, 10), slug="food_and_drink.groceries")

    [row] = tools.run("budget_status", {"month": "2026-07-20"})["budgets"]

    assert row["actual"] == "150.00"
    assert row["remaining"] == "450.00"
    assert row["used_percent"] == "25.00"


def test_forecast_says_how_much_history_it_used(db: Session, tools, authenticated_user):
    """A projection from one month and a projection from six are different
    objects. Returning the count is what lets the model say which it has."""
    user, _ = authenticated_user

    result = tools.run("forecast_next_month", {})

    assert result["based_on_months"] == 0
    assert "average, not a model" in result["note"]


def test_cash_flow_warns_that_the_current_month_is_partial(tools):
    result = tools.run("cash_flow_summary", {})

    assert "partial" in result["note"]


def test_list_categories_exposes_slugs_for_the_search_tool(tools):
    """The model needs real slugs; guessing one silently returns zero rows,
    which reads as "you spent nothing" rather than "I looked in the wrong
    place"."""
    result = tools.run("list_categories", {})

    slugs = {row["slug"] for row in result["categories"]}
    assert "food_and_drink.groceries" in slugs


def test_categories_are_scoped_to_the_user(db: Session, tools, other_user):
    stranger, _ = other_user
    db.add(Category(user_id=stranger.id, name="Their Secret Category", slug="secret"))
    db.flush()

    slugs = {row["slug"] for row in tools.run("list_categories", {})["categories"]}

    assert "secret" not in slugs


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def test_the_analytics_used_by_tools_never_write(db: Session, tools, authenticated_user):
    """Every tool is a SELECT. Running all of them must leave the database
    untouched -- the model can describe your finances, not change them."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "25.00", day=dt.date(2026, 7, 10), slug="travel")
    db.commit()

    before = analytics.spending_between(
        db, user, start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31)
    )

    for name in tools.names():
        arguments = {}
        if name in {"spending_by_category", "spending_by_merchant", "search_transactions"}:
            arguments = {"start": "2026-07-01", "end": "2026-07-31"}
        tools.run(name, arguments)

    after = analytics.spending_between(
        db, user, start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31)
    )
    assert before == after
    assert not db.dirty and not db.new and not db.deleted

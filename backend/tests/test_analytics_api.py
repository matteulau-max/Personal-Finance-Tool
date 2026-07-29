"""Analytics endpoint tests."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AccountType, Budget, Category, User
from app.services import analytics
from tests.auth_helpers import auth_header
from tests.conftest import make_account, make_transaction


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


def spend(db: Session, account: Account, amount: str, *, day: dt.date, slug: str | None = None):
    txn = make_transaction(
        db,
        account,
        plaid_transaction_id=f"api_{uuid.uuid4().hex[:12]}",
        raw_amount=amount,
        raw_date=day,
    )
    if slug:
        txn.auto_category_id = category(db, slug).id
    db.flush()
    return txn


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_analytics_require_authentication(client: TestClient):
    assert client.get("/api/analytics/overview").status_code == 401
    assert client.get("/api/analytics/monthly").status_code == 401
    assert client.get("/api/analytics/net-worth").status_code == 401


def test_overview_only_covers_your_own_data(
    client: TestClient, db: Session, authenticated_user, other_user
):
    """An aggregate that silently includes someone else's money is the worst
    possible leak: it is invisible, because no row is displayed."""
    user, token = authenticated_user
    stranger, _ = other_user
    today = dt.date.today()

    spend(db, make_account(db, user), "100.00", day=today)
    spend(db, make_account(db, stranger), "99999.00", day=today)

    body = client.get("/api/analytics/overview", headers=auth_header(token)).json()

    assert Decimal(body["month_to_date_spending"]) == Decimal("100.00")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_overview_returns_the_whole_dashboard_in_one_request(
    client: TestClient, db: Session, authenticated_user
):
    """One request, not eight. A dashboard firing eight parallel calls pays
    eight round trips and eight token verifications for one screen."""
    user, token = authenticated_user
    account = make_account(db, user)
    today = dt.date.today()
    spend(db, account, "50.00", day=today, slug="food_and_drink.groceries")

    response = client.get("/api/analytics/overview", headers=auth_header(token))

    assert response.status_code == 200
    body = response.json()
    for key in (
        "net_worth",
        "liquid_balance",
        "month_to_date_spending",
        "rolling_30_day_spending",
        "burn_rate",
        "monthly",
        "top_categories",
        "top_merchants",
        "net_worth_series",
        "recurring",
        "budgets",
    ):
        assert key in body


def test_net_worth_falls_back_to_current_balances(
    client: TestClient, db: Session, authenticated_user
):
    """A brand-new connection has balances but no snapshot history.

    Showing zero net worth on day one would look like a bug, so the overview
    falls back to the accounts' current balances.
    """
    user, token = authenticated_user
    db.add_all(
        [
            Account(
                user_id=user.id,
                name="Checking",
                type=AccountType.DEPOSITORY,
                current_balance=Decimal("4000"),
                plaid_account_id=f"c_{uuid.uuid4().hex[:8]}",
            ),
            Account(
                user_id=user.id,
                name="Card",
                type=AccountType.CREDIT,
                current_balance=Decimal("1000"),
                credit_limit=Decimal("5000"),
                plaid_account_id=f"k_{uuid.uuid4().hex[:8]}",
            ),
        ]
    )
    db.flush()

    body = client.get("/api/analytics/overview", headers=auth_header(token)).json()

    assert Decimal(body["net_worth"]) == Decimal("3000")
    assert Decimal(body["liquid_balance"]) == Decimal("4000.0000")


def test_utilization_is_totals_not_an_average_of_percentages(
    client: TestClient, db: Session, authenticated_user
):
    """Averaging per-card percentages weights a $500 card the same as a
    $50,000 one and produces a figure no credit bureau would recognize.

    Here: 500/1000 and 500/9000. The average of the two rates is 27.8%; the
    correct answer -- total balance over total limit -- is 10%.
    """
    user, token = authenticated_user
    db.add_all(
        [
            Account(
                user_id=user.id,
                name="Small card",
                type=AccountType.CREDIT,
                current_balance=Decimal("500"),
                credit_limit=Decimal("1000"),
                plaid_account_id=f"s_{uuid.uuid4().hex[:8]}",
            ),
            Account(
                user_id=user.id,
                name="Big card",
                type=AccountType.CREDIT,
                current_balance=Decimal("500"),
                credit_limit=Decimal("9000"),
                plaid_account_id=f"b_{uuid.uuid4().hex[:8]}",
            ),
        ]
    )
    db.flush()

    body = client.get("/api/analytics/overview", headers=auth_header(token)).json()

    assert Decimal(body["credit_utilization"]) == Decimal("0.1000")


def test_utilization_is_null_without_any_credit_limit(
    client: TestClient, db: Session, authenticated_user
):
    _, token = authenticated_user

    body = client.get("/api/analytics/overview", headers=auth_header(token)).json()

    assert body["credit_utilization"] is None


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_setting_a_budget_is_idempotent(
    client: TestClient, db: Session, authenticated_user
):
    """PUT, not POST. Sending the same request twice must not create two
    budgets for one month -- the number would then depend on which row the
    database returned first."""
    _, token = authenticated_user
    groceries = category(db, "food_and_drink.groceries")
    today = dt.date.today()

    payload = {
        "category_id": str(groceries.id),
        "period_start": today.isoformat(),
        "amount": "600.00",
    }

    first = client.put("/api/analytics/budgets", headers=auth_header(token), json=payload)
    second = client.put(
        "/api/analytics/budgets",
        headers=auth_header(token),
        json={**payload, "amount": "700.00"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert Decimal(second.json()["amount"]) == Decimal("700.00")


def test_the_period_is_normalized_to_the_first_of_the_month(
    client: TestClient, db: Session, authenticated_user
):
    """Otherwise a client sending the 5th and another sending the 20th would
    create two budgets for the same month."""
    _, token = authenticated_user
    groceries = category(db, "food_and_drink.groceries")

    body = client.put(
        "/api/analytics/budgets",
        headers=auth_header(token),
        json={
            "category_id": str(groceries.id),
            "period_start": "2026-05-17",
            "amount": "600.00",
        },
    ).json()

    assert body["period_start"] == "2026-05-01"


def test_cannot_budget_another_users_category(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    stranger, _ = other_user
    theirs = Category(user_id=stranger.id, name="Their Category")
    db.add(theirs)
    db.flush()

    response = client.put(
        "/api/analytics/budgets",
        headers=auth_header(token),
        json={
            "category_id": str(theirs.id),
            "period_start": dt.date.today().isoformat(),
            "amount": "100.00",
        },
    )

    assert response.status_code == 404


def test_budget_comparison_reflects_actual_spending(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    groceries = category(db, "food_and_drink.groceries")
    today = dt.date.today()

    db.add(
        Budget(
            user_id=user.id,
            category_id=groceries.id,
            period_start=analytics._start_of_month(today),
            amount=Decimal("600"),
        )
    )
    spend(db, account, "150.00", day=today, slug="food_and_drink.groceries")

    [comparison] = client.get(
        "/api/analytics/budgets", headers=auth_header(token)
    ).json()

    assert Decimal(comparison["actual"]) == Decimal("150.00")
    assert Decimal(comparison["remaining"]) == Decimal("450.0000")


def test_a_negative_budget_is_rejected(client: TestClient, db: Session, authenticated_user):
    _, token = authenticated_user
    groceries = category(db, "food_and_drink.groceries")

    response = client.put(
        "/api/analytics/budgets",
        headers=auth_header(token),
        json={
            "category_id": str(groceries.id),
            "period_start": dt.date.today().isoformat(),
            "amount": "-100.00",
        },
    )

    assert response.status_code == 422


def test_budgets_are_scoped(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    _, stranger_token = other_user
    groceries = category(db, "food_and_drink.groceries")

    client.put(
        "/api/analytics/budgets",
        headers=auth_header(token),
        json={
            "category_id": str(groceries.id),
            "period_start": dt.date.today().isoformat(),
            "amount": "600.00",
        },
    )

    assert client.get("/api/analytics/budgets", headers=auth_header(stranger_token)).json() == []


def test_deleting_another_users_budget_returns_404(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    stranger, _ = other_user
    groceries = category(db, "food_and_drink.groceries")

    theirs = Budget(
        user_id=stranger.id,
        category_id=groceries.id,
        period_start=analytics._start_of_month(dt.date.today()),
        amount=Decimal("500"),
    )
    db.add(theirs)
    db.flush()

    response = client.delete(
        f"/api/analytics/budgets/{theirs.id}", headers=auth_header(token)
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Other endpoints
# ---------------------------------------------------------------------------


def test_monthly_endpoint(client: TestClient, db: Session, authenticated_user):
    user, token = authenticated_user
    account = make_account(db, user)
    spend(db, account, "42.00", day=dt.date.today())

    body = client.get("/api/analytics/monthly", headers=auth_header(token)).json()

    assert len(body) == 1
    assert Decimal(body[0]["spending"]) == Decimal("42.00")


def test_category_endpoint_defaults_to_this_month(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    today = dt.date.today()

    spend(db, account, "10.00", day=today, slug="travel")
    # Two months ago, so outside the default window.
    spend(db, account, "999.00", day=analytics._add_months(today.replace(day=1), -2), slug="travel")

    body = client.get("/api/analytics/categories", headers=auth_header(token)).json()

    assert len(body) == 1
    assert Decimal(body[0]["total"]) == Decimal("10.00")

"""Insight endpoint tests.

These check the things a caller can observe: that the route is authenticated,
that an unverifiable answer produces an error rather than text, and that the
fabricated figure does not appear anywhere in the response body.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Category
from app.services import analytics
from app.services.ai_gateway import get_ai_gateway
from tests.auth_helpers import auth_header
from tests.conftest import make_account, make_transaction
from tests.fake_ai import ExplodingAIGateway, FakeAIGateway, calls_tool, says


@pytest.fixture
def ai_enabled():
    """Turn the feature on for the duration of a test."""
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
    get_settings.cache_clear()
    yield
    os.environ.pop("ANTHROPIC_API_KEY", None)
    get_settings.cache_clear()


@pytest.fixture
def use_gateway():
    """Install a scripted gateway for one test, then remove it."""
    from app.main import app

    installed: list = []

    def _install(gateway):
        app.dependency_overrides[get_ai_gateway] = lambda: gateway
        installed.append(gateway)
        return gateway

    yield _install

    app.dependency_overrides.pop(get_ai_gateway, None)


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


def spend(db, account, amount: str, *, day: dt.date, slug: str | None = None):
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


def test_insights_require_authentication(client: TestClient):
    assert client.post("/api/insights", json={"question": "hi"}).status_code == 401
    assert client.get("/api/insights/suggestions").status_code == 401


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_without_an_api_key_the_endpoint_is_plainly_unavailable(
    client: TestClient, authenticated_user
):
    """503, not a degraded answer. An AI feature that quietly falls back to
    something else is a feature nobody can reason about -- and here the
    fallback would have to be a number from somewhere."""
    _, token = authenticated_user

    response = client.post(
        "/api/insights", headers=auth_header(token), json={"question": "What did I spend?"}
    )

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_grounded_answer_is_returned_with_its_sources(
    client: TestClient, db: Session, authenticated_user, ai_enabled, use_gateway
):
    """The sources are part of the response on purpose: a figure you can
    trace to a named query over a stated date range is checkable, and "trust
    me" is not an acceptable answer about somebody's money."""
    user, token = authenticated_user
    account = make_account(db, user)
    today = dt.date.today()
    spend(db, account, "412.00", day=today, slug="food_and_drink.groceries")

    use_gateway(
        FakeAIGateway(
            calls_tool(
                "spending_by_category",
                {
                    "start": analytics._start_of_month(today).isoformat(),
                    "end": today.isoformat(),
                },
            ),
            says("Groceries were your largest category, at $412.00."),
        )
    )

    response = client.post(
        "/api/insights",
        headers=auth_header(token),
        json={"question": "What did I spend the most on?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "$412.00" in body["answer"]
    assert body["sources"] == [
        {
            "tool": "spending_by_category",
            "arguments": {
                "start": analytics._start_of_month(today).isoformat(),
                "end": today.isoformat(),
            },
        }
    ]


# ---------------------------------------------------------------------------
# The failure that matters
# ---------------------------------------------------------------------------


def test_an_ungrounded_answer_produces_an_error_and_not_the_number(
    client: TestClient, db: Session, authenticated_user, ai_enabled, use_gateway
):
    """The end-to-end version of the guarantee.

    The assertion that matters is the last one: the fabricated figure is
    absent from the *entire* response body. Returning the answer with a
    warning attached would not do -- a number on screen is a number people
    remember, and the warning is not what they would repeat later.
    """
    _, token = authenticated_user
    use_gateway(FakeAIGateway(says("You spent $8,675.30 on dining last month.")))

    response = client.post(
        "/api/insights",
        headers=auth_header(token),
        json={"question": "What did I spend on dining?"},
    )

    assert response.status_code == 422
    assert "8,675.30" not in response.text
    assert "8675.30" not in response.text


def test_the_offending_figures_are_not_echoed_in_the_error(
    client: TestClient, authenticated_user, ai_enabled, use_gateway
):
    """Listing what was rejected would be helpful for debugging and would put
    the invented number on the user's screen. It goes to the log instead."""
    _, token = authenticated_user
    use_gateway(FakeAIGateway(says("Your balance is $99,999.")))

    body = client.post(
        "/api/insights", headers=auth_header(token), json={"question": "balance?"}
    ).json()

    assert "99,999" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Other failure modes
# ---------------------------------------------------------------------------


def test_a_provider_outage_is_a_502_not_a_500(
    client: TestClient, authenticated_user, ai_enabled, use_gateway
):
    _, token = authenticated_user
    use_gateway(ExplodingAIGateway())

    response = client.post(
        "/api/insights", headers=auth_header(token), json={"question": "What did I spend?"}
    )

    assert response.status_code == 502


def test_an_over_long_question_is_rejected_by_the_schema(
    client: TestClient, authenticated_user, ai_enabled, use_gateway
):
    """Rejected by Pydantic before any of our code runs, so the oversized
    prompt never reaches the provider or the bill."""
    _, token = authenticated_user
    gateway = use_gateway(FakeAIGateway(says("unused")))

    response = client.post(
        "/api/insights", headers=auth_header(token), json={"question": "a" * 5000}
    )

    assert response.status_code == 422
    assert gateway.calls == []


def test_an_empty_question_is_rejected(
    client: TestClient, authenticated_user, ai_enabled, use_gateway
):
    _, token = authenticated_user
    use_gateway(FakeAIGateway(says("unused")))

    response = client.post(
        "/api/insights", headers=auth_header(token), json={"question": ""}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


def test_suggestions_are_static_and_need_no_model(
    client: TestClient, authenticated_user
):
    """No API key configured, and this still works. Paying for a round trip to
    render four buttons would be a strange way to spend a model call."""
    _, token = authenticated_user

    response = client.get("/api/insights/suggestions", headers=auth_header(token))

    assert response.status_code == 200
    assert len(response.json()) == 4

"""Insight engine tests.

The model is scripted (`tests/fake_ai.py`), so every scenario below is a
scenario about *our* code: what happens when the model behaves, what happens
when it fabricates, what happens when it asks for something that does not
exist, and what it is able to reach when it tries.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category
from app.services import analytics
from app.services.ai_gateway import AIError
from app.services.insights import MAX_TURNS, UngroundedAnswer, answer_question
from tests.conftest import make_account, make_transaction
from tests.fake_ai import ExplodingAIGateway, FakeAIGateway, calls_tool, says


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


def spend(db, account, amount: str, *, day: dt.date, slug: str | None = None, name: str = "STORE"):
    txn = make_transaction(
        db,
        account,
        plaid_transaction_id=f"ai_{uuid.uuid4().hex[:12]}",
        raw_name=name,
        raw_amount=amount,
        raw_date=day,
    )
    if slug:
        txn.auto_category_id = category(db, slug).id
    db.flush()
    return txn


# ---------------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------------


def test_a_fabricated_figure_never_reaches_the_caller(db: Session, authenticated_user):
    """The headline test of this milestone.

    The model answers confidently, with a plausible amount, having asked no
    questions at all. Nothing about the sentence looks wrong -- that is the
    whole problem with this failure mode. The engine refuses it.
    """
    user, _ = authenticated_user
    gateway = FakeAIGateway(says("You spent $4,213.77 on groceries last month."))

    with pytest.raises(UngroundedAnswer) as caught:
        answer_question(db, user, gateway, "What did I spend on groceries?")

    assert "4,213.77" in caught.value.unsupported


def test_a_figure_from_a_real_query_is_returned(db: Session, authenticated_user):
    """The other half: grounding must not be so strict that it rejects
    correct answers. A check that fails everything is not a safety property,
    it is a broken feature."""
    user, _ = authenticated_user
    account = make_account(db, user)
    today = dt.date.today()
    spend(db, account, "412.00", day=today, slug="food_and_drink.groceries")

    gateway = FakeAIGateway(
        calls_tool(
            "spending_by_category",
            {"start": analytics._start_of_month(today).isoformat(), "end": today.isoformat()},
        ),
        says("Groceries were your largest category at $412.00."),
    )

    insight = answer_question(db, user, gateway, "What did I spend the most on?")

    assert "$412.00" in insight.answer
    assert [source.name for source in insight.sources] == ["spending_by_category"]


def test_a_real_figure_mixed_with_an_invented_one_is_still_refused(
    db: Session, authenticated_user
):
    """Partial grounding is not grounding. An answer that is 90% sourced is
    the most dangerous kind, because the sourced parts make the rest
    credible."""
    user, _ = authenticated_user
    account = make_account(db, user)
    today = dt.date.today()
    spend(db, account, "412.00", day=today, slug="food_and_drink.groceries")

    gateway = FakeAIGateway(
        calls_tool(
            "spending_by_category",
            {"start": analytics._start_of_month(today).isoformat(), "end": today.isoformat()},
        ),
        says("Groceries were $412.00, up from $250.00 last month."),
    )

    with pytest.raises(UngroundedAnswer) as caught:
        answer_question(db, user, gateway, "What did I spend the most on?")

    assert caught.value.unsupported == ("250.00",)


def test_an_answer_with_no_figures_at_all_is_allowed(db: Session, authenticated_user):
    """"I could not find that" must remain sayable. If the only safe answer
    were a numeric one, the model would be pushed towards inventing one."""
    user, _ = authenticated_user
    gateway = FakeAIGateway(says("You have no transactions in that period yet."))

    insight = answer_question(db, user, gateway, "What did I spend?")

    assert insight.answer.startswith("You have no transactions")


def test_a_figure_from_a_failed_tool_call_is_not_quotable(
    db: Session, authenticated_user
):
    """A tool error echoes the argument the model supplied. Letting that
    widen the supported set would mean the model could authorize its own
    figure by passing it into a call that fails."""
    user, _ = authenticated_user

    gateway = FakeAIGateway(
        calls_tool("spending_by_category", {"start": "9999.99", "end": "not-a-date"}),
        says("You spent $9999.99."),
    )

    with pytest.raises(UngroundedAnswer):
        answer_question(db, user, gateway, "What did I spend?")


# ---------------------------------------------------------------------------
# Scoping -- the model is bound to one user
# ---------------------------------------------------------------------------


def test_tools_only_ever_see_the_asking_users_data(
    db: Session, authenticated_user, other_user
):
    """Not a test of the prompt. The tools close over one user, so there is
    no argument the model could pass to reach another."""
    user, _ = authenticated_user
    stranger, _ = other_user
    today = dt.date.today()

    spend(db, make_account(db, user), "100.00", day=today)
    spend(db, make_account(db, stranger), "88888.00", day=today)

    gateway = FakeAIGateway(
        calls_tool(
            "spending_by_category",
            {"start": analytics._start_of_month(today).isoformat(), "end": today.isoformat()},
        ),
        says("You spent $100.00."),
    )

    insight = answer_question(db, user, gateway, "What did I spend?")

    assert insight.answer == "You spent $100.00."
    # The decisive assertion: the other user's figure was never even sent.
    assert "88888" not in gateway.prompt_text()


def test_no_tool_accepts_a_user_identifier(db: Session, authenticated_user):
    """A schema that advertised a user id would be a parameter the model
    could set. The absence is the control, so it is worth asserting."""
    user, _ = authenticated_user
    gateway = FakeAIGateway(says("No data yet."))

    answer_question(db, user, gateway, "hello")

    for tool in gateway.calls[0]["tools"]:
        assert "user_id" not in tool.input_schema.get("properties", {})
        assert "user" not in tool.input_schema.get("properties", {})


def test_every_offered_tool_actually_exists(db: Session, authenticated_user):
    """Advertising a tool with no handler would make the model's first
    correct move fail."""
    from app.services.insight_tools import InsightTools

    user, _ = authenticated_user
    tools = InsightTools(db, user)

    assert {spec.name for spec in tools.specs()} == tools.names()


# ---------------------------------------------------------------------------
# Loop behaviour
# ---------------------------------------------------------------------------


def test_a_bad_argument_comes_back_as_an_error_the_model_can_fix(
    db: Session, authenticated_user
):
    """A malformed date should not end the conversation. A human handed a 400
    corrects the field and retries; the model gets the same chance."""
    user, _ = authenticated_user

    gateway = FakeAIGateway(
        calls_tool("spending_by_category", {"start": "last tuesday", "end": "today"}),
        calls_tool(
            "spending_by_category",
            {"start": "2026-07-01", "end": "2026-07-31"},
            id="toolu_2",
        ),
        says("You have no spending recorded."),
    )

    insight = answer_question(db, user, gateway, "What did I spend?")

    assert insight.turns == 3
    assert [source.ok for source in insight.sources] == [False, True]


def test_an_unknown_tool_name_is_a_dead_end_not_a_crash(
    db: Session, authenticated_user
):
    user, _ = authenticated_user

    gateway = FakeAIGateway(
        calls_tool("transfer_all_money", {"to": "attacker"}),
        says("I could not do that."),
    )

    insight = answer_question(db, user, gateway, "help")

    assert insight.sources[0].ok is False


def test_the_loop_is_capped(db: Session, authenticated_user):
    """Each turn is a paid API call over somebody's financial history. A model
    that keeps exploring must hit a wall rather than a bill."""
    user, _ = authenticated_user
    gateway = FakeAIGateway(
        *[calls_tool("financial_position", {}, id=f"t{i}") for i in range(MAX_TURNS)]
    )

    with pytest.raises(AIError):
        answer_question(db, user, gateway, "tell me everything")

    assert len(gateway.calls) == MAX_TURNS


def test_a_refusal_is_not_treated_as_an_answer(db: Session, authenticated_user):
    """On `stop_reason == "refusal"` the content blocks are not an answer.
    Rendering them anyway is how a refusal becomes a confusing non-sequitur
    in the user's dashboard."""
    user, _ = authenticated_user
    gateway = FakeAIGateway(says("I cannot help with that.", stop_reason="refusal"))

    with pytest.raises(AIError):
        answer_question(db, user, gateway, "something disallowed")


def test_an_empty_answer_is_an_error_not_a_blank_panel(
    db: Session, authenticated_user
):
    user, _ = authenticated_user

    with pytest.raises(AIError):
        answer_question(db, user, FakeAIGateway(says("   ")), "What did I spend?")


def test_provider_failures_surface_as_ai_errors(db: Session, authenticated_user):
    user, _ = authenticated_user

    with pytest.raises(AIError):
        answer_question(db, user, ExplodingAIGateway(), "What did I spend?")


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def test_an_empty_question_is_rejected_before_any_api_call(
    db: Session, authenticated_user
):
    user, _ = authenticated_user
    gateway = FakeAIGateway(says("anything"))

    with pytest.raises(ValueError):
        answer_question(db, user, gateway, "   ")

    assert gateway.calls == []


def test_an_enormous_question_is_rejected(db: Session, authenticated_user):
    """The question is the untrusted part of the prompt. Bounding it bounds
    how much room anyone has to try steering the model."""
    user, _ = authenticated_user
    gateway = FakeAIGateway(says("anything"))

    with pytest.raises(ValueError):
        answer_question(db, user, gateway, "a" * 5000)

    assert gateway.calls == []


def test_the_system_prompt_contains_no_financial_figures(
    db: Session, authenticated_user
):
    """If the prompt carried a balance, that balance would be quotable without
    any query having run -- and the provenance chain would have a hole in it
    on turn one."""
    user, _ = authenticated_user
    account = make_account(db, user)
    spend(db, account, "777.77", day=dt.date.today())

    gateway = FakeAIGateway(says("No figures here."))
    answer_question(db, user, gateway, "hello")

    assert "777.77" not in gateway.calls[0]["system"]

"""Rules engine and enrichment tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Category,
    CategorySource,
    Merchant,
    Rule,
    Tag,
    Transaction,
    User,
)
from app.schemas.rules import validate_actions, validate_conditions
from app.services.enrichment import enrich_transaction, enrich_transactions
from app.services.rules import evaluate, matches
from tests.conftest import make_transaction


def make_rule(
    db: Session,
    user: User,
    *,
    name: str = "Test rule",
    conditions: dict,
    actions: dict,
    priority: int = 100,
    stop_processing: bool = False,
    is_active: bool = True,
) -> Rule:
    rule = Rule(
        user_id=user.id,
        name=name,
        conditions=conditions,
        actions=actions,
        priority=priority,
        stop_processing=stop_processing,
        is_active=is_active,
    )
    db.add(rule)
    db.flush()
    return rule


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_a_valid_rule_validates():
    group = validate_conditions(
        {
            "operator": "AND",
            "conditions": [
                {"field": "raw_name", "op": "contains", "value": "STARBUCKS"},
                {"field": "amount", "op": "gt", "value": "5.00"},
            ],
        }
    )
    assert len(group.conditions) == 2


def test_an_unknown_field_is_rejected():
    """The field list is an allowlist, so a rule cannot probe columns that
    are none of its business."""
    with pytest.raises(ValueError):
        validate_conditions(
            {"conditions": [{"field": "user_id", "op": "equals", "value": "x"}]}
        )


def test_an_unknown_operator_is_rejected():
    with pytest.raises(ValueError):
        validate_conditions(
            {"conditions": [{"field": "raw_name", "op": "regex", "value": ".*"}]}
        )


def test_extra_keys_are_rejected():
    """`extra="forbid"` catches typos.

    Without it, `{"feild": "raw_name"}` would be silently ignored and the
    rule would quietly match nothing -- looking like it worked.
    """
    with pytest.raises(ValueError):
        validate_conditions(
            {
                "conditions": [
                    {"field": "raw_name", "op": "contains", "value": "x", "oops": 1}
                ]
            }
        )


def test_deeply_nested_conditions_are_rejected():
    """Bounded nesting keeps evaluation cost predictable."""
    innermost = {"conditions": [{"field": "raw_name", "op": "contains", "value": "x"}]}
    nested = innermost
    for _ in range(5):
        nested = {"conditions": [nested]}

    with pytest.raises(ValueError, match="nest at most"):
        validate_conditions(nested)


def test_an_empty_condition_group_is_rejected():
    """A rule with no conditions would match everything."""
    with pytest.raises(ValueError):
        validate_conditions({"conditions": []})


def test_a_rule_that_does_nothing_is_rejected():
    """Always a mistake, and a silent one -- it looks like it is working."""
    with pytest.raises(ValueError, match="must do something"):
        validate_actions({})


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_contains_is_case_insensitive_by_default(db: Session, account: Account):
    transaction = make_transaction(db, account, raw_name="STARBUCKS STORE 123")
    conditions = validate_conditions(
        {"conditions": [{"field": "raw_name", "op": "contains", "value": "starbucks"}]}
    )

    assert matches(conditions, transaction) is True


def test_case_sensitivity_can_be_requested(db: Session, account: Account):
    transaction = make_transaction(db, account, raw_name="STARBUCKS")
    conditions = validate_conditions(
        {
            "conditions": [
                {
                    "field": "raw_name",
                    "op": "contains",
                    "value": "starbucks",
                    "case_sensitive": True,
                }
            ]
        }
    )

    assert matches(conditions, transaction) is False


def test_and_requires_every_condition(db: Session, account: Account):
    transaction = make_transaction(db, account, raw_name="STARBUCKS", raw_amount="3.00")
    conditions = validate_conditions(
        {
            "operator": "AND",
            "conditions": [
                {"field": "raw_name", "op": "contains", "value": "starbucks"},
                {"field": "amount", "op": "gt", "value": "5.00"},
            ],
        }
    )

    assert matches(conditions, transaction) is False


def test_or_requires_only_one(db: Session, account: Account):
    transaction = make_transaction(db, account, raw_name="STARBUCKS", raw_amount="3.00")
    conditions = validate_conditions(
        {
            "operator": "OR",
            "conditions": [
                {"field": "raw_name", "op": "contains", "value": "starbucks"},
                {"field": "amount", "op": "gt", "value": "5.00"},
            ],
        }
    )

    assert matches(conditions, transaction) is True


def test_nested_groups_work(db: Session, account: Account):
    """"coffee AND (over $10 OR tagged-ish name)"."""
    transaction = make_transaction(db, account, raw_name="BLUE BOTTLE COFFEE", raw_amount="4.00")
    conditions = validate_conditions(
        {
            "operator": "AND",
            "conditions": [
                {"field": "raw_name", "op": "contains", "value": "coffee"},
                {
                    "operator": "OR",
                    "conditions": [
                        {"field": "amount", "op": "gt", "value": "10.00"},
                        {"field": "raw_name", "op": "starts_with", "value": "blue"},
                    ],
                },
            ],
        }
    )

    assert matches(conditions, transaction) is True


def test_matching_uses_the_effective_amount(db: Session, account: Account):
    """A rule written against what the user SEES must use the corrected value."""
    transaction = make_transaction(
        db, account, raw_amount="100.00", user_amount=Decimal("5.00")
    )
    conditions = validate_conditions(
        {"conditions": [{"field": "amount", "op": "lt", "value": "10.00"}]}
    )

    assert matches(conditions, transaction) is True


def test_a_missing_field_matches_only_negative_operators(
    db: Session, account: Account
):
    """"notes does not contain X" is TRUE when there are no notes.

    Getting this backwards makes every not_contains rule match nothing (or
    everything), and the failure is completely silent.
    """
    transaction = make_transaction(db, account)  # user_notes is None

    positive = validate_conditions(
        {"conditions": [{"field": "notes", "op": "contains", "value": "x"}]}
    )
    negative = validate_conditions(
        {"conditions": [{"field": "notes", "op": "not_contains", "value": "x"}]}
    )

    assert matches(positive, transaction) is False
    assert matches(negative, transaction) is True


# ---------------------------------------------------------------------------
# Priority and ordering
# ---------------------------------------------------------------------------


def test_lower_priority_number_wins(db: Session, user: User, account: Account):
    """Two rules disagree. The order must be defined, not incidental."""
    groceries = category(db, "food_and_drink.groceries")
    restaurants = category(db, "food_and_drink.restaurants")

    make_rule(
        db,
        user,
        name="Low priority",
        priority=200,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(restaurants.id)},
    )
    make_rule(
        db,
        user,
        name="High priority",
        priority=10,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    outcome = evaluate(db, user, transaction)

    assert outcome.category_id == groceries.id


def test_stop_processing_halts_the_chain(db: Session, user: User, account: Account):
    groceries = category(db, "food_and_drink.groceries")
    business = Tag(user_id=user.id, name="Business")
    db.add(business)
    db.flush()

    make_rule(
        db,
        user,
        name="Final word",
        priority=10,
        stop_processing=True,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )
    make_rule(
        db,
        user,
        name="Should not run",
        priority=20,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"add_tag_ids": [str(business.id)]},
    )

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    outcome = evaluate(db, user, transaction)

    assert outcome.category_id == groceries.id
    assert outcome.tag_ids == []


def test_inactive_rules_are_skipped(db: Session, user: User, account: Account):
    groceries = category(db, "food_and_drink.groceries")
    make_rule(
        db,
        user,
        is_active=False,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    assert evaluate(db, user, transaction).category_id is None


def test_a_malformed_stored_rule_is_skipped_not_fatal(
    db: Session, user: User, account: Account
):
    """A bad rule must not break categorization for everything else.

    Rules are validated on write, but a rule stored before a validation change
    -- or edited directly in the database -- can still be malformed. Skipping
    it loudly is the only safe behaviour: raising would abort the whole sync.
    """
    groceries = category(db, "food_and_drink.groceries")

    broken = Rule(
        user_id=user.id,
        name="Broken",
        priority=1,
        conditions={"nonsense": True},
        actions={"set_category_id": str(groceries.id)},
    )
    db.add(broken)
    make_rule(
        db,
        user,
        name="Good",
        priority=2,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )
    db.flush()

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    outcome = evaluate(db, user, transaction)

    assert outcome.category_id == groceries.id


def test_match_statistics_are_recorded(db: Session, user: User, account: Account):
    """A rule that has matched nothing in six months is how a user discovers
    they wrote it wrong."""
    groceries = category(db, "food_and_drink.groceries")
    rule = make_rule(
        db,
        user,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    evaluate(db, user, transaction)

    assert rule.match_count == 1
    assert rule.last_matched_at is not None


def test_tags_from_a_rule_must_belong_to_the_user(
    db: Session, user: User, other_user, account: Account
):
    """`add_tag_ids` comes from stored JSON, and JSON is not a foreign key.

    Without an ownership check, editing a rule's raw payload could attach
    somebody else's tag to your transaction.
    """
    stranger, _ = other_user
    their_tag = Tag(user_id=stranger.id, name="Theirs")
    db.add(their_tag)
    db.flush()

    make_rule(
        db,
        user,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"add_tag_ids": [str(their_tag.id)]},
    )

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    enrich_transaction(db, user, transaction)
    db.flush()

    assert transaction.tags == []


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def test_enrichment_assigns_merchant_and_category(
    db: Session, user: User, account: Account
):
    transaction = make_transaction(
        db, account, raw_name="STARBUCKS #123", raw_category="FOOD_AND_DRINK"
    )

    enrich_transaction(db, user, transaction)

    assert transaction.auto_merchant_id is not None
    assert transaction.auto_category_id == category(db, "food_and_drink").id
    assert transaction.auto_category_source == CategorySource.PLAID


def test_a_merchants_default_category_beats_plaids_guess(
    db: Session, user: User, account: Account
):
    """Teach the system once, and it applies everywhere."""
    coffee = category(db, "food_and_drink.coffee")
    merchant = Merchant(
        user_id=None,
        normalized_name="starbucks",
        display_name="Starbucks",
        default_category_id=coffee.id,
    )
    db.add(merchant)
    db.flush()

    transaction = make_transaction(
        db, account, raw_name="STARBUCKS #123", raw_category="FOOD_AND_DRINK"
    )
    enrich_transaction(db, user, transaction)

    assert transaction.auto_category_id == coffee.id
    assert transaction.auto_category_source == CategorySource.HEURISTIC


def test_a_rule_beats_the_merchant_default(db: Session, user: User, account: Account):
    coffee = category(db, "food_and_drink.coffee")
    business_meals = category(db, "food_and_drink.restaurants")

    merchant = Merchant(
        user_id=None,
        normalized_name="starbucks",
        display_name="Starbucks",
        default_category_id=coffee.id,
    )
    db.add(merchant)
    db.flush()

    make_rule(
        db,
        user,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "starbucks"}]},
        actions={"set_category_id": str(business_meals.id)},
    )

    transaction = make_transaction(db, account, raw_name="STARBUCKS #123")
    enrich_transaction(db, user, transaction)

    assert transaction.auto_category_id == business_meals.id
    assert transaction.auto_category_source == CategorySource.RULE
    assert transaction.auto_rule_id is not None


def test_enrichment_never_touches_user_columns(
    db: Session, user: User, account: Account
):
    """THE invariant.

    Re-running enrichment across three years of history -- after adding a
    rule, or improving the normalizer -- must be safe. It is safe precisely
    because the columns it would have to write are ones it does not touch.
    """
    groceries = category(db, "food_and_drink.groceries")
    restaurants = category(db, "food_and_drink.restaurants")

    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    transaction.user_category_id = restaurants.id
    transaction.user_description = "my own words"
    transaction.user_amount = Decimal("1.23")
    db.flush()

    make_rule(
        db,
        user,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )

    enrich_transaction(db, user, transaction)

    # The automatic layer changed...
    assert transaction.auto_category_id == groceries.id
    # ...the user's layer did not, and still wins.
    assert transaction.user_category_id == restaurants.id
    assert transaction.user_description == "my own words"
    assert transaction.user_amount == Decimal("1.23")
    assert transaction.effective_category_id == restaurants.id


def test_enrichment_is_idempotent(db: Session, user: User, account: Account):
    transaction = make_transaction(db, account, raw_name="STARBUCKS #123")

    enrich_transaction(db, user, transaction)
    first_merchant = transaction.auto_merchant_id
    first_category = transaction.auto_category_id

    enrich_transaction(db, user, transaction)

    assert transaction.auto_merchant_id == first_merchant
    assert transaction.auto_category_id == first_category


def test_a_rule_may_only_turn_flags_on(db: Session, user: User, account: Account):
    """A rule can mark something reviewed; it cannot un-review it.

    Clearing a user-set flag would undo a deliberate human action -- the same
    principle as never writing user_* columns.
    """
    transaction = make_transaction(db, account, raw_name="WHOLE FOODS MARKET")
    transaction.is_reviewed = True
    db.flush()

    make_rule(
        db,
        user,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"add_tag_ids": [], "set_notes": "from rule"},
    )

    enrich_transaction(db, user, transaction)

    assert transaction.is_reviewed is True


def test_batch_enrichment_loads_rules_once(db: Session, user: User, account: Account):
    """Correctness check for the batch path, which is the one sync uses."""
    groceries = category(db, "food_and_drink.groceries")
    make_rule(
        db,
        user,
        conditions={"conditions": [{"field": "raw_name", "op": "contains", "value": "market"}]},
        actions={"set_category_id": str(groceries.id)},
    )

    transactions = [
        make_transaction(
            db, account, raw_name="WHOLE FOODS MARKET", plaid_transaction_id=f"t{i}"
        )
        for i in range(5)
    ]

    counts = enrich_transactions(db, user, transactions)

    assert counts.processed == 5
    assert counts.categories_assigned == 5
    assert all(t.auto_category_id == groceries.id for t in transactions)

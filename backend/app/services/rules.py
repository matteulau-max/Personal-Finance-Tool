"""Evaluating user-defined rules against transactions.

A rule is "if a transaction looks like X, do Y". The shapes are validated in
`app/schemas/rules.py`; this module decides whether a given transaction
matches, and applies the result.

===========================================================================
Ordering, and why it must be explicit
===========================================================================

Two rules can match the same transaction and disagree. Without a defined
order, which one wins depends on the order the database happened to return
rows -- which SQL does not guarantee and which changes as the table grows.
The symptom is a transaction that is categorized differently on Tuesday than
it was on Monday, with nothing having changed.

So rules are always evaluated by `(priority, created_at)`: lowest priority
number first, ties broken by age. `stop_processing` lets a rule declare
itself the final word without the user having to disable everything below it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Rule, Tag, Transaction, TransactionTag, User
from app.schemas.rules import (
    ConditionGroup,
    IdCondition,
    NumericCondition,
    RuleActions,
    TextCondition,
    validate_actions,
    validate_conditions,
)

logger = logging.getLogger(__name__)


@dataclass
class RuleOutcome:
    """What the rules collectively decided for one transaction."""

    category_id: uuid.UUID | None = None
    merchant_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] = field(default_factory=list)
    notes: str | None = None
    mark_reviewed: bool = False
    hide: bool = False
    matched_rule_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def first_rule_id(self) -> uuid.UUID | None:
        return self.matched_rule_ids[0] if self.matched_rule_ids else None


# ---------------------------------------------------------------------------
# Field access
# ---------------------------------------------------------------------------


def _text_value(transaction: Transaction, field_name: str) -> str | None:
    if field_name == "description":
        # The EFFECTIVE description, so a rule written against what the user
        # sees behaves the way they expect.
        return transaction.effective_description
    if field_name == "raw_name":
        return transaction.raw_name
    if field_name == "merchant_name":
        return transaction.raw_merchant_name
    if field_name == "notes":
        return transaction.user_notes
    return None


def _id_value(transaction: Transaction, field_name: str) -> uuid.UUID | None:
    if field_name == "account_id":
        return transaction.account_id
    if field_name == "category_id":
        return transaction.effective_category_id
    if field_name == "merchant_id":
        return transaction.effective_merchant_id
    return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _match_text(condition: TextCondition, transaction: Transaction) -> bool:
    actual = _text_value(transaction, condition.field)

    if condition.op == "is_empty":
        return not actual
    if condition.op == "is_not_empty":
        return bool(actual)

    if actual is None:
        # A missing field matches only the negative operators. "notes does not
        # contain X" is true when there are no notes; "notes contains X" is
        # not. Getting this backwards makes every not_contains rule match
        # everything.
        return condition.op in ("not_contains", "not_equals")

    haystack = actual if condition.case_sensitive else actual.lower()
    needle = condition.value if condition.case_sensitive else condition.value.lower()

    match condition.op:
        case "contains":
            return needle in haystack
        case "not_contains":
            return needle not in haystack
        case "equals":
            return haystack == needle
        case "not_equals":
            return haystack != needle
        case "starts_with":
            return haystack.startswith(needle)
        case "ends_with":
            return haystack.endswith(needle)

    return False


def _match_numeric(condition: NumericCondition, transaction: Transaction) -> bool:
    actual: Decimal = transaction.effective_amount
    expected = condition.value

    match condition.op:
        case "eq":
            return actual == expected
        case "ne":
            return actual != expected
        case "gt":
            return actual > expected
        case "gte":
            return actual >= expected
        case "lt":
            return actual < expected
        case "lte":
            return actual <= expected

    return False


def _match_id(condition: IdCondition, transaction: Transaction) -> bool:
    actual = _id_value(transaction, condition.field)

    if condition.op == "is":
        return actual == condition.value
    return actual != condition.value


def matches(group: ConditionGroup, transaction: Transaction) -> bool:
    """Recursively evaluate a condition tree."""
    results: list[bool] = []

    for condition in group.conditions:
        if isinstance(condition, ConditionGroup):
            results.append(matches(condition, transaction))
        elif isinstance(condition, TextCondition):
            results.append(_match_text(condition, transaction))
        elif isinstance(condition, NumericCondition):
            results.append(_match_numeric(condition, transaction))
        elif isinstance(condition, IdCondition):
            results.append(_match_id(condition, transaction))

    if group.operator == "OR":
        return any(results)
    return all(results)


# ---------------------------------------------------------------------------
# Running the rule set
# ---------------------------------------------------------------------------


def active_rules(db: Session, user: User) -> list[Rule]:
    """This user's active rules, in evaluation order.

    The ORDER BY is not cosmetic -- see the note on ordering at the top of
    this file.
    """
    return (
        db.execute(
            select(Rule)
            .where(Rule.user_id == user.id, Rule.is_active.is_(True))
            .order_by(Rule.priority, Rule.created_at)
        )
        .scalars()
        .all()
    )


def evaluate(
    db: Session,
    user: User,
    transaction: Transaction,
    *,
    rules: list[Rule] | None = None,
    record_stats: bool = True,
) -> RuleOutcome:
    """Run the rule set against one transaction.

    `rules` can be passed in to avoid re-querying per transaction when
    processing a batch -- with a thousand transactions and ten rules, that is
    one query instead of a thousand.
    """
    outcome = RuleOutcome()
    rule_list = rules if rules is not None else active_rules(db, user)

    for rule in rule_list:
        try:
            conditions = validate_conditions(rule.conditions)
            actions = validate_actions(rule.actions)
        except ValueError as exc:
            # A stored rule that no longer validates must not break the sync
            # for everything else. Skip it loudly and carry on.
            logger.warning("Skipping malformed rule %s: %s", rule.id, exc)
            continue

        if not matches(conditions, transaction):
            continue

        outcome.matched_rule_ids.append(rule.id)
        _merge_actions(outcome, actions)

        if record_stats:
            rule.match_count += 1
            rule.last_matched_at = datetime.now(timezone.utc)

        if rule.stop_processing:
            break

    return outcome


def _merge_actions(outcome: RuleOutcome, actions: RuleActions) -> None:
    """Combine a matching rule's actions into the running outcome.

    First writer wins for single-valued fields, because rules are evaluated
    highest-priority first -- a later, lower-priority rule must not override
    a decision already made above it. Tags accumulate instead, since applying
    two tags is not a conflict.
    """
    if actions.set_category_id and outcome.category_id is None:
        outcome.category_id = actions.set_category_id
    if actions.set_merchant_id and outcome.merchant_id is None:
        outcome.merchant_id = actions.set_merchant_id
    if actions.set_notes and outcome.notes is None:
        outcome.notes = actions.set_notes

    for tag_id in actions.add_tag_ids:
        if tag_id not in outcome.tag_ids:
            outcome.tag_ids.append(tag_id)

    outcome.mark_reviewed = outcome.mark_reviewed or actions.mark_reviewed
    outcome.hide = outcome.hide or actions.hide


def apply_tags(
    db: Session, user: User, transaction: Transaction, tag_ids: list[uuid.UUID]
) -> int:
    """Attach tags, ignoring ones already present or not owned by this user.

    Filtering by owner matters: a rule's `add_tag_ids` comes from stored JSON,
    and JSON is not a foreign key. Without this check, editing a rule's raw
    payload could attach somebody else's tag to your transaction.
    """
    if not tag_ids:
        return 0

    owned = set(
        db.execute(
            select(Tag.id).where(Tag.user_id == user.id, Tag.id.in_(tag_ids))
        )
        .scalars()
        .all()
    )

    already = set(
        db.execute(
            select(TransactionTag.tag_id).where(
                TransactionTag.transaction_id == transaction.id
            )
        )
        .scalars()
        .all()
    )

    added = 0
    for tag_id in owned - already:
        db.add(TransactionTag(transaction_id=transaction.id, tag_id=tag_id))
        added += 1

    return added

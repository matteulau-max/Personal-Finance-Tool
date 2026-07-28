"""The enrichment pipeline.

Turns a raw imported transaction into a meaningful one:

    WHOLEFDS MKT #10259  ->  Whole Foods  ·  Groceries  ·  #reimbursable

Runs in one place so that a transaction arriving from a Plaid sync, a CSV
import, or a manual entry is enriched identically. Anything that only ran
inside the sync engine would silently not apply to the other two.

===========================================================================
The invariant this module must never break
===========================================================================

Enrichment writes ONLY `auto_*` columns (plus tags, which are additive).
It never writes a `user_*` column.

That is what makes re-running it safe. Backfilling a new rule across three
years of history, or re-enriching after improving the normalizer, cannot
destroy a single manual correction -- the columns it would have to write are
ones it does not touch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CategorySource, Rule, Transaction, User
from app.services.categorization import decide_category
from app.services.merchants import resolve_merchant
from app.services.rules import apply_tags, evaluate

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentCounts:
    processed: int = 0
    merchants_assigned: int = 0
    categories_assigned: int = 0
    tags_applied: int = 0
    rules_matched: int = 0


def enrich_transaction(
    db: Session,
    user: User,
    transaction: Transaction,
    *,
    rules: list[Rule] | None = None,
    record_stats: bool = True,
) -> None:
    """Enrich one transaction in place. Caller commits."""
    # 1. Merchant first: category resolution depends on it, since a merchant
    #    carries a default category.
    merchant = resolve_merchant(
        db,
        user,
        raw_name=transaction.raw_name,
        plaid_merchant_name=transaction.raw_merchant_name,
    )
    if merchant is not None:
        transaction.auto_merchant_id = merchant.id

    # 2. Rules, which outrank everything automatic.
    outcome = evaluate(
        db, user, transaction, rules=rules, record_stats=record_stats
    )

    if outcome.merchant_id is not None:
        transaction.auto_merchant_id = outcome.merchant_id

    # 3. Category, with the rule's answer taking precedence if there was one.
    decision = decide_category(
        db,
        user,
        transaction,
        rule_category_id=outcome.category_id,
        rule_id=outcome.first_rule_id,
    )
    transaction.auto_category_id = decision.category_id
    transaction.auto_category_source = decision.source
    transaction.auto_rule_id = decision.rule_id

    # 4. Side effects from rules.
    if outcome.tag_ids:
        apply_tags(db, user, transaction, outcome.tag_ids)

    # `is_reviewed` and `is_hidden` are user-facing flags, so a rule may only
    # ever turn them ON. Letting a rule clear them would undo a deliberate
    # human action -- the same principle as never writing user_* columns.
    if outcome.mark_reviewed:
        transaction.is_reviewed = True
    if outcome.hide:
        transaction.is_hidden = True

    db.flush()


def enrich_transactions(
    db: Session,
    user: User,
    transactions: list[Transaction],
    *,
    record_stats: bool = True,
) -> EnrichmentCounts:
    """Enrich a batch.

    Loads the rule set once rather than per transaction. With a thousand
    transactions and ten rules that is one query instead of a thousand -- the
    difference between a sync taking a second and taking a minute.
    """
    counts = EnrichmentCounts()
    if not transactions:
        return counts

    rules = (
        db.execute(
            select(Rule)
            .where(Rule.user_id == user.id, Rule.is_active.is_(True))
            .order_by(Rule.priority, Rule.created_at)
        )
        .scalars()
        .all()
    )

    for transaction in transactions:
        before_merchant = transaction.auto_merchant_id
        before_category = transaction.auto_category_id

        enrich_transaction(
            db, user, transaction, rules=rules, record_stats=record_stats
        )

        counts.processed += 1
        if transaction.auto_merchant_id and before_merchant != transaction.auto_merchant_id:
            counts.merchants_assigned += 1
        if transaction.auto_category_id and before_category != transaction.auto_category_id:
            counts.categories_assigned += 1
        if transaction.auto_category_source == CategorySource.RULE:
            counts.rules_matched += 1

    return counts


def enrich_user_transactions(
    db: Session, user: User, *, limit: int | None = None
) -> EnrichmentCounts:
    """Re-enrich a user's existing transactions.

    Used after adding a rule, correcting a merchant, or improving the
    normalizer. Safe to run repeatedly and at any time, precisely because
    enrichment never touches a user_* column.
    """
    statement = (
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.raw_date.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)

    transactions = db.execute(statement).scalars().all()
    return enrich_transactions(db, user, transactions)

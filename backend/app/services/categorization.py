"""Deciding what category a transaction belongs to.

===========================================================================
Four sources, in order of trust
===========================================================================

    1. The user           user_category_id -- always wins, never touched here
    2. A matching rule    "SQ *BLUE BOTTLE" -> Coffee Shops
    3. The merchant       Whole Foods -> Groceries, learned once
    4. Plaid's guess      personal_finance_category, mapped onto our tree

This module handles 2-4 and writes only `auto_category_id`. Layer 1 lives in
a different column entirely and is never written by automation -- the
three-layer model from Milestone 2, applied here.

`auto_category_source` records WHICH of 2-4 decided, which is what lets the
UI answer "why is this Groceries?" and lets a better categorizer overwrite a
worse one's guess without ever overwriting a human's.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, CategorySource, Merchant, Transaction, User

logger = logging.getLogger(__name__)

# Plaid's `personal_finance_category.primary` mapped onto our category slugs.
#
# Plaid's taxonomy is coarser than ours, so these map to a PARENT category
# (Food & Drink, not Groceries). That is deliberate: a confident coarse answer
# is more useful than a specific guess that is often wrong. Rules and merchant
# defaults refine it, and both outrank this.
PLAID_CATEGORY_MAP: dict[str, str] = {
    "INCOME": "income",
    "TRANSFER_IN": "transfer",
    "TRANSFER_OUT": "transfer",
    "LOAN_PAYMENTS": "financial",
    "BANK_FEES": "financial.fees",
    "ENTERTAINMENT": "entertainment",
    "FOOD_AND_DRINK": "food_and_drink",
    "GENERAL_MERCHANDISE": "shopping",
    "HOME_IMPROVEMENT": "housing.maintenance",
    "MEDICAL": "health",
    "PERSONAL_CARE": "personal.personal_care",
    "GENERAL_SERVICES": "personal",
    "GOVERNMENT_AND_NON_PROFIT": "financial.taxes",
    "TRANSPORTATION": "transportation",
    "TRAVEL": "travel",
    "RENT_AND_UTILITIES": "housing",
}


@dataclass(frozen=True)
class CategoryDecision:
    category_id: uuid.UUID | None
    source: CategorySource
    rule_id: uuid.UUID | None = None


def category_id_for_slug(db: Session, slug: str) -> uuid.UUID | None:
    """Look up a seeded system category by its stable slug.

    Slugs rather than names because names get renamed and translated; the
    slug is the contract between code and seed data.
    """
    return db.execute(
        select(Category.id).where(
            Category.slug == slug, Category.user_id.is_(None)
        )
    ).scalar_one_or_none()


def categorize_from_plaid(db: Session, plaid_category: str | None) -> uuid.UUID | None:
    if not plaid_category:
        return None

    slug = PLAID_CATEGORY_MAP.get(plaid_category.upper())
    if slug is None:
        # An unmapped Plaid category is not an error -- Plaid adds new ones.
        # Log it so the map can be extended, and leave the transaction
        # uncategorized rather than guessing.
        logger.info("No mapping for Plaid category %r", plaid_category)
        return None

    return category_id_for_slug(db, slug)


def categorize_from_merchant(
    db: Session, merchant_id: uuid.UUID | None
) -> uuid.UUID | None:
    """A merchant's default category.

    This one field is what makes categorization feel intelligent: teach the
    system "Whole Foods is Groceries" once, and every past and future Whole
    Foods transaction is categorized -- no rule to write, no per-transaction
    work.
    """
    if merchant_id is None:
        return None

    merchant = db.get(Merchant, merchant_id)
    return merchant.default_category_id if merchant else None


def decide_category(
    db: Session,
    user: User,
    transaction: Transaction,
    *,
    rule_category_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
) -> CategoryDecision:
    """Pick the best available automatic category.

    Note what is absent: any consideration of `user_category_id`. This
    function's answer lands in `auto_category_id`, and the effective category
    is COALESCE(user, auto). A user's choice is not overridden here because it
    is not visible here -- the separation is structural, not a rule someone
    has to remember.
    """
    if rule_category_id is not None:
        return CategoryDecision(rule_category_id, CategorySource.RULE, rule_id)

    merchant_id = transaction.user_merchant_id or transaction.auto_merchant_id
    from_merchant = categorize_from_merchant(db, merchant_id)
    if from_merchant is not None:
        return CategoryDecision(from_merchant, CategorySource.HEURISTIC)

    from_plaid = categorize_from_plaid(db, transaction.raw_category)
    if from_plaid is not None:
        return CategoryDecision(from_plaid, CategorySource.PLAID)

    return CategoryDecision(None, CategorySource.NONE)


def teach_merchant_category(
    db: Session, merchant: Merchant, category_id: uuid.UUID, user: User
) -> Merchant:
    """Remember that this merchant means this category, for this user.

    When the merchant is global (shared by everyone), we do NOT edit it --
    that would change other people's data based on one person's opinion.
    Instead we create the user's own copy, which `resolve_merchant` already
    prefers over the global one.

    This is the same principle as `learn_alias`: corrections are personal
    until there is real evidence they are universal.
    """
    if merchant.user_id == user.id:
        merchant.default_category_id = category_id
        db.flush()
        return merchant

    existing = db.execute(
        select(Merchant).where(
            Merchant.user_id == user.id,
            Merchant.normalized_name == merchant.normalized_name,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.default_category_id = category_id
        db.flush()
        return existing

    personal = Merchant(
        user_id=user.id,
        normalized_name=merchant.normalized_name,
        display_name=merchant.display_name,
        logo_url=merchant.logo_url,
        website=merchant.website,
        is_subscription=merchant.is_subscription,
        default_category_id=category_id,
    )
    db.add(personal)
    db.flush()
    return personal

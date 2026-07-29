"""Endpoints for categories, merchants, tags, and rules."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.db.scoping import scoped_get, scoped_select
from app.models import Category, Merchant, Rule, Tag, Transaction, TransactionStatus
from app.schemas.taxonomy import (
    CategoryCreateRequest,
    CategoryResponse,
    MerchantResponse,
    MerchantUpdateRequest,
    RuleApplyResponse,
    RuleCreateRequest,
    RulePreviewResponse,
    RuleResponse,
    RuleUpdateRequest,
    TagCreateRequest,
    TagResponse,
    TagUpdateRequest,
)
from app.schemas.rules import validate_conditions
from app.services.enrichment import enrich_transactions
from app.services.rules import matches

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["taxonomy"])


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=list[CategoryResponse])
def list_categories(current_user: CurrentUser, db: DbSession) -> list[CategoryResponse]:
    """The user's categories plus the shared built-in ones."""
    rows = (
        db.execute(
            scoped_select(Category, current_user)
            .where(Category.is_archived.is_(False))
            .order_by(Category.sort_order, Category.name)
        )
        .scalars()
        .all()
    )
    return [CategoryResponse.model_validate(row) for row in rows]


@router.post(
    "/categories", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED
)
def create_category(
    payload: CategoryCreateRequest, current_user: CurrentUser, db: DbSession
) -> CategoryResponse:
    if payload.parent_id is not None:
        parent = db.execute(
            select(Category).where(
                Category.id == payload.parent_id,
                (Category.user_id == current_user.id) | (Category.user_id.is_(None)),
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Parent category not found")

    category = Category(
        user_id=current_user.id,
        name=payload.name,
        parent_id=payload.parent_id,
        icon=payload.icon,
        color=payload.color,
        is_income=payload.is_income,
        is_transfer=payload.is_transfer,
        is_system=False,
    )
    db.add(category)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have a category with that name under that parent.",
        ) from exc

    db.refresh(category)
    return CategoryResponse.model_validate(category)


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


@router.get("/merchants", response_model=list[MerchantResponse])
def list_merchants(
    current_user: CurrentUser,
    db: DbSession,
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MerchantResponse]:
    statement = scoped_select(Merchant, current_user)

    if search:
        statement = statement.where(Merchant.display_name.ilike(f"%{search}%"))

    rows = (
        db.execute(statement.order_by(Merchant.display_name).limit(limit))
        .scalars()
        .all()
    )

    return [
        MerchantResponse.model_validate(row).model_copy(
            update={"is_personal": row.user_id is not None}
        )
        for row in rows
    ]


@router.patch("/merchants/{merchant_id}", response_model=MerchantResponse)
def update_merchant(
    merchant_id: uuid.UUID,
    payload: MerchantUpdateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> MerchantResponse:
    """Rename a merchant, or set its default category.

    Editing a GLOBAL merchant creates the user's own copy instead of changing
    the shared row. One person's opinion about a merchant's name is not
    evidence for everybody, and a global write would silently change other
    users' data.
    """
    merchant = db.execute(
        select(Merchant).where(
            Merchant.id == merchant_id,
            (Merchant.user_id == current_user.id) | (Merchant.user_id.is_(None)),
        )
    ).scalar_one_or_none()

    if merchant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Merchant not found")

    updates = payload.model_dump(exclude_unset=True)

    if merchant.user_id is None:
        existing = db.execute(
            select(Merchant).where(
                Merchant.user_id == current_user.id,
                Merchant.normalized_name == merchant.normalized_name,
            )
        ).scalar_one_or_none()

        target = existing or Merchant(
            user_id=current_user.id,
            normalized_name=merchant.normalized_name,
            display_name=merchant.display_name,
            logo_url=merchant.logo_url,
            website=merchant.website,
            is_subscription=merchant.is_subscription,
            default_category_id=merchant.default_category_id,
        )
        if existing is None:
            db.add(target)
    else:
        target = merchant

    for field, value in updates.items():
        setattr(target, field, value)

    db.commit()
    db.refresh(target)

    return MerchantResponse.model_validate(target).model_copy(
        update={"is_personal": True}
    )


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@router.get("/tags", response_model=list[TagResponse])
def list_tags(current_user: CurrentUser, db: DbSession) -> list[TagResponse]:
    rows = (
        db.execute(scoped_select(Tag, current_user).order_by(Tag.name))
        .scalars()
        .all()
    )
    return [TagResponse.model_validate(row) for row in rows]


@router.post("/tags", response_model=TagResponse, status_code=status.HTTP_201_CREATED)
def create_tag(
    payload: TagCreateRequest, current_user: CurrentUser, db: DbSession
) -> TagResponse:
    tag = Tag(
        user_id=current_user.id,
        name=payload.name,
        color=payload.color,
        description=payload.description,
    )
    db.add(tag)

    try:
        db.commit()
    except IntegrityError as exc:
        # Caught rather than pre-checked, because a pre-check is a race: two
        # requests can both find nothing and both insert. The unique index
        # from migration 5d94c4e6a26a is what actually decides, and it is
        # case-insensitive -- so "vacation" collides with "Vacation".
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "You already have a tag with that name."
        ) from exc

    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.patch("/tags/{tag_id}", response_model=TagResponse)
def update_tag(
    tag_id: uuid.UUID,
    payload: TagUpdateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> TagResponse:
    tag = scoped_get(db, Tag, tag_id, current_user)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tag, field, value.strip() if field == "name" and value else value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "You already have a tag with that name."
        ) from exc

    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.delete(
    "/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def delete_tag(tag_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    """Delete a tag. The transactions it was applied to are untouched.

    The join rows go via ON DELETE CASCADE; the transactions themselves do
    not. Removing a label must never remove the money.
    """
    tag = scoped_get(db, Tag, tag_id, current_user)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")

    db.delete(tag)
    db.commit()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@router.get("/rules", response_model=list[RuleResponse])
def list_rules(current_user: CurrentUser, db: DbSession) -> list[RuleResponse]:
    rows = (
        db.execute(
            scoped_select(Rule, current_user).order_by(Rule.priority, Rule.created_at)
        )
        .scalars()
        .all()
    )
    return [RuleResponse.model_validate(row) for row in rows]


@router.post("/rules", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RuleCreateRequest, current_user: CurrentUser, db: DbSession
) -> RuleResponse:
    rule = Rule(
        user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        priority=payload.priority,
        is_active=payload.is_active,
        stop_processing=payload.stop_processing,
        conditions=payload.conditions,
        actions=payload.actions,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return RuleResponse.model_validate(rule)


@router.patch("/rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> RuleResponse:
    rule = scoped_get(db, Rule, rule_id, current_user)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)

    db.commit()
    db.refresh(rule)
    return RuleResponse.model_validate(rule)


@router.delete(
    "/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def delete_rule(rule_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    """Delete a rule.

    Transactions it previously categorized keep their category -- the
    `auto_rule_id` foreign key is ON DELETE SET NULL. Deleting a rule means
    "stop applying this going forward", not "undo everything it ever did".
    Re-running enrichment is how you undo it, deliberately.
    """
    rule = scoped_get(db, Rule, rule_id, current_user)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")

    db.delete(rule)
    db.commit()


@router.post("/rules/{rule_id}/preview", response_model=RulePreviewResponse)
def preview_rule(
    rule_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=2000, ge=1, le=20_000),
) -> RulePreviewResponse:
    """Report what a rule WOULD match, changing nothing.

    Applying an untested rule across years of history is a frightening button
    to press. Seeing "matches 12 transactions, for example these three" first
    turns it into an informed decision -- and a rule matching 4,000 rows when
    you expected 12 is obviously wrong before it touches anything.
    """
    rule = scoped_get(db, Rule, rule_id, current_user)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")

    try:
        conditions = validate_conditions(rule.conditions)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Rule is malformed: {exc}"
        ) from exc

    transactions = (
        db.execute(
            scoped_select(Transaction, current_user)
            .where(Transaction.status != TransactionStatus.REMOVED)
            .order_by(Transaction.raw_date.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    matched = [t for t in transactions if matches(conditions, t)]

    return RulePreviewResponse(
        matched=len(matched),
        sample=[t.effective_description for t in matched[:5]],
    )


@router.post("/rules/apply", response_model=RuleApplyResponse)
def apply_rules(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=5000, ge=1, le=50_000),
) -> RuleApplyResponse:
    """Re-run enrichment over existing transactions.

    Safe to run at any time and as often as you like: enrichment writes only
    `auto_*` columns, so it cannot destroy a manual correction no matter how
    many times it runs.
    """
    transactions = (
        db.execute(
            scoped_select(Transaction, current_user)
            .order_by(Transaction.raw_date.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    counts = enrich_transactions(db, current_user, transactions)
    db.commit()

    return RuleApplyResponse(enriched=counts.processed)

"""Transaction endpoints: listing, filtering, correcting, bulk editing."""

from __future__ import annotations

import logging
import uuid
import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, DbSession
from app.db.scoping import scoped_get, scoped_select
from app.models import (
    AuditAction,
    AuditActor,
    AuditLog,
    Category,
    Merchant,
    Tag,
    Transaction,
    TransactionStatus,
    TransactionTag,
    User,
)
from app.schemas.transaction import (
    BulkUpdateRequest,
    BulkUpdateResponse,
    CategorySummary,
    MerchantSummary,
    TagSummary,
    TransactionPage,
    TransactionResponse,
    TransactionUpdateRequest,
)
from app.services.categorization import teach_merchant_category
from app.services.merchants import learn_alias

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def to_response(transaction: Transaction) -> TransactionResponse:
    """Flatten the three-layer model into what a client wants to render."""
    category = transaction.user_category or transaction.auto_category
    merchant = transaction.user_merchant or transaction.auto_merchant

    return TransactionResponse(
        id=transaction.id,
        account_id=transaction.account_id,
        status=transaction.status,
        source=transaction.source,
        amount=transaction.effective_amount,
        date=transaction.effective_date,
        description=transaction.effective_description,
        currency_code=transaction.raw_currency_code,
        category=CategorySummary.model_validate(category) if category else None,
        merchant=MerchantSummary.model_validate(merchant) if merchant else None,
        tags=[TagSummary.model_validate(tag) for tag in transaction.tags],
        notes=transaction.user_notes,
        is_hidden=transaction.is_hidden,
        is_reviewed=transaction.is_reviewed,
        category_source=transaction.auto_category_source,
        is_user_modified=transaction.is_user_modified,
        raw_name=transaction.raw_name,
        raw_amount=transaction.raw_amount,
        raw_date=transaction.raw_date,
        created_at=transaction.created_at,
    )


@router.get("", response_model=TransactionPage)
def list_transactions(
    current_user: CurrentUser,
    db: DbSession,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    merchant_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=200),
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    is_reviewed: bool | None = None,
    include_hidden: bool = False,
    include_removed: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TransactionPage:
    """List transactions with filters.

    Every filter composes: "restaurants over $100 last month tagged Business"
    is one request. That combination is exactly what the AI questions in
    Milestone 7 will need, so the filtering lives here rather than being
    reinvented later.

    Note the filters use `effective_*` throughout. Filtering on `raw_amount`
    would ignore user corrections -- searching for "over $100" would miss a
    transaction the user had corrected TO $150.
    """
    statement = scoped_select(Transaction, current_user).options(
        selectinload(Transaction.tags),
        selectinload(Transaction.user_category),
        selectinload(Transaction.auto_category),
        selectinload(Transaction.user_merchant),
        selectinload(Transaction.auto_merchant),
    )

    if not include_removed:
        # Soft-deleted rows are excluded by default. They exist for recovery
        # and audit, not for everyday viewing.
        statement = statement.where(Transaction.status != TransactionStatus.REMOVED)
    if not include_hidden:
        statement = statement.where(Transaction.is_hidden.is_(False))

    if start_date is not None:
        statement = statement.where(Transaction.effective_date >= start_date)
    if end_date is not None:
        statement = statement.where(Transaction.effective_date <= end_date)
    if account_id is not None:
        statement = statement.where(Transaction.account_id == account_id)
    if category_id is not None:
        statement = statement.where(Transaction.effective_category_id == category_id)
    if merchant_id is not None:
        statement = statement.where(Transaction.effective_merchant_id == merchant_id)
    if min_amount is not None:
        statement = statement.where(Transaction.effective_amount >= min_amount)
    if max_amount is not None:
        statement = statement.where(Transaction.effective_amount <= max_amount)
    if is_reviewed is not None:
        statement = statement.where(Transaction.is_reviewed.is_(is_reviewed))

    if tag_id is not None:
        # EXISTS rather than a JOIN: a join would duplicate a transaction row
        # once per matching tag, breaking both the count and the page size.
        statement = statement.where(
            select(TransactionTag.transaction_id)
            .where(
                TransactionTag.transaction_id == Transaction.id,
                TransactionTag.tag_id == tag_id,
            )
            .exists()
        )

    if search:
        # ILIKE with a leading wildcard, backed by the pg_trgm index added in
        # migration 5d94c4e6a26a.
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                Transaction.raw_name.ilike(pattern),
                Transaction.raw_merchant_name.ilike(pattern),
                Transaction.user_description.ilike(pattern),
                Transaction.user_notes.ilike(pattern),
            )
        )

    # Count before pagination. `order_by(None)` drops the ORDER BY, which a
    # COUNT does not need and which PostgreSQL would otherwise sort for
    # nothing.
    total = db.execute(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ).scalar_one()

    rows = (
        db.execute(
            statement.order_by(
                Transaction.effective_date.desc(), Transaction.created_at.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .unique()
        .all()
    )

    return TransactionPage(
        items=[to_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{transaction_id}", response_model=TransactionResponse)
def read_transaction(
    transaction_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> TransactionResponse:
    transaction = scoped_get(db, Transaction, transaction_id, current_user)
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return to_response(transaction)


@router.patch("/{transaction_id}", response_model=TransactionResponse)
def update_transaction(
    transaction_id: uuid.UUID,
    payload: TransactionUpdateRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> TransactionResponse:
    """Correct a transaction.

    Everything here writes a `user_*` column. The bank's values are never
    touched, so every edit is reversible by sending the same field as `null`.
    """
    transaction = scoped_get(db, Transaction, transaction_id, current_user)
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")

    # `exclude_unset` distinguishes "explicitly set to null" (reset this
    # override) from "not mentioned" (leave it alone). Without it, a PATCH
    # containing only `{"notes": "x"}` would clear the user's category.
    updates = payload.model_dump(exclude_unset=True)
    updates.pop("apply_to_similar", None)
    tag_ids = updates.pop("tag_ids", None)

    before: dict[str, str | None] = {}
    after: dict[str, str | None] = {}

    field_map = {
        "category_id": "user_category_id",
        "merchant_id": "user_merchant_id",
        "description": "user_description",
        "amount": "user_amount",
        "date": "user_date",
        "notes": "user_notes",
        "is_hidden": "is_hidden",
        "is_reviewed": "is_reviewed",
    }

    for request_field, column in field_map.items():
        if request_field not in updates:
            continue

        value = updates[request_field]

        if request_field == "category_id" and value is not None:
            _assert_category_visible(db, current_user, value)
        if request_field == "merchant_id" and value is not None:
            _assert_merchant_visible(db, current_user, value)

        old = getattr(transaction, column)
        if old == value:
            continue

        before[column] = _stringify(old)
        after[column] = _stringify(value)
        setattr(transaction, column, value)

    if tag_ids is not None:
        _replace_tags(db, current_user, transaction, tag_ids)
        before["tags"] = "(replaced)"
        after["tags"] = ",".join(str(t) for t in tag_ids)

    db.flush()

    if payload.apply_to_similar:
        _apply_to_similar(db, current_user, transaction, payload)

    if before:
        # Every human edit is recorded. This is what makes "why does this say
        # Groceries?" answerable, and what distinguishes a user correction
        # from a bank restatement in the audit trail.
        db.add(
            AuditLog(
                user_id=current_user.id,
                entity_type="transaction",
                entity_id=transaction.id,
                action=AuditAction.UPDATE,
                actor=AuditActor.USER,
                before_values=before,
                after_values=after,
                reason="manual correction",
            )
        )

    db.commit()
    db.refresh(transaction)
    return to_response(transaction)


@router.post("/bulk", response_model=BulkUpdateResponse)
def bulk_update(
    payload: BulkUpdateRequest, current_user: CurrentUser, db: DbSession
) -> BulkUpdateResponse:
    """Apply one change to many transactions.

    Ids the user does not own are silently skipped rather than rejected. A
    single foreign id would otherwise fail the whole batch and, worse, the
    error would confirm which ids exist.
    """
    transactions = (
        db.execute(
            scoped_select(Transaction, current_user)
            .options(selectinload(Transaction.tags))
            .where(Transaction.id.in_(payload.transaction_ids))
        )
        .scalars()
        .unique()
        .all()
    )

    if payload.category_id is not None:
        _assert_category_visible(db, current_user, payload.category_id)

    owned_tag_ids = set(
        db.execute(
            select(Tag.id).where(
                Tag.user_id == current_user.id,
                Tag.id.in_(payload.add_tag_ids + payload.remove_tag_ids),
            )
        )
        .scalars()
        .all()
    )

    for transaction in transactions:
        if payload.category_id is not None:
            transaction.user_category_id = payload.category_id
        if payload.is_reviewed is not None:
            transaction.is_reviewed = payload.is_reviewed
        if payload.is_hidden is not None:
            transaction.is_hidden = payload.is_hidden

        current = {tag.id for tag in transaction.tags}

        for tag_id in payload.add_tag_ids:
            if tag_id in owned_tag_ids and tag_id not in current:
                db.add(
                    TransactionTag(transaction_id=transaction.id, tag_id=tag_id)
                )
        for tag_id in payload.remove_tag_ids:
            if tag_id in current:
                db.execute(
                    TransactionTag.__table__.delete().where(
                        TransactionTag.transaction_id == transaction.id,
                        TransactionTag.tag_id == tag_id,
                    )
                )

        db.add(
            AuditLog(
                user_id=current_user.id,
                entity_type="transaction",
                entity_id=transaction.id,
                action=AuditAction.UPDATE,
                actor=AuditActor.USER,
                after_values={"bulk": "true"},
                reason="bulk edit",
            )
        )

    db.commit()

    return BulkUpdateResponse(
        updated=len(transactions),
        skipped=len(payload.transaction_ids) - len(transactions),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stringify(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (Decimal, dt.date, uuid.UUID)):
        return str(value)
    return str(value)


def _assert_category_visible(db: Session, user: User, category_id: uuid.UUID) -> None:
    """A user may only assign a category they can see.

    Without this, a client could set any UUID -- including another user's
    private category, which would then leak its name into their transaction
    list.
    """
    visible = db.execute(
        select(Category.id).where(
            Category.id == category_id,
            (Category.user_id == user.id) | (Category.user_id.is_(None)),
        )
    ).scalar_one_or_none()

    if visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")


def _assert_merchant_visible(db: Session, user: User, merchant_id: uuid.UUID) -> None:
    visible = db.execute(
        select(Merchant.id).where(
            Merchant.id == merchant_id,
            (Merchant.user_id == user.id) | (Merchant.user_id.is_(None)),
        )
    ).scalar_one_or_none()

    if visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Merchant not found")


def _replace_tags(
    db: Session, user: User, transaction: Transaction, tag_ids: list[uuid.UUID]
) -> None:
    owned = set(
        db.execute(
            select(Tag.id).where(Tag.user_id == user.id, Tag.id.in_(tag_ids))
        )
        .scalars()
        .all()
    )

    db.execute(
        TransactionTag.__table__.delete().where(
            TransactionTag.transaction_id == transaction.id
        )
    )
    for tag_id in owned:
        db.add(TransactionTag(transaction_id=transaction.id, tag_id=tag_id))


def _apply_to_similar(
    db: Session,
    user: User,
    transaction: Transaction,
    payload: TransactionUpdateRequest,
) -> None:
    """Turn one correction into a lasting rule.

    ===================================================================
    Why this is opt-in rather than automatic
    ===================================================================

    It is tempting to learn from every correction silently. Don't. A user
    fixing ONE transaction ("this particular Amazon order was a gift") does
    not mean every Amazon order is a gift. Applying that silently to three
    years of history would be both surprising and hard to undo.

    So learning is explicit: the client sends `apply_to_similar: true`, which
    the UI surfaces as a checkbox. The user says whether this is a one-off or
    a pattern -- they are the only one who knows.

    What IS remembered here:
      * merchant corrections -> a MerchantAlias, so the descriptor resolves
        correctly forever
      * category corrections -> the merchant's default category, so every
        past and future transaction from that merchant is categorized
    """
    merchant = transaction.user_merchant or transaction.auto_merchant

    if merchant is not None and "merchant_id" in payload.model_fields_set:
        learn_alias(db, user, raw_name=transaction.raw_name, merchant=merchant)

    if merchant is not None and payload.category_id is not None:
        teach_merchant_category(db, merchant, payload.category_id, user)

    db.flush()

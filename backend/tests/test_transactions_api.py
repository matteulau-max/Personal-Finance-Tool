"""Transaction, tag, and rule endpoint tests."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Category, Merchant, Tag, Transaction, User
from tests.auth_helpers import auth_header
from tests.conftest import make_account, make_transaction


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


def seed(db: Session, user: User, count: int = 3):
    account = make_account(db, user)
    rows = [
        make_transaction(
            db,
            account,
            plaid_transaction_id=f"seed_{uuid.uuid4().hex[:8]}",
            raw_name=f"MERCHANT {i}",
            raw_amount=f"{10 * (i + 1)}.00",
            raw_date=dt.date(2026, 5, i + 1),
        )
        for i in range(count)
    ]
    db.flush()
    return account, rows


# ---------------------------------------------------------------------------
# Listing and filtering
# ---------------------------------------------------------------------------


def test_listing_requires_authentication(client: TestClient):
    assert client.get("/api/transactions").status_code == 401


def test_listing_returns_only_your_own(
    client: TestClient, db: Session, authenticated_user, other_user
):
    user, token = authenticated_user
    stranger, _ = other_user

    seed(db, user, 2)
    seed(db, stranger, 3)

    body = client.get("/api/transactions", headers=auth_header(token)).json()

    assert body["total"] == 2


def test_pagination_reports_the_total(
    client: TestClient, db: Session, authenticated_user
):
    """Without a total, pagination controls cannot say "1-2 of 5"."""
    user, token = authenticated_user
    seed(db, user, 5)

    body = client.get(
        "/api/transactions?limit=2&offset=0", headers=auth_header(token)
    ).json()

    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2


def test_results_are_newest_first(client: TestClient, db: Session, authenticated_user):
    user, token = authenticated_user
    seed(db, user, 3)

    items = client.get("/api/transactions", headers=auth_header(token)).json()["items"]
    dates = [item["date"] for item in items]

    assert dates == sorted(dates, reverse=True)


def test_date_range_filter(client: TestClient, db: Session, authenticated_user):
    user, token = authenticated_user
    seed(db, user, 5)

    body = client.get(
        "/api/transactions?start_date=2026-05-03&end_date=2026-05-04",
        headers=auth_header(token),
    ).json()

    assert body["total"] == 2


def test_amount_filters(client: TestClient, db: Session, authenticated_user):
    user, token = authenticated_user
    seed(db, user, 4)  # 10, 20, 30, 40

    body = client.get(
        "/api/transactions?min_amount=25", headers=auth_header(token)
    ).json()

    assert body["total"] == 2


def test_search_matches_the_description(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    make_transaction(db, account, raw_name="BLUE BOTTLE COFFEE")
    make_transaction(db, account, raw_name="SHELL GAS STATION")
    db.flush()

    body = client.get(
        "/api/transactions?search=coffee", headers=auth_header(token)
    ).json()

    assert body["total"] == 1


def test_filters_use_the_corrected_amount_not_the_raw_one(
    client: TestClient, db: Session, authenticated_user
):
    """Searching "over $100" must find a transaction the user corrected TO
    $150, and must NOT find one they corrected down to $5."""
    user, token = authenticated_user
    account = make_account(db, user)

    corrected_up = make_transaction(db, account, raw_amount="10.00")
    corrected_up.user_amount = Decimal("150.00")
    corrected_down = make_transaction(db, account, raw_amount="500.00")
    corrected_down.user_amount = Decimal("5.00")
    db.flush()

    body = client.get(
        "/api/transactions?min_amount=100", headers=auth_header(token)
    ).json()

    assert body["total"] == 1
    assert body["items"][0]["id"] == str(corrected_up.id)


def test_tag_filter_does_not_duplicate_rows(
    client: TestClient, db: Session, authenticated_user
):
    """A JOIN would return one row per matching tag, breaking the count.

    The endpoint uses EXISTS instead; this test is what would catch a
    regression back to a join.
    """
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account)

    tags = [Tag(user_id=user.id, name=f"Tag{i}") for i in range(3)]
    db.add_all(tags)
    db.flush()
    for tag in tags:
        client.patch(
            f"/api/transactions/{transaction.id}",
            headers=auth_header(token),
            json={"tag_ids": [str(t.id) for t in tags]},
        )

    body = client.get(
        f"/api/transactions?tag_id={tags[0].id}", headers=auth_header(token)
    ).json()

    assert body["total"] == 1


def test_hidden_and_removed_are_excluded_by_default(
    client: TestClient, db: Session, authenticated_user
):
    from app.models import TransactionStatus

    user, token = authenticated_user
    account = make_account(db, user)

    make_transaction(db, account, raw_name="VISIBLE")
    hidden = make_transaction(db, account, raw_name="HIDDEN")
    hidden.is_hidden = True
    removed = make_transaction(db, account, raw_name="REMOVED")
    removed.status = TransactionStatus.REMOVED
    db.flush()

    default = client.get("/api/transactions", headers=auth_header(token)).json()
    assert default["total"] == 1

    with_hidden = client.get(
        "/api/transactions?include_hidden=true", headers=auth_header(token)
    ).json()
    assert with_hidden["total"] == 2


def test_response_hides_internal_fields(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    make_transaction(db, account)
    db.flush()

    [item] = client.get("/api/transactions", headers=auth_header(token)).json()["items"]

    assert "user_id" not in item
    assert "plaid_transaction_id" not in item
    assert "raw_payload" not in item


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_correcting_a_transaction_leaves_the_raw_values_intact(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account, raw_amount="52.30", raw_name="WHOLEFDS")
    db.flush()

    response = client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"amount": "48.00", "description": "Weekly shop"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == "48.0000"
    assert body["description"] == "Weekly shop"
    # The bank's version is still there, and still returned.
    assert body["raw_amount"] == "52.3000"
    assert body["raw_name"] == "WHOLEFDS"
    assert body["is_user_modified"] is True


def test_sending_null_resets_an_override(
    client: TestClient, db: Session, authenticated_user
):
    """"Reset to original" is just setting the user_ column back to NULL."""
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account, raw_amount="52.30")
    db.flush()

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"amount": "48.00"},
    )
    body = client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"amount": None},
    ).json()

    assert body["amount"] == "52.3000"
    assert body["is_user_modified"] is False


def test_a_patch_does_not_clear_unmentioned_fields(
    client: TestClient, db: Session, authenticated_user
):
    """The classic PATCH bug, tested at the API level this time."""
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account)
    db.flush()

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"description": "Keep me"},
    )
    body = client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"notes": "a note"},
    ).json()

    assert body["description"] == "Keep me"
    assert body["notes"] == "a note"


def test_a_correction_is_audited(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account)
    db.flush()

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"notes": "why I changed it"},
    )

    entry = db.execute(
        select(AuditLog).where(
            AuditLog.entity_id == transaction.id, AuditLog.reason == "manual correction"
        )
    ).scalar_one()
    assert entry.after_values["user_notes"] == "why I changed it"


def test_cannot_assign_another_users_category(
    client: TestClient, db: Session, authenticated_user, other_user
):
    """Without this check, a private category's name would leak into the
    other user's transaction list."""
    user, token = authenticated_user
    stranger, _ = other_user

    theirs = Category(user_id=stranger.id, name="Their Secret Project")
    db.add(theirs)
    account = make_account(db, user)
    transaction = make_transaction(db, account)
    db.flush()

    response = client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"category_id": str(theirs.id)},
    )

    assert response.status_code == 404


def test_cannot_correct_another_users_transaction(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    stranger, _ = other_user
    account = make_account(db, stranger)
    theirs = make_transaction(db, account)
    db.flush()

    response = client.patch(
        f"/api/transactions/{theirs.id}",
        headers=auth_header(token),
        json={"notes": "hacked"},
    )

    assert response.status_code == 404


def test_only_your_own_tags_can_be_applied(
    client: TestClient, db: Session, authenticated_user, other_user
):
    user, token = authenticated_user
    stranger, _ = other_user

    mine = Tag(user_id=user.id, name="Mine")
    theirs = Tag(user_id=stranger.id, name="Theirs")
    db.add_all([mine, theirs])
    account = make_account(db, user)
    transaction = make_transaction(db, account)
    db.flush()

    body = client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"tag_ids": [str(mine.id), str(theirs.id)]},
    ).json()

    assert [tag["name"] for tag in body["tags"]] == ["Mine"]


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


def test_apply_to_similar_teaches_the_merchant_category(
    client: TestClient, db: Session, authenticated_user
):
    """One correction, applied forever -- but only when asked for."""
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account, raw_name="BLUE BOTTLE COFFEE")

    from app.services.enrichment import enrich_transaction

    enrich_transaction(db, user, transaction)
    db.flush()

    coffee = category(db, "food_and_drink.coffee")

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"category_id": str(coffee.id), "apply_to_similar": True},
    )

    # A NEW transaction from the same merchant now categorizes itself.
    future = make_transaction(
        db, account, raw_name="BLUE BOTTLE COFFEE", plaid_transaction_id="future"
    )
    enrich_transaction(db, user, future)

    assert future.auto_category_id == coffee.id


def test_learning_is_opt_in(client: TestClient, db: Session, authenticated_user):
    """A one-off correction must NOT rewrite the merchant's default.

    "This particular Amazon order was a gift" does not mean every Amazon order
    is a gift. Silent learning would apply that across years of history.
    """
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account, raw_name="BLUE BOTTLE COFFEE")

    from app.services.enrichment import enrich_transaction

    enrich_transaction(db, user, transaction)
    db.flush()

    coffee = category(db, "food_and_drink.coffee")

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"category_id": str(coffee.id)},  # apply_to_similar defaults to False
    )

    future = make_transaction(
        db, account, raw_name="BLUE BOTTLE COFFEE", plaid_transaction_id="future"
    )
    enrich_transaction(db, user, future)

    assert future.auto_category_id != coffee.id


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------


def test_bulk_categorize(client: TestClient, db: Session, authenticated_user):
    user, token = authenticated_user
    _, rows = seed(db, user, 3)
    groceries = category(db, "food_and_drink.groceries")

    response = client.post(
        "/api/transactions/bulk",
        headers=auth_header(token),
        json={
            "transaction_ids": [str(r.id) for r in rows],
            "category_id": str(groceries.id),
        },
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 3

    db.expire_all()
    for row in rows:
        db.refresh(row)
        assert row.user_category_id == groceries.id


def test_bulk_silently_skips_ids_you_do_not_own(
    client: TestClient, db: Session, authenticated_user, other_user
):
    """Rejecting the batch would confirm which ids exist."""
    user, token = authenticated_user
    stranger, _ = other_user
    _, mine = seed(db, user, 1)
    _, theirs = seed(db, stranger, 1)
    groceries = category(db, "food_and_drink.groceries")

    body = client.post(
        "/api/transactions/bulk",
        headers=auth_header(token),
        json={
            "transaction_ids": [str(mine[0].id), str(theirs[0].id)],
            "category_id": str(groceries.id),
        },
    ).json()

    assert body["updated"] == 1
    assert body["skipped"] == 1


def test_bulk_requires_at_least_one_change(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    _, rows = seed(db, user, 1)

    response = client.post(
        "/api/transactions/bulk",
        headers=auth_header(token),
        json={"transaction_ids": [str(rows[0].id)]},
    )

    assert response.status_code == 422


def test_bulk_is_bounded(client: TestClient, authenticated_user):
    """An unbounded bulk endpoint is a denial of service waiting to happen."""
    _, token = authenticated_user

    response = client.post(
        "/api/transactions/bulk",
        headers=auth_header(token),
        json={
            "transaction_ids": [str(uuid.uuid4()) for _ in range(501)],
            "is_reviewed": True,
        },
    )

    assert response.status_code == 422

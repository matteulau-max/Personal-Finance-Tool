"""Category, merchant, tag, and rule endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, Merchant, Tag, User
from tests.auth_helpers import auth_header
from tests.conftest import make_account, make_transaction


def category(db: Session, slug: str) -> Category:
    return db.execute(select(Category).where(Category.slug == slug)).scalar_one()


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def test_categories_include_the_system_tree(client: TestClient, authenticated_user):
    _, token = authenticated_user

    body = client.get("/api/categories", headers=auth_header(token)).json()

    assert len(body) > 50
    assert any(c["slug"] == "food_and_drink.groceries" for c in body)


def test_a_user_can_create_their_own_category(client: TestClient, authenticated_user):
    _, token = authenticated_user

    response = client.post(
        "/api/categories",
        headers=auth_header(token),
        json={"name": "Boat Maintenance", "color": "#0af"},
    )

    assert response.status_code == 201
    assert response.json()["is_system"] is False


def test_categories_are_scoped(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    stranger, _ = other_user

    db.add(Category(user_id=stranger.id, name="Their Private Project"))
    db.flush()

    names = [
        c["name"] for c in client.get("/api/categories", headers=auth_header(token)).json()
    ]

    assert "Their Private Project" not in names


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_creating_and_listing_tags(client: TestClient, authenticated_user):
    _, token = authenticated_user

    created = client.post(
        "/api/tags", headers=auth_header(token), json={"name": "Vacation"}
    )
    assert created.status_code == 201

    body = client.get("/api/tags", headers=auth_header(token)).json()
    assert [tag["name"] for tag in body] == ["Vacation"]


def test_tag_names_are_case_insensitively_unique(
    client: TestClient, authenticated_user
):
    """The functional index from migration 5d94c4e6a26a, exercised end to end.

    Without it a user ends up with "Vacation" and "vacation" -- two tags they
    believe are one, silently splitting their vacation report in half.
    """
    _, token = authenticated_user

    first = client.post(
        "/api/tags", headers=auth_header(token), json={"name": "Vacation"}
    )
    second = client.post(
        "/api/tags", headers=auth_header(token), json={"name": "vacation"}
    )

    assert first.status_code == 201
    assert second.status_code == 409


def test_tag_names_are_trimmed(client: TestClient, authenticated_user):
    """" Vacation" and "Vacation" are the same tag to a human."""
    _, token = authenticated_user

    client.post("/api/tags", headers=auth_header(token), json={"name": "Vacation"})
    duplicate = client.post(
        "/api/tags", headers=auth_header(token), json={"name": "  Vacation  "}
    )

    assert duplicate.status_code == 409


def test_two_users_may_have_the_same_tag_name(
    client: TestClient, authenticated_user, other_user
):
    _, token = authenticated_user
    _, stranger_token = other_user

    first = client.post(
        "/api/tags", headers=auth_header(token), json={"name": "Business"}
    )
    second = client.post(
        "/api/tags", headers=auth_header(stranger_token), json={"name": "Business"}
    )

    assert first.status_code == 201
    assert second.status_code == 201


def test_deleting_a_tag_keeps_the_transactions(
    client: TestClient, db: Session, authenticated_user
):
    """Removing a label must never remove the money."""
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account)
    db.flush()

    tag_id = client.post(
        "/api/tags", headers=auth_header(token), json={"name": "Temporary"}
    ).json()["id"]

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"tag_ids": [tag_id]},
    )

    assert (
        client.delete(f"/api/tags/{tag_id}", headers=auth_header(token)).status_code
        == 204
    )

    body = client.get(
        f"/api/transactions/{transaction.id}", headers=auth_header(token)
    ).json()
    assert body["tags"] == []


def test_cannot_delete_another_users_tag(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    stranger, _ = other_user
    theirs = Tag(user_id=stranger.id, name="Theirs")
    db.add(theirs)
    db.flush()

    response = client.delete(f"/api/tags/{theirs.id}", headers=auth_header(token))

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


def test_editing_a_global_merchant_creates_a_personal_copy(
    client: TestClient, db: Session, authenticated_user, other_user
):
    """One person's opinion is not evidence for everybody.

    Renaming a shared merchant must not change what other users see.
    """
    _, token = authenticated_user
    stranger, stranger_token = other_user

    shared = Merchant(
        user_id=None, normalized_name="acme corp", display_name="Acme Corp"
    )
    db.add(shared)
    db.flush()

    response = client.patch(
        f"/api/merchants/{shared.id}",
        headers=auth_header(token),
        json={"display_name": "My Name For Acme"},
    )

    assert response.status_code == 200
    assert response.json()["is_personal"] is True

    # The other user still sees the original.
    theirs = client.get("/api/merchants", headers=auth_header(stranger_token)).json()
    assert any(m["display_name"] == "Acme Corp" for m in theirs)


def test_merchant_search(client: TestClient, db: Session, authenticated_user):
    _, token = authenticated_user
    db.add_all(
        [
            Merchant(user_id=None, normalized_name="starbucks", display_name="Starbucks"),
            Merchant(user_id=None, normalized_name="shell", display_name="Shell"),
        ]
    )
    db.flush()

    body = client.get(
        "/api/merchants?search=star", headers=auth_header(token)
    ).json()

    assert [m["display_name"] for m in body] == ["Starbucks"]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


VALID_RULE = {
    "name": "Coffee",
    "conditions": {
        "conditions": [{"field": "raw_name", "op": "contains", "value": "BLUE BOTTLE"}]
    },
}


def test_creating_a_rule(client: TestClient, db: Session, authenticated_user):
    _, token = authenticated_user
    coffee = category(db, "food_and_drink.coffee")

    response = client.post(
        "/api/rules",
        headers=auth_header(token),
        json={**VALID_RULE, "actions": {"set_category_id": str(coffee.id)}},
    )

    assert response.status_code == 201
    assert response.json()["match_count"] == 0


def test_a_malformed_rule_is_rejected_at_the_api(
    client: TestClient, db: Session, authenticated_user
):
    """Rejected when written, not when it silently fails during a sync."""
    _, token = authenticated_user
    coffee = category(db, "food_and_drink.coffee")

    response = client.post(
        "/api/rules",
        headers=auth_header(token),
        json={
            "name": "Bad",
            "conditions": {"conditions": [{"field": "nope", "op": "contains", "value": "x"}]},
            "actions": {"set_category_id": str(coffee.id)},
        },
    )

    assert response.status_code == 422


def test_a_rule_that_does_nothing_is_rejected(client: TestClient, authenticated_user):
    _, token = authenticated_user

    response = client.post(
        "/api/rules", headers=auth_header(token), json={**VALID_RULE, "actions": {}}
    )

    assert response.status_code == 422


def test_preview_reports_matches_without_changing_anything(
    client: TestClient, db: Session, authenticated_user
):
    """Applying an untested rule to years of history is a frightening button.

    Seeing the count first turns it into an informed decision.
    """
    user, token = authenticated_user
    account = make_account(db, user)
    for i in range(3):
        make_transaction(
            db, account, raw_name="BLUE BOTTLE COFFEE", plaid_transaction_id=f"bb{i}"
        )
    make_transaction(db, account, raw_name="SHELL", plaid_transaction_id="shell")
    db.flush()

    coffee = category(db, "food_and_drink.coffee")
    rule_id = client.post(
        "/api/rules",
        headers=auth_header(token),
        json={**VALID_RULE, "actions": {"set_category_id": str(coffee.id)}},
    ).json()["id"]

    body = client.post(
        f"/api/rules/{rule_id}/preview", headers=auth_header(token)
    ).json()

    assert body["matched"] == 3
    assert len(body["sample"]) == 3

    # ...and nothing was actually categorized.
    items = client.get("/api/transactions", headers=auth_header(token)).json()["items"]
    assert all(item["category"] is None for item in items)


def test_applying_rules_backfills_existing_transactions(
    client: TestClient, db: Session, authenticated_user
):
    user, token = authenticated_user
    account = make_account(db, user)
    for i in range(3):
        make_transaction(
            db, account, raw_name="BLUE BOTTLE COFFEE", plaid_transaction_id=f"bb{i}"
        )
    db.flush()

    coffee = category(db, "food_and_drink.coffee")
    client.post(
        "/api/rules",
        headers=auth_header(token),
        json={**VALID_RULE, "actions": {"set_category_id": str(coffee.id)}},
    )

    applied = client.post("/api/rules/apply", headers=auth_header(token))
    assert applied.status_code == 200

    items = client.get("/api/transactions", headers=auth_header(token)).json()["items"]
    assert all(item["category"]["slug"] == "food_and_drink.coffee" for item in items)


def test_backfilling_does_not_overwrite_corrections(
    client: TestClient, db: Session, authenticated_user
):
    """The invariant, exercised through the API.

    Running "apply rules" across everything must be safe at any time.
    """
    user, token = authenticated_user
    account = make_account(db, user)
    transaction = make_transaction(db, account, raw_name="BLUE BOTTLE COFFEE")
    db.flush()

    restaurants = category(db, "food_and_drink.restaurants")
    coffee = category(db, "food_and_drink.coffee")

    client.patch(
        f"/api/transactions/{transaction.id}",
        headers=auth_header(token),
        json={"category_id": str(restaurants.id)},
    )
    client.post(
        "/api/rules",
        headers=auth_header(token),
        json={**VALID_RULE, "actions": {"set_category_id": str(coffee.id)}},
    )
    client.post("/api/rules/apply", headers=auth_header(token))

    body = client.get(
        f"/api/transactions/{transaction.id}", headers=auth_header(token)
    ).json()

    assert body["category"]["slug"] == "food_and_drink.restaurants"


def test_rules_are_scoped(
    client: TestClient, db: Session, authenticated_user, other_user
):
    _, token = authenticated_user
    _, stranger_token = other_user
    coffee = category(db, "food_and_drink.coffee")

    client.post(
        "/api/rules",
        headers=auth_header(token),
        json={**VALID_RULE, "actions": {"set_category_id": str(coffee.id)}},
    )

    assert client.get("/api/rules", headers=auth_header(stranger_token)).json() == []


def test_deleting_a_rule_keeps_what_it_categorized(
    client: TestClient, db: Session, authenticated_user
):
    """Deleting a rule means "stop applying it", not "undo everything"."""
    user, token = authenticated_user
    account = make_account(db, user)
    make_transaction(db, account, raw_name="BLUE BOTTLE COFFEE")
    db.flush()

    coffee = category(db, "food_and_drink.coffee")
    rule_id = client.post(
        "/api/rules",
        headers=auth_header(token),
        json={**VALID_RULE, "actions": {"set_category_id": str(coffee.id)}},
    ).json()["id"]
    client.post("/api/rules/apply", headers=auth_header(token))

    client.delete(f"/api/rules/{rule_id}", headers=auth_header(token))

    items = client.get("/api/transactions", headers=auth_header(token)).json()["items"]
    assert items[0]["category"]["slug"] == "food_and_drink.coffee"

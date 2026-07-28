"""seed system categories

Revision ID: fc0c7beca47e
Revises: 13005205e074
Create Date: 2026-07-28 02:18:00

===========================================================================
This is a DATA migration, not a schema migration.
===========================================================================

Schema migrations change the shape of tables. Data migrations change their
contents. Both belong in Alembic, because both need to happen in a specific
order on every environment -- your laptop, staging, and production must all
end up with the same category list.

Two techniques used here that are worth learning:

1. **Deterministic UUIDs.** Each category's ID is derived from its slug with
   `uuid5`, which always produces the same UUID for the same input. So the
   "Groceries" category has an identical ID in every database, forever. That
   makes seed data safe to reference from code and safe to re-run.

2. **A frozen table definition.** We declare a minimal `categories` table
   inline with `sa.table(...)` instead of importing our SQLAlchemy model.
   This is important: models change over time, and a migration must keep
   working exactly as written. If this file imported the live model and
   someone later added a NOT NULL column, this old migration would start
   failing on fresh databases -- and new developers could never set up the
   project. A migration is a historical record; it must never depend on
   today's code.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fc0c7beca47e"
down_revision: str | None = "13005205e074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A fixed namespace so uuid5 values never change.
CATEGORY_NAMESPACE = uuid.UUID("6f1c8a4e-9b3d-4c7a-8e2f-5d0a1b2c3d4e")


def category_id(slug: str) -> uuid.UUID:
    return uuid.uuid5(CATEGORY_NAMESPACE, slug)


# (slug, name, is_income, is_transfer, [children])
CATEGORY_TREE: list[tuple[str, str, bool, bool, list[tuple[str, str]]]] = [
    ("income", "Income", True, False, [
        ("income.salary", "Salary"),
        ("income.freelance", "Freelance"),
        ("income.investment", "Investment Income"),
        ("income.refund", "Refunds"),
        ("income.other", "Other Income"),
    ]),
    ("food_and_drink", "Food & Drink", False, False, [
        ("food_and_drink.groceries", "Groceries"),
        ("food_and_drink.restaurants", "Restaurants"),
        ("food_and_drink.coffee", "Coffee Shops"),
        ("food_and_drink.delivery", "Food Delivery"),
        ("food_and_drink.alcohol", "Alcohol & Bars"),
    ]),
    ("housing", "Housing", False, False, [
        ("housing.rent", "Rent"),
        ("housing.mortgage", "Mortgage"),
        ("housing.utilities", "Utilities"),
        ("housing.internet", "Internet & Cable"),
        ("housing.maintenance", "Home Maintenance"),
        ("housing.furnishings", "Furnishings"),
    ]),
    ("transportation", "Transportation", False, False, [
        ("transportation.gas", "Gas & Fuel"),
        ("transportation.parking", "Parking & Tolls"),
        ("transportation.rideshare", "Rideshare & Taxi"),
        ("transportation.public_transit", "Public Transit"),
        ("transportation.car_payment", "Car Payment"),
        ("transportation.car_maintenance", "Car Maintenance"),
        ("transportation.car_insurance", "Car Insurance"),
    ]),
    ("shopping", "Shopping", False, False, [
        ("shopping.clothing", "Clothing"),
        ("shopping.electronics", "Electronics"),
        ("shopping.household", "Household Goods"),
        ("shopping.gifts", "Gifts"),
        ("shopping.hobbies", "Hobbies"),
    ]),
    ("entertainment", "Entertainment", False, False, [
        ("entertainment.streaming", "Streaming Services"),
        ("entertainment.events", "Events & Tickets"),
        ("entertainment.games", "Games"),
        ("entertainment.sports", "Sports & Recreation"),
    ]),
    ("health", "Health & Wellness", False, False, [
        ("health.medical", "Medical"),
        ("health.dental", "Dental"),
        ("health.pharmacy", "Pharmacy"),
        ("health.fitness", "Fitness"),
        ("health.insurance", "Health Insurance"),
    ]),
    ("travel", "Travel", False, False, [
        ("travel.flights", "Flights"),
        ("travel.lodging", "Lodging"),
        ("travel.rental_car", "Rental Car"),
        ("travel.vacation", "Vacation"),
    ]),
    ("personal", "Personal", False, False, [
        ("personal.education", "Education"),
        ("personal.childcare", "Childcare"),
        ("personal.pets", "Pets"),
        ("personal.personal_care", "Personal Care"),
        ("personal.subscriptions", "Subscriptions"),
    ]),
    ("financial", "Financial", False, False, [
        ("financial.fees", "Bank Fees"),
        ("financial.interest", "Interest Charged"),
        ("financial.taxes", "Taxes"),
        ("financial.insurance", "Insurance"),
        ("financial.charity", "Charity & Donations"),
    ]),
    # Transfers are neither income nor spending. Without this, paying your
    # credit card would be counted as spending on top of the purchases the
    # card already recorded -- double-counting every expense.
    ("transfer", "Transfers", False, True, [
        ("transfer.credit_card_payment", "Credit Card Payment"),
        ("transfer.internal", "Between Accounts"),
        ("transfer.savings", "To Savings"),
        ("transfer.investment", "To Investments"),
    ]),
    ("uncategorized", "Uncategorized", False, False, []),
]


def upgrade() -> None:
    categories = sa.table(
        "categories",
        sa.column("id", sa.Uuid),
        sa.column("user_id", sa.Uuid),
        sa.column("parent_id", sa.Uuid),
        sa.column("name", sa.String),
        sa.column("slug", sa.String),
        sa.column("is_income", sa.Boolean),
        sa.column("is_transfer", sa.Boolean),
        sa.column("is_system", sa.Boolean),
        sa.column("is_archived", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )

    rows: list[dict] = []
    for order, (slug, name, is_income, is_transfer, children) in enumerate(CATEGORY_TREE):
        parent_uuid = category_id(slug)
        rows.append({
            "id": parent_uuid,
            "user_id": None,  # NULL user_id = built-in, shared by everyone
            "parent_id": None,
            "name": name,
            "slug": slug,
            "is_income": is_income,
            "is_transfer": is_transfer,
            "is_system": True,
            "is_archived": False,
            "sort_order": order * 100,
        })
        for child_order, (child_slug, child_name) in enumerate(children):
            rows.append({
                "id": category_id(child_slug),
                "user_id": None,
                "parent_id": parent_uuid,
                "name": child_name,
                "slug": child_slug,
                # Children inherit these flags: a subcategory of Income is
                # income, a subcategory of Transfers is a transfer.
                "is_income": is_income,
                "is_transfer": is_transfer,
                "is_system": True,
                "is_archived": False,
                "sort_order": order * 100 + child_order + 1,
            })

    op.bulk_insert(categories, rows)


def downgrade() -> None:
    # Remove only the system categories. A user's own categories, and any
    # transaction that references one, are untouched -- which is why we filter
    # on is_system rather than truncating the table.
    #
    # Children must go before parents: the parent_id foreign key is RESTRICT,
    # so deleting a parent while its children exist is (correctly) refused.
    op.execute("DELETE FROM categories WHERE is_system = true AND parent_id IS NOT NULL")
    op.execute("DELETE FROM categories WHERE is_system = true")

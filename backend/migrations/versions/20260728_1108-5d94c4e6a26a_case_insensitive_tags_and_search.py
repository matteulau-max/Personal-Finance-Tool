"""case insensitive tag names and transaction search index

Revision ID: 5d94c4e6a26a
Revises: 7c3fd28a1580
Create Date: 2026-07-28 11:08:00

Two changes, both paying off promises made in earlier milestones.

---------------------------------------------------------------------------
1. Tag names become case-insensitively unique
---------------------------------------------------------------------------
Milestone 2 left a comment on the `tags` model: uniqueness "should be
case-insensitive -- the service layer lowercases before comparing, and
Milestone 5 adds a functional index to enforce it in the database as well."

This is that index. A plain UNIQUE(user_id, name) lets "Vacation" and
"vacation" coexist, and the user then has two tags they believe are one --
their vacation report silently shows half their spending.

A **functional index** indexes the result of an expression rather than a
column. `UNIQUE (user_id, lower(name))` makes the two spellings collide in
the database, so application code cannot forget the rule.

---------------------------------------------------------------------------
2. Trigram index for transaction search
---------------------------------------------------------------------------
Searching descriptions means `WHERE raw_name ILIKE '%coffee%'`. A leading
wildcard makes a normal B-tree index useless -- PostgreSQL must read every
row.

`pg_trgm` indexes three-character sequences, which makes substring search
indexable. At a few thousand transactions this changes little; at a hundred
thousand it is the difference between instant and unusable. Adding it now
costs nothing and means the search page never needs revisiting.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d94c4e6a26a"
down_revision: str | None = "7c3fd28a1580"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Tags -----------------------------------------------------------
    # Fold any existing duplicates first. Creating a unique index while
    # conflicting rows exist fails, and on a live database that means a failed
    # deploy at the worst moment. A migration must consider the data already
    # there, not just the schema.
    op.execute(
        """
        UPDATE tags AS t
        SET name = t.name || ' (' || ranked.dup_rank || ')'
        FROM (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY user_id, lower(name) ORDER BY created_at
                   ) AS dup_rank
            FROM tags
        ) AS ranked
        WHERE t.id = ranked.id AND ranked.dup_rank > 1
        """
    )

    op.drop_constraint("uq_tags_user_id_name", "tags", type_="unique")
    # `sa.text(...)`, not a plain string: Alembic must be told this is a SQL
    # EXPRESSION rather than a column name, otherwise it quotes it and
    # PostgreSQL rejects the statement.
    op.create_index(
        "uq_tags_user_id_lower_name",
        "tags",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )

    # --- Search ---------------------------------------------------------
    # CREATE EXTENSION is idempotent with IF NOT EXISTS and needs to run once
    # per database.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX ix_transactions_raw_name_trgm
        ON transactions USING gin (raw_name gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_merchants_display_name_trgm
        ON merchants USING gin (display_name gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_merchants_display_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_transactions_raw_name_trgm")
    # The extension is deliberately NOT dropped: something else in the
    # database may be using it, and dropping a shared extension because one
    # migration reversed is a bad neighbour.

    op.drop_index("uq_tags_user_id_lower_name", table_name="tags")
    op.create_unique_constraint("uq_tags_user_id_name", "tags", ["user_id", "name"])

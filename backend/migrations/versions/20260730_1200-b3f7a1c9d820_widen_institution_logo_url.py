"""widen institutions.logo_url to text

Revision ID: b3f7a1c9d820
Revises: e2b7a9c31d45
Create Date: 2026-07-30 12:00:00.000000

Plaid's institution `logo` field is not a link -- it is the image itself,
base64-encoded. American Express's is roughly 3.5 KB, against a column bounded
at 1024 characters, so connecting that bank raised

    StringDataRightTruncation: value too long for type character varying(1024)

PostgreSQL refuses to truncate on INSERT rather than quietly trimming, which
is the correct behaviour and the reason this surfaced as a failed bank
connection instead of a corrupted logo. The failure came from
`_get_or_create_institution`, before the item row was written, so the whole
link rolled back -- meaning the bank could never be added at all.

Widening to TEXT rather than to a larger VARCHAR: the length is decided by
whatever image a bank chooses to publish, so any bound is a guess that some
institution will eventually exceed, and the next person to hit it would get
the same total failure. TEXT and VARCHAR are the same type internally in
PostgreSQL, stored identically and with the same performance -- the length
limit is a check, not a storage decision, so nothing is given up here.

Is this safe on a live database? Yes. Removing a length constraint from a
varchar is a catalog-only change: PostgreSQL does not rewrite the table or
verify existing rows, because every existing value is by definition already
short enough. The lock is held for microseconds.

The downgrade is the direction that is NOT free. Narrowing to varchar(1024)
must check every row, and would fail outright on any logo stored since this
migration ran -- which is the common case, since that is the point of it. It
truncates explicitly instead, because a downgrade that cannot run is not a
downgrade. Truncated base64 yields a broken image, not corrupt data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f7a1c9d820'
down_revision: Union[str, None] = 'e2b7a9c31d45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'institutions',
        'logo_url',
        existing_type=sa.String(length=1024),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # postgresql_using is required: without it PostgreSQL rejects the whole
    # statement if any single row is too long, rather than narrowing what it
    # can. See the module docstring.
    op.alter_column(
        'institutions',
        'logo_url',
        existing_type=sa.Text(),
        type_=sa.String(length=1024),
        existing_nullable=True,
        postgresql_using='left(logo_url, 1024)',
    )

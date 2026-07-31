"""retire accounts belonging to disconnected items

Revision ID: d5c2f81a3b90
Revises: b3f7a1c9d820
Create Date: 2026-07-31 09:00:00.000000

Nothing in the application had ever set `accounts.is_active` to false. The
column existed, the accounts list and every balance query filtered on it, and
no code path ever flipped it -- so disconnecting a bank left its accounts
looking live, holding whatever balance they had at the moment of
disconnection and counting toward net worth indefinitely.

Frozen figures presented as current ones are worse than absent ones: they are
wrong in a way that looks right, and there is nothing on screen to suggest
the number is months stale.

The route now retires accounts as part of disconnecting, but that only helps
future disconnections. Anyone who had already disconnected a bank -- which
includes everyone who linked a sandbox connection before switching to
production, since those cannot be carried over -- is left with the stale rows
and no way to reach them: the item is filtered out of the connections list,
so there is no longer a button to press.

Hence a data migration. It is idempotent and states the same invariant the
route now maintains: no account of a disconnected item is active.

Accounts are deactivated, never deleted. Every historical transaction points
at an account row, so removing them to tidy the list would take the history
with them -- the opposite of what disconnecting promises. `is_active` gates
balance queries only; transaction history and every analytic over past
spending are unaffected.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd5c2f81a3b90'
down_revision: Union[str, None] = 'b3f7a1c9d820'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The enum is stored by value, lower case -- see app/models/enums.py and
    # the schema-integrity test that pins it. Comparing against 'DISCONNECTED'
    # would match nothing and fail silently, which is the worst outcome
    # available to a data migration.
    op.execute(
        """
        UPDATE accounts
           SET is_active = false
         WHERE is_active = true
           AND plaid_item_id IN (
               SELECT id FROM plaid_items WHERE status = 'disconnected'
           )
        """
    )


def downgrade() -> None:
    """Deliberately does nothing.

    Reactivating every account of a disconnected item would be wrong: some
    were inactive before this ran, and nothing recorded which. Restoring them
    all would resurrect exactly the stale balances the upgrade removed.

    A downgrade that declines to invent information it does not have is the
    correct behaviour here, and leaving the rows deactivated is harmless --
    the schema is unchanged either way.
    """

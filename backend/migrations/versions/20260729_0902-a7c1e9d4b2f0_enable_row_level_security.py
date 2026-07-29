"""Enable Row-Level Security

Attaches PostgreSQL RLS policies to every table that holds user data, so the
database itself refuses to return one user's rows to another -- even if the
application forgets its WHERE clause. See `app/db/rls.py` for the reasoning
and for how a request supplies its user identity.

Three things happen here, in order:

  1. A role, `finance_app`, that owns nothing and bypasses nothing. The
     application switches into it for the duration of every request.
  2. Privileges for that role: DML on the data tables, SELECT on Alembic's
     bookkeeping, nothing else.
  3. Policies. Four commands are separated deliberately for the shared tables
     so that "anyone may read a global merchant, nobody may edit one" is
     enforced by the database rather than by everyone remembering to
     copy-on-write.

The table names below are written out literally rather than imported from
`app/db/rls.py`. A migration has to keep doing exactly what it did the day it
ran; importing a module that is still being edited would mean this file
quietly changes meaning. `tests/test_rls.py` compares the live database
against the model registry, which is what stops the two drifting apart.

Revision ID: a7c1e9d4b2f0
Revises: ff5166b6ed4c
Create Date: 2026-07-29 09:02:00

"""

from alembic import op

revision = "a7c1e9d4b2f0"
down_revision = "ff5166b6ed4c"
branch_labels = None
depends_on = None

APP_ROLE = "finance_app"

# `user_id` names the owner.
OWNED_TABLES = (
    "accounts",
    "audit_log",
    "budgets",
    "plaid_items",
    "rules",
    "tags",
    "transactions",
)

# `user_id IS NULL` means shared with everyone.
SHARED_TABLES = ("categories", "merchants", "merchant_aliases")

# Tables where the application legitimately creates *global* rows. Only
# merchants: `get_or_create_merchant` writes a shared row on purpose, because
# recognising "WHOLEFDS MKT" once should help every user.
SHARED_TABLES_ALLOWING_GLOBAL_INSERT = ("merchants",)

# (table, foreign key, parent table, parent key)
DERIVED_TABLES = (
    ("account_balances", "account_id", "accounts", "id"),
    ("sync_history", "plaid_item_id", "plaid_items", "id"),
    ("transaction_tags", "transaction_id", "transactions", "id"),
)

ALL_PROTECTED = (
    OWNED_TABLES
    + SHARED_TABLES
    + tuple(name for name, *_ in DERIVED_TABLES)
    + ("users",)
)


def upgrade() -> None:
    # --- 1. The role -----------------------------------------------------
    #
    # NOLOGIN: this role is a set of privileges, not an account. In
    # production a real login role is made a member of it; locally the
    # application simply switches into it with SET ROLE.
    #
    # CREATE ROLE has no IF NOT EXISTS, and roles are cluster-wide -- so on a
    # machine that already hosts another copy of this database the role is
    # already there. Guarding rather than failing keeps the migration
    # re-runnable against a fresh database on a shared cluster.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )

    # The migrating role must be able to SET ROLE into it. A superuser can
    # already; a plain owner cannot without this membership.
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")

    # --- 2. Privileges ---------------------------------------------------
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # Alembic's version table is not application data. The app has no reason
    # to write it, and a bug that did would corrupt the migration history --
    # the one piece of state you cannot reconstruct from a backup of the rows.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {APP_ROLE}")

    # Tables created by later migrations inherit these grants automatically.
    # They do NOT inherit RLS -- that is on purpose, and tests/test_rls.py
    # fails on any table that has neither a policy nor an explicit exemption.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )

    # --- 3. The identity function ----------------------------------------
    #
    # `current_setting(name, true)` returns NULL instead of raising when the
    # setting is missing, which is what makes an unauthenticated session see
    # nothing rather than error. NULLIF turns the empty string we reset to
    # into the same NULL, so "cleared" and "never set" behave identically.
    #
    # STABLE lets the planner call it once per query instead of once per row.
    # A pinned search_path stops a caller with a mischievous one from
    # resolving `current_setting` to something of their own.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid
        $$;
        """
    )

    # --- 4. Policies ------------------------------------------------------
    for table in ALL_PROTECTED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # A user is their own row.
    op.execute(
        """
        CREATE POLICY users_self ON users
            USING (id = app_current_user_id())
            WITH CHECK (id = app_current_user_id())
        """
    )

    # Owned tables: one policy covering every command. `USING` filters what
    # you can see and change; `WITH CHECK` stops you writing a row owned by
    # somebody else -- without it, an UPDATE could hand your transaction to
    # another user, and an INSERT could plant one.
    for table in OWNED_TABLES:
        op.execute(
            f"""
            CREATE POLICY {table}_owner ON {table}
                USING (user_id = app_current_user_id())
                WITH CHECK (user_id = app_current_user_id())
            """
        )

    # Shared tables: readable if global or mine, writable only if mine.
    for table in SHARED_TABLES:
        op.execute(
            f"""
            CREATE POLICY {table}_read ON {table} FOR SELECT
                USING (user_id IS NULL OR user_id = app_current_user_id())
            """
        )

        insert_check = (
            "user_id IS NULL OR user_id = app_current_user_id()"
            if table in SHARED_TABLES_ALLOWING_GLOBAL_INSERT
            else "user_id = app_current_user_id()"
        )
        op.execute(
            f"""
            CREATE POLICY {table}_insert ON {table} FOR INSERT
                WITH CHECK ({insert_check})
            """
        )

        # The interesting one. A global category or merchant is visible to
        # everyone, so allowing UPDATE on it would let any user rewrite what
        # every other user sees. Editing a shared row is a copy-on-write in
        # the application; this makes that the only possibility.
        op.execute(
            f"""
            CREATE POLICY {table}_modify ON {table} FOR UPDATE
                USING (user_id = app_current_user_id())
                WITH CHECK (user_id = app_current_user_id())
            """
        )
        op.execute(
            f"""
            CREATE POLICY {table}_delete ON {table} FOR DELETE
                USING (user_id = app_current_user_id())
            """
        )

    # Derived tables have no user_id; ownership comes from the parent row.
    # The subquery is itself subject to the parent's policy, which is
    # harmless (same answer) and means there is only one definition of who
    # owns an account.
    for table, fk, parent, parent_key in DERIVED_TABLES:
        predicate = (
            f"EXISTS (SELECT 1 FROM {parent} p "
            f"WHERE p.{parent_key} = {table}.{fk} "
            f"AND p.user_id = app_current_user_id())"
        )
        op.execute(
            f"""
            CREATE POLICY {table}_via_parent ON {table}
                USING ({predicate})
                WITH CHECK ({predicate})
            """
        )


def downgrade() -> None:
    for table, *_ in DERIVED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_via_parent ON {table}")

    for table in SHARED_TABLES:
        for suffix in ("read", "insert", "modify", "delete"):
            op.execute(f"DROP POLICY IF EXISTS {table}_{suffix} ON {table}")

    for table in OWNED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_owner ON {table}")

    op.execute("DROP POLICY IF EXISTS users_self ON users")

    for table in ALL_PROTECTED:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS app_current_user_id()")

    # The role is deliberately left in place. Dropping it would fail if any
    # object still depends on it, and a leftover NOLOGIN role with no grants
    # is harmless -- whereas a downgrade that errors halfway through is not.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )

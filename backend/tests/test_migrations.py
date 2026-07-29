"""Migration round-trip test.

The rest of the suite proves the migrations go *up*: `conftest.py` builds the
test database by running them, so a broken upgrade fails everything.

Nothing proved they go back down -- and a downgrade is exercised at the worst
possible moment, when somebody is backing out of a bad deploy at 3am. Writing
one and never running it is how you find out then that it does not work.

This test found exactly that. The audit-log partitioning downgrade renamed
the table out of the way but not its indexes, and index names share a
namespace across the schema, so recreating the table failed with
`relation "pk_audit_log" already exists`.

It runs against its own throwaway database rather than the shared test one,
because dropping every table out from under the other tests would be a
memorable way to make the suite order-dependent.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings

DATABASE = "finance_migration_roundtrip_test"

# The revision this milestone's work sits on top of. Downgrading to it and
# back is the interesting range -- Row-Level Security, the job queue, and the
# partition swap, which is by far the most involved migration in the project.
BEFORE_MILESTONE_8 = "ff5166b6ed4c"


def alembic(*args: str, url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["alembic", *args],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def scratch_database() -> str:
    url = make_url(get_settings().DATABASE_URL).set(database=DATABASE)
    admin_url = make_url(url).set(database="postgres")

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{DATABASE}"'))
    admin.dispose()

    yield url.render_as_string(hide_password=False)

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
    admin.dispose()


def test_the_milestone_8_migrations_can_be_rolled_back_and_reapplied(scratch_database):
    """Up, down, and up again.

    Reapplying matters as much as the rollback: a downgrade that leaves a
    renamed index or an orphaned policy behind appears to succeed and then
    breaks the next deploy, which is a worse failure than not being able to
    roll back at all.
    """
    steps = [
        ("upgrade", "head"),
        ("downgrade", BEFORE_MILESTONE_8),
        ("upgrade", "head"),
    ]

    for command, revision in steps:
        result = alembic(command, revision, url=scratch_database)
        assert result.returncode == 0, (
            f"`alembic {command} {revision}` failed:\n{result.stderr[-2000:]}"
        )


def test_the_schema_can_be_torn_down_completely(scratch_database):
    """`downgrade base` is what a developer runs to start over.

    It is also the only thing that exercises the oldest migrations' downgrade
    paths, which nothing else in the project has ever run.
    """
    assert alembic("upgrade", "head", url=scratch_database).returncode == 0

    result = alembic("downgrade", "base", url=scratch_database)

    assert result.returncode == 0, result.stderr[-2000:]

    engine = create_engine(scratch_database)
    with engine.connect() as connection:
        remaining = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
            )
        ).scalar_one()
    engine.dispose()

    assert remaining == 0, "downgrade base left tables behind"

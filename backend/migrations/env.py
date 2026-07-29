"""Alembic environment configuration.

Two changes from the file Alembic generates by default, both important:

1. The database URL comes from our application settings (and therefore from
   the environment), not from `alembic.ini`. If it lived in the .ini file, the
   production database password would be committed to Git.

2. `target_metadata` points at our models, which is what makes
   `alembic revision --autogenerate` able to diff the code against the live
   database and write the migration for you.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the model registry populates Base.metadata with every table.
from app.core.config import get_settings
from app.models import Base

config = context.config

# Inject the real URL at runtime.
#
# `config.attributes` is how a *caller* (rather than the command line) can
# hand Alembic a connection URL. The test suite uses it to migrate a throwaway
# `_test` database; without this hook, env.py would always read the developer's
# DATABASE_URL and the tests would silently migrate -- and then truncate --
# your real development data.
#
# `%` is escaped because ConfigParser treats it as interpolation syntax; a
# password containing % would otherwise crash Alembic with a confusing error.
settings = get_settings()
database_url = config.attributes.get("sqlalchemy_url") or settings.DATABASE_URL
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL to a file instead of running it.

    Useful when a DBA must review and apply changes by hand -- common in
    regulated environments.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations directly. This is the normal path."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column TYPE changes (e.g. String(64) -> String(128)).
            # Off by default, which silently misses real schema drift.
            compare_type=True,
            # Detect changes to server-side defaults.
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

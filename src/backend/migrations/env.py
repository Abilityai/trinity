"""Alembic environment for the PostgreSQL backend (#1183).

SQLite is **not** managed by Alembic — it keeps the legacy bespoke
``db/migrations.py`` runner (the two systems coexist during the Postgres
transition). This env is wired to the same ``DATABASE_URL`` the app resolves
and targets the ``db/tables.py`` SQLAlchemy ``MetaData`` so future revisions
can be ``--autogenerate``d from the single source of truth.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import String, create_engine, pool

# #1420: Alembic's default ``alembic_version.version_num`` is VARCHAR(32), but
# Trinity's descriptive ``NNNN_<table>_<change>`` revision ids exceed that (e.g.
# ``0009_agent_ownership_public_channel_model`` = 41). Configure the auto-created
# version table wide so fresh PostgreSQL builds never truncation-fail the stamp;
# existing DBs (already created at 32) are widened by the 0008a migration.
_VERSION_TABLE_COLUMN_TYPE = String(255)

# src/backend is normally on sys.path (PYTHONPATH) at runtime; make the env
# importable when alembic is invoked from the src/backend dir too.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.tables import metadata as target_metadata  # noqa: E402
from db.engine import resolve_database_url  # noqa: E402

config = context.config

# The app's own resolution wins over any alembic.ini placeholder so there is a
# single source for the connection string.
_url = config.get_main_option("sqlalchemy.url") or resolve_database_url()
config.set_main_option("sqlalchemy.url", _url)

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass


def run_migrations_offline() -> None:
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table_column_type=_VERSION_TABLE_COLUMN_TYPE,  # #1420
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url, poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table_column_type=_VERSION_TABLE_COLUMN_TYPE,  # #1420
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

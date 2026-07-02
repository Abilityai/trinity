"""baseline — full head schema (#1183)

The Alembic baseline for the PostgreSQL backend. ``upgrade`` reuses the exact
head-schema DDL that ``db/schema.py:init_schema_postgres`` emits (tables +
indexes + triggers), so a fresh PG database built by ``alembic upgrade head``
is byte-for-byte identical to the pre-Alembic fresh build. A pre-Alembic PG
database (already at this schema) is stamped at this revision instead of
rebuilt — see ``db/alembic_runner.py``.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-12
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Run the same DDL strings init_schema_postgres uses, on Alembic's own
    # connection/transaction (do NOT call init_schema_postgres directly — it
    # opens its own engine.begin(), which would escape Alembic's transaction).
    from db.schema import (
        TABLES,
        INDEXES,
        POSTGRES_TRIGGERS,
        to_postgres_table_ddl,
    )

    for create_sql in TABLES.values():
        op.execute(to_postgres_table_ddl(create_sql))
    for index_sql in INDEXES:
        op.execute(index_sql)
    for trigger_sql in POSTGRES_TRIGGERS:
        op.execute(trigger_sql)


def downgrade() -> None:
    # Baseline reset: there is no "previous" schema to return to, so drop
    # everything in the public schema. Cheap + reliable on Postgres (the
    # FK-laden schema makes per-table drops order-sensitive) and only used by
    # tests / a full teardown. Recreate an empty ``alembic_version`` afterwards
    # so Alembic's own post-downgrade bookkeeping (DELETE FROM alembic_version)
    # has a table to write to — DROP SCHEMA CASCADE would otherwise take it too.
    op.execute("DROP SCHEMA public CASCADE")
    op.execute("CREATE SCHEMA public")
    op.execute(
        # #1420: VARCHAR(255), matching env.py's version_table_column_type — the
        # 32-char default can't hold Trinity's descriptive revision ids.
        "CREATE TABLE alembic_version ("
        "version_num VARCHAR(255) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    )
    # Re-seed the row Alembic is mid-removing: its post-downgrade bookkeeping
    # deletes exactly this revision and asserts one row matched, leaving the
    # table empty (= base).
    op.execute("INSERT INTO alembic_version (version_num) VALUES ('0001_baseline')")

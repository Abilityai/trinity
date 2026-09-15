"""agent_sync_state.git_dir_bytes INTEGER -> BIGINT (#2800)

``0019_agent_sync_state_git_dir_bytes`` added the column as ``INTEGER``, which
on PostgreSQL is int4 — ceiling 2,147,483,647 bytes (2 GiB). The column exists
to observe workspace-repo bloat (#1596), so the values it is there to record
are exactly the ones that overflow: an agent whose ``.git`` passes 2 GiB made
every ``SyncHealthService`` upsert raise ``NumericValueOutOfRange: integer out
of range``, and the agent's sync health went dark at the moment it mattered.
SQLite never showed it (its INTEGER is 64-bit), which is how it shipped.

``ALTER COLUMN ... TYPE BIGINT`` is a metadata-plus-rewrite of one small table
(one row per agent); int4 -> int8 needs no ``USING`` and loses nothing.

Mirrors the DDL in ``db/schema.py`` and the MetaData in ``db/tables.py``. The
SQLite track deliberately carries NO migration: INTEGER and BIGINT are the
same 64-bit affinity there, so nothing changes for an upgraded file, and the
schema-parity suite cannot observe the declared type of this column (both of
its fixtures build from empty). See the note beside
``_migrate_agent_sync_state_git_dir_bytes`` in ``db/migrations.py``.

Fresh PG builds already get BIGINT via ``0001_baseline`` (it reuses the
``schema.py`` DDL); the ALTER is then a no-op.

Revision ID: 0063_agent_sync_state_git_dir_bytes_bigint
Revises: 0062_execution_fan_out_task_id
Create Date: 2026-09-15
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0063_agent_sync_state_git_dir_bytes_bigint"
down_revision = "0062_execution_fan_out_task_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_sync_state ALTER COLUMN git_dir_bytes TYPE BIGINT")


def downgrade() -> None:
    # Narrowing back to int4 fails on any row already holding a >2 GiB value —
    # that is the honest inverse (the data would not fit), not something to
    # paper over with a CAST that silently truncates.
    op.execute("ALTER TABLE agent_sync_state ALTER COLUMN git_dir_bytes TYPE INTEGER")

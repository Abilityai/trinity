"""agent_ownership.volume_base_name — pinned data-volume identity (#1664)

Adds ``volume_base_name`` (nullable TEXT) on the PostgreSQL backend. NULL means
"the volume base is ``agent_name``" — true for every agent that was never
renamed, so no backfill is needed. Rename pins the pre-rename name here because
the agent keeps its existing volumes (Docker can rename neither a volume nor its
immutable ``trinity.agent-name`` label), which makes this row the only record of
which volumes belong to the agent — and therefore the only safe orphan predicate
for the #1581 reclaim sweep.

Mirrors the SQLite ``agent_ownership_volume_base_name`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the column via ``0001_baseline``; ``ADD COLUMN IF NOT
EXISTS`` keeps this a no-op there.

Revision ID: 0024_agent_ownership_volume_base_name
Revises: 0023_agent_sync_state_gc_signals
Create Date: 2026-07-17
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0024_agent_ownership_volume_base_name"
down_revision = "0023_agent_sync_state_gc_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS volume_base_name TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS volume_base_name")

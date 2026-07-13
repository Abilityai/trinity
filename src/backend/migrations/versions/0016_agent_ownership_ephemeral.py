"""agent_ownership ephemeral-ghost columns (trinity-enterprise#69)

Adds the ephemeral-agent lifecycle + spawn-provenance columns on the PostgreSQL
backend: ``is_ephemeral`` (INTEGER, default 0), ``ephemeral_max_executions``
(INTEGER, nullable), ``ephemeral_expires_at`` (TEXT, nullable — always stamped
for ghosts and doubles as the durable discard-intent marker),
``spawned_by_agent`` (TEXT) and ``spawned_by_key_id`` (TEXT). Mirrors the SQLite
``agent_ownership_ephemeral`` migration in ``db/migrations.py`` and the DDL in
``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the columns via ``0001_baseline`` (which iterates
``db/schema.py:TABLES``). This revision lets an existing PG deployment pick them
up on ``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op
when the baseline already created them.

Revision ID: 0016_agent_ownership_ephemeral
Revises: 0015_enterprise_connectors
Create Date: 2026-07-12
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_agent_ownership_ephemeral"
down_revision = "0015_enterprise_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS "
        "is_ephemeral INTEGER DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS "
        "ephemeral_max_executions INTEGER"
    )
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS "
        "ephemeral_expires_at TEXT"
    )
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS spawned_by_agent TEXT"
    )
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS spawned_by_key_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS spawned_by_key_id")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS spawned_by_agent")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS ephemeral_expires_at")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS ephemeral_max_executions")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS is_ephemeral")

"""operator_queue.request_id — agent-scoped correlation id (#1631)

Adds ``request_id`` (nullable TEXT) plus a ``UNIQUE(agent_name, request_id)``
index on the PostgreSQL backend. The operator_queue ``id`` was doing two jobs:
the fleet-wide PRIMARY KEY *and* the agent-authored correlation string read from
``~/.trinity/operator-queue.json``. Two agents choosing the same id silently
lost the second agent's item. Split the jobs — ``id`` becomes a platform-minted
uuid, and the agent's string moves to ``request_id`` scoped unique per agent.

Existing rows are backfilled with their own id (already unique fleet-wide, so it
can't violate the new index) so they stay addressable by request_id.

Mirrors the SQLite ``operator_queue_request_id`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the column + index via ``0001_baseline``'s reuse of
the schema DDL; ``ADD COLUMN IF NOT EXISTS`` / ``CREATE UNIQUE INDEX IF NOT
EXISTS`` keep this a no-op there.

Revision ID: 0025_operator_queue_request_id
Revises: 0024_agent_ownership_volume_base_name
Create Date: 2026-07-17
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0025_operator_queue_request_id"
down_revision = "0024_agent_ownership_volume_base_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS request_id TEXT"
    )
    op.execute(
        "UPDATE operator_queue SET request_id = id WHERE request_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_queue_agent_request "
        "ON operator_queue(agent_name, request_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_operator_queue_agent_request")
    op.execute("ALTER TABLE operator_queue DROP COLUMN IF EXISTS request_id")

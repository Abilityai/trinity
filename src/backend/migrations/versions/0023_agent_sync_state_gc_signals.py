"""agent_sync_state gc-health signals — pack_count / loose_objects / maintenance_failures (#1595)

Adds three nullable columns on the PostgreSQL backend. The auto-sync heartbeat
measures the agent repo's pack and loose-object counts (`git count-objects -v`)
and its consecutive maintenance-failure streak, and reports them via
``GET /api/git/status``; ``SyncHealthService`` persists them here so the
killed-auto-gc failure class (#1595 — silent unbounded .git bloat) is
observable and alertable instead of "completely silent until the disk fills".
Mirrors the SQLite ``agent_sync_state_gc_signals`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the columns via ``0001_baseline``'s reuse of the
schema DDL; ``ADD COLUMN IF NOT EXISTS`` keeps this a no-op there.

Revision ID: 0023_agent_sync_state_gc_signals
Revises: 0022_agent_loops_failure_policy
Create Date: 2026-07-14
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0023_agent_sync_state_gc_signals"
down_revision = "0022_agent_loops_failure_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS pack_count INTEGER"
    )
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS loose_objects INTEGER"
    )
    op.execute(
        "ALTER TABLE agent_sync_state "
        "ADD COLUMN IF NOT EXISTS maintenance_failures INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS pack_count")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS loose_objects")
    op.execute(
        "ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS maintenance_failures"
    )

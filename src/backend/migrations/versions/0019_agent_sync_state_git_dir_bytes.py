"""agent_sync_state.git_dir_bytes — workspace .git size (#1596)

Adds ``git_dir_bytes`` (nullable INTEGER) on the PostgreSQL backend. The
auto-sync heartbeat measures the agent's ``.git`` on-disk size and reports it via
``GET /api/git/status``; ``SyncHealthService`` persists it here so operators can
watch workspace-repo bloat before the disk fills (git-sync data churn, #1596).
Mirrors the SQLite ``agent_sync_state_git_dir_bytes`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the column via ``0001_baseline``; ``ADD COLUMN IF NOT
EXISTS`` keeps this a no-op there.

Revision ID: 0019_agent_sync_state_git_dir_bytes
Revises: 0018_schedule_executions_source_channel
Create Date: 2026-07-13
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_agent_sync_state_git_dir_bytes"
down_revision = "0018_schedule_executions_source_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS git_dir_bytes INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS git_dir_bytes")

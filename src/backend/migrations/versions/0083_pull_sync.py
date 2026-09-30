"""pull_sync — the container's pull cycle (trinity-enterprise#703).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::pull_sync``.

Adds ``agent_git_config.pull_sync_enabled`` (the per-agent switch) and
``agent_sync_state.last_pull_at / last_pull_status / behind_after_pull /
last_pull_error / last_successful_pull_at / consecutive_pull_failures /
consecutive_pull_skips`` (the pull cycle's outcome and health), then turns the pull on only where auto-sync is already
on (operator ruling 2026-09-25).

Revision ID: 0083_pull_sync
Revises: 0082_agent_sync_state_divergence
"""
from alembic import op


revision = "0083_pull_sync"
down_revision = "0082_agent_sync_state_divergence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`: a fresh PostgreSQL database is built from db/schema.py's
    # DDL, which already declares these columns, and only THEN runs revisions.
    op.execute(
        "ALTER TABLE agent_git_config ADD COLUMN IF NOT EXISTS "
        "pull_sync_enabled INTEGER DEFAULT 0"
    )
    op.execute("ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS last_pull_at TEXT")
    op.execute("ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS last_pull_status TEXT")
    op.execute("ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS behind_after_pull INTEGER")
    op.execute("ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS last_pull_error TEXT")
    op.execute("ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS last_successful_pull_at TEXT")
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS "
        "consecutive_pull_failures INTEGER DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS "
        "consecutive_pull_skips INTEGER DEFAULT 0"
    )
    op.execute(
        "UPDATE agent_git_config SET pull_sync_enabled = 1 "
        "WHERE COALESCE(auto_sync_enabled, 0) = 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS consecutive_pull_skips")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS consecutive_pull_failures")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS last_successful_pull_at")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS last_pull_error")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS behind_after_pull")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS last_pull_status")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS last_pull_at")
    op.execute("ALTER TABLE agent_git_config DROP COLUMN IF EXISTS pull_sync_enabled")

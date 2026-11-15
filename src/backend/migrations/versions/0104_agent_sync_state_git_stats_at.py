"""agent_sync_state.git_stats_at — when the agent measured its git stats (#4000).

PostgreSQL half of the dual-track pair; SQLite half is
db/migrations.py::agent_sync_state_git_stats_at.

Revision ID: 0104_agent_sync_state_git_stats_at
Revises: 0103_a2a_caller_identity
"""
revision = "0104_agent_sync_state_git_stats_at"
down_revision = "0103_a2a_caller_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS git_stats_at TEXT"
    )


def downgrade() -> None:
    from alembic import op
    op.execute(
        "ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS git_stats_at"
    )

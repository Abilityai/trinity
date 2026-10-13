"""auto_sync_enabled_backfill — the auto-sync toggle becomes authoritative, data only.

PostgreSQL half of the dual-track pair (#3010); the SQLite half is
``db/migrations.py::auto_sync_enabled_backfill``.

From this release the agent's auto-sync loop obeys ``auto_sync_enabled`` alone.
Live non-source-mode ghost agents baked ``GIT_SYNC_AUTO`` at creation but never
had the DB flag written, so they are set to 1 here and keep auto-pushing. No DDL.

Revision ID: 0075_auto_sync_enabled_backfill
Revises: 0074_role_readiness_rollout_seed
"""
from alembic import op
import sqlalchemy as sa

revision = "0075_auto_sync_enabled_backfill"
down_revision = "0074_role_readiness_rollout_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE agent_git_config SET auto_sync_enabled = 1
            WHERE COALESCE(auto_sync_enabled, 0) = 0
              AND COALESCE(source_mode, 0) = 0
              AND agent_name IN (
                  SELECT agent_name FROM agent_ownership
                  WHERE is_ephemeral = 1 AND deleted_at IS NULL
              )
            """
        )
    )


def downgrade() -> None:
    # Not reversible without a record of which rows were set; the flag it set
    # matches what those agents were already doing.
    pass

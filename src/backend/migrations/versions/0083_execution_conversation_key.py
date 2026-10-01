"""#2843 — one turn per conversation at a time on the pull queue.

``schedule_executions.conversation_key`` names the conversation a queued turn
continues; the partial unique index allows at most one ``running`` row per
(agent, key), and the pull claim relies on the IntegrityError it raises.
Nullable, no backfill.

Mirrors the SQLite ``execution_conversation_key`` migration.

Revision ID: 0083_execution_conversation_key
Revises: 0082_agent_sync_state_divergence
"""
from alembic import op


revision = "0083_execution_conversation_key"
down_revision = "0082_agent_sync_state_divergence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS conversation_key TEXT"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_one_running_turn "
        "ON schedule_executions(agent_name, conversation_key) "
        "WHERE status = 'running' AND conversation_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_executions_one_running_turn")
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS conversation_key")

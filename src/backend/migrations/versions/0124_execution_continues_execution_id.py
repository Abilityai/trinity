"""trinity-enterprise#1225 — a continuation turn names the turn it continues.

``schedule_executions.continues_execution_id`` records, platform-side, the turn
a wake, an ask resume or ending, or a gate notice continues (and, on a
delegated callee row, the caller's turn), so the continuation is rated as that
turn. Nullable, no backfill.

Mirrors the SQLite ``execution_continues_execution_id`` migration.

Revision ID: 0124_execution_continues_execution_id
Revises: 0123_agent_ownership_default_model
"""
from alembic import op


revision = "0124_execution_continues_execution_id"
down_revision = "0123_agent_ownership_default_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS continues_execution_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS continues_execution_id")

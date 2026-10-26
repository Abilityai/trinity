"""#3380 — a /chat turn names the thread it answers.

``schedule_executions.chat_session_id`` lets the #2944 watchdog, which claims a
finished turn whose waiting connection a restart destroyed, write the reply
into the chat thread as well as the execution row. Nullable, no backfill.

Mirrors the SQLite ``execution_chat_session_id`` migration.

Revision ID: 0103_execution_chat_session_id
Revises: 0102_nevermined_delegations
"""
from alembic import op


revision = "0103_execution_chat_session_id"
down_revision = "0102_nevermined_delegations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS chat_session_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS chat_session_id")

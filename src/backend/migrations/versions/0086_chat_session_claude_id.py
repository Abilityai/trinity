"""#3127 — per-user ``/chat`` memory on pull pilots.

``chat_sessions.cached_claude_session_id`` holds the Claude session id the
session's next pulled ``/chat`` turn resumes. Nullable, no backfill.

Mirrors the SQLite ``chat_session_claude_id`` migration.

Revision ID: 0086_chat_session_claude_id
Revises: 0085_ent720_email_identity
"""
from alembic import op


revision = "0086_chat_session_claude_id"
down_revision = "0085_ent720_email_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chat_sessions "
        "ADD COLUMN IF NOT EXISTS cached_claude_session_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE chat_sessions DROP COLUMN IF EXISTS cached_claude_session_id")

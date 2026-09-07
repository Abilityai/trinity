"""ent#534 — a Workspace voice call's turns land in the chat, marked spoken.

Two nullable columns on ``enterprise_portal_messages``: ``source`` (NULL for a
typed turn, ``'voice'`` for one spoken in a Workspace voice call) and
``voice_call_id`` (the voice session id, so one call's rows fold into a single
collapsed "Voice call · N min" block). Both are written by the platform only —
no client request carries them.

A per-row call id rather than a header row, deliberately: the history read
returns the newest 100 rows and a 30-minute call is ~180, so a grouping keyed on
an opener row falls apart exactly when the call was long enough to matter.

Additive, no backfill: every existing row is a typed turn.

Mirrors the SQLite ``portal_messages_voice_source`` migration.

Revision ID: 0056_portal_messages_voice_source
Revises: 0055_portal_session_main_chat
"""
from alembic import op


revision = "0056_portal_messages_voice_source"
down_revision = "0055_portal_session_main_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0055: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares both columns — and only
    # THEN runs the revisions, so a bare `add_column` would raise
    # DuplicateColumn on every fresh install.
    op.execute("ALTER TABLE enterprise_portal_messages ADD COLUMN IF NOT EXISTS source TEXT")
    op.execute("ALTER TABLE enterprise_portal_messages ADD COLUMN IF NOT EXISTS voice_call_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE enterprise_portal_messages DROP COLUMN IF EXISTS voice_call_id")
    op.execute("ALTER TABLE enterprise_portal_messages DROP COLUMN IF EXISTS source")

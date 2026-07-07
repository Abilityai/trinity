"""Add sender_email/sender_label to public_chat_messages (#903)

Persists per-message speaker attribution on the PostgreSQL backend. Mirrors the
SQLite ``public_chat_messages_sender`` migration in ``db/migrations.py`` and the
DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Thread-scoped Slack channel sessions (#903) drop ``sender_id`` from the session
key so multi-participant threads share one context; who-said-what moves onto each
message row (``sender_label`` for attributed history replay, ``sender_email`` for
sender-filtered MEM-001 summarization).

Fresh PG builds already get the columns because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
columns up on ``alembic upgrade head``.

Revision ID: 0013_public_chat_messages_sender
Revises: 0012_agent_ownership_public_channel_prompt
Create Date: 2026-07-04
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_public_chat_messages_sender"
down_revision = "0012_agent_ownership_public_channel_prompt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public_chat_messages ADD COLUMN IF NOT EXISTS sender_email TEXT"
    )
    op.execute(
        "ALTER TABLE public_chat_messages ADD COLUMN IF NOT EXISTS sender_label TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public_chat_messages DROP COLUMN IF EXISTS sender_label"
    )
    op.execute(
        "ALTER TABLE public_chat_messages DROP COLUMN IF EXISTS sender_email"
    )

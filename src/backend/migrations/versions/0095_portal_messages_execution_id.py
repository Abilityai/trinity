"""#3166 — each Workspace message names the turn that wrote it.

One nullable column, ``execution_id``, on ``enterprise_portal_messages``. Two
turns on one thread (the chat open in two tabs) wrote replies the client could
not tell apart, so a tab could show the other turn's answer as its own.

Additive, no backfill: old rows stay NULL and the client keeps its old matching
for them.

Mirrors the SQLite ``portal_messages_execution_id`` migration.

Revision ID: 0095_portal_messages_execution_id
Revises: 0094_agent_skill_gates
"""
from alembic import op


revision = "0095_portal_messages_execution_id"
down_revision = "0094_agent_skill_gates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, as in 0057: a fresh PostgreSQL database is built from
    # `db/schema.py`'s DDL, which already declares the column, before the
    # revisions run.
    op.execute("ALTER TABLE enterprise_portal_messages ADD COLUMN IF NOT EXISTS execution_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE enterprise_portal_messages DROP COLUMN IF EXISTS execution_id")

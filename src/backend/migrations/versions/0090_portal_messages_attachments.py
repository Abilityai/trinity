"""#3265 — a sent Workspace message shows what was attached to it.

One nullable column on ``enterprise_portal_messages``: ``attachments``, a JSON
list on a user turn — ``{filename, size_bytes, mime_type}`` for an upload the
server found in the sender's inbox, ``{filename, failed, error}`` for one that
did not land. NULL for every other row.

Additive, no backfill: no existing row recorded what it carried.

Mirrors the SQLite ``portal_messages_attachments`` migration.

Revision ID: 0090_portal_messages_attachments
Revises: 0089_supersede_queue_flood_backlog
"""
from alembic import op


revision = "0090_portal_messages_attachments"
down_revision = "0089_supersede_queue_flood_backlog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, as in 0057: a fresh PostgreSQL database is built from
    # `db/schema.py`'s DDL (which declares the column) before the revisions run.
    op.execute("ALTER TABLE enterprise_portal_messages ADD COLUMN IF NOT EXISTS attachments TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE enterprise_portal_messages DROP COLUMN IF EXISTS attachments")

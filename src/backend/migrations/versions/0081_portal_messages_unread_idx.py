"""portal_messages_unread_idx — cover the Workspace unread count (#3064).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::portal_messages_unread_index``.

`count_unread_by_session` runs on every 20s Workspace poll per open tab and
filters ``enterprise_portal_messages`` by the viewer, ``role = 'assistant'``,
the thread and ``created_at`` past a read cursor. No index led on the viewer,
so the message arm read every row in the install. Index-only: no column, no
data, no behaviour change.

Revision ID: 0081_portal_messages_unread_idx
Revises: 0080_agent_skill_sets
"""
from alembic import op

revision = "0081_portal_messages_unread_idx"
down_revision = "0080_agent_skill_sets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`: a fresh PostgreSQL database is built from `db/schema.py`'s
    # DDL — which now declares this index — before the revisions run.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_messages_unread "
        "ON enterprise_portal_messages(client_email, role, session_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_portal_messages_unread")

"""enterprise_portal_chat_state — per-user star + read cursor for Workspace chats (ent#359)

The sidebar gains starred chats pinned above the date groups, and a per-agent
"waiting on you" badge. Both need state that belongs to the *viewer*, not to the
conversation.

That rules out a column on the chat row. A room (``shared_sessions``) is shared
between several participants, so a ``starred`` column there would be one user's
star rendered in everybody else's sidebar; and rooms live in the enterprise
submodule, so a per-kind column would split one feature across two repos.

Keying on the caller's own email makes the row itself the per-user scope — there
is nothing to filter on read, and no way to address another user's state.
``chat_kind`` separates the two independent id spaces (``thread`` = a portal
session, ``room`` = a room).

Mirrors the SQLite ``portal_chat_state`` migration + ``db/schema.py`` /
``db/tables.py`` (Invariant #3, dual-track).

Revision ID: 0038_portal_chat_state
Revises: 0037_portal_session_resume
Create Date: 2026-08-12
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0038_portal_chat_state"
down_revision = "0037_portal_session_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_portal_chat_state (
            client_email TEXT NOT NULL,
            chat_kind TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            starred_at TEXT,
            last_read_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (client_email, chat_kind, chat_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS enterprise_portal_chat_state")

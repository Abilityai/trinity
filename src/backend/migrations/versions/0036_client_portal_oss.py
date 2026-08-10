"""Workspace / client portal tables adopted onto the OSS track (ent#356)

The module moved out of the entitled enterprise seam into OSS core, so its three
tables move from the enterprise Alembic line (`enterprise_schema_migrations`) to
the OSS one (`alembic_version`).

ADOPTION, NOT CREATION. On an existing entitled PostgreSQL install these tables
already exist — the enterprise line created them and they hold live client
conversations. The move's acceptance criteria are explicit: no data migration,
no re-auth for signed-in clients. So every statement here is `IF NOT EXISTS`
and this revision is a NO-OP on those installs; it does real work only on a
fresh or community build.

That is also why the `enterprise_` table-name prefix stays. Renaming
`enterprise_portal_messages` would be precisely the data migration this forbids,
so the prefix is historical, not a statement about licensing.

`downgrade` deliberately does NOT drop the tables — see its docstring.

Mirrors the SQLite `client_portal_tables_to_oss` migration + `db/schema.py`.

Revision ID: 0036_client_portal_oss
Revises: 0035_agent_ownership_a2a_exposed
Create Date: 2026-08-10
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0036_client_portal_oss"
down_revision = "0035_agent_ownership_a2a_exposed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_portal_sessions (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            client_email TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            last_message_at TEXT,
            message_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_portal_messages (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            client_email TEXT NOT NULL,
            session_id TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            cost REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_client_blocks (
            email TEXT PRIMARY KEY,
            blocked_at TEXT NOT NULL,
            blocked_by_id TEXT,
            blocked_by_email TEXT,
            reason TEXT
        )
        """
    )
    # `session_id` was added by the enterprise line to an already-shipped
    # messages table; a fresh build gets it from the CREATE above. Added
    # defensively so a half-migrated install cannot reach the index below
    # without the column it needs. PostgreSQL supports IF NOT EXISTS here.
    op.execute(
        "ALTER TABLE enterprise_portal_messages "
        "ADD COLUMN IF NOT EXISTS session_id TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_messages_convo "
        "ON enterprise_portal_messages(agent_name, client_email, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_sessions_convo "
        "ON enterprise_portal_sessions(agent_name, client_email, last_message_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_messages_session "
        "ON enterprise_portal_messages(session_id, created_at)"
    )


def downgrade() -> None:
    """Intentionally a no-op.

    These tables hold client conversation history and predate this revision on
    every entitled install — this revision ADOPTED them, it did not create them.
    Dropping on downgrade would destroy data the migration never owned, which is
    the one outcome the move promised could not happen.
    """

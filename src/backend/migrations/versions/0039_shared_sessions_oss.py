"""Multi-agent room tables adopted onto the OSS track (ent#443)

The `shared_sessions` module moved out of the entitled enterprise seam into OSS
core, so its three tables move from the enterprise Alembic line
(`enterprise_schema_migrations`) to the OSS one (`alembic_version`).

ADOPTION, NOT CREATION — the `0036_client_portal_oss` contract, for the same
reason. On an existing entitled PostgreSQL install these tables already exist:
the enterprise line's `0011_shared_sessions` created them and they hold live
room transcripts. The move's acceptance criteria are explicit — no data
migration, no lost rooms — so every statement here is `IF NOT EXISTS` and this
revision is a NO-OP on those installs; it does real work only on a fresh or
community build.

That is also why the `enterprise_` table-name prefix stays. Renaming
`enterprise_room_messages` would be precisely the data migration this forbids,
so the prefix is historical provenance, not a statement about licensing.

The enterprise revision `0011_shared_sessions` is deliberately left in place on
its own line: deleting it would break that chain's `down_revision` graph, and it
is idempotent, so an entitled install running both lines creates nothing twice.

`downgrade` deliberately does NOT drop the tables — see its docstring.

Mirrors the SQLite `shared_sessions_tables_to_oss` migration + `db/schema.py`.

Revision ID: 0039_shared_sessions_oss
Revises: 0038_portal_chat_state
Create Date: 2026-08-20
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0039_shared_sessions_oss"
down_revision = "0038_portal_chat_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_rooms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            topic TEXT,
            created_by TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            stop_reason TEXT,
            max_messages INTEGER NOT NULL DEFAULT 60,
            max_cost_usd DOUBLE PRECISION,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_room_participants (
            id SERIAL PRIMARY KEY,
            room_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            identity TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at TEXT NOT NULL,
            left_at TEXT,
            last_read_seq INTEGER NOT NULL DEFAULT 0,
            cached_session_id TEXT,
            consecutive_resume_failures INTEGER NOT NULL DEFAULT 0,
            UNIQUE (room_id, kind, identity)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_room_messages (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            sender_kind TEXT NOT NULL,
            sender_identity TEXT,
            kind TEXT NOT NULL DEFAULT 'message',
            mentions TEXT,
            content TEXT NOT NULL,
            execution_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (room_id, seq)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_rooms_status ON enterprise_rooms(status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_participants_room "
        "ON enterprise_room_participants(room_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_participants_identity "
        "ON enterprise_room_participants(kind, identity)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_messages_room_seq "
        "ON enterprise_room_messages(room_id, seq)"
    )


def downgrade() -> None:
    """Intentionally a no-op.

    These tables hold room transcripts and predate this revision on every
    entitled install — this revision ADOPTED them, it did not create them.
    Dropping on downgrade would destroy data the migration never owned, which is
    the one outcome the move promised could not happen.
    """

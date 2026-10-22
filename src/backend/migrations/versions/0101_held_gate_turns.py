"""held_gate_turns (trinity#3402).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::held_gate_turns``.

A turn the skill gate held or refused, and the platform's notice for it, is
never replayed to the agent:

* ``gate_hold`` on ``public_chat_messages`` and ``enterprise_portal_messages`` —
  NULL for an ordinary row; otherwise the request id, ``refused:<code>`` or
  ``named``, and every prompt read leaves the row out;
* ``room_gate_holds`` — the room messages one agent's gate stopped, per agent;
  its ``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py``;
* one backfill — a Workspace turn held before the column existed is marked from
  the gate record naming that turn as its origin (the SQL is shared with the
  SQLite track, ``db.migrations.HELD_PORTAL_ROWS_BACKFILL``).

Additive.

Revision ID: 0101_held_gate_turns
Revises: 0100_admin_email_not_username
"""
from alembic import op


revision = "0101_held_gate_turns"
down_revision = "0100_admin_email_not_username"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import HELD_PORTAL_ROWS_BACKFILL

    # `IF NOT EXISTS` throughout: a fresh PostgreSQL database is built from
    # `db/schema.py`'s DDL, which already declares the columns and the table,
    # before the revisions run (the 0095 shape).
    for table in ("public_chat_messages", "enterprise_portal_messages"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS gate_hold TEXT")
    op.execute(
        "CREATE TABLE IF NOT EXISTS room_gate_holds ("
        " room_id TEXT NOT NULL,"
        " agent_name TEXT NOT NULL,"
        " seq INTEGER NOT NULL,"
        " gate_hold TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (room_id, agent_name, seq))"
    )
    op.execute(HELD_PORTAL_ROWS_BACKFILL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS room_gate_holds")
    for table in ("public_chat_messages", "enterprise_portal_messages"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS gate_hold")

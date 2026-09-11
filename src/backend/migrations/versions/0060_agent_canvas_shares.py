"""ent#554 — share links for a canvas.

A separate table rather than a typed row in ``agent_public_links``: nothing in
that table's read path filters on ``type`` (``get_public_link_by_token`` /
``is_link_valid`` / ``routers/public.py::_validate_public_link`` all resolve a
token whatever its type), so a canvas row there would also be a working
public-CHAT token — the silent audience widening ent#554 forbids.

Mirrors the SQLite ``agent_canvas_shares_table`` migration.

Revision ID: 0060_agent_canvas_shares
Revises: 0059_agent_canvases_pinned
"""
from alembic import op


revision = "0060_agent_canvas_shares"
down_revision = "0059_agent_canvases_pinned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching the surrounding revisions: a fresh PostgreSQL
    # database is built from `db/schema.py`'s DDL — which already declares this
    # table — and only THEN runs the revisions.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_canvas_shares (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            canvas_id TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            scope TEXT NOT NULL DEFAULT 'authorized',
            created_by TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_viewed_at TEXT,
            view_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_canvas_shares_token "
        "ON agent_canvas_shares(token)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_canvas_shares_canvas "
        "ON agent_canvas_shares(agent_name, canvas_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_canvas_shares")

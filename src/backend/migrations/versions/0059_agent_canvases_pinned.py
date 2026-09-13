"""ent#553 — a human may pin a canvas so it stays at the top of the pile.

``agent_canvases.pinned`` is 0/1, default 0, written ONLY by the human-facing
pin route and never by the agent write path: ``audience`` is the agent's
decision about who may read a canvas, ``pinned`` is the reader's decision about
what they want to see first.

NOT NULL DEFAULT 0, so every pre-#553 row reads as unpinned with no backfill.

Mirrors the SQLite ``agent_canvases_pinned`` migration.

Revision ID: 0059_agent_canvases_pinned
Revises: 0058_portal_file_dismissals
"""
from alembic import op


revision = "0059_agent_canvases_pinned"
down_revision = "0058_portal_file_dismissals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0057: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which already declares the column — and only
    # THEN runs the revisions, so a bare `add_column` raises DuplicateColumn on
    # every fresh install.
    op.execute(
        "ALTER TABLE agent_canvases "
        "ADD COLUMN IF NOT EXISTS pinned INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_canvases DROP COLUMN IF EXISTS pinned")

"""ent#537 — a canvas may declare a starter layout by name.

``agent_canvases.template`` holds 'dashboard' | 'report' | 'brief' |
'status-board', or NULL for the stacked default every pre-#537 row keeps. A
property of the surface (like ``audience``), so a column rather than a key
inside ``blocks``; the per-block ``slot`` that fills a layout lives in the
blocks JSON because it travels with the block through ``patch_canvas``.
No backfill.

Mirrors the SQLite ``agent_canvases_template`` migration.

Revision ID: 0054_agent_canvases_template
Revises: 0053_user_ui_preferences
"""
from alembic import op


revision = "0054_agent_canvases_template"
down_revision = "0053_user_ui_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0053: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares the column — and only THEN
    # runs the revisions, so a bare `add_column` raises DuplicateColumn on
    # every fresh install.
    op.execute("ALTER TABLE agent_canvases ADD COLUMN IF NOT EXISTS template TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_canvases DROP COLUMN IF EXISTS template")

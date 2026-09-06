"""ent#473 — which hand wrote a Workspace thread's title.

``enterprise_portal_sessions.title`` is written by three hands: the derived
fallback (first-message prefix), the ent#186 generated title, and — from
ent#473 — a person renaming the chat. The generator must never overwrite a
person's title, and the ent#473 second generation pass has to know whether the
first attempt landed, so the hand is recorded beside the value:
NULL = derived fallback (or any row that predates this column), 'generated',
'user'. No backfill — an existing title keeps working as it did.

Mirrors the SQLite ``portal_session_title_source`` migration.

Revision ID: 0052_portal_session_title_source
Revises: 0051_agent_loops_terminal_driven
"""
from alembic import op


revision = "0052_portal_session_title_source"
down_revision = "0051_agent_loops_terminal_driven"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0051: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares the column — and only THEN
    # runs the revisions, so a bare `add_column` raises DuplicateColumn on
    # every fresh install.
    op.execute(
        "ALTER TABLE enterprise_portal_sessions ADD COLUMN IF NOT EXISTS title_source TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE enterprise_portal_sessions DROP COLUMN IF EXISTS title_source"
    )

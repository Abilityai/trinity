"""enterprise_portal_sessions resume state — Workspace absorbs the Session surface (ent#358)

Adds the three columns that make a Workspace thread resumable, the same trio
``agent_sessions`` already carries: ``cached_claude_session_id`` (the Claude
session id a turn reattaches to via ``claude --print --resume``),
``last_resume_at``, and ``consecutive_resume_failures``.

Before this, Workspace chat was stateless — prior messages were replayed to the
agent as a text prompt prefix, which recovers conversation but not tool-result
memory, mid-skill state, or reasoning state. Retiring the Agent Detail Session
surface on top of that would have been a silent continuity downgrade, so parity
lands with the removal.

Additive and defaulted: existing threads start with a NULL cache and their next
turn runs cold (writing a JSONL and caching its id). No stored history changes.

Mirrors the SQLite ``portal_session_resume`` migration + ``db/schema.py`` /
``db/tables.py`` (Invariant #3, dual-track).

Revision ID: 0037_portal_session_resume
Revises: 0036_client_portal_oss
Create Date: 2026-08-12
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0037_portal_session_resume"
down_revision = "0036_client_portal_oss"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE enterprise_portal_sessions "
        "ADD COLUMN IF NOT EXISTS cached_claude_session_id TEXT"
    )
    op.execute(
        "ALTER TABLE enterprise_portal_sessions "
        "ADD COLUMN IF NOT EXISTS last_resume_at TEXT"
    )
    op.execute(
        "ALTER TABLE enterprise_portal_sessions "
        "ADD COLUMN IF NOT EXISTS consecutive_resume_failures INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE enterprise_portal_sessions "
        "DROP COLUMN IF EXISTS consecutive_resume_failures"
    )
    op.execute(
        "ALTER TABLE enterprise_portal_sessions DROP COLUMN IF EXISTS last_resume_at"
    )
    op.execute(
        "ALTER TABLE enterprise_portal_sessions "
        "DROP COLUMN IF EXISTS cached_claude_session_id"
    )

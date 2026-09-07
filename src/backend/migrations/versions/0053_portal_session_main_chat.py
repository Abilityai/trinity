"""ent#523 — the pinned Main chat, and the tombstone Reset leaves behind.

Every (user, agent) pair has one **Main** chat: the place the agent reaches you
when no conversation named itself — an agent-initiated message, an ask raised
outside a chat (ent#364/#429), a scheduled brief (ent#498). ``is_main`` marks
it; ``archived_at`` marks the one Reset retired, which stays an ordinary past
chat (readable, resumable, renameable) and simply stops being that place.

The partial unique index is the substance here. ``ensure_main_session`` is
reachable from two request paths and runs in every uvicorn worker, so a
check-then-insert races two Mains into existence for one pair, after which
"the pinned first tab" has no single answer. The ``WHERE is_main = 1``
predicate is load-bearing rather than an optimisation: an archived row keeps
its (agent, client) pair forever, so an unconditional unique index would refuse
the SECOND Reset.

No backfill, deliberately — the ent#473 precedent one revision back. Existing
rows read ``is_main = 0`` and Main is created lazily on the next visit;
backfilling would have to anoint one existing thread per pair, and "whichever
was most recent when we migrated" is not a fact anyone asked for.

Mirrors the SQLite ``portal_session_main_chat`` migration.

Revision ID: 0053_portal_session_main_chat
Revises: 0052_portal_session_title_source
"""
from alembic import op


revision = "0053_portal_session_main_chat"
down_revision = "0052_portal_session_title_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS` throughout, matching 0046-0052: a fresh PostgreSQL
    # database is built from `db/schema.py`'s DDL — which now declares both
    # columns AND this index — and only THEN runs the revisions, so a bare
    # `add_column` raises DuplicateColumn on every fresh install.
    op.execute(
        "ALTER TABLE enterprise_portal_sessions "
        "ADD COLUMN IF NOT EXISTS is_main INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE enterprise_portal_sessions ADD COLUMN IF NOT EXISTS archived_at TEXT"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_portal_sessions_main "
        "ON enterprise_portal_sessions(agent_name, client_email) WHERE is_main = 1"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_portal_sessions_main")
    op.execute(
        "ALTER TABLE enterprise_portal_sessions DROP COLUMN IF EXISTS archived_at"
    )
    op.execute("ALTER TABLE enterprise_portal_sessions DROP COLUMN IF EXISTS is_main")

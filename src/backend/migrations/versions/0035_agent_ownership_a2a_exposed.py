"""agent_ownership.a2a_exposed — A2A inbound-server exposure opt-in (ent#157)

Adds the nullable/default-0 ``a2a_exposed`` column on PostgreSQL. When set, the
public A2A surface (``GET /a2a/{name}/.well-known/agent-card.json`` + the
JSON-RPC task endpoint) serves/accepts the agent; default OFF (safe by default).
Edition-agnostic OSS primitive — the OSS routes read + enforce it; the WRITE is
entitlement-gated by the enterprise A2A module. Mirrors the SQLite
``agent_ownership_a2a_exposed`` migration + ``db/schema.py`` / ``db/tables.py``.

Renumbered 0029 -> 0033 -> 0035 across two rebases. Each time, `dev` had
independently cut a revision off the same parent, and two revisions sharing a
parent is a branch point: `alembic upgrade head` then raises "Multiple head
revisions are present", which is a PostgreSQL BOOT failure (`init_database()`'s
non-SQLite branch calls `upgrade_to_head()`), not a migration warning.

0034 is deliberately skipped rather than taken: PR #1901 is holding
`0034_skill_sources` unmerged off the same head. Whichever of the two lands
second still has to re-chain its `down_revision` onto the other — that is
inherent to two open PRs both adding a revision — but skipping the number keeps
the *filenames* from colliding as well, so the second rebase is a one-line edit
instead of a rename plus an edit.

Revision ID: 0035_agent_ownership_a2a_exposed
Revises: 0032_telegram_progress_indicator
Create Date: 2026-07-15
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0035_agent_ownership_a2a_exposed"
down_revision = "0033_agent_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS a2a_exposed INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS a2a_exposed")

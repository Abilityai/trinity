"""trinity-enterprise#817 — proactive consent says "not asked" apart from "no".

`agent_sharing.allow_proactive` was 0 by default for every share, so "never
asked" and "declined" were one value. From now on NULL = not asked,
0 = declined, 1 = consented, and a new share is written NULL. The existing 0s
move to NULL: the default wrote 0 for everyone, so almost none was ever an
answer (operator ruling 2026-10-06).

Data only; the column is unchanged. Who may be messaged is unchanged: only 1
allows it. Idempotent.

Mirrors the SQLite ``proactive_consent_not_asked`` migration.

Revision ID: 0090_proactive_consent_not_asked
Revises: 0089_supersede_queue_flood_backlog
"""
from alembic import op


revision = "0090_proactive_consent_not_asked"
down_revision = "0089_supersede_queue_flood_backlog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE agent_sharing SET allow_proactive = NULL WHERE allow_proactive = 0")


def downgrade() -> None:
    # The pre-#817 reading: everything that is not consent is 0.
    op.execute("UPDATE agent_sharing SET allow_proactive = 0 WHERE allow_proactive IS NULL")

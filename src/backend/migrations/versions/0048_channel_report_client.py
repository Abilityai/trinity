"""ent#457 review — record WHICH client a portal channel context belongs to.

`source_channel_agent` (ent#265) records the agent whose binding owns the
context; nothing recorded the human. The portal leg files into a per-client
thread, and its authorization to do so came from a guard that checks the AGENT
only — so an agent shared with two clients could cite one client's execution id
while serving the other and route a report into the wrong person's thread.

Nullable, no default: pre-existing rows report NULL and the portal resolver
fails CLOSED on NULL rather than delivering unverified.

Mirrors the SQLite `channel_report_client` migration.

Numbered 0048, not 0047: #2384 (ent#366) also mints an 0047 off this same
parent. Ids are strings, so the prefix is only a human ordering cue — but a
duplicate one is exactly what makes a two-head graph hard to read afterwards
(the ent#443 hotfix precedent). NOTE both revisions still declare
`down_revision = "0046_report_audience"`, so whichever of the two merges SECOND
forks the graph and needs an `alembic merge` revision; `check_alembic_heads`
fails loudly at that point rather than silently applying nothing.

Revision ID: 0048_channel_report_client
Revises: 0046_report_audience
"""
from alembic import op


revision = "0048_channel_report_client"
down_revision = "0046_report_audience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046 and the rest of this line: a fresh PostgreSQL
    # database is built from `db/schema.py`'s DDL — which now declares this
    # column — and only THEN runs the revisions, so a bare `add_column` raises
    # DuplicateColumn on every fresh install. Caught by pg-migrations, which
    # exists to exercise exactly that boot path.
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS source_channel_client TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS source_channel_client")

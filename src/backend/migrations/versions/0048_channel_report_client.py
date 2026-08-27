"""ent#457 review — record WHICH client a portal channel context belongs to.

`source_channel_agent` (ent#265) records the agent whose binding owns the
context; nothing recorded the human. The portal leg files into a per-client
thread, and its authorization to do so came from a guard that checks the AGENT
only — so an agent shared with two clients could cite one client's execution id
while serving the other and route a report into the wrong person's thread.

Nullable, no default: pre-existing rows report NULL and the portal resolver
fails CLOSED on NULL rather than delivering unverified.

Mirrors the SQLite `channel_report_client` migration.

Numbered 0048, not 0047: #2384 (ent#366) mints 0047. Ids are strings, so the
prefix is only a human ordering cue — but a duplicate one is exactly what makes
a two-head graph hard to read afterwards (the ent#443 hotfix precedent).

Chained off 0047, not off 0046. Both revisions were written against
`0046_report_audience` while each PR was open, which is a fork: `alembic upgrade
head` is singular and resolves its target BEFORE applying anything, so two heads
apply **zero** revisions — not just the offending one, but everything merged
since the fork — and PostgreSQL boots on a schema that silently stopped
advancing. #2384 merged first (2026-08-27), so 0047 is on `dev` and may already
be applied; this revision is not applied anywhere yet, so re-parenting the
unapplied one onto the applied one converges the line with no merge revision
needed. `alembic merge` is the tool for the other case — when BOTH forked
revisions may already exist in some database — and buying it here would leave a
permanent extra node for nothing.

Revision ID: 0048_channel_report_client
Revises: 0047_workspace_ratings
"""
from alembic import op


revision = "0048_channel_report_client"
down_revision = "0047_workspace_ratings"
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

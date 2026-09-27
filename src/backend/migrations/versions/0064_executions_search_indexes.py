"""schedule_executions search indexes — pg_trgm GIN + started_at (ent#653)

The enterprise execution search (`GET /api/enterprise/execution-search`) grep's
`message` / `response` / `error` with `ILIKE '%needle%'` and, in regex mode,
the `~*` operator. Without an index both are a sequential scan over every body
in the window. A GIN index with `gin_trgm_ops` makes BOTH operators
index-assisted (trigram indexes accelerate LIKE/ILIKE and POSIX regex alike),
which is why trigram — not full-text `tsvector` — is the right tool here: it
keeps substring semantics (a partial execution id still matches) instead of
stemmed tokens.

`idx_executions_started_at` is the portable half: the ADMIN path of every fleet
read (`agent_names=None`) has no agent filter, so the composite
`idx_executions_agent_started` never applies and `ORDER BY started_at DESC
LIMIT n` was a full scan + sort. Mirrors `db/schema.py` INDEXES and the SQLite
migration `executions_started_at_index`; the trigram indexes are PostgreSQL-only
and exist ONLY here.

`CREATE EXTENSION` is deliberately NOT wrapped in try/continue: if the
extension cannot be created the index DDL below fails anyway, and a boot that
"succeeds" without the indexes is the failure nobody notices until the table
is large. `pg_trgm` ships with the stock `postgres` images and every managed
offering; a deploy role lacking the privilege needs an admin to run it once.

Plain (non-CONCURRENT) builds: at the table sizes this ships into the build is
sub-second, and it keeps the revision inside Alembic's transaction — a
CONCURRENTLY build would need an autocommit block and would not roll back
with the rest of the boot migration.

Fresh PG builds: `0001_baseline` applies `schema.py` INDEXES (so `started_at`
is already there — `IF NOT EXISTS` makes it a no-op) and every revision after
it runs in order, so the trigram indexes land on fresh installs too.

Revision ID: 0064_executions_search_indexes
Revises: 0063_agent_sync_state_git_dir_bytes_bigint
Create Date: 2026-09-20
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0064_executions_search_indexes"
down_revision = "0063_agent_sync_state_git_dir_bytes_bigint"
branch_labels = None
depends_on = None

_TRIGRAM_COLUMNS = ("message", "response", "error")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for column in _TRIGRAM_COLUMNS:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_executions_{column}_trgm "
            f"ON schedule_executions USING gin ({column} gin_trgm_ops)"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_started_at "
        "ON schedule_executions (started_at DESC)"
    )


def downgrade() -> None:
    for column in _TRIGRAM_COLUMNS:
        op.execute(f"DROP INDEX IF EXISTS idx_executions_{column}_trgm")
    op.execute("DROP INDEX IF EXISTS idx_executions_started_at")
    # The extension is left in place: another object may depend on it, and
    # dropping it is an admin decision, not this revision's.

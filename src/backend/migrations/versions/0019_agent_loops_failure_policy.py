"""Add per-loop failure-policy columns to agent_loops (#1167)

Persists the configurable loop failure policy on the PostgreSQL backend.
Mirrors the SQLite ``agent_loops_failure_policy`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in
``db/tables.py``.

Columns: ``on_failure`` ('abort'|'continue', default 'abort' = current
fail-fast behavior), ``max_consecutive_failures`` (continue-mode cutoff,
default 3), and a ``failed_runs`` counter for the terminal summary.

Fresh PG builds already get the columns because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
columns up on ``alembic upgrade head``.

Revision ID: 0019_agent_loops_failure_policy
Revises: 0018_schedule_executions_source_channel
Create Date: 2026-06-30
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_agent_loops_failure_policy"
down_revision = "0018_schedule_executions_source_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS "
        "on_failure TEXT NOT NULL DEFAULT 'abort'"
    )
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS "
        "max_consecutive_failures INTEGER NOT NULL DEFAULT 3"
    )
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS "
        "failed_runs INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_loops DROP COLUMN IF EXISTS failed_runs")
    op.execute("ALTER TABLE agent_loops DROP COLUMN IF EXISTS max_consecutive_failures")
    op.execute("ALTER TABLE agent_loops DROP COLUMN IF EXISTS on_failure")

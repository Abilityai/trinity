"""agent_loops.no_progress_threshold (#1157)

Adds the no-progress / doom-loop detection threshold column on the PostgreSQL
backend. Mirrors the SQLite ``agent_loops_no_progress`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py``.

NULL = disabled (back-compat for in-flight loops); new loops default to 3.

Fresh PG builds already get this column because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (whose ``agent_loops`` DDL now includes it). This
revision exists so an *existing* PG deployment — stamped at an earlier revision
and never re-running baseline — also picks the column up on
``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op when the
baseline already created the table with the column.

Revision ID: 0007_agent_loops_no_progress
Revises: 0006_agent_reports
Create Date: 2026-06-28
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_agent_loops_no_progress"
down_revision = "0006_agent_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS no_progress_threshold INTEGER"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_loops DROP COLUMN IF EXISTS no_progress_threshold"
    )

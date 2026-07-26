"""schedule_executions lease-reaper re-delivery counter (#1081 Phase 3, #429/#1402)

Adds ``redelivery_count`` (INTEGER, default 0) to ``schedule_executions`` on the
PostgreSQL backend. This is the poison-task counter the single lease-reaper
increments each time a lease-expired pull-claimed task is re-queued (preserving
the SAME ``execution_id``); at ``MAX_REDELIVERY`` (default 3) the row is
poison-parked to the operator queue instead.

DISTINCT from ``retry_count`` (#678 reader-race in-line retry) — that column is
untouched. Mirrors the SQLite ``schedule_executions_redelivery_count`` migration
in ``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in
``db/tables.py``.

Fresh PG builds already get this column because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (whose ``schedule_executions`` DDL now includes it). This
revision exists so an *existing* PG deployment — stamped at an earlier revision
and never re-running baseline — also picks the column up on
``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op when the
baseline already created the table with the column.

Revision ID: 0021_schedule_executions_redelivery_count
Revises: 0020_schedule_executions_pull_claim_lease
Create Date: 2026-07-02
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0021_schedule_executions_redelivery_count"
down_revision = "0020_schedule_executions_pull_claim_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS redelivery_count INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS redelivery_count"
    )

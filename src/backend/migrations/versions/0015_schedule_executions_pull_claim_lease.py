"""schedule_executions pull/work-stealing claim+lease columns (#1081 Phase 0)

Adds three DARK, nullable coordination columns to ``schedule_executions`` on the
PostgreSQL backend: ``claim_token``, ``lease_expires_at`` (ISO-8601 UTC string),
and ``claimed_by_worker``. Nothing reads or writes them yet — this is pure schema
groundwork for the pull-coordination phases (umbrella #1081). Mirrors the SQLite
``schedule_executions_pull_claim_lease`` migration in ``db/migrations.py`` and the
DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get these columns because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (whose ``schedule_executions`` DDL now includes them).
This revision exists so an *existing* PG deployment — stamped at an earlier
revision and never re-running baseline — also picks the columns up on
``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op when the
baseline already created the table with the columns.

Revision ID: 0015_schedule_executions_pull_claim_lease
Revises: 0014_agent_schedules_webhook_auth
Create Date: 2026-07-02
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_schedule_executions_pull_claim_lease"
down_revision = "0014_agent_schedules_webhook_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS claim_token TEXT"
    )
    op.execute(
        "ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS lease_expires_at TEXT"
    )
    op.execute(
        "ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS claimed_by_worker"
    )
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS lease_expires_at"
    )
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS claim_token"
    )

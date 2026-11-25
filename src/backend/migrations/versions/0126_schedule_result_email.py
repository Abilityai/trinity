"""Mail each run of a schedule to the person who asked (abilityai/trinity-enterprise#1391).

One nullable column on `agent_schedules`, no backfill and no index. `IF NOT
EXISTS` on both directions: a fresh PostgreSQL build runs `init_schema_postgres`
first and then meets a column that already exists (the 0052 rule).

Revision ID: 0126_schedule_result_email
Revises: 0125_guardrail_events
"""
from alembic import op

revision = "0126_schedule_result_email"
down_revision = "0125_guardrail_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_schedules ADD COLUMN IF NOT EXISTS deliver_to_email TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_schedules DROP COLUMN IF EXISTS deliver_to_email")

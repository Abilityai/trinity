"""Let a schedule deliver its output into a Workspace conversation (ent#498).

One nullable column on `agent_schedules`, no backfill and no index.

`IF NOT EXISTS` is mandatory on both directions, not defensive: a fresh
PostgreSQL build runs `init_schema_postgres` from `db/schema.py` FIRST and is
then stamped through the revision chain, so this revision routinely meets a
column that already exists (the rule 0052 states in full).

No index: the column is read only through the schedule row the scheduler has
already loaded by id, never selected on.

Revision ID: 0056_schedule_workspace_delivery
Revises: 0055_portal_session_main_chat
"""
from alembic import op

revision = "0056_schedule_workspace_delivery"
down_revision = "0055_portal_session_main_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_schedules "
        "ADD COLUMN IF NOT EXISTS deliver_to_workspace_email TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_schedules "
        "DROP COLUMN IF EXISTS deliver_to_workspace_email"
    )

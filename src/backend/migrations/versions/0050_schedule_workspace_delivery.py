"""ent#498 — a schedule can name ONE Workspace user as its delivery target.

When the schedule fires, the execution's terminal is filed as a new turn in that
person's Workspace conversation with the agent, riding the ent#457 portal
completion-report leg. The column carries the ADDRESS only; which thread the
output lands in is resolved live at dispatch (`services/workspace_delivery`).

Nullable, no default: every pre-existing schedule reports NULL and is unchanged.

Mirrors the SQLite `schedule_workspace_delivery` migration.

Revision ID: 0050_schedule_workspace_delivery
Revises: 0049_execution_turn_integrity
"""
from alembic import op


revision = "0050_schedule_workspace_delivery"
down_revision = "0049_execution_turn_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching the rest of this line: a fresh PostgreSQL
    # database is built from `db/schema.py`'s DDL — which already declares this
    # column — and only THEN runs the revisions, so a bare `add_column` raises
    # DuplicateColumn on every fresh install. `pg-migrations` exercises exactly
    # that boot path.
    op.execute(
        "ALTER TABLE agent_schedules "
        "ADD COLUMN IF NOT EXISTS deliver_to_workspace_email TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_schedules DROP COLUMN IF EXISTS deliver_to_workspace_email"
    )

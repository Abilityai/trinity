"""operator_queue.wake_state — the answer wake's status on the ask (abilityai/trinity-enterprise#1108 AC #4).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::operator_queue_wake_state``.

One nullable TEXT column: NULL (no wake owed, or before the column) |
``pending`` (the answer wake found the agent stopped or missing — "agent not
yet woken"; its next start re-dispatches it once) | ``woken``. No default, no
backfill. Chained after 0116 so the version-line keeps ONE head (#2068).

Revision ID: 0117_operator_queue_wake_state
Revises: 0116_operator_resume_default_on
"""
from alembic import op


revision = "0117_operator_queue_wake_state"
down_revision = "0116_operator_resume_default_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS wake_state TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE operator_queue DROP COLUMN IF EXISTS wake_state")

"""Mail each run of a schedule to the person who asked (abilityai/trinity-enterprise#1391).

Three nullable columns on `agent_schedules`, no backfill and no index:

* `email_results_user_id`: WHO turned it on. The address is never stored; it is
  read from that person's account at each send, so it follows an account email
  change, and mail stops once that person no longer manages the agent.
* `email_results_error` / `email_results_error_at`: why the last result mail
  did not go, shown on the schedule. NULL after a mail that went.

`IF NOT EXISTS` on both directions: a fresh PostgreSQL build runs
`init_schema_postgres` first and then meets columns that already exist (the
0052 rule).

Revision ID: 0126_schedule_email_results
Revises: 0125_guardrail_events
"""
from alembic import op

revision = "0126_schedule_email_results"
down_revision = "0125_guardrail_events"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("email_results_user_id", "INTEGER"),
    ("email_results_error", "TEXT"),
    ("email_results_error_at", "TEXT"),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.execute(f"ALTER TABLE agent_schedules ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE agent_schedules DROP COLUMN IF EXISTS {name}")

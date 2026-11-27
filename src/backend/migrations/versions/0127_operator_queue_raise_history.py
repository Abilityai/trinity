"""A re-raised alert keeps its age and its history (abilityai/trinity-enterprise#1367).

Two nullable columns on ``operator_queue``, written on the successor when an
agent's alert replaces its own pending alert, in the one locked transaction
that ends the predecessor and inserts the successor:

* ``first_raised_at``: the first raise in the chain (ISO-Z).
* ``raise_count``: how many times the condition was raised.

No default, no backfill, no index: a chain raised before the columns is
counted at raise time by walking ``replaces``, and every reader falls back to
``created_at`` and a count of 1. ``IF NOT EXISTS`` on both directions: a fresh
PostgreSQL build meets columns that already exist (the 0052 rule). Mirrors the
SQLite ``operator_queue_raise_history`` migration.

Revision ID: 0127_operator_queue_raise_history
Revises: 0126_schedule_email_results
"""
from alembic import op

revision = "0127_operator_queue_raise_history"
down_revision = "0126_schedule_email_results"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("first_raised_at", "TEXT"),
    ("raise_count", "INTEGER"),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.execute(f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {name}")

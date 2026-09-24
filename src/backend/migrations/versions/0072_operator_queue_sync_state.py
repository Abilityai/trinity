"""#2915 — the operator-queue file sync tells the truth.

Eight nullable columns on ``operator_queue`` record what the poller last
established about the agent's file entry (``sync_state`` / ``sync_detail`` /
``sync_updated_at`` / ``last_confirmed_at``), whether the human's answer ever
reached the agent (``delivery_state`` / ``delivery_detail`` /
``delivery_updated_at``) and whether the human answered a diverged item
knowingly (``divergence_acknowledged_at``). No default and no backfill: NULL means "not yet
checked", which is the honest state for every existing row, never "confirmed".

Mirrors the SQLite ``operator_queue_sync_state`` migration.

Revision ID: 0072_operator_queue_sync_state
Revises: 0071_seat_decisions
"""
from alembic import op


revision = "0072_operator_queue_sync_state"
down_revision = "0071_seat_decisions"
branch_labels = None
depends_on = None

_COLUMNS = (
    "sync_state",
    "sync_detail",
    "sync_updated_at",
    "last_confirmed_at",
    "delivery_state",
    "delivery_detail",
    "delivery_updated_at",
    "divergence_acknowledged_at",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {column} TEXT")


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {column}")

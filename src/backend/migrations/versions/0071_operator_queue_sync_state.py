"""#2915 — the operator-queue file sync tells the truth.

Seven nullable columns on ``operator_queue`` record what the poller last
established about the agent's file entry (``sync_state`` / ``sync_detail`` /
``sync_updated_at`` / ``last_confirmed_at``) and whether the human's answer ever
reached the agent (``delivery_state`` / ``delivery_detail`` /
``delivery_updated_at``). No default and no backfill: NULL means "not yet
checked", which is the honest state for every existing row, never "confirmed".

Mirrors the SQLite ``operator_queue_sync_state`` migration.

Revision ID: 0071_operator_queue_sync_state
Revises: 0070_metric_points
"""
from alembic import op


revision = "0071_operator_queue_sync_state"
down_revision = "0070_metric_points"
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
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {column} TEXT")


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {column}")

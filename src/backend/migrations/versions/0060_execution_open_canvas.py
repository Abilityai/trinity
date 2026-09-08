"""ent#555 — which canvas the user had open when they sent a turn.

A per-turn CONTEXT field of the same shape as the ``source_channel*`` columns
already on this table. It never widens what the agent may reach: the boundary
that stamps it validates the canvas belongs to that agent.

Mirrors the SQLite ``execution_open_canvas`` migration.

Revision ID: 0060_execution_open_canvas
Revises: 0059_agent_canvas_shares
"""
from alembic import op


revision = "0060_execution_open_canvas"
down_revision = "0059_agent_canvas_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS open_canvas_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS open_canvas_id")

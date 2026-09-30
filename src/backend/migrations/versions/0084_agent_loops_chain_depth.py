"""#2973 — the inter-agent chain depth a loop inherits from its starter.

Captured when an agent principal starts the loop and stamped on every
iteration's execution row, because later iterations run after the starter's own
turn has ended and its running rows can no longer be read. NULL on a loop
started by a human (a root). Nullable, no backfill.

Mirrors the SQLite ``loop_chain_depth`` migration.

Revision ID: 0084_agent_loops_chain_depth
Revises: 0083_execution_conversation_key
"""
from alembic import op


revision = "0084_agent_loops_chain_depth"
down_revision = "0083_execution_conversation_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS chain_depth INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_loops DROP COLUMN IF EXISTS chain_depth")

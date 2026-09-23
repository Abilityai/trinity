"""#2806 — how many agent-to-agent hops deep an execution is.

Stamped at dispatch on the child row of an agent-principal call; NULL (read as
0) on every root. The inter-agent chain-depth guard reads it to refuse a hop
past ``inter_agent_max_chain_depth``. Nullable with no default, so existing rows
are roots and nothing is backfilled.

Mirrors the SQLite ``execution_chain_depth`` migration.

Revision ID: 0073_execution_chain_depth
Revises: 0072_agent_capability_grants
"""
from alembic import op


revision = "0073_execution_chain_depth"
down_revision = "0072_agent_capability_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS chain_depth INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS chain_depth")

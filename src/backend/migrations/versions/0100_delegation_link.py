"""delegation link (abilityai/trinity-enterprise#935, addendum A).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::delegation_link``.

``schedule_executions.parent_execution_id`` names the caller turn that
delegated a run — stamped at admission only when that turn belongs to the
child's authenticated source agent. ``agent_evaluations`` gains
``caller_agent`` / ``parent_execution_id`` / ``delegation_mode`` and a
per-caller index. All nullable with no default, so every existing row reads as
"not delegated" and nothing is backfilled.

Revision ID: 0100_delegation_link
Revises: 0099_agent_peer_skill_grants
"""
from alembic import op


revision = "0100_delegation_link"
down_revision = "0099_agent_peer_skill_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS parent_execution_id TEXT"
    )
    for column in ("caller_agent", "parent_execution_id", "delegation_mode"):
        op.execute(
            f"ALTER TABLE agent_evaluations ADD COLUMN IF NOT EXISTS {column} TEXT"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_evaluations_caller "
        "ON agent_evaluations(caller_agent, agent_name, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_evaluations_caller")
    for column in ("delegation_mode", "parent_execution_id", "caller_agent"):
        op.execute(f"ALTER TABLE agent_evaluations DROP COLUMN IF EXISTS {column}")
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS parent_execution_id"
    )

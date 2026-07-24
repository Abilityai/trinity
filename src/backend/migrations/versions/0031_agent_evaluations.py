"""agent_evaluations — behavioral-eval referee surface (ent#206)

The run-quality score: `completion` (a mirror of schedule_executions clean-exit)
and a separate `quality` axis, written ONLY by the platform/evaluator, never by
the graded agent's key (the load-bearing rule of the eval epic). SQLite track
creates the same shape via db/schema.py + db/migrations.py.

Revision ID: 0031_agent_evaluations
Revises: 0030_slack_channel_allow_proactive
Create Date: 2026-07-23
"""
from alembic import op

revision = "0031_agent_evaluations"
down_revision = "0030_slack_channel_allow_proactive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_evaluations (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            execution_id TEXT,
            archetype TEXT,
            completion INTEGER,
            quality DOUBLE PRECISION,
            checks_json TEXT,
            judge_json TEXT,
            evaluator TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_evaluations_agent "
        "ON agent_evaluations(agent_name, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_evaluations_execution "
        "ON agent_evaluations(execution_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_evaluations")

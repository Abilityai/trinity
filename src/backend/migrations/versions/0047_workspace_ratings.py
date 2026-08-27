"""Workspace ratings land in `agent_evaluations` (ent#366).

The PostgreSQL half of `db/migrations.py::_migrate_workspace_ratings`. All four
columns are nullable with no default, so every existing row keeps its meaning: a
graded run, with no rated object and no comment.

Revision ID: 0047_workspace_ratings
Revises: 0046_report_audience
"""
from alembic import op

revision = "0047_workspace_ratings"
down_revision = "0046_report_audience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("target_kind", "target_id", "comment", "updated_at"):
        op.execute(f"ALTER TABLE agent_evaluations ADD COLUMN IF NOT EXISTS {column} TEXT")
    # One rating per person per thing — the property behind "changing your mind
    # updates rather than appends". Partial, so graded-run rows (no target) are
    # unaffected however many a Tier-0 pass writes.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_evaluations_rating_target "
        "ON agent_evaluations(evaluator, target_kind, target_id) WHERE target_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_evaluations_target "
        "ON agent_evaluations(agent_name, target_kind, target_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_evaluations_target")
    op.execute("DROP INDEX IF EXISTS idx_agent_evaluations_rating_target")
    for column in ("updated_at", "comment", "target_id", "target_kind"):
        op.execute(f"ALTER TABLE agent_evaluations DROP COLUMN IF EXISTS {column}")

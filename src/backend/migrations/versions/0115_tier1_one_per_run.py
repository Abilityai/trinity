"""one judge row per run (abilityai/trinity-enterprise#936).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::tier1_one_per_run``.

A partial unique index over ``agent_evaluations(target_kind, target_id)`` for
``platform:tier1:%`` evaluators: the claim's existence check and its INSERT are
not serialised, and the evaluator key embeds the model, so two claimers under
different model ids could both insert. A run already holding more than one
such row keeps the finished one over a claim, then the newest.

Revision ID: 0115_tier1_one_per_run
Revises: 0114_execution_parent_index
"""
from alembic import op


revision = "0115_tier1_one_per_run"
down_revision = "0114_execution_parent_index"
branch_labels = None
depends_on = None


# The same text as ``db/migrations.py::TIER1_DEDUPE_SQL`` (a revision imports
# nothing from the app); parity is pinned by test_936_tier1_review.py.
TIER1_DEDUPE_SQL = (
    "DELETE FROM agent_evaluations WHERE evaluator LIKE 'platform:tier1:%' "
    "AND target_id IS NOT NULL AND id NOT IN ("
    "SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
    "PARTITION BY target_kind, target_id ORDER BY "
    "CASE WHEN judge_json LIKE '%\"state\": \"claimed\"%' THEN 1 ELSE 0 END, "
    "COALESCE(updated_at, '') DESC, id DESC) AS rn FROM agent_evaluations "
    "WHERE evaluator LIKE 'platform:tier1:%' AND target_id IS NOT NULL) ranked "
    "WHERE rn = 1)"
)


def upgrade() -> None:
    op.execute(TIER1_DEDUPE_SQL)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_evaluations_tier1_target "
        "ON agent_evaluations(target_kind, target_id) WHERE evaluator LIKE 'platform:tier1:%'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_evaluations_tier1_target")

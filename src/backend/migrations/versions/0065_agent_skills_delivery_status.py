"""#2914 — the durable per-assignment injection verdict on ``agent_skills``.

Assigning a library skill whose name matched an agent-authored
``.claude/skills/<name>/`` used to overwrite the agent's copy and bury an
``unmanaged_dir_overwritten`` warning in the assignment response. The inject
path now refuses to write into a directory the platform did not create and
records ``conflict`` on the assignment row instead; NULL means no standing
conflict. Recorded on the row rather than derived at read time because the
Skills tab must show the conflict to an operator who never saw the injection
response; the inject path clears it on the next sync once the name lands.

Mirrors the SQLite ``agent_skills_delivery_status`` migration.

Revision ID: 0065_agent_skills_delivery_status
Revises: 0064_executions_search_indexes
"""
from alembic import op


revision = "0065_agent_skills_delivery_status"
down_revision = "0064_executions_search_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0062: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares this column — and only THEN
    # runs the revisions, so a bare `add_column` raises DuplicateColumn on every
    # fresh install.
    op.execute(
        "ALTER TABLE agent_skills ADD COLUMN IF NOT EXISTS delivery_status TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_skills DROP COLUMN IF EXISTS delivery_status")

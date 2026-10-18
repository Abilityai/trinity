"""execution parent index (abilityai/trinity-enterprise#937).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::execution_parent_index``.

The evaluation chain read walks descendants by
``schedule_executions.parent_execution_id IN (...)`` one level at a time.
Index only: no column, no backfill.

Revision ID: 0101_execution_parent_index
Revises: 0100_delegation_link
"""
from alembic import op


revision = "0101_execution_parent_index"
down_revision = "0100_delegation_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_parent "
        "ON schedule_executions(parent_execution_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_executions_parent")

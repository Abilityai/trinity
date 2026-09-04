"""#2524 — the caller's fan-out subtask id, on the execution row.

``FanOutService`` used to hold the whole batch in one coroutine and key its
results by the caller's task id in a local dict. The aggregate is a query over
``fan_out_id`` now — that is what lets a fan-out run on the durable queue, and
what lets a status endpoint answer after the dispatching request is gone — so
the id has to be persisted next to the execution it belongs to.

Also adds a composite ``(fan_out_id, status)`` index: the join counts
non-terminal rows for one batch on every fan-out terminal, which the existing
single-column ``idx_executions_fan_out`` cannot serve without reading every row
of the batch.

Mirrors the SQLite ``execution_fan_out_task_id`` migration.

Revision ID: 0051_execution_fan_out_task_id
Revises: 0050_agent_loops_terminal_driven
"""
from alembic import op


revision = "0051_execution_fan_out_task_id"
down_revision = "0050_agent_loops_terminal_driven"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0050: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares this column — and only THEN
    # runs the revisions, so a bare `add_column` raises DuplicateColumn on every
    # fresh install.
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS fan_out_task_id TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_fan_out_status "
        "ON schedule_executions(fan_out_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_executions_fan_out_status")
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS fan_out_task_id"
    )

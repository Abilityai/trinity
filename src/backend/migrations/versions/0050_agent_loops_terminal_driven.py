"""#2523 — the two columns that let a loop live without an in-process runner.

``LoopService._run`` used to hold the whole loop in one ``asyncio.Task``: a
``for`` loop over iterations, the stop flag on an in-memory ``_LoopHandle``, and
the inter-run pause as ``asyncio.sleep``. None of that survives a backend
restart, so startup recovery flipped every in-flight loop to ``interrupted``.
The loop is now advanced by execution terminals, which needs two pieces of the
old runner's state on the row: ``next_run_at`` (when the next iteration is due —
NULL means "not waiting") and ``stop_requested_at`` (replacing
``_LoopHandle.should_stop``, so a stop works on a loop this process never
started).

Everything else the runner kept locally was already persisted or is derivable
from ``agent_loop_runs``, which is why only two columns are needed.

Mirrors the SQLite ``agent_loops_terminal_driven`` migration.

Revision ID: 0050_agent_loops_terminal_driven
Revises: 0049_execution_turn_integrity
"""
from alembic import op


revision = "0050_agent_loops_terminal_driven"
down_revision = "0049_execution_turn_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0049: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares both columns — and only
    # THEN runs the revisions, so a bare `add_column` raises DuplicateColumn on
    # every fresh install.
    op.execute("ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS next_run_at TEXT")
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS stop_requested_at TEXT"
    )
    # The due-loop sweep runs every few seconds; without this it is a full scan.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_loops_next_run ON agent_loops(next_run_at)"
    )
    # Every execution terminal asks "is this a loop run?" — an indexed point
    # read, not a scan of every loop run ever recorded.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_loop_runs_execution "
        "ON agent_loop_runs(execution_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_loop_runs_execution")
    op.execute("DROP INDEX IF EXISTS idx_loops_next_run")
    op.execute("ALTER TABLE agent_loops DROP COLUMN IF EXISTS stop_requested_at")
    op.execute("ALTER TABLE agent_loops DROP COLUMN IF EXISTS next_run_at")

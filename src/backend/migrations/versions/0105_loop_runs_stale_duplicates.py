"""loop_runs_stale_duplicates — close the stale `running` loop-run rows (#4058).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::loop_runs_stale_duplicates``. Both run the shared
``run_loop_runs_stale_cleanup``, whose decision is
``db.loop_run_hygiene.stale_running_run_ids`` (the one the runtime uses too):
duplicate run rows the pre-#4023 race inserted, rows numbered below their
loop's ``runs_completed``, and every ``running`` row of an ended loop close
``abandoned``. Data only — no DDL.

Revision ID: 0105_loop_runs_stale_duplicates
Revises: 0104_agent_sync_state_git_stats_at
"""
from alembic import op
from sqlalchemy import text


revision = "0105_loop_runs_stale_duplicates"
down_revision = "0104_agent_sync_state_git_stats_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import run_loop_runs_stale_cleanup
    from utils.helpers import utc_now_iso

    bind = op.get_bind()

    def _run(sql, params, fetch=False):
        result = bind.execute(text(sql), params)
        return result.fetchall() if fetch else None

    run_loop_runs_stale_cleanup(_run, now_iso=utc_now_iso())


def downgrade() -> None:
    # Deliberately a no-op: the inverse is "re-open run rows no terminal can
    # ever advance", which is the defect this revision removes.
    pass

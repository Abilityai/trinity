"""platform_alert_resweep — the #3246 upgrade sweep, run a second time (#3254).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::platform_alert_subjects_resweep``. Data only: no schema
change. ``0090_platform_alert_subjects`` swept the platform-alert backlog
once; the emitters #3254 moves onto the seam kept filing rows with no
``subject`` until each move landed, and the seam cannot see such a row. This
re-runs the unchanged shared ``run_platform_alert_sweep`` over the same
connection the first sweep used: the subject's existing holder keeps the slot
whatever its age, else the newest pending row stays; the rest end
``cancelled`` / ``disposed_by = 'platform'`` / ``superseded`` as one batch.
Agent-raised and gate rows, and the gitignore and problem-report rows (no
subject, no lifetime), are never touched; a git-bloat row gets a lifetime
only. A fresh database has no rows and plans nothing.

Revision ID: 0104_platform_alert_resweep
Revises: 0103_a2a_caller_identity
"""
from alembic import op
from sqlalchemy import text


revision = "0104_platform_alert_resweep"
down_revision = "0103_a2a_caller_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import run_platform_alert_sweep

    bind = op.get_bind()

    def _run(sql, params, fetch=False):
        result = bind.execute(text(sql), params)
        return result.fetchall() if fetch else None

    run_platform_alert_sweep(_run)


def downgrade() -> None:
    # Data only, and deliberately not reverted, as in 0090: the inverse is
    # "re-open the duplicate platform alarms the sweep ended".
    pass

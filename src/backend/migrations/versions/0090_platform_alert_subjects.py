"""platform_alert_subjects — one pending platform alert per subject (#3246).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::platform_alert_subjects``. Ordered columns → sweep →
index in ONE revision: the partial unique index can only be created once the
sweep has collapsed the duplicates an installed backlog holds.

Adds ``subject TEXT`` and ``last_seen_at TEXT`` (nullable, no backfill beyond
the sweep), runs the shared ``run_platform_alert_sweep`` (both tracks call the
same function over their own connection, so the survivor rule cannot drift:
per (agent, subject) the newest pending row — by ``created_at``, then ``id`` —
stays stamped; the rest end ``cancelled`` / ``disposed_by = 'platform'`` /
``superseded`` as one batch; rows whose subject cannot be derived are never
merged on a guess), then creates ``uq_operator_queue_pending_subject``
(partial: ``status = 'pending' AND subject IS NOT NULL``) and
``idx_operator_queue_agent_subject``.

Revision ID: 0090_platform_alert_subjects
Revises: 0089_supersede_queue_flood_backlog
"""
from alembic import op
from sqlalchemy import text


revision = "0090_platform_alert_subjects"
down_revision = "0089_supersede_queue_flood_backlog"
branch_labels = None
depends_on = None

_COLUMNS = ("subject", "last_seen_at")
_INDEXES = ("uq_operator_queue_pending_subject", "idx_operator_queue_agent_subject")


def upgrade() -> None:
    from db.migrations import PLATFORM_ALERT_INDEX_DDL, run_platform_alert_sweep

    for column in _COLUMNS:
        op.execute(f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {column} TEXT")

    bind = op.get_bind()

    def _run(sql, params, fetch=False):
        result = bind.execute(text(sql), params)
        return result.fetchall() if fetch else None

    run_platform_alert_sweep(_run)

    for ddl in PLATFORM_ALERT_INDEX_DDL:
        op.execute(ddl)


def downgrade() -> None:
    for name in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {column}")
    # The sweep's endings are deliberately not reverted: the inverse is
    # "re-open hundreds of duplicate platform alarms", which no rollback wants.

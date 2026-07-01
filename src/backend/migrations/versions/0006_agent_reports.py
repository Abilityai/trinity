"""agent_reports table (#918)

Agent-published structured reports (telemetry / domain reports) surfaced on the
dashboard. Mirrors the SQLite ``agent_reports_table`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py``.

Fresh PG builds already get this table because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
table up on ``alembic upgrade head``. ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` keep it a no-op when baseline already created it.

Revision ID: 0006_agent_reports
Revises: 0005_agent_loops_max_duration
Create Date: 2026-06-27
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_agent_reports"
down_revision = "0005_agent_loops_max_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_reports (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            user_id INTEGER,
            report_type TEXT NOT NULL,
            title TEXT NOT NULL,
            payload TEXT NOT NULL,
            display_hint TEXT,
            schema_version INTEGER DEFAULT 1,
            period_start TEXT,
            period_end TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_agent "
        "ON agent_reports(agent_name, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_type "
        "ON agent_reports(report_type, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_created "
        "ON agent_reports(created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_reports")

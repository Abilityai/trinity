"""agent_compatibility_results table (#668)

Creates the latest-snapshot-per-agent compatibility results table on the
PostgreSQL backend. Mirrors the SQLite ``agent_compatibility_results_table``
migration in ``db/migrations.py`` and the DDL in ``db/schema.py``.

Fresh PG builds already get this table because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (which now includes it). This revision exists so an
*existing* PG deployment — stamped at an earlier revision and never re-running
baseline — also picks the table up on ``alembic upgrade head``.

Revision ID: 0003_agent_compatibility_results
Revises: 0002_activities_created_index
Create Date: 2026-06-20
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_agent_compatibility_results"
down_revision = "0002_activities_created_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_compatibility_results (
            agent_name TEXT PRIMARY KEY,
            overall_status TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            hard_count INTEGER NOT NULL DEFAULT 0,
            soft_count INTEGER NOT NULL DEFAULT 0,
            info_count INTEGER NOT NULL DEFAULT 0,
            container_running INTEGER NOT NULL DEFAULT 0,
            ai_ran_at TEXT,
            static_ran_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_compatibility_results")

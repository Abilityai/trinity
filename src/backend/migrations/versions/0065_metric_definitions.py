"""trinity-enterprise#477 — the declared metric registry.

One row per metric an agent's ``template.yaml metrics:`` block declares,
reconciled on create / git pull / reset / sync-pull_first / container start /
explicit refresh. ``UNIQUE(agent_name, name)`` is the conflict target
``MetricDefinitionOperations.reconcile`` upserts against, and the key ent#478's
``metric_points`` validates against.

Mirrors the SQLite ``metric_definitions_table`` migration.

**Numbering (decision 15 / E1).** Numbered from the LIVE ``dev`` head at branch
time, which is ``0064_executions_search_indexes``. Three migration PRs (#2920,
#2924, #2927) are open against ``dev`` and each claims ``0065``; whichever lands
first makes this revision a FORK, and a forked version-line applies **zero**
revisions on PostgreSQL while git reports no conflict (#2068). So this file is
re-chained onto the real head at PR time — ``check_alembic_heads.py`` and
``alembic-head-watch`` are the gates that catch it if it is not. Delete
``migrations/versions/__pycache__`` when renumbering (learning 2026-08-24).

Revision ID: 0065_metric_definitions
Revises: 0064_executions_search_indexes
"""
from alembic import op


revision = "0065_metric_definitions"
down_revision = "0064_executions_search_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching the surrounding revisions: a fresh PostgreSQL
    # database is built from `db/schema.py`'s DDL — which already declares this
    # table — and only THEN runs the revisions.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_definitions (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            label TEXT,
            description TEXT,
            unit TEXT,
            warning_threshold REAL,
            critical_threshold REAL,
            status_values_json TEXT,
            cadence TEXT,
            cadence_seconds INTEGER,
            direction TEXT NOT NULL DEFAULT 'neutral',
            aggregation TEXT NOT NULL DEFAULT 'last',
            dimensions_json TEXT,
            extensions_json TEXT,
            definition_hash TEXT,
            type_conflict TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT,
            first_declared_at TEXT,
            last_synced_at TEXT,
            retired_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(agent_name, name)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_definitions_agent_status "
        "ON metric_definitions(agent_name, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS metric_definitions")

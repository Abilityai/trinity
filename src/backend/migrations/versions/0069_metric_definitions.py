"""trinity-enterprise#477 — the declared metric registry.

One row per metric an agent's ``template.yaml metrics:`` block declares,
reconciled on create / git pull / reset / sync-pull_first / container start /
explicit refresh. ``UNIQUE(agent_name, name)`` is the conflict target
``MetricDefinitionOperations.reconcile`` upserts against, and the key ent#478's
``metric_points`` validates against.

Mirrors the SQLite ``metric_definitions_table`` migration.

**Numbering (decision 15 / E1).** Originally numbered ``0065`` from the ``dev``
head at branch time (``0064_executions_search_indexes``); #2920 then landed
``0065_agent_skills_delivery_status`` on the same parent, making this revision a
FORK — and a forked version-line applies **zero** revisions on PostgreSQL while
git reports no conflict (#2068). It was re-chained once to ``0066`` on top of
``0065``, and then #2924, #2927 and #2936 landed ``0066_public_user_memory_writes``
→ ``0067_agent_role_readiness`` → ``0068_agent_shared_files_audience`` ahead of
it, forking it a second time. The merge train re-chained it again at landing
time, so it is ``0069_metric_definitions`` on top of
``0068_agent_shared_files_audience`` — ``alembic-head-watch`` is advisory, so
nothing in CI forces a fork to be fixed before merge. ``check_alembic_heads.py``
over a merge-tree against LIVE ``dev`` is the proof, and it must be re-run
immediately before merge: the PR-event result goes stale the moment ``dev``
advances (#2533). ``migrations/versions/__pycache__`` was deleted with the
rename (learning 2026-08-24).

Revision ID: 0069_metric_definitions
Revises: 0068_agent_shared_files_audience
"""
from alembic import op


revision = "0069_metric_definitions"
down_revision = "0068_agent_shared_files_audience"
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

"""trinity-enterprise#478 — the recorded metric point store.

The append-only table `record_metrics` writes: one row per observation of a
metric the ent#477 registry declares for that agent.

**No surrogate id.** The primary key IS the point identity
`(agent_name, ts, idempotency_key)`, where `idempotency_key` is
`sha256(metric \0 ts \0 canonical_dims)` — the same observation posted twice
conflicts with itself and the insert's `ON CONFLICT DO NOTHING` drops the
second copy. Keeping `agent_name` inside the only unique constraint is what
lets ent#80 partition by month later without a table rebuild (PostgreSQL
requires the partition key in every unique constraint).

`dims` is **JSONB** here, and TEXT on SQLite. The shared DDL in `db/schema.py`
carries a `/* pg:JSONB */` marker that `to_postgres_table_ddl` rewrites, so the
fresh-PostgreSQL path (`0001_baseline`, which replays that DDL) and this
upgrade path converge on the same column type with no `ALTER ... USING`
anywhere.

`value_numeric` is `DOUBLE PRECISION`, not `REAL`: `REAL` is float4 on
PostgreSQL and would silently round a revenue metric's cents.

Mirrors the SQLite `metric_points_table` migration.

**Numbering.** Chains after `0066_metric_definitions` (ent#477, the branch this
one stacks on). Per the operator's train order this lands ahead of the open
#2924 and #2927, which renumber behind it. `check_alembic_heads.py` over a
`git merge-tree` against LIVE `dev` is the proof and must be re-run immediately
before merge — the PR-event result goes stale the moment `dev` advances
(#2533).

Revision ID: 0067_metric_points
Revises: 0066_metric_definitions
"""
from alembic import op


revision = "0067_metric_points"
down_revision = "0066_metric_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching the surrounding revisions: a fresh PostgreSQL
    # database is built from `db/schema.py`'s DDL — which already declares this
    # table — and only THEN runs the revisions.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_points (
            agent_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            ts TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            value_numeric DOUBLE PRECISION,
            value_text TEXT,
            dims JSONB,
            execution_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (agent_name, ts, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_points_agent_metric_ts "
        "ON metric_points(agent_name, metric, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_points_ts ON metric_points(ts)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_points_agent_created "
        "ON metric_points(agent_name, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS metric_points")

"""Create agent_connectors table (ent#46 — per-agent MCP connector)

Per-agent MCP connector config on the PostgreSQL backend. Mirrors the SQLite
`agent_connectors_table` migration in `db/migrations.py` and the DDL in
`db/schema.py` / MetaData in `db/tables.py`.

The scoped connector key lives in `mcp_api_keys` (scope='connector'); this row
holds the per-agent config: whether the connector is enabled and which
playbooks are exposed (`exposed_playbooks` = JSON array of names; NULL = all
user_invocable playbooks).

Fresh PG builds already get the table because `0001_baseline` iterates
`db/schema.py:TABLES`. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
table up on `alembic upgrade head`.

Revision ID: 0006_agent_connectors
Revises: 0005_agent_loops_max_duration
Create Date: 2026-06-26
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_agent_connectors"
down_revision = "0005_agent_loops_max_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_connectors (
            agent_name TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            exposed_playbooks TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_connectors")

"""enterprise_connectors table — per-agent MCP connector (ent#46 → OSS-core #118)

Config row per agent: enabled + exposed-playbook allow-list. Relocated from the
enterprise submodule into OSS core (#118). The table keeps its ``enterprise_connectors``
name so an existing enterprise PG install — which created it via the enterprise
Alembic line (``enterprise/.../0006_mcp_connector``) — adopts its data with zero
migration. Mirrors the SQLite ``enterprise_connectors_table`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py``.

Fresh PG builds already get this table because ``0001_baseline`` iterates
``db/schema.py:TABLES``. ``CREATE TABLE IF NOT EXISTS`` keeps it a no-op when
baseline (or the enterprise line) already created it.

Revision ID: 0015_enterprise_connectors
Revises: 0014_agent_schedules_webhook_auth
Create Date: 2026-07-09
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_enterprise_connectors"
down_revision = "0014_agent_schedules_webhook_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_connectors (
            agent_name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            exposed_playbooks TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS enterprise_connectors")

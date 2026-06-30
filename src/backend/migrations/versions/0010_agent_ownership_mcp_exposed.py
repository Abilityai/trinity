"""agent_ownership.mcp_exposed (#846)

Adds the per-agent MCP-exposure toggle column on the PostgreSQL backend. Mirrors
the SQLite ``agent_ownership_mcp_exposed`` migration in ``db/migrations.py`` and
the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get this column because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (whose ``agent_ownership`` DDL now includes it). This
revision exists so an *existing* PG deployment — stamped at an earlier revision
and never re-running baseline — also picks the column up on
``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op when the
baseline already created the table with the column.

Revision ID: 0010_agent_ownership_mcp_exposed
Revises: 0009_agent_ownership_public_channel_model
Create Date: 2026-06-29
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_agent_ownership_mcp_exposed"
down_revision = "0009_agent_ownership_public_channel_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS mcp_exposed INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership DROP COLUMN IF EXISTS mcp_exposed"
    )

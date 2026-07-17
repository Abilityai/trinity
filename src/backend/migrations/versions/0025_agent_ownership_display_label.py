"""agent_ownership.display_label — the human-facing agent label (ent#181)

Adds ``display_label`` (nullable TEXT) on the PostgreSQL backend. NULL means
"render the slug", which is exactly today's behaviour — no backfill, and every
existing agent looks unchanged until an owner sets a label.

Presentation only: the slug (``agent_name``) stays the identity that routes,
container/volume names, MCP keys, A2A cards and every ``agent_name`` column key
on. That separation is the feature — the slug rename (RENAME-001) has to rewrite
~20 tables and still strands the agent's volumes under the old base (#1664).

Mirrors the SQLite ``agent_ownership_display_label`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the column via ``0001_baseline``; ``ADD COLUMN IF NOT
EXISTS`` keeps this a no-op there.

Revision ID: 0025_agent_ownership_display_label
Revises: 0024_agent_ownership_volume_base_name
Create Date: 2026-07-17
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0025_agent_ownership_display_label"
down_revision = "0024_agent_ownership_volume_base_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS display_label TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS display_label")

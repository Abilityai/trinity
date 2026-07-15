"""agent_ownership.a2a_exposed — A2A inbound-server exposure opt-in (ent#157)

Adds the nullable/default-0 ``a2a_exposed`` column on PostgreSQL. When set, the
public A2A surface (``GET /a2a/{name}/.well-known/agent-card.json`` + the
JSON-RPC task endpoint) serves/accepts the agent; default OFF (safe by default).
Edition-agnostic OSS primitive — the OSS routes read + enforce it; the WRITE is
entitlement-gated by the enterprise A2A module. Mirrors the SQLite
``agent_ownership_a2a_exposed`` migration + ``db/schema.py`` / ``db/tables.py``.

Revision ID: 0024_agent_ownership_a2a_exposed
Revises: 0023_agent_sync_state_gc_signals
Create Date: 2026-07-15
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0024_agent_ownership_a2a_exposed"
down_revision = "0023_agent_sync_state_gc_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS a2a_exposed INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS a2a_exposed")

"""a2a_internal_scope — A2A exposure scope and the keyless trusted caller (trinity-enterprise#838)

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::a2a_internal_scope``. Adds ``agent_ownership.a2a_scope``
(``public`` | ``internal``, default ``public``) and
``agent_ownership.a2a_keyless_internal`` (default 1), plus
``schedule_executions.source_host``, the address a keyless trusted-network
A2A caller's run is attributed to.

Revision ID: 0096_a2a_internal_scope
Revises: 0095_portal_messages_execution_id
"""
from alembic import op


revision = "0096_a2a_internal_scope"
down_revision = "0095_portal_messages_execution_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS a2a_scope TEXT DEFAULT 'public'")
    op.execute("ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS a2a_keyless_internal INTEGER DEFAULT 1")
    op.execute("ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS source_host TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS source_host")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS a2a_keyless_internal")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS a2a_scope")

"""Add public_channel_system_prompt column to agent_ownership (#1205)

Persists the per-agent public/channel custom-instructions fragment on the
PostgreSQL backend. Mirrors the SQLite ``agent_ownership_public_channel_prompt``
migration in ``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in
``db/tables.py``.

Fresh PG builds already get the column because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
column up on ``alembic upgrade head``.

Revision ID: 0012_agent_ownership_public_channel_prompt
Revises: 0011_agent_ownership_tts_voice
Create Date: 2026-06-25
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_agent_ownership_public_channel_prompt"
down_revision = "0011_agent_ownership_tts_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS public_channel_system_prompt TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership DROP COLUMN IF EXISTS public_channel_system_prompt"
    )

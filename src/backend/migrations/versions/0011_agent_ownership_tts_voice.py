"""agent_ownership TTS voice-out columns (epic #24 / #25)

Adds the shared agent-level outbound-voice config on the PostgreSQL backend:
``tts_voice_replies_enabled`` (INTEGER, default 0) and ``tts_voice_id`` (TEXT).
Mirrors the SQLite ``agent_ownership_tts_voice`` migration in ``db/migrations.py``
and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the columns via ``0001_baseline`` (which iterates
``db/schema.py:TABLES``). This revision lets an existing PG deployment pick them
up on ``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op
when the baseline already created them.

Revision ID: 0011_agent_ownership_tts_voice
Revises: 0010_agent_ownership_mcp_exposed
Create Date: 2026-07-01
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_agent_ownership_tts_voice"
down_revision = "0010_agent_ownership_mcp_exposed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS "
        "tts_voice_replies_enabled INTEGER DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS tts_voice_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS tts_voice_id")
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS tts_voice_replies_enabled")

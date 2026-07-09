"""agent_ownership per-channel voice-allowed flags (trinity-enterprise#117)

Adds ``tts_voice_telegram_enabled`` / ``tts_voice_slack_enabled`` /
``tts_voice_whatsapp_enabled`` (INTEGER, default 1) on the PostgreSQL backend.
Voice enablement + voice selection stay agent-level (``tts_voice_replies_enabled``
/ ``tts_voice_id``); these three flags let an owner allow/deny voice per channel.
DEFAULT 1 preserves the capability on every channel for already-enabled agents.
Mirrors the SQLite ``agent_ownership_tts_channel_flags`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the columns via ``0001_baseline``; ``ADD COLUMN IF
NOT EXISTS`` keeps this a no-op there.

Revision ID: 0015_agent_ownership_tts_channel_flags
Revises: 0014_agent_schedules_webhook_auth
Create Date: 2026-07-09
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_agent_ownership_tts_channel_flags"
down_revision = "0014_agent_schedules_webhook_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for channel in ("telegram", "slack", "whatsapp"):
        op.execute(
            f"ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS "
            f"tts_voice_{channel}_enabled INTEGER DEFAULT 1"
        )


def downgrade() -> None:
    for channel in ("telegram", "slack", "whatsapp"):
        op.execute(
            f"ALTER TABLE agent_ownership DROP COLUMN IF EXISTS "
            f"tts_voice_{channel}_enabled"
        )

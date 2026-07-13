"""schedule_executions channel-destination columns (trinity-enterprise#117)

Adds ``source_channel`` / ``source_channel_chat_id`` / ``source_channel_thread``
(all nullable TEXT) on the PostgreSQL backend. Populated by the channel message
router so the ``send_voice_reply`` MCP tool (#117) can reconstruct the exact
delivery destination from an ``execution_id`` alone. NULL for non-channel
executions. Mirrors the SQLite ``schedule_executions_source_channel`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the columns via ``0001_baseline``; ``ADD COLUMN IF
NOT EXISTS`` keeps this a no-op there.

Revision ID: 0016_schedule_executions_source_channel
Revises: 0016_agent_ownership_tts_channel_flags
Create Date: 2026-07-09
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018_schedule_executions_source_channel"
down_revision = "0017_agent_ownership_tts_channel_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("source_channel", "source_channel_chat_id", "source_channel_thread"):
        op.execute(
            f"ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS {col} TEXT"
        )


def downgrade() -> None:
    for col in ("source_channel", "source_channel_chat_id", "source_channel_thread"):
        op.execute(
            f"ALTER TABLE schedule_executions DROP COLUMN IF EXISTS {col}"
        )

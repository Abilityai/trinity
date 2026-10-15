"""#3722 — authoritative persistent agent configuration transport identity.

NULL leaves existing agents on unchanged legacy recovery. New persistent agents
record volume-v1 with their ownership row, so a lost configuration volume fails
closed instead of silently resetting owner instructions. No legacy file adoption.

Mirrors SQLite agent_config_storage_format.
Revision ID: 0099_agent_config_storage_format
Revises: 0098_proactive_consent_not_asked
"""
from alembic import op

revision = "0099_agent_config_storage_format"
down_revision = "0098_proactive_consent_not_asked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS config_storage_format TEXT")


def downgrade() -> None:
    # Dropping a live marker would make a later upgrade classify volume agents
    # as legacy and silently skip their required config during recovery.
    op.execute("LOCK TABLE agent_ownership IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_ownership WHERE config_storage_format IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while agents require persistent configuration volumes';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS config_storage_format")

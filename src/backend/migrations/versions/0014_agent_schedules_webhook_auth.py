"""Add webhook signature-auth columns to agent_schedules (trinity-enterprise#77)

Optional HMAC-SHA256 signature auth on the public schedule webhook. Mirrors the
SQLite ``agent_schedules_webhook_auth`` migration in ``db/migrations.py`` and the
DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

- ``webhook_secret_encrypted`` — AES-256-GCM envelope of the signing secret
  (Invariant #12); never stored in plaintext.
- ``webhook_auth_enabled`` — gates verification in the public trigger; default 0
  so an existing token-in-URL webhook is unchanged.

Fresh PG builds already get the columns because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
columns up on ``alembic upgrade head``.

Revision ID: 0014_agent_schedules_webhook_auth
Revises: 0013_public_chat_messages_sender
Create Date: 2026-07-06
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_agent_schedules_webhook_auth"
down_revision = "0013_public_chat_messages_sender"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_schedules ADD COLUMN IF NOT EXISTS webhook_secret_encrypted TEXT"
    )
    op.execute(
        "ALTER TABLE agent_schedules ADD COLUMN IF NOT EXISTS webhook_auth_enabled INTEGER DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_schedules DROP COLUMN IF EXISTS webhook_auth_enabled"
    )
    op.execute(
        "ALTER TABLE agent_schedules DROP COLUMN IF EXISTS webhook_secret_encrypted"
    )

"""Add voice_name column to agent_ownership (#28)

Persists the per-agent Gemini Live voice on the PostgreSQL backend. Mirrors the
SQLite ``agent_ownership_voice_name`` migration in ``db/migrations.py`` and the
DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the column because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
column up on ``alembic upgrade head``.

Revision ID: 0004_agent_ownership_voice_name
Revises: 0003_agent_compatibility_results
Create Date: 2026-06-23
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_agent_ownership_voice_name"
down_revision = "0003_agent_compatibility_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS voice_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_ownership DROP COLUMN IF EXISTS voice_name")

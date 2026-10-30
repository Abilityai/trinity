"""skill_sources follows the bundled pin (trinity-enterprise#529).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::skill_sources_follow_bundled_pin``.

Adds ``follows_bundled_pin INTEGER NOT NULL DEFAULT 0`` (set by the seed,
cleared by an admin change to url/ref/ref_type), ``followed_from_ref TEXT`` and
``followed_at TEXT`` (the latest follow, shown in Settings → Library), then runs
the shared ``SKILL_SOURCE_FOLLOW_BACKFILL_SQL`` so rows seeded before the marker
existed are classified by the same rule on both tracks. ``IF NOT EXISTS``
because a fresh database is built by 0001_baseline from the current DDL, which
already carries the columns.

Revision ID: 0107_skill_sources_follow_bundled_pin
Revises: 0106_execution_chat_session_id
"""
from alembic import op
from sqlalchemy import text


revision = "0107_skill_sources_follow_bundled_pin"
down_revision = "0106_execution_chat_session_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import SKILL_SOURCE_FOLLOW_BACKFILL_SQL

    op.execute(
        "ALTER TABLE skill_sources ADD COLUMN IF NOT EXISTS "
        "follows_bundled_pin INTEGER NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE skill_sources ADD COLUMN IF NOT EXISTS followed_from_ref TEXT")
    op.execute("ALTER TABLE skill_sources ADD COLUMN IF NOT EXISTS followed_at TEXT")
    op.execute(text(SKILL_SOURCE_FOLLOW_BACKFILL_SQL))


def downgrade() -> None:
    for column in ("followed_at", "followed_from_ref", "follows_bundled_pin"):
        op.execute(f"ALTER TABLE skill_sources DROP COLUMN IF EXISTS {column}")

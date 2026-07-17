"""users.github_pat_encrypted — per-user GitHub PAT (ent#162)

Adds ``github_pat_encrypted`` (nullable TEXT) on the PostgreSQL backend. NULL
means "no personal GitHub credential" — true for every existing user, so no
backfill is needed. A user configures one PAT in their own settings; agent
creation resolves per-agent → owner's per-user → global, so a non-admin is no
longer confined to the admin PAT's repo scope. Stored as an AES-256-GCM envelope
(Invariant #12), the same shape as ``agent_git_config.github_pat_encrypted``.

Mirrors the SQLite ``users_github_pat`` migration in ``db/migrations.py`` and the
DDL in ``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get the column via ``0001_baseline``; ``ADD COLUMN IF NOT
EXISTS`` keeps this a no-op there.

Revision ID: 0025_users_github_pat
Revises: 0024_agent_ownership_volume_base_name
Create Date: 2026-07-17
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0025_users_github_pat"
down_revision = "0024_agent_ownership_volume_base_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_pat_encrypted TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS github_pat_encrypted")

"""Widen alembic_version.version_num to VARCHAR(255) (#1420)

**P0 fix.** Alembic auto-creates ``alembic_version.version_num`` as
``VARCHAR(32)`` (its historical default). Trinity's descriptive
``NNNN_<table>_<change>`` revision slugs exceed 32 chars — e.g.
``0009_agent_ownership_public_channel_model`` is 41 — so the version-stamp
``UPDATE`` truncation-fails on **PostgreSQL** and the backend never boots
(``StringDataRightTruncation``), taking every ``depends_on: service_healthy``
service down with it. SQLite doesn't enforce ``VARCHAR`` length, so the same
migration succeeds there — which is why SQLite-only CI never caught it.

This revision is slotted **before** ``0009`` (which is rechained onto it) so the
column is already wide when 0009's long id is stamped. Its own id is ≤32 chars,
so its stamp fits the still-32 column on an existing DB. On PostgreSQL the ALTER
is a no-op when the column is already 255 (a fresh build whose table ``env.py``
created at 255, or a re-run); on SQLite / other dialects it is skipped entirely
(no ``alembic_version`` width problem to fix).

Revision ID: 0008a_widen_alembic_version
Revises: 0008_agent_loops_max_cost
Create Date: 2026-07-02
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008a_widen_alembic_version"
down_revision = "0008_agent_loops_max_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # 255 matches env.py's version_table_column_type for fresh auto-creates.
        op.execute(
            "ALTER TABLE alembic_version "
            "ALTER COLUMN version_num TYPE VARCHAR(255)"
        )


def downgrade() -> None:
    # Deliberately NOT narrowing back to VARCHAR(32): a wider column is harmless,
    # and narrowing would truncation-fail the moment a >32-char id is stamped —
    # reintroducing the very bug this migration fixes.
    pass

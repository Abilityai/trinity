"""public_user_memory_writes.section (trinity-enterprise#962).

The per-user memory's write history records which half of the memory blob a
row changed: ``notes`` (the agent's ``write_user_memory``; every row before this
revision) or ``summary`` (the background summarizer, recorded from #962 on so
the owner can undo it). NOT NULL with a server default, so existing rows read
as ``notes`` — which is what every one of them is.

Mirrors the SQLite ``public_user_memory_writes_section`` migration. Chained
after 0119 so the version-line keeps ONE head (#2068).

Revision ID: 0120_public_user_memory_writes_section
Revises: 0119_terms_acceptances
"""
import sqlalchemy as sa
from alembic import op


revision = "0120_public_user_memory_writes_section"
down_revision = "0119_terms_acceptances"
branch_labels = None
depends_on = None


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns("public_user_memory_writes")}


def upgrade() -> None:
    # Guarded: 0001_baseline builds every table from the live db/schema.py DDL,
    # so on a FRESH PostgreSQL database the column already exists.
    bind = op.get_bind()
    if "section" not in _columns(bind):
        op.add_column(
            "public_user_memory_writes",
            sa.Column("section", sa.Text(), nullable=False, server_default="notes"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "section" in _columns(bind):
        op.drop_column("public_user_memory_writes", "section")

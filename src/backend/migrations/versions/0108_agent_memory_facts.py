"""trinity-enterprise#1022 (MEM-002) — retrieval memory facts.

One row per extracted fact, scoped by ``(agent_name, user_email)`` where ``''``
is the agent scope shared with the agent's owner. ``embedding`` is the fact's
float32 little-endian vector; ``text_hash`` is the dedupe key, and the partial
unique index on it (live rows only) turns a two-worker duplicate into an
IntegrityError the writer catches. Forget is a soft delete that records who.

Mirrors the SQLite ``agent_memory_facts`` migration.

Revision ID: 0108_agent_memory_facts
Revises: 0107_skill_sources_follow_bundled_pin
"""
import sqlalchemy as sa
from alembic import op


revision = "0108_agent_memory_facts"
down_revision = "0107_skill_sources_follow_bundled_pin"
branch_labels = None
depends_on = None

_TABLE = "agent_memory_facts"


def _has_index(bind, table: str, name: str) -> bool:
    return name in {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    # Guarded: 0001_baseline builds every table from the live db/tables.py
    # metadata, so on a FRESH PostgreSQL database this table (and its indexes)
    # already exist here, and an unguarded create aborts the whole upgrade — and
    # therefore boot — with DuplicateTable. Same idiom as 0042 for a column.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("user_email", sa.Text(), nullable=False, server_default=""),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("text_hash", sa.Text(), nullable=False),
            sa.Column("embedding", sa.LargeBinary(), nullable=False),
            sa.Column("embedding_model", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("execution_id", sa.Text()),
            sa.Column("recall_count", sa.Integer(), server_default="0"),
            sa.Column("last_recalled_at", sa.Text()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("deleted_at", sa.Text()),
            sa.Column("deleted_by", sa.Text()),
        )
    if not _has_index(bind, _TABLE, "idx_agent_memory_facts_scope"):
        op.create_index(
            "idx_agent_memory_facts_scope", _TABLE,
            ["agent_name", "user_email", "deleted_at"],
        )
    if not _has_index(bind, _TABLE, "idx_agent_memory_facts_live_hash"):
        op.create_index(
            "idx_agent_memory_facts_live_hash", _TABLE,
            ["agent_name", "user_email", "text_hash"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
            sqlite_where=sa.text("deleted_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)

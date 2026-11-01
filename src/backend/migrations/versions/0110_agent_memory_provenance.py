"""trinity-enterprise#1022 — retrieval-memory provenance: the fact journal and
the last writer.

``agent_memory_fact_events`` is one append-only row per thing that happened to
a fact or to a candidate that never became one (add / update / delete / none /
screened / prune / recall / forget), keyed to the execution that did it, with
the candidate the extractor proposed, the text it replaced and the neighbours
the reconciler was shown. ``agent_memory_facts.updated_by_execution_id`` is the
last run that rewrote or retired the row. Together they answer "where did this
fact come from and why does it read like this", and they are the corpus the
extraction prompt is tuned against.

Mirrors the SQLite ``agent_memory_provenance`` migration. Chained after 0109 so
the version-line keeps ONE head (#2068).

Revision ID: 0110_agent_memory_provenance
Revises: 0109_agent_ownership_memory_opt_out
"""
import sqlalchemy as sa
from alembic import op


revision = "0110_agent_memory_provenance"
down_revision = "0109_agent_ownership_memory_opt_out"
branch_labels = None
depends_on = None

_TABLE = "agent_memory_fact_events"


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_index(bind, table: str, name: str) -> bool:
    return name in {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    # Guarded like 0108/0109: 0001_baseline builds every table and column from
    # the live db/tables.py metadata, so on a FRESH PostgreSQL database both
    # already exist here and an unguarded create would abort boot.
    bind = op.get_bind()
    if not _has_column(bind, "agent_memory_facts", "updated_by_execution_id"):
        op.add_column("agent_memory_facts", sa.Column("updated_by_execution_id", sa.Text(), nullable=True))
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("user_email", sa.Text(), nullable=False, server_default=""),
            sa.Column("fact_id", sa.Text()),
            sa.Column("op", sa.Text(), nullable=False),
            sa.Column("execution_id", sa.Text()),
            sa.Column("candidate_text", sa.Text()),
            sa.Column("previous_text", sa.Text()),
            sa.Column("neighbour_ids", sa.Text()),
            sa.Column("score", sa.Float()),
            sa.Column("actor", sa.Text()),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
    if not _has_index(bind, _TABLE, "idx_agent_memory_fact_events_fact"):
        op.create_index("idx_agent_memory_fact_events_fact", _TABLE, ["agent_name", "fact_id", "created_at"])
    if not _has_index(bind, _TABLE, "idx_agent_memory_fact_events_execution"):
        op.create_index("idx_agent_memory_fact_events_execution", _TABLE, ["agent_name", "execution_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)
    if _has_column(bind, "agent_memory_facts", "updated_by_execution_id"):
        op.drop_column("agent_memory_facts", "updated_by_execution_id")

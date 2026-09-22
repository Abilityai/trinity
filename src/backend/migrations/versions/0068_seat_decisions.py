"""seat_decisions — the seat-level decision record (trinity-enterprise#638, R25).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::seat_decisions_table``.

Why a thing was approved, deferred or killed — the alternatives that were live,
the criterion that discriminated, who decided (role and person), ``review_by``
and what would reverse it — owned by the seat (``agent_name`` × ``seat_email``,
the ent#637 memory scope). Correction supersedes rather than edits; expiry is
computed from ``review_by`` on read. ``alternatives`` / ``cites`` are JSON
documents in TEXT (no JSON type on either engine — the tables.py convention).

``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py``.

Revision ID: 0068_seat_decisions
Revises: 0067_agent_role_readiness
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_seat_decisions"
down_revision = "0067_agent_role_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions, so guard on
    # existence rather than create blindly (0046-0058's rule). The indexes
    # are `IF NOT EXISTS` for the same reason (the 0066 shape).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("seat_decisions"):
        op.create_table(
            "seat_decisions",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("seat_email", sa.Text(), nullable=False),
            sa.Column("outcome", sa.Text(), nullable=False),
            sa.Column("decided", sa.Text(), nullable=False),
            sa.Column("alternatives", sa.Text(), nullable=False),
            sa.Column("criterion", sa.Text(), nullable=False),
            sa.Column("reversal", sa.Text(), nullable=False),
            sa.Column("decided_by_role", sa.Text(), nullable=True),
            sa.Column("decided_by_person", sa.Text(), nullable=False),
            sa.Column("decided_at", sa.Text(), nullable=False),
            sa.Column("review_by", sa.Text(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("ask_class", sa.Text(), nullable=True),
            sa.Column("scope", sa.Text(), nullable=False, server_default='seat'),
            sa.Column("status", sa.Text(), nullable=False, server_default='active'),
            sa.Column("supersedes_id", sa.Text(), nullable=True),
            sa.Column("cites", sa.Text(), nullable=False, server_default='[]'),
            sa.Column("request_id", sa.Text(), nullable=True),
            sa.Column("close_reason", sa.Text(), nullable=True),
            sa.Column("closed_at", sa.Text(), nullable=True),
            sa.Column("closed_by", sa.Text(), nullable=True),
            sa.Column("reconfirmed_at", sa.Text(), nullable=True),
            sa.Column("source_execution_id", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_seat_decisions_seat ON seat_decisions(agent_name, seat_email, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_seat_decisions_review ON seat_decisions(agent_name, review_by)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS seat_decisions")

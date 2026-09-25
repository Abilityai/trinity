"""seat_ask_class_state — the autonomy dial's earned half (trinity-enterprise#641, P12).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::seat_ask_class_state_table``.

One row per (agent, seat, ask class) recording what was EARNED: the state, the
evidence it rests on, and ``evidence_expires_at`` (the EARLIEST ``review_by`` of
the window). The instance LEVEL is deliberately not a table — it is one
validated ``system_settings`` key — and the live conjuncts (level, the agent's
autonomy switch, the clock) are ANDed at read time, never materialised here.

``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py``.

Revision ID: 0073_seat_ask_class_state
Revises: 0072_agent_capability_grants
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_seat_ask_class_state"
down_revision = "0072_agent_capability_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions, so guard on
    # existence rather than create blindly (0046-0058's rule).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("seat_ask_class_state"):
        op.create_table(
            "seat_ask_class_state",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("seat_email", sa.Text(), nullable=False),
            sa.Column("ask_class", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'on_request'")),
            sa.Column("blocked_by", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("evidence", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("evidence_hash", sa.Text(), nullable=True),
            sa.Column("evidence_expires_at", sa.Text(), nullable=True),
            sa.Column("guard_metric", sa.Text(), nullable=False, server_default=sa.text("'not_assessed'")),
            sa.Column("held", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("held_by", sa.Text(), nullable=True),
            sa.Column("held_at", sa.Text(), nullable=True),
            sa.Column("promoted_at", sa.Text(), nullable=True),
            sa.Column("demoted_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.UniqueConstraint("agent_name", "seat_email", "ask_class",
                                name="uq_seat_ask_class_state_triple"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_seat_ask_class_state_seat ON seat_ask_class_state(agent_name, seat_email)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS seat_ask_class_state")

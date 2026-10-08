"""agent_skill_gates — the per-agent skill gate map (trinity-enterprise#753).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::agent_skill_gates``.

One row per (agent, skill) that needs approval before it runs: the approver
kind (``primary`` | ``approver``), an optional deadline, and where the row came
from (``set`` | ``library_default`` | ``cleared``, the last a tombstone that
gates nothing). ``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py``.
Additive: no row = ungated, so no existing agent changes on upgrade.

Revision ID: 0094_agent_skill_gates
Revises: 0093_platform_alert_responded_heal
"""
from alembic import op
import sqlalchemy as sa

revision = "0094_agent_skill_gates"
down_revision = "0093_platform_alert_responded_heal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares the table — before the revisions run, so guard on existence
    # (the 0072 shape).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("agent_skill_gates"):
        op.create_table(
            "agent_skill_gates",
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("skill_name", sa.Text(), nullable=False),
            sa.Column("approver", sa.Text(), nullable=False),
            sa.Column("deadline_hours", sa.Integer(), nullable=True),
            sa.Column("origin", sa.Text(), nullable=False),
            sa.Column("set_by", sa.Text(), nullable=False),
            sa.Column("set_by_agent", sa.Text(), nullable=True),
            sa.Column("set_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("agent_name", "skill_name"),
        )


def downgrade() -> None:
    # Dropping the table ungates every skill on every agent — the honest
    # inverse of an additive upgrade.
    op.drop_table("agent_skill_gates")

"""agent_skill_sets + agent_skills.individual — skill sets.

PostgreSQL half of the dual-track pair (trinity-enterprise#530); the SQLite half
is ``db/migrations.py::agent_skill_sets``.

* ``agent_skill_sets`` — a named set of library skills assigned to an agent.
* ``agent_skills.individual`` — 1 (the default, so every existing row keeps its
  meaning) for a skill assigned on its own; 0 when present only because an
  assigned set names it.

Additive only. ``agent_skill_sets.agent_name`` is a CASCADE entry in
``db/agent_cleanup.py``.

Revision ID: 0075_agent_skill_sets
Revises: 0074_role_readiness_rollout_seed
"""
from alembic import op
import sqlalchemy as sa

revision = "0075_agent_skill_sets"
down_revision = "0074_role_readiness_rollout_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("agent_skill_sets"):
        op.create_table(
            "agent_skill_sets",
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("set_name", sa.Text(), nullable=False),
            sa.Column("source_id", sa.Text(), nullable=True),
            sa.Column("assigned_by", sa.Text(), nullable=False),
            sa.Column("assigned_by_agent", sa.Text(), nullable=True),
            sa.Column("assigned_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("agent_name", "set_name"),
        )
    cols = {c["name"] for c in inspector.get_columns("agent_skills")}
    if "individual" not in cols:
        op.add_column(
            "agent_skills",
            sa.Column("individual", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )


def downgrade() -> None:
    op.drop_column("agent_skills", "individual")
    op.execute("DROP TABLE IF EXISTS agent_skill_sets")

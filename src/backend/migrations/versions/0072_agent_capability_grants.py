"""agent_capability_grants + agent_skills.assigned_by_agent (trinity-enterprise#596).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::agent_capability_grants``.

Only designated agents may change an agent's skills — its own included. An
instance admin grants the ``skills.manage`` capability to named agents (the
fleet orchestrators); every other agent key is refused. The grant is a row
rather than a column on ``agent_ownership`` so that who granted it and when is
answerable, and later capabilities (ent#590, ent#341) share the seam.
``agent_capability_grants.agent_name`` is a CASCADE entry in
``db/agent_cleanup.py``.

``agent_skills.assigned_by_agent`` records the AGENT that made an assignment
(NULL for a human). Additive and nullable: no existing row changes meaning.

Revision ID: 0072_agent_capability_grants
Revises: 0071_seat_decisions
"""
from alembic import op
import sqlalchemy as sa

revision = "0072_agent_capability_grants"
down_revision = "0071_seat_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares both — and only THEN runs the revisions, so guard on existence
    # rather than create blindly (the 0046-0058 rule; the 0066 index shape).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("agent_capability_grants"):
        op.create_table(
            "agent_capability_grants",
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("capability", sa.Text(), nullable=False),
            sa.Column("granted_by", sa.Text(), nullable=False),
            sa.Column("granted_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("agent_name", "capability"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_capability_grants_cap "
        "ON agent_capability_grants(capability)"
    )
    columns = {c["name"] for c in inspector.get_columns("agent_skills")}
    if "assigned_by_agent" not in columns:
        op.add_column("agent_skills", sa.Column("assigned_by_agent", sa.Text(), nullable=True))


def downgrade() -> None:
    # Dropping the column loses attribution; dropping the table revokes every
    # grant. Both are the honest inverse of an additive upgrade.
    op.drop_column("agent_skills", "assigned_by_agent")
    op.execute("DROP INDEX IF EXISTS idx_agent_capability_grants_cap")
    op.drop_table("agent_capability_grants")

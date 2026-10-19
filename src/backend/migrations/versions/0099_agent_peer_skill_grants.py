"""agent_peer_skill_grants (abilityai/trinity-enterprise#662).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::agent_peer_skill_grants``.

A peer may run what an agent declares; what an admin assigned needs a grant.
One row says caller X may run library-assigned skill Y on target Z over their
``agent_permissions`` edge. Additive: nobody holds a grant on upgrade. Both
agent columns are CASCADE entries in ``db/agent_cleanup.py``.

Revision ID: 0099_agent_peer_skill_grants
Revises: 0098_proactive_consent_not_asked
"""
from alembic import op
import sqlalchemy as sa

revision = "0099_agent_peer_skill_grants"
down_revision = "0098_proactive_consent_not_asked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares the table — and only THEN runs the revisions, so guard on
    # existence rather than create blindly (the 0072 shape).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("agent_peer_skill_grants"):
        op.create_table(
            "agent_peer_skill_grants",
            sa.Column("caller_agent", sa.Text(), nullable=False),
            sa.Column("target_agent", sa.Text(), nullable=False),
            sa.Column("skill_name", sa.Text(), nullable=False),
            sa.Column("granted_by", sa.Text(), nullable=False),
            sa.Column("granted_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("caller_agent", "target_agent", "skill_name"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_peer_skill_grants_target "
        "ON agent_peer_skill_grants(target_agent)"
    )


def downgrade() -> None:
    # Dropping the table revokes every grant: the honest inverse of an additive
    # upgrade (peers fall back to the declared-skills default).
    op.execute("DROP INDEX IF EXISTS idx_agent_peer_skill_grants_target")
    op.drop_table("agent_peer_skill_grants")

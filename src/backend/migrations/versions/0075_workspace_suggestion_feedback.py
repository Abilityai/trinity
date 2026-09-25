"""workspace_suggestion_feedback — accept/dismiss of a Workspace suggestion.

PostgreSQL half of the dual-track pair (trinity-enterprise#465); the SQLite half
is ``db/migrations.py::workspace_suggestion_feedback_table``.

One row per viewer + agent + suggestion key. A dismissal holds while the
suggestion's state fingerprint is unchanged; an accept is counted for
usefulness. ``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py``.

Revision ID: 0073_workspace_suggestion_feedback
Revises: 0072_agent_capability_grants
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_workspace_suggestion_feedback"
down_revision = "0072_agent_capability_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions, so guard on
    # existence rather than create blindly (0046-0058's rule).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("workspace_suggestion_feedback"):
        op.create_table(
            "workspace_suggestion_feedback",
            sa.Column("client_email", sa.Text(), nullable=False),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("suggestion_key", sa.Text(), nullable=False),
            sa.Column("surface", sa.Text(), nullable=False, server_default=sa.text("'agent'")),
            sa.Column("source", sa.Text(), nullable=True),
            sa.Column("dismissed_at", sa.Text(), nullable=True),
            sa.Column("dismissed_fingerprint", sa.Text(), nullable=True),
            sa.Column("accepted_at", sa.Text(), nullable=True),
            sa.Column("accept_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("client_email", "agent_name", "suggestion_key"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_workspace_suggestion_feedback_agent "
        "ON workspace_suggestion_feedback(agent_name)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_workspace_suggestion_feedback_agent")
    op.execute("DROP TABLE IF EXISTS workspace_suggestion_feedback")

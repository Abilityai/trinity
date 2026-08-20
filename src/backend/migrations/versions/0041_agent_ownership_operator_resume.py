"""agent_ownership.operator_resume_enabled — owner opt-in for respond→resume (ent#329)

An operator's answer reaches the agent's queue file in ~5s but is only *processed*
at the agent's next turn. An agent with no schedule has no next turn, so an
approved action silently never runs. With this flag on, a CAS-won respond
dispatches one execution carrying the item and the answer.

Per-AGENT and default OFF on purpose: a dispatch spends money, so it is never
unconditional, and a per-request flag the agent itself sets would let any agent
turn any answer — including a Workspace client's — into spend (ent#430 AC #3).

Revision ID: 0041_agent_ownership_operator_resume
Revises: 0040_rl_events_failure_kind
Create Date: 2026-08-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0041_agent_ownership_operator_resume"
down_revision = "0040_rl_events_failure_kind"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # Guarded: 0001_baseline builds agent_ownership from the live db/tables.py
    # metadata, which declares this column — so on a FRESH PostgreSQL database it
    # already exists here and an unguarded add_column aborts the whole upgrade
    # (and therefore boot) with DuplicateColumn. The SQLite track hides that,
    # since _safe_add_column guards for us there. Same idiom as 0031.
    bind = op.get_bind()
    if not _has_column(bind, "agent_ownership", "operator_resume_enabled"):
        op.add_column(
            "agent_ownership",
            sa.Column(
                "operator_resume_enabled",
                sa.Integer(),
                nullable=True,
                server_default="0",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "agent_ownership", "operator_resume_enabled"):
        op.drop_column("agent_ownership", "operator_resume_enabled")

"""guardrail_events — the guardrail event store (trinity-enterprise#165, GUARD-005).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::guardrail_events_table``.

One row per event an agent's guardrail hooks reported (a refusal, a redaction,
a hook error), pushed under the agent's own key. ``UNIQUE(agent_name,
event_id)`` makes a resend idempotent. ``execution_id`` is attribution only and
carries no foreign key: execution rows have their own retention. The three
indexes serve the per-agent read, the per-execution counts and the retention
sweep, which keys on the server receive time. Additive: nothing existing is
touched. Chained after 0123_agent_ownership_default_model so the version-line
keeps ONE head (#2068).

Revision ID: 0124_guardrail_events
Revises: 0123_agent_ownership_default_model
"""
import sqlalchemy as sa
from alembic import op


revision = "0124_guardrail_events"
down_revision = "0123_agent_ownership_default_model"
branch_labels = None
depends_on = None

_TABLE = "guardrail_events"


def upgrade() -> None:
    # Guarded: a FRESH PostgreSQL database is built from the live schema before
    # the revisions run, so the table can already exist here.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("event_id", sa.Text(), nullable=False),
            sa.Column("occurred_at", sa.Text(), nullable=False),
            sa.Column("received_at", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("hook", sa.Text(), nullable=False),
            sa.Column("tool", sa.Text()),
            sa.Column("code", sa.Text()),
            sa.Column("execution_id", sa.Text()),
            sa.Column("details", sa.Text()),
            sa.UniqueConstraint("agent_name", "event_id"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_guardrail_events_agent_occurred "
        "ON guardrail_events(agent_name, occurred_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_guardrail_events_execution "
        "ON guardrail_events(execution_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_guardrail_events_received "
        "ON guardrail_events(received_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS guardrail_events")

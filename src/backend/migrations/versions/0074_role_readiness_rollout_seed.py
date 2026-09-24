"""role_readiness_rollout_seed — the readiness gate's rollout, data only.

PostgreSQL half of the dual-track pair (trinity-enterprise#689); the SQLite half
is ``db/migrations.py::role_readiness_rollout_seed``.

From this release a companion's cron seat brief runs only when its owner stamp
says ``ready``. Every agent whose proactive brief fires today (an enabled, live,
seat-delivery schedule on a live agent with autonomy on) is stamped ``ready`` at the value in
force (#2085), so no install changes behaviour. ``ON CONFLICT DO NOTHING``: an
existing stamp is never overwritten. No DDL.

Revision ID: 0074_role_readiness_rollout_seed
Revises: 0072_agent_capability_grants
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0074_role_readiness_rollout_seed"
down_revision = "0073_operator_queue_sync_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO agent_role_readiness (agent_name, status, changed_at, changed_by)
            SELECT DISTINCT s.agent_name, 'ready', :now, 'rollout:ent#689'
            FROM agent_schedules s
            JOIN agent_ownership o ON o.agent_name = s.agent_name
            WHERE s.enabled = 1
              AND s.deleted_at IS NULL
              AND s.deliver_to_workspace_email IS NOT NULL
              AND s.deliver_to_workspace_email != ''
              AND o.deleted_at IS NULL
              AND o.autonomy_enabled = 1
            ON CONFLICT (agent_name) DO NOTHING
            """
        ),
        {"now": now},
    )


def downgrade() -> None:
    # Only the rollout's own rows; an owner's later flip carries their email
    # and is left alone.
    op.execute("DELETE FROM agent_role_readiness WHERE changed_by = 'rollout:ent#689'")

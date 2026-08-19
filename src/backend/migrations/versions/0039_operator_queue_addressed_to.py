"""operator_queue.addressed_to_email — address an ask to a human (ent#364)

`agent_name` says which AGENT an item belongs to; nothing said which PERSON should
answer it. Workspace asks need that.

It is a column and not a key inside `context` on purpose: `context` is
agent-authored free-form JSON whose hygiene clamp bounds size and type only, so
putting the addressee there would let an agent decide who may answer an ask and
whose sidebar it appears in. The value is validated at the ingestion boundary
(`services/operator_queue_service.py`) against the agent's roster.

Nullable, no default: every existing row keeps meaning what it meant — an ask for
the operator.

Revision ID: 0039_operator_queue_addressed_to
Revises: 0038_portal_chat_state
Create Date: 2026-08-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0039_operator_queue_addressed_to"
down_revision = "0038_portal_chat_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operator_queue", sa.Column("addressed_to_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("operator_queue", "addressed_to_email")

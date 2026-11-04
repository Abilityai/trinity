"""admin_email_not_username — clear the provisioned admin's username-as-email (#3554).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::_migrate_admin_email_not_username``.

The PostgreSQL admin seed wrote ``email = username``, so a hosted install's
admin read as ``email = "admin"`` and the first-run "Sign-in email" step never
appeared. Data only: where ``email = username`` and it is not an address (no
``@``), set it NULL. Idempotent.

Revision ID: 0100_admin_email_not_username
Revises: 0099_agent_peer_skill_grants
"""
from alembic import op

revision = "0100_admin_email_not_username"
down_revision = "0099_agent_peer_skill_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE users SET email = NULL WHERE email = username AND email NOT LIKE '%@%'"
    )


def downgrade() -> None:
    # Deliberate no-op: the honest inverse would write the username back into
    # the email column, which is the defect this migration removes.
    pass

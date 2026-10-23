"""a2a_caller_identity (abilityai/trinity-enterprise#862).

PostgreSQL half of the dual-track pair; SQLite half is db/migrations.py::a2a_caller_identity.

Revision ID: 0102_a2a_caller_identity
Revises: 0101_held_gate_turns
"""
revision = "0102_a2a_caller_identity"
down_revision = "0101_held_gate_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    op.execute(
        "ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS a2a_caller_identity TEXT"
    )


def downgrade() -> None:
    from alembic import op
    op.execute(
        "ALTER TABLE schedule_executions DROP COLUMN IF EXISTS a2a_caller_identity"
    )

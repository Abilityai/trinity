"""portal_file_dismissals — a Workspace viewer removes a shared file from their list.

PostgreSQL half of the dual-track pair (#2582 / ent#548); the SQLite half is
``db/migrations.py::portal_file_dismissals_table``.

``agent_shared_files`` carries no audience column, so ``portal_documents`` lists
every active share of an agent to every rostered client. "Remove it from MY
list" is therefore a per-viewer preference with nowhere to live: the one generic
per-user store (``user_ui_preferences``) is FK'd to ``users.id``, and a
Workspace client has no user row.

``agent_name`` is load-bearing rather than decoration — ``agent_shared_files``
is a CASCADE entry in ``db/agent_cleanup.py``, so deleting an agent hard-deletes
its share rows without going through the revoke sweeper, and every dismissal
keyed on those ids would be orphaned forever.

Revision ID: 0058_portal_file_dismissals
Revises: 0057_portal_messages_voice_source
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_portal_file_dismissals"
down_revision = "0057_portal_messages_voice_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions (0046-0057's rule),
    # so guard on existence rather than create blindly.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("portal_file_dismissals"):
        op.create_table(
            "portal_file_dismissals",
            sa.Column("client_email", sa.Text(), nullable=False),
            sa.Column("file_id", sa.Text(), nullable=False),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("dismissed_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("client_email", "file_id"),
        )
    # The PK's leading column serves `dismissed_file_ids(email)`; this one is
    # for the sweeper, which purges by the share id.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_file_dismissals_file "
        "ON portal_file_dismissals(file_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_portal_file_dismissals_file")
    op.execute("DROP TABLE IF EXISTS portal_file_dismissals")

"""user_ui_preferences — per-user UI state (trinity-enterprise#413, OSS-core).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::user_ui_preferences_table``. One row per (user_id, key),
value an opaque JSON object, ``updated_at`` per key because it is the
compare-and-set base of the PUT contract. The Dashboard Grid's layout, tile
prefs and org toggles are the first keys; anything per-user-UI that used to
live in browser-global localStorage is a new allowlisted key, not a new table.

Revision ID: 0053_user_ui_preferences
Revises: 0052_portal_session_title_source
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_user_ui_preferences"
down_revision = "0052_portal_session_title_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions (0046-0052's
    # rule), so guard on existence rather than create blindly.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("user_ui_preferences"):
        return
    op.create_table(
        "user_ui_preferences",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "key"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("user_ui_preferences")

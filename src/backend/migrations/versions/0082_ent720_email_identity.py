"""ent720_email_identity — sign-in email unique; codes carry a purpose.

PostgreSQL half of the dual-track pair (trinity-enterprise#720); the SQLite half
is ``db/migrations.py::ent720_email_identity``. Both resolve duplicates with the
same rule; ``_resolve_duplicate_emails`` below is a frozen copy of
``db.migrations.resolve_duplicate_emails`` (a revision never imports app code),
pinned equal by ``tests/unit/test_ent720_email_binding.py``.

1. ``email_login_codes.purpose`` — NULL is a sign-in code; ``email_bind:<id>``
   is the mailbox proof a bind now requires.
2. Duplicate ``users.email`` values are resolved (blank → NULL; per lower-cased
   address the earliest-created account keeps it, the rest → NULL, logged by
   username only), then ``idx_users_email_unique`` is created.

Revision ID: 0082_ent720_email_identity
Revises: 0081_portal_messages_unread_idx
"""
from alembic import op
import sqlalchemy as sa

revision = "0082_ent720_email_identity"
down_revision = "0081_portal_messages_unread_idx"
branch_labels = None
depends_on = None


def _resolve_duplicate_emails(rows):
    """[(id, username, email, created_at)] → [(id, username)] that must LOSE
    their email: per lower-cased address, all but the earliest-created account
    (ties broken by the lower id). Shared by both migration tracks (ent#720)."""
    groups = {}
    for user_id, username, email, created_at in rows:
        groups.setdefault((email or "").strip().lower(), []).append(
            (created_at or "", user_id, username))
    losers = []
    for key, members in groups.items():
        if not key or len(members) < 2:
            continue
        members.sort()
        losers += [(uid, uname) for _, uid, uname in members[1:]]
    return losers


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("email_login_codes") and "purpose" not in {
            c["name"] for c in insp.get_columns("email_login_codes")}:
        op.add_column("email_login_codes", sa.Column("purpose", sa.Text(), nullable=True))

    if not insp.has_table("users"):
        return
    bind.execute(sa.text("UPDATE users SET email = NULL WHERE email IS NOT NULL AND TRIM(email) = ''"))
    rows = bind.execute(sa.text(
        "SELECT id, username, email, created_at FROM users WHERE email IS NOT NULL")).fetchall()
    for user_id, username in _resolve_duplicate_emails([tuple(r) for r in rows]):
        bind.execute(sa.text("UPDATE users SET email = NULL WHERE id = :id"), {"id": user_id})
        print(f"[ent#720] duplicate sign-in email: cleared on account '{username}' "
              "(an earlier account holds it)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
        "ON users(lower(email)) WHERE email IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_users_email_unique")
    # The cleared duplicates are not restored — the honest inverse would be
    # handing an identity back to an account that never proved it.
    with op.batch_alter_table("email_login_codes") as batch:
        batch.drop_column("purpose")

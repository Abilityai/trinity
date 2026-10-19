"""#3554 — the provisioned admin has no sign-in email, on either DB track.

The PostgreSQL admin seed (`UserOperations.update_user_password`'s create
branch) inserted `email=username`, so a hosted install's admin read as
`email = "admin"`. The first-run overlay's "Sign-in email" step is pending
while the admin has no email, and `"admin"` is truthy, so the step never
appeared. The SQLite seed already left `email` NULL; both tracks now agree, and
a data migration (SQLite entry + Alembic 0100) clears the value on installs
that already carry it.
"""
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _email(username):
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT email FROM users WHERE username = :u"), {"u": username}).scalar()


def test_a_created_admin_has_no_email():
    from db.users import UserOperations
    name = f"admin-3554-{uuid.uuid4().hex[:6]}"
    assert UserOperations().update_user_password(name, "$2b$12$hash")
    assert _email(name) is None


def test_the_migration_clears_username_as_email_and_nothing_else():
    from db.engine import get_engine
    from db.migrations import _migrate_admin_email_not_username
    from sqlalchemy import text
    tag = uuid.uuid4().hex[:6]
    seeded, real, same_at = f"admin-{tag}", f"person-{tag}", f"odd-{tag}@x.test"
    with get_engine().begin() as conn:
        for u, e in ((seeded, seeded), (real, f"{real}@example.com"), (same_at, same_at)):
            conn.execute(text("INSERT INTO users (username, email, role, created_at, updated_at) "
                              "VALUES (:u, :e, 'user', 'x', 'x')"), {"u": u, "e": e})
    for _ in range(2):                                   # idempotent
        raw = get_engine().raw_connection()
        try:
            _migrate_admin_email_not_username(raw.cursor(), raw)
        finally:
            raw.close()
    assert _email(seeded) is None
    assert _email(real) == f"{real}@example.com"
    assert _email(same_at) == same_at                    # has an "@": a real address, kept


def test_the_migration_is_registered_on_both_tracks():
    from db import migrations
    assert "admin_email_not_username" in [n for n, _ in migrations.MIGRATIONS]
    rev = (Path(migrations.__file__).resolve().parents[1] / "migrations" / "versions"
           / "0100_admin_email_not_username.py").read_text()
    assert 'down_revision = "0099_agent_peer_skill_grants"' in rev
    assert "email = username" in rev

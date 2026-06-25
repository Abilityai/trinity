"""#82 Phase 1 — admin sign-in by registered email + password.

`authenticate_user` resolves the identifier by username, and (when it looks like
an email and no username matches) falls back to an email lookup. The password
check still runs, so only an account that actually has a password hash — the
admin — can authenticate this way; email-code-only users (no password) can't.
"""
import pytest

pytestmark = pytest.mark.unit

import dependencies
from dependencies import hash_password


class FakeDB:
    def __init__(self, by_username=None, by_email=None):
        self._by_username = by_username or {}
        self._by_email = by_email or {}

    def get_user_by_username(self, username):
        return self._by_username.get(username)

    def get_user_by_email(self, email):
        return self._by_email.get(email)


PW = "Sup3rSecret!!"


def test_admin_logs_in_with_username(monkeypatch):
    admin = {"username": "admin", "email": "admin", "password": hash_password(PW)}
    monkeypatch.setattr(dependencies, "db", FakeDB(by_username={"admin": admin}))
    assert dependencies.authenticate_user("admin", PW)["username"] == "admin"
    assert dependencies.authenticate_user("admin", "wrong") is False


def test_admin_logs_in_with_registered_email(monkeypatch):
    admin = {"username": "admin", "email": "me@acme.com", "password": hash_password(PW)}
    db = FakeDB(by_username={"admin": admin}, by_email={"me@acme.com": admin})
    monkeypatch.setattr(dependencies, "db", db)

    res = dependencies.authenticate_user("me@acme.com", PW)
    assert res and res["username"] == "admin"
    assert dependencies.authenticate_user("me@acme.com", "wrong") is False


def test_email_identifier_is_normalized(monkeypatch):
    admin = {"username": "admin", "email": "me@acme.com", "password": hash_password(PW)}
    db = FakeDB(by_username={"admin": admin}, by_email={"me@acme.com": admin})
    monkeypatch.setattr(dependencies, "db", db)
    # Mixed-case / padded identifier still resolves.
    res = dependencies.authenticate_user("  Me@Acme.com  ", PW)
    assert res and res["username"] == "admin"


def test_passwordless_email_user_cannot_password_login(monkeypatch):
    # Regular email-code user — no password hash. Must never password-authenticate.
    user = {"username": "u@x.com", "email": "u@x.com", "password": None}
    db = FakeDB(by_username={"u@x.com": user}, by_email={"u@x.com": user})
    monkeypatch.setattr(dependencies, "db", db)
    assert dependencies.authenticate_user("u@x.com", "anything") is False


def test_unknown_identifier(monkeypatch):
    monkeypatch.setattr(dependencies, "db", FakeDB())
    assert dependencies.authenticate_user("nobody@x.com", "pw") is False

"""trinity-enterprise#837 — a deliberate whitelist grant replaces a row sharing wrote.

Before #837, sharing an agent and approving an access request each wrote a
login-whitelist row at role `user`, and `get_or_create_email_user` gives a NEW
account the role on its row. #837 stopped those writes, and made `user`
Workspace-only, so such a row now decides something: an admin who later adds
the same email as `operator` got 409 "already whitelisted", and the person's
first sign-in landed at `user`, refused on every operator route.

Pinned here, on a real database (both backends via `db_harness`):
  * a grant from the admin form (or any non-sharing source) replaces a row
    whose source is `agent_sharing` or `access_request`, and the first sign-in
    takes the granted role;
  * any other existing row is left alone (the 409 stays);
  * the route tells the admin when an account already exists, because a
    whitelist role applies only at a first sign-in.
"""
from __future__ import annotations

import sys

import pytest

from db_harness import db_backend  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.unit


@pytest.fixture
def ops(db_backend, monkeypatch):  # noqa: F811
    for mod in ("db.connection", "db.users", "db.email_auth", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    from db.email_auth import EmailAuthOperations
    from db.users import UserOperations

    users = UserOperations()
    users.insert_email_user("admin@example.com", "admin")
    return users, EmailAuthOperations(users)


@pytest.mark.parametrize("legacy_source", ["agent_sharing", "access_request"])
def test_a_grant_replaces_a_row_sharing_wrote(ops, legacy_source):
    users, auth = ops
    assert auth.add_to_whitelist("bob@example.com", "admin@example.com",
                                 source=legacy_source, default_role="user")

    assert auth.add_to_whitelist("bob@example.com", "admin@example.com",
                                 source="manual", default_role="operator") is True

    assert auth.get_whitelist_default_role("bob@example.com") == "operator"
    assert auth.get_or_create_email_user("bob@example.com")["role"] == "operator"


def test_any_other_existing_row_is_left_alone(ops):
    _users, auth = ops
    assert auth.add_to_whitelist("carol@example.com", "admin@example.com",
                                 source="manual", default_role="user")

    assert auth.add_to_whitelist("carol@example.com", "admin@example.com",
                                 source="manual", default_role="admin") is False
    assert auth.get_whitelist_default_role("carol@example.com") == "user"


def test_a_sharing_source_never_replaces_a_row(ops):
    """Only a deliberate grant replaces; a stray sharing-sourced write must not
    downgrade a row an admin wrote."""
    _users, auth = ops
    assert auth.add_to_whitelist("dana@example.com", "admin@example.com",
                                 source="manual", default_role="operator")

    assert auth.add_to_whitelist("dana@example.com", "admin@example.com",
                                 source="agent_sharing", default_role="user") is False
    assert auth.get_whitelist_default_role("dana@example.com") == "operator"


# ---------------------------------------------------------------------------
# The route: a whitelist role applies at a first sign-in only
# ---------------------------------------------------------------------------

def _whitelist_client(monkeypatch, *, added=True, account=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.settings.whitelist as wl
    from models import User

    monkeypatch.setattr(wl.db, "add_to_whitelist", lambda *_a, **_k: added)
    monkeypatch.setattr(wl.db, "get_user_by_email", lambda *_a, **_k: account)
    monkeypatch.setattr(wl.db, "get_whitelist_default_role", lambda *_a, **_k: "operator")
    app = FastAPI()
    app.include_router(wl.router)
    # Keyed on the router's own reference: a sibling test may have re-imported
    # `dependencies`, and an override on another copy of the function is inert.
    app.dependency_overrides[wl.get_current_user] = lambda: User(
        id=1, username="admin", email="admin@example.com", role="admin")
    return TestClient(app)


def _post(client, role="operator"):
    paths = [r.path for r in client.app.routes if r.path.endswith("/email-whitelist")]
    return client.post(paths[0], json={"email": "bob@example.com", "source": "manual",
                                       "default_role": role})


def test_the_route_names_an_existing_accounts_role(monkeypatch):
    client = _whitelist_client(monkeypatch, account={"role": "user", "username": "bob"})
    r = _post(client)
    assert r.status_code == 200
    assert r.json()["existing_account_role"] == "user"


def test_the_route_stays_quiet_for_a_new_person(monkeypatch):
    client = _whitelist_client(monkeypatch, account=None)
    r = _post(client)
    assert r.status_code == 200
    assert "existing_account_role" not in r.json()


def test_the_409_says_how_to_change_the_role(monkeypatch):
    client = _whitelist_client(monkeypatch, added=False)
    r = _post(client)
    assert r.status_code == 409
    assert "User Management" in r.json()["detail"]

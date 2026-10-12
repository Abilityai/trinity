"""trinity-enterprise#837 PR 2 — a platform role change is audited, and only a
person makes one.

The Access tab now carries a role select over `PUT /api/users/{username}/role`
(AC5), so the route that changes what a person may do on EVERY agent needs the
two properties a grant has:

* **an audit row** — `role_change`, from → to, on a change; none on a refusal or
  a no-op (a repeated PUT is not a second change);
* **an interactive person** — `require_admin` admits `trinity-system`'s key and an
  admin's user-scoped key (both are in `ADMIN_GATE_SCOPES`), so either could
  promote anyone to admin over REST, though the router's own `# mcp:` header says
  human-only. `reject_non_interactive_principal` closes it (the approval gate,
  2026-10-12).

Real sqlite from the platform's `init_schema`, the real users router with
`get_current_user` overridden on the router's own reference (so the REAL admin
and interactive gates run), the audit captured where the route resolves it.
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture()
def users_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-837-role.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from database import db
    return db


def _make_user(db, role):
    # Unique names per test: the engine may outlive one test's database file.
    from db_models import UserCreate
    name = f"u837-{uuid.uuid4().hex[:8]}"
    return db.create_user(UserCreate(username=name, password=None, role=role, email=f"{name}@example.com"))


@pytest.fixture()
def api(users_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import users as users_router

    audit = AsyncMock()
    monkeypatch.setattr(users_router.platform_audit_service, "log", audit)
    state = {"principal": None}
    app = FastAPI()
    app.include_router(users_router.router)
    app.dependency_overrides[users_router.get_current_user] = lambda: state["principal"]
    User = users_router.User

    def as_(row, **over):
        state["principal"] = User(id=row["id"], username=row["username"], email=row.get("email"),
                                  role=row.get("role", "admin"), **over)
        return TestClient(app)

    return {"as": as_, "audit": audit, "db": users_db}


def _put(client, username, role):
    return client.put(f"/api/users/{username}/role", json={"role": role})


def _role_changes(audit):
    return [c.kwargs for c in audit.await_args_list if c.kwargs.get("event_action") == "role_change"]


class TestTheChangeIsAudited:
    def test_a_change_records_from_and_to(self, api):
        admin = _make_user(api["db"], "admin")
        member = _make_user(api["db"], "operator")
        r = _put(api["as"](admin), member["username"], "user")
        assert r.status_code == 200, r.text
        assert api["db"].get_user_by_username(member["username"])["role"] == "user"
        [kw] = _role_changes(api["audit"])
        assert kw["event_type"].value == "authorization"
        assert (kw["target_type"], kw["target_id"]) == ("user", member["username"])
        assert kw["details"] == {"from": "operator", "to": "user"}
        assert kw["actor_user"].username == admin["username"]
        assert kw["endpoint"] == f"/api/users/{member['username']}/role"

    def test_a_raise_is_recorded_the_same_way(self, api):
        admin = _make_user(api["db"], "admin")
        member = _make_user(api["db"], "user")
        assert _put(api["as"](admin), member["username"], "admin").status_code == 200
        [kw] = _role_changes(api["audit"])
        assert kw["details"] == {"from": "user", "to": "admin"}

    def test_a_no_op_is_not_a_change(self, api):
        admin = _make_user(api["db"], "admin")
        member = _make_user(api["db"], "operator")
        assert _put(api["as"](admin), member["username"], "operator").status_code == 200
        assert _role_changes(api["audit"]) == []


class TestRefusalsWriteNothing:
    def test_your_own_role(self, api):
        admin = _make_user(api["db"], "admin")
        assert _put(api["as"](admin), admin["username"], "user").status_code == 400
        assert api["db"].get_user_by_username(admin["username"])["role"] == "admin"
        assert _role_changes(api["audit"]) == []

    def test_an_invalid_role(self, api):
        admin = _make_user(api["db"], "admin")
        member = _make_user(api["db"], "operator")
        assert _put(api["as"](admin), member["username"], "owner").status_code == 400
        assert api["db"].get_user_by_username(member["username"])["role"] == "operator"
        assert _role_changes(api["audit"]) == []

    def test_an_unknown_account(self, api):
        admin = _make_user(api["db"], "admin")
        assert _put(api["as"](admin), f"nobody-{uuid.uuid4().hex[:6]}", "user").status_code == 404
        assert _role_changes(api["audit"]) == []

    def test_a_non_admin(self, api):
        creator = _make_user(api["db"], "creator")
        member = _make_user(api["db"], "operator")
        assert _put(api["as"](creator), member["username"], "admin").status_code == 403
        assert api["db"].get_user_by_username(member["username"])["role"] == "operator"
        assert _role_changes(api["audit"]) == []


class TestOnlyAPersonChangesARole:
    @pytest.mark.parametrize("over", [
        pytest.param(dict(mcp_scope="system"), id="trinity-system-key"),
        pytest.param(dict(mcp_scope="user"), id="admin-user-scoped-key"),
        pytest.param(dict(mcp_scope="agent", agent_name="some-agent"), id="agent-key"),
    ])
    def test_a_key_cannot_mint_an_admin(self, api, over):
        admin = _make_user(api["db"], "admin")
        member = _make_user(api["db"], "user")
        r = _put(api["as"](admin, **over), member["username"], "admin")
        assert r.status_code == 403, r.text
        assert api["db"].get_user_by_username(member["username"])["role"] == "user"
        assert _role_changes(api["audit"]) == []

    def test_an_interactive_admin_can(self, api):
        """The positive control for the negatives above."""
        admin = _make_user(api["db"], "admin")
        member = _make_user(api["db"], "user")
        assert _put(api["as"](admin, mcp_scope=None), member["username"], "operator").status_code == 200
        assert api["db"].get_user_by_username(member["username"])["role"] == "operator"


class TestTheWhitelistRowFollowsTheRole:
    """The security pass (2026-10-12): a whitelist row's role is what an account
    created for the address gets — at a first sign-in, and AGAIN if the account
    later re-binds to another address. A role change that left the row at the old
    role let a demoted person re-bind away and sign in with the old address as a
    NEW account at the old role. The row is now capped at every role the route
    sets — lowered, never raised (review on #3634: raised, a promotion would hand
    the old address a second account at the new role after a re-bind, one a later
    demotion of the first account would not reach)."""

    def _row(self, db, email, role, by):
        assert db.add_to_whitelist(email, by["username"], "manual", default_role=role)

    def test_a_demotion_cannot_be_undone_by_re_binding(self, api):
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "admin")
        self._row(db, member["email"], "admin", admin)
        assert _put(api["as"](admin), member["username"], "operator").status_code == 200
        assert db.get_whitelist_default_role(member["email"]) == "operator"

        old = member["email"]
        db.update_user(member["username"], {"email": f"moved-{uuid.uuid4().hex[:8]}@example.com"})
        fresh = db.get_or_create_email_user(old)
        assert fresh["id"] != member["id"]
        assert fresh["role"] == "operator"

    def test_an_account_with_no_row_gets_none(self, api):
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "operator")
        assert _put(api["as"](admin), member["username"], "user").status_code == 200
        assert db.get_whitelist_default_role(member["email"]) is None

    def test_a_refusal_leaves_the_row_alone(self, api):
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "admin")
        self._row(db, member["email"], "admin", admin)
        assert _put(api["as"](admin, mcp_scope="user"), member["username"], "user").status_code == 403
        assert _put(api["as"](admin), member["username"], "owner").status_code == 400
        assert db.get_whitelist_default_role(member["email"]) == "admin"

    def test_a_same_role_put_heals_a_row_left_stale_before(self, api):
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "operator")
        self._row(db, member["email"], "admin", admin)          # demoted before this fix
        assert _put(api["as"](admin), member["username"], "operator").status_code == 200
        assert db.get_whitelist_default_role(member["email"]) == "operator"
        assert _role_changes(api["audit"]) == []                # a no-op is still not a change

    def test_a_promotion_never_raises_the_row(self, api):
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "user")
        self._row(db, member["email"], "user", admin)
        assert _put(api["as"](admin), member["username"], "admin").status_code == 200
        assert db.get_whitelist_default_role(member["email"]) == "user"

        old = member["email"]
        db.update_user(member["username"], {"email": f"moved-{uuid.uuid4().hex[:8]}@example.com"})
        fresh = db.get_or_create_email_user(old)
        assert fresh["id"] != member["id"]
        assert fresh["role"] == "user"          # not a second admin account

    def test_a_row_already_below_the_new_role_stays(self, api):
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "operator")
        self._row(db, member["email"], "user", admin)
        assert _put(api["as"](admin), member["username"], "creator").status_code == 200
        assert db.get_whitelist_default_role(member["email"]) == "user"

    def test_a_failed_whitelist_write_leaves_the_role_unchanged(self, api, monkeypatch):
        """The row is capped BEFORE the role is written, so the only partial state
        a failure can leave is a row already lowered — the safe direction — never
        a 500 after the role has changed (review on #3634)."""
        db = api["db"]
        admin = _make_user(db, "admin")
        member = _make_user(db, "admin")
        self._row(db, member["email"], "admin", admin)

        def boom(*_a, **_k):
            raise RuntimeError("whitelist write failed")

        monkeypatch.setattr(db, "cap_whitelist_default_role", boom)
        with pytest.raises(RuntimeError):
            _put(api["as"](admin), member["username"], "operator")
        assert db.get_user_by_username(member["username"])["role"] == "admin"
        assert _role_changes(api["audit"]) == []

    def test_the_whitelist_role_order_is_the_role_hierarchy(self):
        """The whitelist module keeps its own copy of the ladder (it cannot import
        `dependencies`); this pins the copy to the original."""
        import db.email_auth as email_auth
        from dependencies import ROLE_HIERARCHY
        assert email_auth._ROLE_ORDER == tuple(ROLE_HIERARCHY)

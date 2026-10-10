"""trinity-enterprise#709 — the admin changes their own password from the UI.

Before this, the admin password was set once at first-run setup and the only
way to rotate it was to edit `ADMIN_PASSWORD` in `.env` and restart. These tests
drive the real `/api/users` routes over a real sqlite built by the platform's
own `init_schema`, with the login rate-limit counters and the session-cutoff
store backed by an in-memory fake Redis, and the audit log captured.

  * the gate: admin AND an interactive (JWT) session — no MCP key of any scope
  * the current password is re-verified, rate-limited like login
  * a second factor only when the `mfa_gate` provider says so (stubbed here)
  * the first-run password rules, named per failing rule
  * success: login works with the new password and not the old one, other
    sessions are cut off, the caller gets a fresh token, an audit row is written
  * the boot path no longer reverts a UI-set password to the `.env` value
"""
from __future__ import annotations

import sqlite3
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

OLD = "Old-Passw0rd!-709"
NEW = "New-Passw0rd!-709"


class _FakeRedis:
    """Enough Redis for the login counters and the session cutoff."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def exists(self, key):
        return 1 if key in self.store else 0

    def ttl(self, key):
        return 600 if key in self.store else -2

    def delete(self, key):
        self.store.pop(key, None)

    def pipeline(self):
        store = self.store

        class _Pipe:
            def incr(self, key):
                store[key] = str(int(store.get(key, 0)) + 1)

            def expire(self, key, seconds):
                pass

            def execute(self):
                pass

        return _Pipe()


@pytest.fixture()
def users_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent709.db"
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


def _real_routers_auth(monkeypatch):
    """The real `routers.auth`, re-imported if a sibling suite parked a stub."""
    import importlib
    mod = sys.modules.get("routers.auth")
    if not hasattr(mod, "check_login_rate_limit"):
        monkeypatch.delitem(sys.modules, "routers.auth", raising=False)
        if not getattr(sys.modules.get("routers"), "__file__", None):
            monkeypatch.delitem(sys.modules, "routers", raising=False)
        mod = importlib.import_module("routers.auth")
    monkeypatch.setattr(sys.modules["routers"], "auth", mod, raising=False)
    return mod


@pytest.fixture()
def api(users_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import users as users_router
    from services import mfa_gate

    get_current_user = users_router.get_current_user
    User = users_router.User
    deps = users_router.revoke_user_sessions.__globals__

    # The engine is cached across tests, so every test gets its own admin name;
    # ADMIN_USERNAME makes that name "the provisioned admin" for the marker.
    admin = f"admin-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("ADMIN_USERNAME", admin)
    users_db.update_user_password(admin, deps["hash_password"](OLD))
    row = users_db.get_user_by_username(admin)

    audit = AsyncMock()
    monkeypatch.setattr(users_router.platform_audit_service, "log", audit)

    redis = _FakeRedis()
    auth_module = _real_routers_auth(monkeypatch)
    monkeypatch.setattr(auth_module, "get_redis_client", lambda: redis)
    monkeypatch.setitem(deps, "get_breaker_redis", lambda: redis)

    mfa_gate.clear_provider()

    app = FastAPI()
    app.include_router(users_router.router)
    state = {"principal": None}
    app.dependency_overrides[get_current_user] = lambda: state["principal"]

    def as_(role="admin", who=None, **over):
        who = who or row
        state["principal"] = User(id=who["id"], username=who["username"], email=who.get("email"), role=role, **over)
        return TestClient(app)

    yield {"as": as_, "admin": admin, "audit": audit, "redis": redis, "db": users_db,
           "deps": deps, "auth": auth_module}
    mfa_gate.clear_provider()


def _body(current=OLD, new=NEW, confirm=None, **extra):
    return {"current_password": current, "new_password": new,
            "confirm_password": new if confirm is None else confirm, **extra}


def _actions(audit):
    return [c.kwargs.get("event_action") for c in audit.await_args_list]


def _code(r):
    return r.json()["detail"]["code"]


# =============================================================================
# The gate — admin, interactive, own account only
# =============================================================================

class TestGate:
    def test_an_agent_scoped_key_is_refused(self, api):
        r = api["as"](mcp_scope="agent", agent_name="some-agent").put("/api/users/me/password", json=_body())
        assert r.status_code == 403

    def test_a_user_scoped_mcp_key_of_an_admin_is_refused(self, api):
        """A grant: the owner's own MCP key still cannot rotate the password."""
        r = api["as"](mcp_scope="user").put("/api/users/me/password", json=_body())
        assert r.status_code == 403

    def test_an_event_loopback_token_is_refused(self, api):
        r = api["as"](vouched_source_agent="a", is_event_loopback=True).put("/api/users/me/password", json=_body())
        assert r.status_code == 403

    def test_a_non_admin_session_is_refused(self, api):
        r = api["as"](role="creator").put("/api/users/me/password", json=_body())
        assert r.status_code == 403

    def test_the_verify_step_has_the_same_gate(self, api):
        r = api["as"](mcp_scope="agent", agent_name="x").post(
            "/api/users/me/password/verify", json={"current_password": OLD})
        assert r.status_code == 403

    def test_no_target_username_is_accepted(self, api):
        r = api["as"]().put("/api/users/me/password", json=_body(username="someone-else"))
        assert r.status_code == 422

    def test_an_account_with_no_password_gets_a_named_refusal(self, api):
        from db_models import UserCreate
        other = api["db"].create_user(UserCreate(username=f"e-{uuid.uuid4().hex[:6]}", password=None, role="admin"))
        r = api["as"](who=other).put("/api/users/me/password", json=_body())
        assert r.status_code == 409 and _code(r) == "no_password"


# =============================================================================
# Step 1 — the current password, rate-limited like login
# =============================================================================

class TestCurrentPassword:
    def test_verify_accepts_the_right_password_and_says_no_second_factor(self, api):
        r = api["as"]().post("/api/users/me/password/verify", json={"current_password": OLD})
        assert r.status_code == 200
        assert r.json()["second_factor_required"] is False

    def test_a_wrong_current_password_is_named_and_counted(self, api):
        r = api["as"]().post("/api/users/me/password/verify", json={"current_password": "nope"})
        assert r.status_code == 400 and _code(r) == "current_password_incorrect"
        assert api["redis"].get(f"login_attempts_acct:{api['admin']}") == "1"
        assert "password_change_failed" in _actions(api["audit"])

    def test_a_wrong_current_password_on_change_leaves_the_hash_alone(self, api):
        before = api["db"].get_user_by_username(api["admin"])["password"]
        r = api["as"]().put("/api/users/me/password", json=_body(current="nope"))
        assert r.status_code == 400 and _code(r) == "current_password_incorrect"
        assert api["db"].get_user_by_username(api["admin"])["password"] == before

    def test_never_a_401_which_the_ui_reads_as_session_ended(self, api):
        r = api["as"]().put("/api/users/me/password", json=_body(current="nope"))
        assert r.status_code != 401

    def test_after_five_wrong_guesses_even_the_right_password_is_locked_out(self, api):
        c = api["as"]()
        for _ in range(5):
            c.post("/api/users/me/password/verify", json={"current_password": "nope"})
        r = c.put("/api/users/me/password", json=_body())
        assert r.status_code == 429 and _code(r) == "too_many_attempts"
        assert api["deps"]["verify_password"](OLD, api["db"].get_user_by_username(api["admin"])["password"])


# =============================================================================
# Step 3 — the new password
# =============================================================================

class TestNewPassword:
    def test_a_weak_password_names_the_failing_rule(self, api):
        r = api["as"]().put("/api/users/me/password", json=_body(new="short"))
        assert r.status_code == 400 and _code(r) == "password_too_weak"
        errors = r.json()["detail"]["errors"]
        assert any("12 characters" in e for e in errors)

    def test_a_mismatch_is_refused(self, api):
        r = api["as"]().put("/api/users/me/password", json=_body(confirm=NEW + "x"))
        assert r.status_code == 400 and _code(r) == "password_mismatch"

    def test_the_same_password_is_refused(self, api):
        r = api["as"]().put("/api/users/me/password", json=_body(new=OLD))
        assert r.status_code == 400 and _code(r) == "password_unchanged"


# =============================================================================
# Success
# =============================================================================

class TestSuccess:
    def test_login_works_with_the_new_password_and_not_the_old(self, api):
        r = api["as"]().put("/api/users/me/password", json=_body())
        assert r.status_code == 200, r.text
        authenticate_user = api["deps"]["authenticate_user"]
        assert authenticate_user(api["admin"], NEW)
        assert not authenticate_user(api["admin"], OLD)

    def test_the_change_is_audited_without_the_password(self, api):
        api["as"]().put("/api/users/me/password", json=_body())
        calls = [c for c in api["audit"].await_args_list if c.kwargs.get("event_action") == "password_changed"]
        assert len(calls) == 1
        kw = calls[0].kwargs
        assert kw["event_type"].value == "authentication"
        assert NEW not in repr(kw) and OLD not in repr(kw)

    def test_the_ui_marker_is_written_for_the_provisioned_admin(self, api):
        api["as"]().put("/api/users/me/password", json=_body())
        assert api["db"].get_setting_value("admin_password_source") == "ui"

    def test_other_sessions_are_cut_off_and_the_caller_gets_a_fresh_token(self, api):
        from jose import jwt
        from config import SECRET_KEY, ALGORITHM
        deps = api["deps"]
        old_token = deps["create_access_token"]({"sub": api["admin"]})
        old_payload = jwt.decode(old_token, SECRET_KEY, algorithms=[ALGORITHM])
        time.sleep(1.01)  # iat is in whole seconds

        r = api["as"]().put("/api/users/me/password", json=_body())
        body = r.json()
        assert body["other_sessions_signed_out"] is True
        new_payload = jwt.decode(body["access_token"], SECRET_KEY, algorithms=[ALGORITHM])
        assert new_payload["sub"] == api["admin"]

        assert deps["is_user_session_revoked"](api["admin"], old_payload) is True
        assert deps["is_user_session_revoked"](api["admin"], new_payload) is False

    def test_a_token_minted_after_the_change_is_not_cut_off(self, api):
        from jose import jwt
        from config import SECRET_KEY, ALGORITHM
        deps = api["deps"]
        api["as"]().put("/api/users/me/password", json=_body())
        time.sleep(1.01)
        later = jwt.decode(deps["create_access_token"]({"sub": api["admin"]}), SECRET_KEY, algorithms=[ALGORITHM])
        assert deps["is_user_session_revoked"](api["admin"], later) is False

    def test_a_token_without_iat_counts_as_before_the_cutoff(self, api):
        deps = api["deps"]
        api["as"]().put("/api/users/me/password", json=_body())
        assert deps["is_user_session_revoked"](api["admin"], {"sub": api["admin"], "jti": "x"}) is True

    def test_another_users_sessions_are_untouched(self, api):
        deps = api["deps"]
        api["as"]().put("/api/users/me/password", json=_body())
        assert deps["is_user_session_revoked"]("someone-else", {"sub": "someone-else", "iat": 1}) is False

    def test_get_current_user_rejects_a_cut_off_session(self, api):
        import asyncio
        from fastapi import HTTPException
        from starlette.requests import Request
        deps = api["deps"]
        old_token = deps["create_access_token"]({"sub": api["admin"]})
        time.sleep(1.01)
        api["as"]().put("/api/users/me/password", json=_body())
        request = Request({"type": "http", "method": "GET", "path": "/api/agents", "headers": []})
        with pytest.raises(HTTPException) as e:
            asyncio.run(deps["get_current_user"](request, old_token))
        assert e.value.status_code == 401


# =============================================================================
# Step 2 — the second factor, only when the gate says so
# =============================================================================

class _Provider:
    def __init__(self, enrolled=True, required=False, code="123456", can_verify=True):
        self.enrolled, self.required, self.code = enrolled, required, code
        if not can_verify:
            self.verify_code = None

    def gate_decision(self, user):
        return {"enrolled": self.enrolled, "required": self.required}

    def verify_code(self, user, code):
        return code == self.code


class TestSecondFactor:
    def test_verify_reports_the_step_when_enrolled(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider())
        r = api["as"]().post("/api/users/me/password/verify", json={"current_password": OLD})
        assert r.status_code == 200 and r.json()["second_factor_required"] is True

    def test_a_missing_code_blocks_the_change(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider())
        r = api["as"]().put("/api/users/me/password", json=_body())
        assert r.status_code == 400 and _code(r) == "second_factor_required"
        assert api["deps"]["authenticate_user"](api["admin"], OLD)

    def test_a_wrong_code_blocks_the_change_and_is_counted(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider())
        r = api["as"]().put("/api/users/me/password", json=_body(second_factor_code="000000"))
        assert r.status_code == 400 and _code(r) == "second_factor_invalid"
        assert api["redis"].get(f"login_attempts_acct:{api['admin']}") == "1"
        assert api["deps"]["authenticate_user"](api["admin"], OLD)

    def test_the_right_code_completes_the_change(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider())
        r = api["as"]().put("/api/users/me/password", json=_body(second_factor_code="123456"))
        assert r.status_code == 200, r.text
        assert api["deps"]["authenticate_user"](api["admin"], NEW)

    def test_required_but_not_enrolled_is_refused(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider(enrolled=False, required=True))
        r = api["as"]().put("/api/users/me/password", json=_body(second_factor_code="123456"))
        assert r.status_code == 409 and _code(r) == "second_factor_enrollment_required"

    def test_a_provider_that_cannot_verify_a_code_fails_closed(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider(can_verify=False))
        r = api["as"]().put("/api/users/me/password", json=_body(second_factor_code="123456"))
        assert r.status_code == 503 and _code(r) == "second_factor_unavailable"
        assert api["deps"]["authenticate_user"](api["admin"], OLD)

    def test_not_enrolled_and_not_required_skips_the_step(self, api):
        from services import mfa_gate
        mfa_gate.register_provider(_Provider(enrolled=False, required=False))
        r = api["as"]().put("/api/users/me/password", json=_body())
        assert r.status_code == 200


# =============================================================================
# Boot — `.env` seeds, it does not revert a UI-set password
# =============================================================================

def _boot_db(tmp_path):
    from db.schema import init_schema
    conn = sqlite3.connect(str(tmp_path / "boot.db"))
    cur = conn.cursor()
    init_schema(cur, conn)
    conn.commit()
    return conn, cur


def _hash_of(cur, username):
    cur.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    return cur.fetchone()[0]


class TestBootReconciliation:
    def test_env_no_longer_clobbers_a_ui_set_password(self, tmp_path, monkeypatch):
        import database
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        conn, cur = _boot_db(tmp_path)
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", OLD)
        database._ensure_admin_user(cur, conn)
        assert ctx.verify(OLD, _hash_of(cur, "admin"))

        # What the endpoint writes: a new hash and the marker.
        cur.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (ctx.hash(NEW),))
        cur.execute("INSERT INTO system_settings (key, value, updated_at) VALUES ('admin_password_source', 'ui', 'now')")
        conn.commit()

        database._ensure_admin_user(cur, conn)  # reboot, old ADMIN_PASSWORD still in .env
        assert ctx.verify(NEW, _hash_of(cur, "admin"))

    def test_without_the_marker_env_still_resyncs(self, tmp_path, monkeypatch):
        """The pre-#709 behaviour is unchanged for installs that never used the dialog."""
        import database
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        conn, cur = _boot_db(tmp_path)
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", OLD)
        database._ensure_admin_user(cur, conn)
        monkeypatch.setenv("ADMIN_PASSWORD", NEW)
        database._ensure_admin_user(cur, conn)
        assert ctx.verify(NEW, _hash_of(cur, "admin"))

    def test_the_marker_does_not_keep_an_unusable_hash(self, tmp_path, monkeypatch):
        """Env still seeds a missing/unusable hash even with the marker set."""
        import database
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        conn, cur = _boot_db(tmp_path)
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", OLD)
        cur.execute("INSERT INTO users (username, password_hash, role, created_at, updated_at) "
                    "VALUES ('admin', '', 'admin', 'now', 'now')")
        cur.execute("INSERT INTO system_settings (key, value, updated_at) VALUES ('admin_password_source', 'ui', 'now')")
        conn.commit()
        database._ensure_admin_user(cur, conn)
        assert ctx.verify(OLD, _hash_of(cur, "admin"))

    def test_the_engine_path_applies_the_same_rule(self, monkeypatch):
        import database
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        stored = {"admin": ctx.hash(NEW)}
        settings = {"admin_password_source": "ui"}
        writes = []

        class _Users:
            def get_user_by_username(self, u):
                return {"username": u, "password": stored[u]} if u in stored else None

            def update_user_password(self, u, h):
                writes.append(u)
                stored[u] = h

        class _Settings:
            def get_setting_value(self, k, default=None):
                return settings.get(k, default)

        monkeypatch.setattr(database, "UserOperations", _Users)
        monkeypatch.setattr(database, "SettingsOperations", _Settings)
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", OLD)
        database._ensure_admin_user_engine()
        assert writes == [] and ctx.verify(NEW, stored["admin"])

        settings.clear()
        database._ensure_admin_user_engine()
        assert writes == ["admin"] and ctx.verify(OLD, stored["admin"])


# =============================================================================
# The census classifies both routes as interactive
# =============================================================================

def test_both_routes_are_interactive_in_the_census():
    # Loaded by path: putting tests/unit on sys.path would shadow the
    # top-level `conftest` that later suites import helpers from.
    import importlib.util
    rc = sys.modules.get("_route_census")
    if rc is None:
        spec = importlib.util.spec_from_file_location(
            "_route_census", Path(__file__).resolve().parent / "_route_census.py")
        rc = importlib.util.module_from_spec(spec)
        sys.modules["_route_census"] = rc
        spec.loader.exec_module(rc)
    classified, _ = rc.classify(rc.walk(), rc.load_baseline())
    assert classified["routers/users.py::change_my_password"] == "interactive"
    assert classified["routers/users.py::verify_my_current_password"] == "interactive"

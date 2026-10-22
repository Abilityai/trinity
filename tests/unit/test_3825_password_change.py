"""#3825 — the admin can change their password in the app, and a restart keeps it.

Three parts, each executed for real:

* `PUT /api/users/me/password` through the real users router on a real SQLite
  store: needs the current password and an account that has one, the setup
  complexity rule, its own wrong-password cap, an audit row with no password
  in it, and a person's session (no MCP key).
* Boot (`database._ensure_admin_user` on a raw SQLite cursor and
  `_ensure_admin_user_engine` on the engine) re-applies `ADMIN_PASSWORD` only
  when it differs from the value it last applied, so an in-app change survives a
  restart while a NEW `.env` value still rotates/recovers it.
* The change signs every other session out (per-user cutoff) and hands the
  caller a fresh token; the boot fingerprint is unreachable through the generic
  settings routes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest
from passlib.context import CryptContext

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

PWD = CryptContext(schemes=["bcrypt"], deprecated="auto")
OLD = "Old-Passw0rd-xyz!"
NEW = "New-Passw0rd-xyz!"
ENV = "Env-Passw0rd-abc!"
ENV2 = "Env2-Passw0rd-def!"


def _identity():
    import utils.admin_identity as m
    return m


def _database():
    import database as m
    return m


# =============================================================================
# The boot decision
# =============================================================================

class TestEnvApplyDecision:
    def _decide(self, env, stored, fp, user="admin"):
        return _identity().env_apply_decision(env, stored, fp, user, PWD.verify)

    def _fp(self, value, user="admin"):
        return _identity().fingerprint_value(user, PWD.hash(value))

    def test_no_env_value_does_nothing(self):
        assert self._decide("", PWD.hash(OLD), None) == ("keep", False)

    def test_a_hashless_row_takes_the_env_value(self):
        assert self._decide(ENV, None, None) == ("apply", True)
        assert self._decide(ENV, "", None) == ("apply", True)

    def test_legacy_plaintext_is_migrated_only_when_it_is_the_env_value(self):
        assert self._decide(ENV, ENV, None) == ("apply", True)
        assert self._decide(ENV, "something-else", None) == ("keep", False)

    def test_in_sync_records_a_missing_fingerprint_once(self):
        assert self._decide(ENV, PWD.hash(ENV), None) == ("keep", True)
        assert self._decide(ENV, PWD.hash(ENV), self._fp(ENV)) == ("keep", False)

    def test_a_password_changed_in_the_app_is_left_alone(self):
        assert self._decide(ENV, PWD.hash(NEW), self._fp(ENV)) == ("skip", False)

    def test_a_new_env_value_wins(self):
        assert self._decide(ENV2, PWD.hash(NEW), self._fp(ENV)) == ("apply", True)

    def test_no_fingerprint_keeps_todays_behaviour(self):
        """An install upgraded with drift between `.env` and the row re-applies, as before."""
        assert self._decide(ENV, PWD.hash(OLD), None) == ("apply", True)

    @pytest.mark.parametrize("fp", ["not json", "[]", json.dumps({"username": "admin"}),
                                    json.dumps({"username": "admin", "hash": "nope"})])
    def test_a_corrupt_fingerprint_is_no_fingerprint(self, fp):
        assert self._decide(ENV, PWD.hash(NEW), fp) == ("apply", True)

    def test_another_users_fingerprint_does_not_count(self):
        assert self._decide(ENV, PWD.hash(NEW), self._fp(ENV, user="root")) == ("apply", True)


# =============================================================================
# Boot, SQLite arm (raw cursor)
# =============================================================================

def _mkdb():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, "
                "password_hash TEXT, role TEXT, created_at TEXT, updated_at TEXT)")
    cur.execute("CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)")
    conn.commit()
    return conn, cur


def _hash_of(cur, user="admin"):
    cur.execute("SELECT password_hash FROM users WHERE username = ?", (user,))
    return cur.fetchone()[0]


def _fingerprint(cur):
    cur.execute("SELECT value FROM system_settings WHERE key = ?", (_identity().ADMIN_ENV_FINGERPRINT_KEY,))
    row = cur.fetchone()
    return json.loads(row[0]) if row else None


def _boot(conn, cur, monkeypatch, env, user=None):
    monkeypatch.setenv("ADMIN_PASSWORD", env)
    if user is None:
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    else:
        monkeypatch.setenv("ADMIN_USERNAME", user)
    _database()._ensure_admin_user(cur, conn)


def _rotate_in_app(conn, cur, value, user="admin"):
    cur.execute("UPDATE users SET password_hash = ? WHERE username = ?", (PWD.hash(value), user))
    conn.commit()


class TestSqliteBoot:
    def test_first_boot_creates_the_admin_and_records_the_fingerprint(self, monkeypatch):
        conn, cur = _mkdb()
        _boot(conn, cur, monkeypatch, ENV)
        assert PWD.verify(ENV, _hash_of(cur))
        fp = _fingerprint(cur)
        assert fp["username"] == "admin" and PWD.verify(ENV, fp["hash"])

    def test_an_in_app_change_survives_a_restart(self, monkeypatch, capsys):
        conn, cur = _mkdb()
        _boot(conn, cur, monkeypatch, ENV)
        _rotate_in_app(conn, cur, NEW)
        _boot(conn, cur, monkeypatch, ENV)   # restart, .env unchanged
        assert PWD.verify(NEW, _hash_of(cur))
        assert not PWD.verify(ENV, _hash_of(cur))
        out = capsys.readouterr().out
        assert "not re-applied" in out and ENV not in out and NEW not in out

    def test_a_new_env_value_rotates_it_back(self, monkeypatch):
        conn, cur = _mkdb()
        _boot(conn, cur, monkeypatch, ENV)
        _rotate_in_app(conn, cur, NEW)
        _boot(conn, cur, monkeypatch, ENV2)
        assert PWD.verify(ENV2, _hash_of(cur))
        assert PWD.verify(ENV2, _fingerprint(cur)["hash"])
        # ...and it is now the applied value: another in-app change survives too.
        _rotate_in_app(conn, cur, NEW)
        _boot(conn, cur, monkeypatch, ENV2)
        assert PWD.verify(NEW, _hash_of(cur))

    def test_an_upgraded_install_in_sync_only_gains_a_fingerprint(self, monkeypatch):
        conn, cur = _mkdb()
        before = PWD.hash(ENV)
        cur.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')", (before,))
        conn.commit()
        _boot(conn, cur, monkeypatch, ENV)
        assert _hash_of(cur) == before
        assert PWD.verify(ENV, _fingerprint(cur)["hash"])

    def test_an_upgraded_install_with_drift_reapplies_as_before(self, monkeypatch):
        conn, cur = _mkdb()
        cur.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')",
                    (PWD.hash(OLD),))
        conn.commit()
        _boot(conn, cur, monkeypatch, ENV)
        assert PWD.verify(ENV, _hash_of(cur))

    def test_a_blank_env_writes_nothing(self, monkeypatch):
        conn, cur = _mkdb()
        cur.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')",
                    (PWD.hash(NEW),))
        conn.commit()
        _boot(conn, cur, monkeypatch, "")
        assert PWD.verify(NEW, _hash_of(cur)) and _fingerprint(cur) is None

    def test_a_padded_admin_username_names_the_same_account_as_the_app(self, monkeypatch):
        conn, cur = _mkdb()
        _boot(conn, cur, monkeypatch, ENV, user="  root  ")
        assert PWD.verify(ENV, _hash_of(cur, "root"))
        assert _fingerprint(cur)["username"] == _identity().admin_username() == "root"


# =============================================================================
# Boot, engine arm (the PostgreSQL code path, run on the SQLite engine)
# =============================================================================

@pytest.fixture()
def users_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3825.db"
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


class TestEngineBoot:
    def test_an_in_app_change_survives_and_a_new_value_wins(self, users_db, monkeypatch):
        # `db.engine` is cached across tests, so each test owns its username.
        user = f"admin-{uuid.uuid4().hex[:8]}"
        monkeypatch.setenv("ADMIN_USERNAME", user)
        from db.settings import SettingsOperations
        settings = SettingsOperations()
        key = _identity().ADMIN_ENV_FINGERPRINT_KEY
        settings.delete_setting(key)

        monkeypatch.setenv("ADMIN_PASSWORD", ENV)
        _database()._ensure_admin_user_engine()
        assert PWD.verify(ENV, users_db.get_user_by_username(user)["password"])
        assert json.loads(settings.get_setting_value(key))["username"] == user

        users_db.update_user_password(user, PWD.hash(NEW))
        _database()._ensure_admin_user_engine()
        assert PWD.verify(NEW, users_db.get_user_by_username(user)["password"])

        monkeypatch.setenv("ADMIN_PASSWORD", ENV2)
        _database()._ensure_admin_user_engine()
        assert PWD.verify(ENV2, users_db.get_user_by_username(user)["password"])


# =============================================================================
# The route
# =============================================================================

@pytest.fixture()
def api(users_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import users as users_router
    from services import password_service

    redis = fakeredis.FakeRedis()
    # The unit conftest may re-import `dependencies` between tests, while
    # `password_service` keeps the copy it imported first. Patch and read through
    # THAT copy — the one the change actually writes the cutoff with — each
    # globals dict once.
    dependencies = SimpleNamespace(**password_service.revoke_user_sessions.__globals__)
    for scope in {id(g): g for g in (password_service.change_own_password.__globals__,
                                     password_service.revoke_user_sessions.__globals__)}.values():
        monkeypatch.setitem(scope, "get_breaker_redis", lambda: redis)
    audit = AsyncMock()
    monkeypatch.setattr(users_router.platform_audit_service, "log", audit)
    get_current_user = users_router.get_current_user
    create_access_token = password_service.create_access_token

    app = FastAPI()
    app.include_router(users_router.router)
    state = {}
    app.dependency_overrides[get_current_user] = lambda: state["principal"]

    def as_(username, **over):
        row = users_db.get_user_by_username(username)
        state["principal"] = users_router.User(
            id=row["id"], username=row["username"], role=row.get("role", "admin"), **over)
        token = create_access_token(data={"sub": username}, mode="admin")
        return TestClient(app, headers={"Authorization": f"Bearer {token}"}), token

    return SimpleNamespace(as_=as_, audit=audit, redis=redis, db=users_db, deps=dependencies,
                           service=password_service)


def _admin(db, password=OLD):
    name = f"admin-{uuid.uuid4().hex[:8]}"
    db.update_user_password(name, PWD.hash(password))
    return name


def _put(client, current, new):
    return client.put("/api/users/me/password", json={"current_password": current, "new_password": new})


def _actions(audit):
    return [c.kwargs.get("event_action") for c in audit.await_args_list]


class TestChangeRoute:
    def test_the_change_lands_and_the_old_password_stops_working(self, api):
        name = _admin(api.db)
        client, old_token = api.as_(name)
        r = _put(client, OLD, NEW)
        assert r.status_code == 200, r.text
        stored = api.db.get_user_by_username(name)["password"]
        assert PWD.verify(NEW, stored) and not PWD.verify(OLD, stored)
        body = r.json()
        assert body["other_sessions_signed_out"] is True and body["token_type"] == "bearer"
        assert _actions(api.audit) == ["password_changed"]

    def test_other_sessions_are_signed_out_and_the_fresh_token_is_kept(self, api):
        from jose import jwt
        name = _admin(api.db)
        client, old_token = api.as_(name)
        fresh = _put(client, OLD, NEW).json()["access_token"]
        claims = lambda t: jwt.get_unverified_claims(t)  # noqa: E731
        assert api.deps.session_predates_user_revoke(claims(old_token)) is True
        assert api.deps.session_predates_user_revoke(claims(fresh)) is False

    def test_the_old_session_is_refused_at_the_auth_door(self, api):
        name = _admin(api.db)
        client, old_token = api.as_(name)
        fresh = _put(client, OLD, NEW).json()["access_token"]
        request = SimpleNamespace(method="GET", scope={"path": "/api/users/me"})
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            asyncio.run(api.deps.resolve_platform_user_unfloored(request, old_token))
        assert e.value.status_code == 401
        assert asyncio.run(api.deps.resolve_platform_user_unfloored(request, fresh)).username == name

    def test_a_wrong_current_password_is_refused_audited_and_capped(self, api):
        name = _admin(api.db)
        client, _ = api.as_(name)
        for _ in range(api.service.CHANGE_ATTEMPT_LIMIT):
            r = _put(client, "Wrong-Passw0rd-1!", NEW)
            assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_current_password"
        r = _put(client, OLD, NEW)   # even the right one, past the cap
        assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_attempts"
        assert PWD.verify(OLD, api.db.get_user_by_username(name)["password"])
        assert set(_actions(api.audit)) == {"password_change_failed"}

    def test_the_cap_holds_under_concurrent_attempts(self, api, monkeypatch):
        """The cap is reserved atomically BEFORE the password is checked: requests
        racing across workers cannot all read "4 so far" and each get a guess."""
        import threading
        name = _admin(api.db)
        racers = api.service.CHANGE_ATTEMPT_LIMIT + 3
        barrier = threading.Barrier(racers, timeout=1.0)
        checked = []
        real_verify = api.service.verify_password

        def slow_verify(plain, hashed):
            checked.append(plain)
            try:
                barrier.wait()   # every racer past the gate before any records a failure
            except threading.BrokenBarrierError:
                pass
            return real_verify(plain, hashed)

        monkeypatch.setitem(api.service.change_own_password.__globals__, "verify_password", slow_verify)
        outcomes = []

        def attempt():
            try:
                api.service.change_own_password(name, "Wrong-Passw0rd-1!", NEW, "admin")
            except api.service.PasswordChangeError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=attempt) for _ in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(checked) == api.service.CHANGE_ATTEMPT_LIMIT
        assert outcomes.count("too_many_attempts") == racers - api.service.CHANGE_ATTEMPT_LIMIT

    def test_a_right_password_does_not_spend_an_attempt(self, api):
        name = _admin(api.db)
        client, _ = api.as_(name)
        for _ in range(api.service.CHANGE_ATTEMPT_LIMIT - 1):
            _put(client, "Wrong-Passw0rd-1!", NEW)
        # Right current password, refused for a weak new one: not a wrong guess.
        assert _put(client, OLD, "weak").status_code == 422
        assert _put(client, OLD, NEW).status_code == 200

    def test_the_cap_is_not_the_login_bucket(self, api):
        """A stolen session must not be able to lock the owner out of signing in."""
        name = _admin(api.db)
        client, _ = api.as_(name)
        _put(client, "Wrong-Passw0rd-1!", NEW)
        keys = [k.decode() for k in api.redis.keys("*")]
        assert keys == [f"pwchange:fail:{name}"]

    def test_a_weak_password_is_refused_with_the_reasons(self, api):
        client, _ = api.as_(_admin(api.db))
        r = _put(client, OLD, "short")
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "weak_password" and any("12" in e for e in detail["errors"])

    def test_the_same_password_is_refused(self, api):
        client, _ = api.as_(_admin(api.db))
        r = _put(client, OLD, OLD)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "password_unchanged"

    def test_an_email_only_account_cannot_gain_a_password(self, api):
        from db_models import UserCreate
        name = f"user-{uuid.uuid4().hex[:8]}"
        api.db.create_user(UserCreate(username=name, password=None, role="user",
                                      email=f"{name}@example.com"))
        client, _ = api.as_(name)
        r = _put(client, "", NEW)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_password"
        assert not api.db.get_user_by_username(name).get("password")

    def test_a_legacy_plaintext_password_is_refused_not_guessed_at(self, api):
        name = f"admin-{uuid.uuid4().hex[:8]}"
        api.db.update_user_password(name, OLD)   # stored as-is, not bcrypt
        client, _ = api.as_(name)
        r = _put(client, OLD, NEW)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "password_not_changeable"
        assert api.redis.keys("*") == []

    @pytest.mark.parametrize("over", [dict(mcp_scope="user"), dict(mcp_scope="agent", agent_name="a1"),
                                      dict(mcp_scope="system")])
    def test_a_key_cannot_change_a_password(self, api, over):
        name = _admin(api.db)
        client, _ = api.as_(name, **over)
        assert _put(client, OLD, NEW).status_code == 403
        assert PWD.verify(OLD, api.db.get_user_by_username(name)["password"])

    def test_no_password_reaches_the_audit_rows(self, api):
        client, _ = api.as_(_admin(api.db))
        _put(client, "Wrong-Passw0rd-1!", NEW)
        _put(client, OLD, NEW)
        dumped = json.dumps([c.kwargs.get("details") for c in api.audit.await_args_list])
        for secret in (OLD, NEW, "Wrong-Passw0rd-1!"):
            assert secret not in dumped

    def test_env_stale_is_reported_for_the_provisioned_admin(self, api, monkeypatch):
        name = _admin(api.db)
        monkeypatch.setenv("ADMIN_USERNAME", name)
        monkeypatch.setenv("ADMIN_PASSWORD", ENV)
        client, _ = api.as_(name)
        assert _put(client, OLD, NEW).json()["admin_env_stale"] is True

    def test_env_is_not_stale_when_the_new_password_is_the_env_value(self, api, monkeypatch):
        name = _admin(api.db)
        monkeypatch.setenv("ADMIN_USERNAME", name)
        monkeypatch.setenv("ADMIN_PASSWORD", NEW)
        client, _ = api.as_(name)
        assert _put(client, OLD, NEW).json()["admin_env_stale"] is False

    def test_redis_down_still_changes_and_says_sessions_were_not_signed_out(self, api, monkeypatch):
        for fn in (api.service.change_own_password, api.service.revoke_user_sessions):
            monkeypatch.setitem(fn.__globals__, "get_breaker_redis", lambda: None)
        name = _admin(api.db)
        client, _ = api.as_(name)
        r = _put(client, OLD, NEW)
        assert r.status_code == 200 and r.json()["other_sessions_signed_out"] is False


class TestApiKeysOnChange:
    """MCP API keys are not sessions: the cutoff never reaches them. A key minted
    from a session opened with a leaked password would outlive the change, so the
    person can revoke their own keys in the same request (CSO-3825-1)."""

    def _keys(self, api, name):
        from db_models import McpApiKeyCreate
        user = api.db.create_mcp_api_key(name, McpApiKeyCreate(name="cli"))
        ops = api.db.create_mcp_api_key(name, McpApiKeyCreate(name="ops", scope="ops"))
        agent = api.db.create_agent_mcp_api_key(f"agent-{uuid.uuid4().hex[:6]}", name)
        return user, ops, agent

    def _active(self, api, key):
        return api.db.validate_mcp_api_key(key.api_key) is not None

    def test_opting_in_revokes_the_persons_keys_and_never_an_agents(self, api):
        name = _admin(api.db)
        user, ops, agent = self._keys(api, name)
        client, _ = api.as_(name)
        r = client.put("/api/users/me/password", json={
            "current_password": OLD, "new_password": NEW, "revoke_api_keys": True})
        assert r.status_code == 200, r.text
        assert (r.json()["api_keys_revoked"], r.json()["api_keys_active"]) == (2, 0)
        assert not self._active(api, user) and not self._active(api, ops)
        assert self._active(api, agent)
        details = api.audit.await_args_list[-1].kwargs["details"]
        assert sorted(details["api_keys_revoked"]) == sorted([user.id, ops.id])
        assert user.api_key not in json.dumps(details)

    def test_without_opting_in_the_keys_stay_and_are_counted(self, api):
        name = _admin(api.db)
        user, ops, agent = self._keys(api, name)
        client, _ = api.as_(name)
        body = _put(client, OLD, NEW).json()
        assert (body["api_keys_revoked"], body["api_keys_active"]) == (0, 2)
        assert self._active(api, user) and self._active(api, ops) and self._active(api, agent)

    def test_a_refused_change_revokes_nothing(self, api):
        name = _admin(api.db)
        user, _, _ = self._keys(api, name)
        client, _ = api.as_(name)
        r = client.put("/api/users/me/password", json={
            "current_password": "Wrong-Passw0rd-1!", "new_password": NEW, "revoke_api_keys": True})
        assert r.status_code == 400 and self._active(api, user)

    def test_only_the_callers_keys_are_touched(self, api):
        """`revoke_mcp_api_key` skips its ownership check for an admin, so the
        ids must come from the caller's own list — another admin's key survives."""
        name, other = _admin(api.db), _admin(api.db)
        theirs, _, _ = self._keys(api, other)
        self._keys(api, name)
        client, _ = api.as_(name)
        client.put("/api/users/me/password", json={
            "current_password": OLD, "new_password": NEW, "revoke_api_keys": True})
        assert self._active(api, theirs)


# =============================================================================
# Session cutoff edges
# =============================================================================

class TestSessionCutoff:
    @pytest.fixture()
    def deps(self, monkeypatch):
        import dependencies
        redis = fakeredis.FakeRedis()
        monkeypatch.setitem(dependencies.revoke_user_sessions.__globals__, "get_breaker_redis", lambda: redis)
        return dependencies

    def test_no_cutoff_refuses_nothing_even_without_iat(self, deps):
        assert deps.session_predates_user_revoke({"sub": "u1"}) is False

    def test_a_pre_3825_token_without_iat_is_refused_once_a_cutoff_exists(self, deps):
        assert deps.revoke_user_sessions("u2")
        assert deps.session_predates_user_revoke({"sub": "u2", "jti": "x"}) is True

    def test_the_cutoff_is_per_user(self, deps):
        assert deps.revoke_user_sessions("u3")
        assert deps.session_predates_user_revoke({"sub": "u4", "iat": 1}) is False

    def test_the_event_loopback_token_is_not_a_session(self, deps):
        """EVT-001 mints `sub` = the provisioned admin with no `iat`; an admin
        password change must not refuse every event dispatch for 7 days."""
        from jose import jwt
        from services import event_dispatch_service
        from utils.admin_identity import admin_username
        assert deps.revoke_user_sessions(admin_username())
        loopback = jwt.get_unverified_claims(event_dispatch_service._get_internal_token())
        assert loopback["scope"] == deps.EVENT_LOOPBACK_SCOPE
        assert deps.session_predates_user_revoke(loopback) is False
        # ...while a real session of the same user is still cut off.
        assert deps.session_predates_user_revoke({"sub": admin_username(), "iat": 1}) is True

    def test_same_second_boundary(self, deps, monkeypatch):
        """Within one second: a token minted before the change is refused, a
        sign-in after it is not (sub-second `iat_ms`, not `iat`, decides)."""
        from jose import jwt
        clock = {"ms": 1_700_000_000_200}
        monkeypatch.setitem(deps.create_access_token.__globals__, "_epoch_ms", lambda: clock["ms"])
        before = jwt.get_unverified_claims(deps.create_access_token(data={"sub": "u6"}))
        clock["ms"] = 1_700_000_000_500
        assert deps.revoke_user_sessions("u6")
        clock["ms"] = 1_700_000_000_800
        after = jwt.get_unverified_claims(deps.create_access_token(data={"sub": "u6"}))
        assert before["iat"] == after["iat"]  # same second
        assert deps.session_predates_user_revoke(before) is True
        assert deps.session_predates_user_revoke(after) is False

    def test_a_token_dated_only_to_the_second_is_refused_in_the_cutoff_second(self, deps, monkeypatch):
        """No `iat_ms` (a caller-supplied `iat`): the cutoff second cannot be
        split, so it is refused — rounding toward revoking."""
        monkeypatch.setitem(deps.create_access_token.__globals__, "_epoch_ms", lambda: 1_700_000_000_500)
        assert deps.revoke_user_sessions("u7")
        assert deps.session_predates_user_revoke({"sub": "u7", "iat": 1_700_000_000}) is True
        assert deps.session_predates_user_revoke({"sub": "u7", "iat": 1_700_000_001}) is False

    def test_every_platform_token_now_carries_iat(self, deps):
        from jose import jwt
        assert "iat" in jwt.get_unverified_claims(deps.create_access_token(data={"sub": "u5"}))


# =============================================================================
# The fingerprint is not a setting
# =============================================================================

class TestFingerprintIsReserved:
    @pytest.fixture()
    def generic(self, users_db):
        from routers.settings import generic
        from db.settings import SettingsOperations
        SettingsOperations().set_setting(_identity().ADMIN_ENV_FINGERPRINT_KEY, "{}")
        admin = generic.User(id=1, username="admin", role="admin")
        return generic, admin, SimpleNamespace(method="GET", scope={"path": "/api/settings"})

    def test_it_is_not_listed(self, generic):
        mod, admin, request = generic
        keys = {s.key for s in asyncio.run(mod.get_all_settings(request, admin))}
        assert _identity().ADMIN_ENV_FINGERPRINT_KEY not in keys

    def test_it_reads_as_absent(self, generic):
        from fastapi import HTTPException
        mod, admin, request = generic
        with pytest.raises(HTTPException) as e:
            asyncio.run(mod.get_setting(_identity().ADMIN_ENV_FINGERPRINT_KEY, request, admin))
        assert e.value.status_code == 404

    def test_it_cannot_be_written_or_cleared(self, generic):
        from fastapi import HTTPException
        from db_models import SystemSettingUpdate
        mod, admin, request = generic
        key = _identity().ADMIN_ENV_FINGERPRINT_KEY
        with pytest.raises(HTTPException) as e:
            asyncio.run(mod.update_setting(key, SystemSettingUpdate(value="{}"), request, admin))
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e:
            asyncio.run(mod.delete_setting(key, request, admin))
        assert e.value.status_code == 422


# =============================================================================
# The voice socket is a platform door too
# =============================================================================

class TestVoiceSocket:
    def test_a_signed_out_session_cannot_open_the_voice_socket(self, monkeypatch):
        import dependencies
        from routers import voice
        redis = fakeredis.FakeRedis()
        monkeypatch.setitem(dependencies.revoke_user_sessions.__globals__, "get_breaker_redis", lambda: redis)
        token = dependencies.create_access_token(data={"sub": "voice-user"}, mode="admin")
        assert dependencies.revoke_user_sessions("voice-user")

        closed = []

        class _Socket:
            async def close(self, code=None, reason=None):
                closed.append((code, reason))

        # Stop at the first step after the gate: an unknown user closes 4001
        # with a different reason, so reaching it would show the gate missed.
        monkeypatch.setattr(voice.db, "get_user_by_username", lambda u: None)
        asyncio.run(voice.voice_websocket(_Socket(), "vs-1", token=token))
        assert closed == [(4001, "Session signed out")]

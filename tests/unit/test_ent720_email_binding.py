"""trinity-enterprise#720 — a sign-in email is proven, unique, and written in one place.

Every sign-in path resolves the account by email ALONE, so whoever holds an
address on a `users` row holds that identity: what is shared with it, its
Workspace threads, and the real person's next sign-in. #711/#2996 closed the
agent/system-key door to `PUT /api/users/me/email`; these are the residual doors:

  1. any signed-in human could bind an UNCLAIMED address (a sharee who never
     signed up) — the 409 only looked at `users` rows. Now: mailbox proof.
  2. no unique constraint on `users.email`; the 409 was check-then-write.
  3. other writers of `users.email` had no duplicate check at all.
  4. a re-bound account's old `username == email` made the next email sign-in
     for that address an unhandled 500.
  5. the channel redeemers skipped `suspended_at`.

Real sqlite built by the platform's own `init_schema` (so the unique index is
the real one), the real `/api/users` routes, a captured mail sender.
"""
from __future__ import annotations

import inspect
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


def _addr(tag: str = "u") -> str:
    # `db.engine` caches the engine at first use, so every test uses its own
    # addresses rather than relying on a fresh database (the ent#428 harness note).
    return f"{tag}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture()
def users_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent720.db"
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


def _users_mod(db):
    """The namespace the live `db` singleton's user code actually raises from.
    Another test may evict and re-import `db.users`, after which
    `sys.modules["db.users"]` is a NEW module while the singleton still raises
    the OLD module's `EmailInUseError` — so read the raising function's own
    globals, not the module registry."""
    import types
    return types.SimpleNamespace(**type(db._user_ops)._insert_user.__globals__)


def _make_user(db, email=None, role="user", username=None):
    from db_models import UserCreate
    username = username or f"user-{uuid.uuid4().hex[:8]}"
    return db.create_user(UserCreate(username=username, password=None, role=role, email=email))


# =============================================================================
# 2 + 3 — unique, and one checked writer
# =============================================================================

class TestUniqueAndOneWriter:
    def test_create_user_refuses_a_held_address_any_case(self, users_db):
        EmailInUseError = _users_mod(users_db).EmailInUseError
        a = _addr()
        _make_user(users_db, email=a)
        with pytest.raises(EmailInUseError):
            _make_user(users_db, email=a.upper())

    def test_update_user_refuses_a_held_address(self, users_db):
        EmailInUseError = _users_mod(users_db).EmailInUseError
        a = _addr()
        _make_user(users_db, email=a)
        other = _make_user(users_db, email=_addr())
        with pytest.raises(EmailInUseError):
            users_db.update_user(other["username"], {"email": f"  {a.upper()} "})

    def test_rebinding_your_own_address_is_not_a_conflict(self, users_db):
        a = _addr()
        me = _make_user(users_db, email=a)
        assert users_db.update_user(me["username"], {"email": a.upper()})["email"] == a

    def test_the_password_upsert_cannot_insert_a_duplicate(self, users_db):
        EmailInUseError = _users_mod(users_db).EmailInUseError
        taken = f"admin-{uuid.uuid4().hex[:6]}@example.com"
        _make_user(users_db, email=taken)
        with pytest.raises(EmailInUseError):
            users_db.update_user_password(taken, "x")   # inserts username=email=taken

    def test_a_lost_race_on_the_index_is_the_same_refusal(self, users_db, monkeypatch):
        """Check-then-write can lose a race; the unique index is what holds, and
        its IntegrityError surfaces as EmailInUseError, never a 500."""
        users_mod = _users_mod(users_db)
        EmailInUseError, UserOperations = users_mod.EmailInUseError, users_mod.UserOperations
        a = _addr()
        _make_user(users_db, email=a)
        other = _make_user(users_db, email=_addr())
        monkeypatch.setattr(UserOperations, "_assert_email_free", staticmethod(lambda *a, **k: None))
        with pytest.raises(EmailInUseError):
            users_db.update_user(other["username"], {"email": a})

    def test_the_unique_index_exists_in_the_fresh_schema(self, users_db):
        from db.schema import INDEXES
        assert any("idx_users_email_unique" in ddl and "lower(email)" in ddl and "UNIQUE" in ddl
                   for ddl in INDEXES)

    def test_lookup_by_email_is_case_insensitive(self, users_db):
        a = _addr()
        u = _make_user(users_db, email=a)
        assert users_db.get_user_by_email(a.upper())["username"] == u["username"]

    def test_every_users_email_writer_goes_through_the_checked_write(self):
        """The writer census. Any `insert(users)` / `update(users)` whose values
        can carry `email`, or raw SQL that writes `users` with an email column,
        must live inside `_insert_user` / `_update_user_row`. A new writer that
        bypasses them fails here, by name."""
        import ast
        allowed_funcs = {"_insert_user", "_update_user_row"}
        offenders = []
        skip_dirs = {"migrations", "enterprise", "__pycache__"}
        for path in _BACKEND.rglob("*.py"):
            rel = path.relative_to(_BACKEND)
            if set(rel.parts) & skip_dirs or rel.parts[:2] == ("db", "migrations.py"):
                continue
            if rel.as_posix() == "db/migrations.py":
                continue   # schema migrations, not runtime writers
            src = path.read_text(encoding="utf-8", errors="replace")
            if "users" not in src:
                continue
            tree = ast.parse(src)
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    # insert(users)/update(users) … .values(email=… | **dyn)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                            and node.func.attr == "values":
                        base = node.func.value
                        while isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute):
                            base = base.func.value
                        is_users_write = (isinstance(base, ast.Call) and getattr(base.func, "id", None)
                                          in ("insert", "update", "make_insert")
                                          and base.args and getattr(base.args[0], "id", None) == "users")
                        if not is_users_write:
                            continue
                        writes_email = any(k.arg == "email" or k.arg is None for k in node.keywords)
                        if writes_email and fn.name not in allowed_funcs:
                            offenders.append(f"{rel}:{node.lineno} in {fn.name}()")
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        sql = " ".join(node.value.upper().split())
                        if ("INSERT INTO USERS" in sql or "UPDATE USERS" in sql) and "EMAIL" in sql:
                            offenders.append(f"{rel}:{node.lineno} raw SQL in {fn.name}()")
        assert offenders == [], "users.email written outside the checked writer:\n" + "\n".join(offenders)


# =============================================================================
# 4 — a reclaimed username is not a 500
# =============================================================================

def test_email_sign_in_for_an_address_whose_username_is_taken_creates_a_suffixed_account(users_db):
    """Account A was created by email sign-in (`username == a`), then re-bound
    to b. The next email sign-in for `a` used to INSERT username=a and 500."""
    a, b = _addr("a"), _addr("b")
    first = users_db.get_or_create_email_user(a)
    assert first["username"] == a
    users_db.update_user(a, {"email": b})
    second = users_db.get_or_create_email_user(a)
    assert second and second["username"] != a and second["username"].startswith(a)
    assert second["email"] == a
    assert users_db.get_user_by_email(b)["username"] == a   # A keeps b


def test_auth0_sign_in_never_hands_a_rebound_address_back_to_its_old_username(users_db):
    """The Auth0 lookup used `username == email` first; an account that re-bound
    AWAY from the address must not get it back through its old username."""
    a, b = _addr("a"), _addr("b")
    users_db.get_or_create_email_user(a)
    users_db.update_user(a, {"email": b})
    holder = users_db.get_or_create_email_user(a)            # the new holder of `a`
    linked = users_db.get_or_create_auth0_user(f"auth0|{uuid.uuid4().hex[:6]}", a)
    assert linked["username"] == holder["username"]


# =============================================================================
# the migration (both tracks)
# =============================================================================

class TestMigration:
    def _legacy_db(self, tmp_path):
        """A pre-ent#720 install: no unique index, duplicates, a blank, no `purpose`."""
        p = tmp_path / "legacy.db"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, email TEXT, created_at TEXT)")
        c.execute("CREATE TABLE email_login_codes (id TEXT PRIMARY KEY, email TEXT, code TEXT, "
                  "created_at TEXT, expires_at TEXT, verified INTEGER, used_at TEXT)")
        rows = [
            (1, "orig", "dup@example.com", "2026-01-01T00:00:00Z"),
            (2, "binder", "DUP@example.com", "2026-06-01T00:00:00Z"),
            (3, "later", "dup@example.com", "2026-07-01T00:00:00Z"),
            (4, "blank1", " ", "2026-01-01T00:00:00Z"),
            (5, "blank2", "", "2026-01-01T00:00:00Z"),
            (6, "solo", "solo@example.com", "2026-01-01T00:00:00Z"),
        ]
        c.executemany("INSERT INTO users VALUES (?,?,?,?)", rows)
        c.commit()
        return c

    def test_the_sqlite_migration_keeps_the_earliest_and_adds_the_index(self, tmp_path, capsys):
        from db import migrations
        c = self._legacy_db(tmp_path)
        fn = dict(migrations.MIGRATIONS)["ent720_email_identity"]
        for _ in range(2):   # idempotent
            fn(c.cursor(), c)
            c.commit()
        emails = dict(c.execute("SELECT username, email FROM users"))
        assert emails == {"orig": "dup@example.com", "binder": None, "later": None,
                          "blank1": None, "blank2": None, "solo": "solo@example.com"}
        idx = c.execute("SELECT sql FROM sqlite_master WHERE name='idx_users_email_unique'").fetchone()
        assert idx and "UNIQUE" in idx[0].upper()
        assert "purpose" in {r[1] for r in c.execute("PRAGMA table_info(email_login_codes)")}
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE users SET email = 'SOLO@example.com' WHERE username = 'orig'")
        out = capsys.readouterr().out
        assert "binder" in out and "dup@example.com" not in out   # usernames logged, never the address

    def test_the_alembic_copy_of_the_rule_is_the_sqlite_rule(self, monkeypatch):
        """A revision never imports app code, so it carries a frozen copy of the
        decision function — pinned identical in behaviour and in source."""
        import importlib.util
        from db.migrations import resolve_duplicate_emails
        path = _BACKEND / "migrations" / "versions" / "0084_ent720_email_identity.py"
        spec = importlib.util.spec_from_file_location("rev_ent720", path)
        rev = importlib.util.module_from_spec(spec)
        if "alembic.op" not in sys.modules:
            monkeypatch.setitem(sys.modules, "alembic.op", type(sys)("alembic.op"))
        spec.loader.exec_module(rev)
        rows = [(1, "a", "X@e.com", "2"), (2, "b", "x@e.com", "1"), (3, "c", "y@e.com", "1"),
                (4, "d", "x@E.com", "1"), (5, "e", "", "0")]
        assert rev._resolve_duplicate_emails(rows) == resolve_duplicate_emails(rows)
        body = lambda f: inspect.getsource(f).split(":", 1)[1]
        assert body(rev._resolve_duplicate_emails) == body(resolve_duplicate_emails)
        assert rev.down_revision == "0083_execution_conversation_key"


# =============================================================================
# 1 — the bind route needs mailbox proof
# =============================================================================

class _FakeRedis:
    """Just enough Redis for the OTP failure counter (get/ttl/delete/incr+expire)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

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


def _real_routers_auth(monkeypatch):
    """The real `routers.auth`, re-imported for this test if a stub holds the slot."""
    import importlib
    mod = sys.modules.get("routers.auth")
    if not hasattr(mod, "check_otp_rate_limit"):
        monkeypatch.delitem(sys.modules, "routers.auth", raising=False)
        if not getattr(sys.modules.get("routers"), "__file__", None):
            monkeypatch.delitem(sys.modules, "routers", raising=False)
        mod = importlib.import_module("routers.auth")
    monkeypatch.setattr(sys.modules["routers"], "auth", mod, raising=False)
    return mod


@pytest.fixture()
def api(users_db, monkeypatch):
    """The real users router; `get_current_user` overridden so the REAL
    interactive gate runs. Mail and audit captured."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import users as users_router
    # Keyed on the ROUTER's own reference: a sibling suite may evict and
    # re-import `dependencies`, and an override keyed on a fresh import would
    # then never match what `require_interactive` depends on.
    get_current_user = users_router.get_current_user
    User = users_router.User
    sent = []

    async def fake_send(self, to_email, code, agent_name=None, context_label=None):
        sent.append((to_email, code))
        return True

    from services.email_service import EmailService
    monkeypatch.setattr(EmailService, "send_verification_code", fake_send)
    audit = AsyncMock()
    monkeypatch.setattr(users_router.platform_audit_service, "log", audit)
    state = {"deliverable": True, "principal": None}
    monkeypatch.setattr(users_router, "_email_can_be_delivered", lambda: state["deliverable"])

    # The bind route reaches the OTP limiter through `routers.auth`. A sibling
    # suite (test_telegram_login_gate) may have parked a bare stub there, so
    # load the real module for this test and back its counter with a fake.
    auth_module = _real_routers_auth(monkeypatch)
    otp = _FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis_client", lambda: otp)

    app = FastAPI()
    app.include_router(users_router.router)
    app.dependency_overrides[get_current_user] = lambda: state["principal"]

    def as_(row, **over):
        state["principal"] = User(id=row["id"], username=row["username"], email=row.get("email"),
                                  role=row.get("role", "user"), **over)
        return TestClient(app)

    return {"as": as_, "sent": sent, "audit": audit, "state": state, "db": users_db,
            "otp": otp, "auth": auth_module}


def _audit_actions(audit):
    return [c.kwargs.get("event_action") for c in audit.await_args_list]


class TestBindNeedsProof:
    def test_a_bind_without_a_code_is_refused(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        r = api["as"](me).put("/api/users/me/email", json={"email": _addr("new")})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "code_required"

    def test_the_unclaimed_shared_address_cannot_be_taken(self, api):
        """Door 1. `victim` was shared an agent but never signed up — no users
        row, so the old 409 saw nothing. Without the victim's mailbox the binder
        now gets nothing but a refusal."""
        victim = _addr("victim")
        attacker = _make_user(api["db"], email=_addr("attacker"))
        c = api["as"](attacker)
        assert c.put("/api/users/me/email", json={"email": victim}).status_code == 400
        assert c.put("/api/users/me/email", json={"email": victim, "code": "000000"}).status_code == 400
        assert api["db"].get_user_by_email(victim) is None

    def test_the_code_round_trip_binds_and_is_audited(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new = _addr("new")
        c = api["as"](me)
        assert c.post("/api/users/me/email/code", json={"email": new}).status_code == 200
        (to, code), = api["sent"]
        assert to == new
        r = c.put("/api/users/me/email", json={"email": new, "code": code})
        assert r.status_code == 200 and r.json()["verified"] is True
        assert api["db"].get_user_by_email(new)["username"] == me["username"]
        assert "email_bound" in _audit_actions(api["audit"])

    def test_a_code_is_single_use(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new, newer = _addr("new"), _addr("newer")
        c = api["as"](me)
        c.post("/api/users/me/email/code", json={"email": new})
        code = api["sent"][-1][1]
        assert c.put("/api/users/me/email", json={"email": new, "code": code}).status_code == 200
        assert c.put("/api/users/me/email", json={"email": new, "code": code}).status_code == 400

    def test_a_bind_code_is_tied_to_the_account_that_asked_for_it(self, api):
        new = _addr("new")
        alice = _make_user(api["db"], email=_addr("alice"))
        bob = _make_user(api["db"], email=_addr("bob"))
        api["as"](alice).post("/api/users/me/email/code", json={"email": new})
        code = api["sent"][-1][1]
        r = api["as"](bob).put("/api/users/me/email", json={"email": new, "code": code})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code"

    def test_a_bind_code_never_signs_anyone_in_and_a_sign_in_code_never_binds(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new = _addr("new")
        c = api["as"](me)
        c.post("/api/users/me/email/code", json={"email": new})
        bind_code = api["sent"][-1][1]
        assert api["db"].verify_login_code(new, bind_code) is None          # not a sign-in code
        login = api["db"].create_login_code(new)["code"]
        r = c.put("/api/users/me/email", json={"email": new, "code": login})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code"

    def test_an_address_another_account_holds_is_refused_at_both_steps(self, api):
        taken = _addr("taken")
        _make_user(api["db"], email=taken)
        me = _make_user(api["db"], email=_addr("me"))
        c = api["as"](me)
        for r in (c.post("/api/users/me/email/code", json={"email": taken}),
                  c.put("/api/users/me/email", json={"email": taken, "code": "123456"})):
            assert r.status_code == 409 and r.json()["detail"]["code"] == "email_in_use"
        assert api["sent"] == []

    def test_codes_are_rate_limited(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new = _addr("new")
        c = api["as"](me)
        codes = [c.post("/api/users/me/email/code", json={"email": new}).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]

    @pytest.mark.parametrize("scope", ["user", "agent", "system"])
    def test_a_key_authenticated_caller_is_refused(self, api, scope):
        me = _make_user(api["db"], email=_addr("me"))
        extra = {"mcp_scope": scope}
        if scope == "agent":
            extra["agent_name"] = "some-agent"
        c = api["as"](me, **extra)
        assert c.post("/api/users/me/email/code", json={"email": _addr()}).status_code == 403
        assert c.put("/api/users/me/email", json={"email": _addr(), "code": "123456"}).status_code == 403


class TestBindGuessCap:
    """A bind code gets the sign-in cap: 5 wrong guesses, then even the right
    code is refused — under a bind-scoped key, not the bare address."""

    def _mint(self, api, who, new):
        c = api["as"](who)
        assert c.post("/api/users/me/email/code", json={"email": new}).status_code == 200
        return c, api["sent"][-1][1]

    @staticmethod
    def _wrong(code):
        return f"{(int(code) + 1) % 1_000_000:06d}"

    def test_five_wrong_codes_lock_out_the_right_one(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new = _addr("new")
        c, code = self._mint(api, me, new)
        for _ in range(5):
            r = c.put("/api/users/me/email", json={"email": new, "code": self._wrong(code)})
            assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code"
        r = c.put("/api/users/me/email", json={"email": new, "code": code})
        assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_attempts"
        assert api["db"].get_user_by_email(new) is None

    def test_the_sign_in_counter_for_that_address_is_untouched(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new = _addr("new")
        c, code = self._mint(api, me, new)
        for _ in range(6):
            c.put("/api/users/me/email", json={"email": new, "code": self._wrong(code)})
        assert api["otp"].get(f"otp_attempts:{new}") is None
        assert api["auth"].check_otp_rate_limit(new) is True   # owner can still sign in

    def test_another_account_has_its_own_counter(self, api):
        new = _addr("new")
        alice = _make_user(api["db"], email=_addr("alice"))
        bob = _make_user(api["db"], email=_addr("bob"))
        ca, code_a = self._mint(api, alice, new)
        for _ in range(5):
            ca.put("/api/users/me/email", json={"email": new, "code": self._wrong(code_a)})
        cb, code_b = self._mint(api, bob, new)
        r = cb.put("/api/users/me/email", json={"email": new, "code": code_b})
        assert r.status_code == 200 and r.json()["verified"] is True

    def test_a_successful_bind_clears_the_counter(self, api):
        me = _make_user(api["db"], email=_addr("me"))
        new = _addr("new")
        c, code = self._mint(api, me, new)
        for _ in range(4):
            c.put("/api/users/me/email", json={"email": new, "code": self._wrong(code)})
        assert c.put("/api/users/me/email", json={"email": new, "code": code}).status_code == 200
        assert api["otp"].get(f"otp_attempts:bind:{me['id']}:{new}") is None


class TestNoDeliveryInstall:
    """The #82 transition on an install that cannot deliver mail (provider console)."""

    def test_an_admin_may_bind_without_a_code_and_it_is_audited_as_unverified(self, api):
        api["state"]["deliverable"] = False
        admin = _make_user(api["db"], email=None, role="admin")
        new = _addr("admin")
        r = api["as"](admin).put("/api/users/me/email", json={"email": new})
        assert r.status_code == 200 and r.json()["verified"] is False
        assert "email_bind_unverified" in _audit_actions(api["audit"])

    def test_anyone_else_is_told_verification_is_unavailable(self, api):
        api["state"]["deliverable"] = False
        me = _make_user(api["db"], email=_addr("me"))
        c = api["as"](me)
        for r in (c.post("/api/users/me/email/code", json={"email": _addr()}),
                  c.put("/api/users/me/email", json={"email": _addr()})):
            assert r.status_code == 409 and r.json()["detail"]["code"] == "email_verification_unavailable"

    def test_even_the_admin_bypass_cannot_take_a_held_address(self, api):
        api["state"]["deliverable"] = False
        taken = _addr("taken")
        _make_user(api["db"], email=taken)
        admin = _make_user(api["db"], email=None, role="admin")
        r = api["as"](admin).put("/api/users/me/email", json={"email": taken})
        assert r.status_code == 409


# =============================================================================
# 5 — the redeemers honour suspension
# =============================================================================

def _suspend(db, username):
    from sqlalchemy import update
    from db.tables import users
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(update(users).where(users.c.username == username).values(suspended_at="2026-09-29T00:00:00Z"))


def test_email_has_agent_access_refuses_a_suspended_account(users_db, monkeypatch):
    a = _addr("sus")
    u = _make_user(users_db, email=a, role="admin")     # admin → normally True
    assert users_db.email_has_agent_access("any-agent", a) is True
    _suspend(users_db, u["username"])
    assert users_db.email_has_agent_access("any-agent", a) is False
    assert users_db.is_email_account_suspended(a) is True
    assert users_db.is_email_account_suspended(_addr("nobody")) is False


def test_the_mcp_inline_redeemer_refuses_a_suspended_account(users_db):
    from services import mcp_auth_service
    a = _addr("sus")
    u = _make_user(users_db, email=a)
    code = users_db.create_login_code(a)["code"]
    _suspend(users_db, u["username"])
    assert mcp_auth_service.verify_login_code(a, code) is None

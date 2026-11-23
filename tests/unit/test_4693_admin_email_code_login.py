"""#4693 — the admin's own email from setup can sign in by emailed code.

Setup stored the admin's email on the admin row but never allow-listed it, so
`POST /api/auth/email/request` answered its generic 200 and minted nothing.

Driven end to end over one in-memory DB: the real setup route (both forms —
password setup and the emailed-link claim of trinity-enterprise#1312), then the
real request route. The admin's address now creates a code and dispatches a
send; any other address still creates nothing, with the same body. Installs set
up before the fix are covered by a boot backfill, and the admin's later email
change is covered at that writer.
"""
import ast
import asyncio
import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi import BackgroundTasks

pytestmark = pytest.mark.unit

PW = "Sup3rSecret!!"
EMAIL = "owner@example.com"
SECRET = "a1" * 32
GENERIC = {"success": True, "message": "If your email is registered, you'll receive a code shortly"}
BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


class FakeDB:
    def __init__(self):
        self.settings = {"setup_completed": "false", "email_auth_enabled": "true"}
        self.users = {"admin": {"username": "admin", "id": 1, "email": None}}
        self.whitelist = {}
        self.codes = []

    # users / settings
    def get_user_by_username(self, username):
        return self.users.get(username)

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def update_user_password(self, username, hashed, email=None):
        row = self.users.setdefault(username, {"username": username})
        row["password"] = hashed
        if email is not None:
            row["email"] = email
        return True

    def update_user(self, username, updates):
        self.users.setdefault(username, {"username": username}).update(updates)
        return self.users[username]

    # allow-list / codes
    def is_email_whitelisted(self, email):
        return email.lower() in self.whitelist

    def add_to_whitelist(self, email, added_by, source, *, default_role):
        if email.lower() in self.whitelist:
            return False
        self.whitelist[email.lower()] = {"added_by": added_by, "source": source,
                                         "default_role": default_role}
        return True

    def count_recent_code_requests(self, email, minutes=10):
        return sum(1 for c in self.codes if c == email)

    def create_login_code(self, email, expiry_minutes=10):
        self.codes.append(email)
        return {"code": "123456", "expires_in_seconds": 600}


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def fake(monkeypatch, tmp_path):
    import routers.setup as setup
    import routers.auth as auth
    import services.admin_email_allowlist as allowlist
    import services.email_service as email_service

    db = FakeDB()
    sent = []

    class _Mailer:
        async def send_verification_code(self, target, code, context_label=None):
            sent.append(target)
            return True

    for mod in (setup, auth, allowlist):
        monkeypatch.setattr(mod, "db", db)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
    monkeypatch.setattr(setup, "hash_password", lambda p: "hashed:" + p)
    monkeypatch.setattr(setup, "_get_client_ip", lambda request: "198.51.100.7")
    monkeypatch.setattr(setup, "check_login_rate_limit", lambda ip, account=None: None)
    monkeypatch.setattr(setup, "record_login_attempt", lambda ip, success, account=None: None)
    monkeypatch.setattr(setup, "_SETUP_CLAIM_PATH", tmp_path / "setup-claim")
    monkeypatch.setattr(auth, "is_setup_completed", lambda: db.settings["setup_completed"] == "true")
    monkeypatch.setattr(auth.login_policy_gate, "email_code_allowed", lambda: True)
    monkeypatch.setattr(auth.channel_login_limit, "address_allowed", lambda e: True)
    monkeypatch.setattr(email_service, "EmailService", _Mailer)
    return db, sent, setup, auth, tmp_path


def _setup_password(setup, email=EMAIL):
    monkey_env = setup.SetAdminPasswordRequest(password=PW, confirm_password=PW, email=email)
    return asyncio.run(setup.set_admin_password(monkey_env, object(), BackgroundTasks()))


def _setup_email_link(setup, tmp_path, monkeypatch, email=EMAIL):
    (tmp_path / "setup-claim").write_text(f"email-bound:{SECRET}\n")
    monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", "instance-id")
    code = hmac.new(bytes.fromhex(SECRET), email.encode(), hashlib.sha256).hexdigest()
    data = setup.SetAdminPasswordRequest(password=PW, confirm_password=PW, email=email,
                                         claim_code=code)
    return asyncio.run(setup.set_admin_password(data, object(), BackgroundTasks()))


def _request(auth, email):
    async def go():
        resp = await auth.request_email_login_code(_Req({"email": email}))
        await asyncio.sleep(0.05)  # let the fire-and-forget send run
        return resp
    return asyncio.run(go())


def test_password_setup_with_email_then_code_request_mints_and_sends(fake, monkeypatch):
    db, sent, setup, auth, _ = fake
    monkeypatch.delenv("ADMIN_PASSWORD_SOURCE", raising=False)
    assert _setup_password(setup)["email_registered"] is True

    assert _request(auth, EMAIL) == GENERIC
    assert db.codes == [EMAIL], "the admin's own address must get a code"
    assert sent == [EMAIL], "and the code must be dispatched"
    assert db.whitelist[EMAIL]["default_role"] == "user"


def test_email_link_setup_then_code_request_mints_and_sends(fake, monkeypatch):
    db, sent, setup, auth, tmp_path = fake
    assert _setup_email_link(setup, tmp_path, monkeypatch)["success"] is True

    assert _request(auth, EMAIL) == GENERIC
    assert db.codes == [EMAIL] and sent == [EMAIL]


def test_another_address_still_creates_nothing_with_the_same_body(fake, monkeypatch):
    db, sent, setup, auth, _ = fake
    monkeypatch.delenv("ADMIN_PASSWORD_SOURCE", raising=False)
    _setup_password(setup)

    assert _request(auth, "stranger@example.com") == GENERIC
    assert db.codes == [] and sent == []


def test_setup_without_email_allow_lists_nothing(fake, monkeypatch):
    db, _, setup, _, tmp_path = fake
    (tmp_path / "setup-claim").write_text("i-0123456789abcdef0\n")
    monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", "instance-id")
    data = setup.SetAdminPasswordRequest(password=PW, confirm_password=PW, email="",
                                         claim_code="i-0123456789abcdef0")
    asyncio.run(setup.set_admin_password(data, object(), BackgroundTasks()))
    assert db.whitelist == {}


def test_backfill_covers_an_install_set_up_before_the_fix(fake):
    db, sent, _, auth, _ = fake
    from services.admin_email_allowlist import backfill_admin_email_once

    # As a pre-fix setup left it: admin email set, no allow-list row.
    db.users["admin"]["email"] = EMAIL
    db.settings["setup_completed"] = "true"
    assert backfill_admin_email_once() is True
    assert backfill_admin_email_once() is False, "a second boot writes nothing"

    _request(auth, EMAIL)
    assert db.codes == [EMAIL] and sent == [EMAIL]


def test_backfill_does_not_overrule_an_operator_removal(fake):
    """Runs once per install: an operator who removes the admin's address from
    the allow-list is not overruled by the next restart."""
    db, *_ = fake
    from services.admin_email_allowlist import backfill_admin_email_once

    db.users["admin"]["email"] = EMAIL
    db.settings["setup_completed"] = "true"
    assert backfill_admin_email_once() is True
    del db.whitelist[EMAIL]  # DELETE /api/settings/email-whitelist/{email}
    assert backfill_admin_email_once() is False
    assert EMAIL not in db.whitelist, "a restart must not put the row back"


def test_setup_removal_is_not_overruled_by_the_first_boot_after_it(fake, monkeypatch):
    db, _, setup, _, _ = fake
    from services.admin_email_allowlist import backfill_admin_email_once

    monkeypatch.delenv("ADMIN_PASSWORD_SOURCE", raising=False)
    _setup_password(setup)
    del db.whitelist[EMAIL]
    backfill_admin_email_once()
    assert EMAIL not in db.whitelist


def test_backfill_marker_is_set_even_when_nothing_was_written(fake):
    db, *_ = fake
    from services.admin_email_allowlist import BACKFILL_MARKER, backfill_admin_email_once

    db.settings["setup_completed"] = "true"  # admin has no email: nothing to write
    assert backfill_admin_email_once() is False
    assert db.settings.get(BACKFILL_MARKER) == "true"

    db.users["admin"]["email"] = EMAIL  # bound later: /me/email owns that path
    assert backfill_admin_email_once() is False
    assert EMAIL not in db.whitelist


def test_backfill_runs_only_on_a_completed_setup(fake):
    db, *_ = fake
    from services.admin_email_allowlist import BACKFILL_MARKER, backfill_admin_email_once

    db.users["admin"]["email"] = EMAIL
    assert db.settings["setup_completed"] == "false"
    assert backfill_admin_email_once() is False
    assert db.whitelist == {}
    assert BACKFILL_MARKER not in db.settings, "an unfinished setup must not spend the one run"


def test_backfill_never_raises(fake, monkeypatch):
    db, *_ = fake
    from services.admin_email_allowlist import backfill_admin_email_once

    db.users["admin"]["email"] = EMAIL
    db.settings["setup_completed"] = "true"

    def boom(*a, **k):
        raise RuntimeError("unique constraint")
    monkeypatch.setattr(db, "add_to_whitelist", boom)
    assert backfill_admin_email_once() is False


def _calls_in(path: Path, func_name: str):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", None)
                    for c in ast.walk(node) if isinstance(c, ast.Call)}
    raise AssertionError(f"{func_name} not found in {path}")


def test_boot_runs_the_backfill():
    """Pins the boot wiring (importing main is too heavy for the unit island):
    the first lifespan phase calls the backfill, whose own setup_completed
    check is executed above."""
    assert "backfill_admin_email_once" in _calls_in(
        BACKEND / "main.py", "_init_logging_and_first_run_notice")


@pytest.fixture
def me_email(fake, monkeypatch):
    """PUT /api/users/me/email, driven through the real handler with a valid code."""
    import routers.users as users
    import routers.auth as auth

    db = fake[0]
    db.users["someone"] = {"username": "someone", "id": 2, "email": None}
    db.get_user_by_email = lambda e: next(
        (u for u in db.users.values() if (u.get("email") or "") == e), None)
    db.verify_login_code = lambda email, code, purpose=None: code == "123456"
    monkeypatch.setattr(users, "db", db)
    monkeypatch.setattr(auth, "check_otp_rate_limit", lambda scope: None)
    monkeypatch.setattr(auth, "record_otp_attempt", lambda scope, success: None)

    async def _log(**kw):
        return None
    monkeypatch.setattr(users.platform_audit_service, "log", _log)

    class _R:
        scope = {"path": "/api/users/me/email"}

        class state:
            request_id = None

    def change(username, role, email):
        from models import User
        user = User(id=db.users[username]["id"], username=username, role=role)
        body = users.UpdateMyEmailRequest(email=email, code="123456")
        return asyncio.run(users.update_my_email(body, _R(), user))
    return db, change


def test_the_admins_email_change_allow_lists_the_new_address(me_email):
    db, change = me_email
    assert change("admin", "admin", "new-owner@example.com")["success"] is True
    assert "new-owner@example.com" in db.whitelist


def test_another_users_email_change_allow_lists_nothing(me_email):
    """Only the setup admin's address joins the list; any other account's
    email change, even another admin's, adds nothing."""
    db, change = me_email
    assert change("someone", "admin", "someone@example.com")["success"] is True
    assert db.users["someone"]["email"] == "someone@example.com"
    assert db.whitelist == {}

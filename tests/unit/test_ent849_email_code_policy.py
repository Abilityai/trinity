"""ent#849 — the SSO "keep email-code sign-in available" policy is enforced.

With SSO entitled, a provider enabled and the box unchecked, an emailed 6-digit
code must not sign a platform user in on ANY surface. The decision lives in the
enterprise SSO module; OSS asks it through ``services/login_policy_gate``. These
tests register a stub provider, so they run in an OSS-only checkout.

Surfaces covered:
* web login        ``POST /api/auth/email/request`` + ``/verify``  → 403 for everyone
* MCP inline auth  ``mcp_auth_service``                            → no code, no redeem
* Workspace guest  ``client_portal.service.portal_signin_*``       → refused for members
* Telegram/WhatsApp ``/login``, Slack ``require_email``            → refused for members
* public-link email verification ``/api/public/verify/*``          → refused for members
* ``POST /api/token`` (admin break-glass) never consults the gate.

"Member" = a platform user or a whitelisted address. Outsiders (shared-agent
clients with no account) are not in the IdP and keep email-code sign-in.

Every refusal on a surface that hides membership is byte-identical to that
surface's existing failure, so the policy adds no enumeration oracle.
"""
import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


class _Policy:
    """Independent variable: the test sets `allowed`, nothing derives it."""

    def __init__(self, allowed=True, raises=False):
        self.allowed = allowed
        self.raises = raises
        self.calls = 0

    def email_code_allowed(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("policy store down")
        return self.allowed


@pytest.fixture
def policy():
    from services import login_policy_gate

    def _register(**kw):
        p = _Policy(**kw)
        login_policy_gate.register_provider(p)
        return p

    yield _register
    login_policy_gate.clear_provider()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ the gate

class TestGate:
    def test_no_provider_allows(self):
        from services import login_policy_gate
        login_policy_gate.clear_provider()
        assert login_policy_gate.email_code_allowed() is True

    def test_provider_decides(self, policy):
        from services import login_policy_gate
        policy(allowed=False)
        assert login_policy_gate.email_code_allowed() is False
        policy(allowed=True)
        assert login_policy_gate.email_code_allowed() is True

    def test_provider_error_fails_open(self, policy):
        from services import login_policy_gate
        p = policy(raises=True)
        assert login_policy_gate.email_code_allowed() is True
        assert p.calls == 1

    @pytest.mark.parametrize("user,whitelisted,expected", [
        (None, False, True),                     # outsider keeps codes
        ({"username": "m@example.com"}, False, False),  # platform user refused
        (None, True, False),                     # whitelisted, no account yet: refused
    ])
    def test_allowed_for_splits_members_from_outsiders(self, policy, user, whitelisted, expected):
        from services import login_policy_gate
        policy(allowed=False)
        db = MagicMock()
        db.get_user_by_email.return_value = user
        db.is_email_whitelisted.return_value = whitelisted
        with patch("database.db", db):
            assert login_policy_gate.email_code_allowed_for("M@example.com ") is expected

    def test_allowed_for_skips_lookup_when_policy_allows(self, policy):
        from services import login_policy_gate
        policy(allowed=True)
        db = MagicMock()
        with patch("database.db", db):
            assert login_policy_gate.email_code_allowed_for("m@example.com") is True
        db.get_user_by_email.assert_not_called()

    def test_allowed_for_lookup_error_fails_open(self, policy):
        from services import login_policy_gate
        policy(allowed=False)
        db = MagicMock()
        db.get_user_by_email.side_effect = RuntimeError("db down")
        with patch("database.db", db):
            assert login_policy_gate.email_code_allowed_for("m@example.com") is True


# ------------------------------------------------------------- web login

class _Reached(Exception):
    """Raised by the first step past the gate: proves the request got through."""


@pytest.fixture
def web(monkeypatch):
    import routers.auth as ra

    monkeypatch.setattr(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra.db, "get_setting_value",
                        lambda k, d=None: "true" if k == "email_auth_enabled" else d)

    def _past_gate(*a, **k):
        raise _Reached()

    # /request's first step after the gate is the whitelist read; /verify's is
    # the per-account rate limit. Either one firing means the gate let it pass.
    monkeypatch.setattr(ra.db, "is_email_whitelisted", _past_gate)
    monkeypatch.setattr(ra, "check_login_rate_limit", _past_gate)
    app = FastAPI()
    app.include_router(ra.router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.parametrize("path,body", [
    ("/api/auth/email/request", {"email": "member@example.com"}),
    ("/api/auth/email/verify", {"email": "member@example.com", "code": "123456"}),
])
class TestWebLogin:
    def test_denied_returns_403_before_any_lookup(self, web, policy, path, body):
        p = policy(allowed=False)
        r = web.post(path, json=body)
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "email_code_disabled"
        assert p.calls == 1

    def test_denied_body_is_identical_for_any_address(self, web, policy, path, body):
        policy(allowed=False)
        a = web.post(path, json=body)
        b = web.post(path, json={**body, "email": "nobody@elsewhere.test"})
        assert (a.status_code, a.content) == (b.status_code, b.content)

    def test_allowed_passes_the_gate(self, web, policy, path, body):
        policy(allowed=True)
        with pytest.raises(_Reached):
            web.post(path, json=body)


def test_auth_mode_reports_email_code_flag(policy, monkeypatch):
    import routers.auth as ra
    monkeypatch.setattr(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra.db, "get_setting_value",
                        lambda k, d=None: "true" if k == "email_auth_enabled" else d)
    app = FastAPI()
    app.include_router(ra.router)
    c = TestClient(app)

    policy(allowed=False)
    body = c.get("/api/auth/mode").json()
    assert body["email_auth_enabled"] is True
    assert body["email_code_login_enabled"] is False

    policy(allowed=True)
    assert c.get("/api/auth/mode").json()["email_code_login_enabled"] is True

    monkeypatch.setattr(ra.db, "get_setting_value",
                        lambda k, d=None: "false" if k == "email_auth_enabled" else d)
    assert c.get("/api/auth/mode").json()["email_code_login_enabled"] is False


def test_admin_token_never_consults_the_gate(policy, monkeypatch):
    """Break-glass: `/token` works while email-code sign-in is off."""
    import routers.auth as ra
    from services import mfa_gate

    mfa_gate.clear_provider()
    monkeypatch.setattr(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra, "check_login_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(ra, "record_login_attempt", lambda *a, **k: None)
    monkeypatch.setattr(ra, "authenticate_user",
                        lambda u, p: {"id": 1, "username": "admin", "role": "admin",
                                      "email": "admin@example.com"})
    monkeypatch.setattr(ra.db, "update_last_login", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(ra.platform_audit_service, "log", _noop)

    p = policy(allowed=False)
    app = FastAPI()
    app.include_router(ra.router)
    r = TestClient(app).post("/api/token", data={"username": "admin", "password": "pw"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    assert p.calls == 0


# --------------------------------------------------------- MCP inline auth

class TestMcpInline:
    def test_denied_mints_no_code(self, policy):
        from services import mcp_auth_service as svc
        policy(allowed=False)
        db = MagicMock()
        db.count_recent_code_requests.return_value = 0
        with patch.object(svc, "db", db), patch.object(svc, "_email_is_known", return_value=True):
            assert svc._resolve_and_create_code("member@example.com", "s1") is None
        db.create_login_code.assert_not_called()

    def test_denied_redeems_nothing(self, policy):
        from services import mcp_auth_service as svc
        policy(allowed=False)
        db = MagicMock()
        with patch.object(svc, "db", db):
            assert svc.verify_login_code("member@example.com", "123456") is None
        db.verify_login_code.assert_not_called()
        db.get_or_create_email_user.assert_not_called()

    def test_allowed_mints(self, policy):
        from services import mcp_auth_service as svc
        policy(allowed=True)
        db = MagicMock()
        db.count_recent_code_requests.return_value = 0
        db.create_login_code.return_value = {"code": "654321"}
        with patch.object(svc, "db", db), patch.object(svc, "_email_is_known", return_value=True):
            assert svc._resolve_and_create_code("member@example.com", "s1") == "654321"


# ----------------------------------------------------- Workspace guest sign-in

class TestWorkspace:
    def _db(self, member):
        db = MagicMock()
        db.get_user_by_email.return_value = {"username": "m"} if member else None
        db.is_email_whitelisted.return_value = False
        db.create_login_code.return_value = {"code": "111111"}
        db.verify_login_code.return_value = {"email": "x"}
        return db

    @pytest.mark.parametrize("member,expect_code", [(True, None), (False, "111111")])
    def test_request(self, policy, member, expect_code):
        from client_portal import service
        policy(allowed=False)
        db = self._db(member)
        with patch("database.db", db), patch.object(service, "email_has_access", return_value=True):
            assert service.portal_signin_request("p@example.com") == expect_code

    def test_verify_refuses_member_without_consuming_code(self, policy):
        from client_portal import service
        policy(allowed=False)
        db = self._db(member=True)
        with patch("database.db", db), patch.object(service, "email_has_access", return_value=True):
            assert service.portal_signin_verify("p@example.com", "111111") is None
        db.verify_login_code.assert_not_called()

    def test_verify_admits_outsider(self, policy):
        from client_portal import service
        policy(allowed=False)
        db = self._db(member=False)
        with patch("database.db", db), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            assert service.portal_signin_verify("p@example.com", "111111") == "tok"


# --------------------------------------------------------- Telegram / WhatsApp

def _message(text):
    from adapters.base import NormalizedMessage
    return NormalizedMessage(
        sender_id="u1", text=text, channel_id="c1", thread_id="1", timestamp="0", files=[],
        metadata={"bot_id": "b", "agent_name": "my-agent", "is_group": False,
                  "chat_type": "private", "username": "u"},
    )


@pytest.mark.parametrize("mod_name,cls_name", [
    ("adapters.telegram_adapter", "TelegramAdapter"),
    ("adapters.whatsapp_adapter", "WhatsAppAdapter"),
])
class TestChannels:
    def _adapter(self, mod_name, cls_name):
        import importlib
        mod = importlib.import_module(mod_name)
        return mod, getattr(mod, cls_name)()

    def _binding_getter(self, mod):
        return "get_telegram_binding" if "telegram" in mod.__name__ else "get_whatsapp_binding"

    def test_code_refused_for_member_like_a_wrong_code(self, policy, mod_name, cls_name):
        mod, adapter = self._adapter(mod_name, cls_name)
        policy(allowed=False)
        db = MagicMock()
        getattr(db, self._binding_getter(mod)).return_value = {"id": 1, "agent_name": "my-agent"}
        db.get_user_by_email.return_value = {"username": "m"}
        db.is_email_whitelisted.return_value = False
        with patch.object(mod, "db", db), patch("database.db", db), \
             patch.object(mod, "_get_pending_login", return_value="m@example.com"), \
             patch.object(mod, "_clear_pending_login"):
            reply = _run(adapter._handle_login_command(_message("/login 123456"), "/login 123456"))
        assert "Invalid or expired code" in reply
        db.verify_login_code.assert_not_called()

    def test_email_step_is_uniform_while_policy_refuses(self, policy, mod_name, cls_name):
        """Same reply, same (detached) path for member and outsider; only the
        outsider gets a code once the background send runs."""
        mod, adapter = self._adapter(mod_name, cls_name)
        policy(allowed=False)
        db = MagicMock()
        getattr(db, self._binding_getter(mod)).return_value = {"id": 1, "agent_name": "my-agent"}
        db.get_user_by_email.side_effect = lambda e: {"username": e} if e.startswith("m@") else None
        db.is_email_whitelisted.return_value = False
        db.create_login_code.return_value = {"code": "222222"}
        email_cls = MagicMock()
        email_cls.return_value.send_verification_code = MagicMock(
            side_effect=lambda *a, **k: asyncio.sleep(0, result=True))

        async def _both():
            m = await adapter._handle_login_command(_message("/login m@example.com"), "/login m@example.com")
            o = await adapter._handle_login_command(_message("/login o@example.com"), "/login o@example.com")
            minted_before_tasks = db.create_login_code.call_count
            await asyncio.gather(*list(mod._login_code_tasks))
            return m, o, minted_before_tasks

        with patch.object(mod, "db", db), patch("database.db", db), \
             patch.object(mod, "_set_pending_login"), \
             patch.object(mod, "EmailService", email_cls):
            member, outsider, minted_inline = _run(_both())
        assert minted_inline == 0, "the reply must not wait on the mint or the send"
        assert [c.args[0] for c in db.create_login_code.call_args_list] == ["o@example.com"]
        assert member.replace("m@example.com", "X") == outsider.replace("o@example.com", "X")


# ---------------------------------------------------------- Slack require_email

class TestSlackRequireEmail:
    def _setup(self, pending):
        import adapters.slack_adapter as mod
        db = MagicMock()
        db.get_slack_workspace_bot_token.return_value = "xoxb-test"
        db.get_slack_connection_by_team.return_value = {"require_email": True, "link_id": "L1"}
        db.get_slack_user_verification.return_value = None
        db.get_slack_pending_verification.return_value = pending
        db.get_user_by_email.side_effect = lambda e: {"username": e} if e.startswith("m@") else None
        db.is_email_whitelisted.return_value = False
        return mod, db

    def _msg(self, text):
        from adapters.base import NormalizedMessage
        return NormalizedMessage(
            sender_id="U1", text=text, channel_id="D1", thread_id=None, timestamp="0",
            files=[], metadata={"team_id": "T1"},
        )

    def test_email_step_is_uniform_while_policy_refuses(self, policy):
        mod, db = self._setup({"state": "awaiting_email"})
        policy(allowed=False)
        sent, emailed = [], []

        async def _send_message(token, channel, text, *a, **k):
            sent.append(text)

        async def _send_code(email, code, *a, **k):
            emailed.append(email)
            return True

        async def _both():
            adapter = mod.SlackAdapter()
            await adapter.handle_verification(self._msg("m@example.com"))
            await adapter.handle_verification(self._msg("o@example.com"))
            inline = len(emailed)
            await asyncio.gather(*list(mod._verification_code_tasks))
            return inline

        with patch.object(mod, "db", db), patch("database.db", db), \
             patch.object(mod.slack_service, "send_message", _send_message), \
             patch.object(mod.email_service, "send_verification_code", _send_code):
            inline = _run(_both())
        assert inline == 0, "the reply must not wait on the mint or the send"
        assert emailed == ["o@example.com"]
        assert sent[0].replace("m@example.com", "X") == sent[1].replace("o@example.com", "X")

    def test_code_refused_for_member_like_a_wrong_code(self, policy):
        mod, db = self._setup({"state": "awaiting_code", "email": "m@example.com", "code": "123456"})
        policy(allowed=False)
        sent = []

        async def _send_message(token, channel, text, *a, **k):
            sent.append(text)

        with patch.object(mod, "db", db), patch("database.db", db), \
             patch.object(mod.slack_service, "send_message", _send_message):
            ok = _run(mod.SlackAdapter().handle_verification(self._msg("123456")))
        assert ok is False
        assert "doesn't match" in sent[0]
        db.create_slack_user_verification.assert_not_called()

    def test_code_admits_outsider(self, policy):
        mod, db = self._setup({"state": "awaiting_code", "email": "o@example.com", "code": "123456"})
        policy(allowed=False)

        async def _send_message(*a, **k):
            return None

        with patch.object(mod, "db", db), patch("database.db", db), \
             patch.object(mod.slack_service, "send_message", _send_message):
            _run(mod.SlackAdapter().handle_verification(self._msg("123456")))
        db.create_slack_user_verification.assert_called_once()


# ------------------------------------------------------- public-link verification

class _PubReq:
    client = None
    headers = {}


@pytest.fixture
def public(monkeypatch):
    import routers.public as rp
    db = MagicMock()
    db.get_user_by_email.side_effect = lambda e: {"username": e} if e.startswith("m@") else None
    db.is_email_whitelisted.return_value = False
    db.count_recent_verification_requests.return_value = 0
    db.create_verification.return_value = {"code": "333333", "expires_in_seconds": 600}
    monkeypatch.setattr(rp, "db", db)
    monkeypatch.setattr(rp, "_get_client_ip", lambda r: "203.0.113.5")
    monkeypatch.setattr(rp, "check_public_link_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(rp, "check_login_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(rp, "record_login_attempt", lambda *a, **k: None)
    monkeypatch.setattr(rp, "_validate_public_link",
                        lambda t: {"id": "L1", "agent_name": "my-agent"})
    monkeypatch.setattr(rp.public_chat_service, "agent_requires_email", lambda a: True)

    async def _send(*a, **k):
        return True
    monkeypatch.setattr(rp.email_service, "send_verification_code", _send)
    return rp, db


class TestPublicLink:
    def test_request_is_uniform_while_policy_refuses(self, policy, public):
        from db_models import VerificationRequest
        rp, db = public
        policy(allowed=False)

        async def _both():
            m = await rp.request_verification_code(VerificationRequest(token="t", email="m@example.com"), _PubReq())
            o = await rp.request_verification_code(VerificationRequest(token="t", email="o@example.com"), _PubReq())
            inline = db.create_verification.call_count
            await asyncio.gather(*list(rp._verification_tasks))
            return m, o, inline

        with patch("database.db", db):
            member, outsider, inline = _run(_both())
        assert member == outsider
        assert inline == 0
        assert [c.kwargs["email"] for c in db.create_verification.call_args_list] == ["o@example.com"]

    def test_request_rate_limit_is_uniform_while_policy_refuses(self, policy, public, monkeypatch):
        """The per-email row count sees only addresses that got a code; the
        429 must still land on the same request for member and outsider."""
        from fastapi import HTTPException
        from db_models import VerificationRequest
        from services import rate_limiter
        rp, db = public
        policy(allowed=False)
        monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
        rate_limiter.clear_inprocess()
        db.count_recent_verification_requests.side_effect = lambda e, minutes=10: sum(
            1 for c in db.create_verification.call_args_list if c.kwargs["email"] == e)

        async def _statuses(email):
            out = []
            for _ in range(rp.MAX_VERIFICATION_REQUESTS_PER_EMAIL + 1):
                try:
                    await rp.request_verification_code(VerificationRequest(token="t", email=email), _PubReq())
                    out.append(200)
                except HTTPException as e:
                    out.append((e.status_code, e.detail))
                await asyncio.gather(*list(rp._verification_tasks))
            return out

        with patch("database.db", db):
            member = _run(_statuses("m@example.com"))
            outsider = _run(_statuses("o@example.com"))
        rate_limiter.clear_inprocess()
        assert member == outsider
        assert member[-1][0] == 429

    def test_confirm_refuses_member_like_a_wrong_code(self, policy, public):
        from db_models import VerificationConfirm
        rp, db = public
        policy(allowed=False)
        with patch("database.db", db):
            r = _run(rp.confirm_verification_code(
                VerificationConfirm(token="t", email="m@example.com", code="333333"), _PubReq()))
        assert r.verified is False and r.error == "invalid_code"
        db.verify_code.assert_not_called()

    def test_confirm_admits_outsider(self, policy, public):
        from db_models import VerificationConfirm
        rp, db = public
        policy(allowed=False)
        db.verify_code.return_value = (True, None, {"session_token": "s", "expires_at": "x"})
        with patch("database.db", db):
            r = _run(rp.confirm_verification_code(
                VerificationConfirm(token="t", email="o@example.com", code="333333"), _PubReq()))
        assert r.verified is True


# ------------------------------------------------------------- caller guard

_CODE_CALLS = {"create_login_code", "verify_login_code", "create_verification", "verify_code",
               "send_verification_code"}
_GATE_CALLS = {"email_code_allowed", "email_code_allowed_for"}

# Functions that mint, redeem or send an emailed code without asking the gate, each with why.
_EXEMPT = {
    # Binds an email to an ALREADY signed-in user; purpose-scoped code.
    ("routers/users.py", "request_email_bind_code"),
    ("routers/users.py", "update_my_email"),
    # Thin router over mcp_auth_service, whose functions hold the gate.
    ("routers/mcp_auth.py", "verify_inline_login"),
    # Deliver a code the enclosing gated function already minted.
    ("routers/auth.py", "_dispatch_code"),
    ("services/mcp_auth_service.py", "_dispatch_code_email"),
    # Sends what service.portal_signin_request returns; that function holds the gate.
    ("client_portal/router.py", "portal_auth_request"),
    ("client_portal/router.py", "_issue_and_send"),
    # `mfa_gate.verify_code` is the authenticator-app (TOTP) check on an already
    # signed-in admin changing their password (trinity-enterprise#709) — no emailed code.
    ("services/password_change_service.py", "check_second_factor"),
}


def _attr_calls(node):
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def test_every_login_code_caller_asks_the_gate():
    """Per function: a new sign-in path on the emailed-code tables must ask the policy."""
    offenders, seen = [], set()
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("db/", "tests/", "enterprise/")) or rel == "database.py":
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _attr_calls(fn)
            if not calls & _CODE_CALLS:
                continue
            seen.add(rel)
            if (rel, fn.name) not in _EXEMPT and not calls & _GATE_CALLS:
                offenders.append(f"{rel}::{fn.name}")
    # The scan is not blind: every known surface is found.
    for expected in ("routers/auth.py", "client_portal/service.py", "routers/public.py",
                     "adapters/telegram_adapter.py", "adapters/whatsapp_adapter.py",
                     "adapters/slack_adapter.py", "services/mcp_auth_service.py"):
        assert expected in seen, f"guard scan missed {expected}"
    assert not offenders, f"emailed-code sign-in without login_policy_gate: {offenders}"

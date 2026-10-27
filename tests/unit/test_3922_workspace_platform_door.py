"""#3922 — a platform account gets the same Workspace whichever door it uses.

The Workspace's own email-code page (`POST /api/enterprise/client-portal/auth/*`)
used to mint a portal session for ANY verified email, so a platform account that
signed in there was resolved as an outside client (`is_platform=False`): no
Projects link, none of its own agents on the roster, the narrower canvas and
activity view. Signed in at `/login`, the same account got the team view.

Ruling (option b, recorded on the issue): a platform account gets no portal
session. After its code verifies, the door answers 409 `platform_login_required`
and the Workspace sends the person to `/login`, the only door that mints a
platform session (2FA, allow-list, rate limits and audit stay there).

"Platform account" means the platform email login would admit the address:
email login on, the login policy allows codes, the address is allow-listed.
Anything else keeps the client door exactly as before, because sending someone
to a `/login` that cannot admit them is a dead end.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

PLATFORM = "member@example.com"
OUTSIDER = "client@example.com"


LEGACY = "legacy-client@example.com"   # allow-listed by a pre-ent#837 share, no account
ADMIN = "admin@example.com"            # an account with a password, not allow-listed
SSO = "sso-member@example.com"         # an account with no password, not allow-listed


def _db(*, whitelisted=(), users=(), passwords=(), email_auth="true", code_ok=True):
    """The allow-list (`whitelisted`), the user rows (`users`) and which rows
    carry a password (`passwords`) are independent: an allow-list row is not
    an account."""
    db = MagicMock()
    db.get_setting_value.return_value = email_auth
    db.is_email_whitelisted.side_effect = lambda e: e in whitelisted
    db.get_user_by_email.side_effect = lambda e: (
        {"username": e.split("@")[0], "password": "$2b$hash" if e in passwords else None}
        if e in users else None
    )
    db.create_login_code.return_value = {"code": "111111"}
    db.verify_login_code.return_value = {"email": "x"} if code_ok else None
    return db


@pytest.fixture(autouse=True)
def _no_policy():
    from services import login_policy_gate
    login_policy_gate.clear_provider()
    yield
    login_policy_gate.clear_provider()


# ------------------------------------------------------------ admission test

class TestPlatformLoginAdmits:
    def test_allow_listed_address_with_email_login_on(self):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})):
            assert service.platform_login_admits(PLATFORM) is True

    def test_allow_listed_without_an_account_is_not_a_platform_account(self):
        """A pre-ent#837 share left an allow-list row for an outside client.
        `/login` would CREATE a user for them, so they keep the client door."""
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM, LEGACY}, users={PLATFORM})):
            assert service.platform_login_admits(LEGACY) is False

    def test_an_account_with_a_password_signs_in_at_login(self):
        """The admin is not allow-listed but `/api/token` takes their email."""
        from client_portal import service
        with patch("database.db", _db(users={ADMIN}, passwords={ADMIN})):
            assert service.platform_login_admits(ADMIN) is True

    def test_an_account_with_a_password_signs_in_even_with_email_login_off(self):
        from client_portal import service
        with patch("database.db", _db(users={ADMIN}, passwords={ADMIN}, email_auth="false")):
            assert service.platform_login_admits(ADMIN) is True

    def test_an_account_with_no_way_in_at_login_keeps_the_client_door(self):
        """No password and no allow-list row: `/login`'s email code refuses it,
        so sending it there is a dead end (SSO is the private module's door)."""
        from client_portal import service
        with patch("database.db", _db(users={SSO})):
            assert service.platform_login_admits(SSO) is False

    def test_outsider_is_not_a_platform_account(self):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})):
            assert service.platform_login_admits(OUTSIDER) is False

    def test_email_login_off_keeps_the_client_door(self):
        """`/login` could not take this person by code, so it is not where to send them."""
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM}, email_auth="false")):
            assert service.platform_login_admits(PLATFORM) is False

    def test_policy_refusing_codes_keeps_the_client_door(self):
        from client_portal import service
        from services import login_policy_gate

        class _Off:
            def email_code_allowed(self):
                return False

        login_policy_gate.register_provider(_Off())
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})):
            assert service.platform_login_admits(PLATFORM) is False

    def test_lookup_error_falls_back_to_the_client_door(self):
        from client_portal import service
        db = _db()
        db.is_email_whitelisted.side_effect = RuntimeError("db down")
        with patch("database.db", db):
            assert service.platform_login_admits(PLATFORM) is False


# ------------------------------------------------------------ service layer

class TestRequest:
    def test_platform_account_with_no_shares_gets_a_code(self):
        """An owner with nothing shared used to get no code at all here."""
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=False):
            assert service.portal_signin_request(PLATFORM) == "111111"

    def test_outsider_without_a_share_still_gets_none(self):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=False):
            assert service.portal_signin_request(OUTSIDER) is None

    def test_outsider_with_a_share_unchanged(self):
        from client_portal import service
        with patch("database.db", _db()), \
             patch.object(service, "email_has_access", return_value=True):
            assert service.portal_signin_request(OUTSIDER) == "111111"


class TestVerify:
    def test_platform_account_gets_no_portal_session(self):
        from client_portal import service
        mint = MagicMock(return_value="tok")
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", mint):
            assert service.portal_signin_verify(PLATFORM, "111111") is service.PLATFORM_LOGIN_REQUIRED
        mint.assert_not_called()

    def test_platform_account_with_a_wrong_code_is_an_ordinary_failure(self):
        """The verdict comes only after the code verifies, so it is no oracle."""
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM}, code_ok=False)), \
             patch.object(service, "email_has_access", return_value=True):
            assert service.portal_signin_verify(PLATFORM, "000000") is None

    def test_outsider_still_gets_a_portal_session(self):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            assert service.portal_signin_verify(OUTSIDER, "111111") == "tok"

    def test_allow_listed_client_without_an_account_still_gets_a_portal_session(self):
        from client_portal import service
        mint = MagicMock(return_value="tok")
        with patch("database.db", _db(whitelisted={LEGACY})), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", mint):
            assert service.portal_signin_verify(LEGACY, "111111") == "tok"
        mint.assert_called_once()

    def test_email_login_off_platform_account_keeps_the_client_session(self):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM}, email_auth="false")), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            assert service.portal_signin_verify(PLATFORM, "111111") == "tok"


# ------------------------------------------------------------ the route

@pytest.fixture()
def route(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "t.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "t.db"))

    import fakeredis
    import routers.auth as oss_auth
    from services import rate_limiter
    from client_portal import router as portal_router

    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(oss_auth, "_redis_client", r, raising=False)
    monkeypatch.setattr(oss_auth, "get_redis_client", lambda: r)
    rl = fakeredis.FakeRedis()
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: rl)
    rate_limiter.clear_inprocess()
    yield portal_router
    rate_limiter.clear_inprocess()


class _Req:
    client = type("C", (), {"host": "203.0.113.7"})()


def _call_verify(portal_router, email, code="111111"):
    from client_portal.models import PortalAuthVerify
    return portal_router.portal_auth_verify(PortalAuthVerify(email=email, code=code), _Req())


class TestRoute:
    def test_platform_account_is_told_to_use_the_platform_login(self, route):
        from fastapi import HTTPException
        from client_portal import service
        mint = MagicMock(return_value="tok")
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", mint):
            with pytest.raises(HTTPException) as exc:
                _call_verify(route, PLATFORM)
        assert exc.value.status_code == 409
        assert exc.value.detail == {"reason": "platform_login_required"}
        mint.assert_not_called()

    def test_platform_account_wrong_code_is_the_uniform_401(self, route):
        from fastapi import HTTPException
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM}, code_ok=False)), \
             patch.object(service, "email_has_access", return_value=True):
            with pytest.raises(HTTPException) as exc:
                _call_verify(route, PLATFORM, code="000000")
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid or expired code"

    def test_outsider_gets_a_portal_session(self, route):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            session = _call_verify(route, OUTSIDER)
        assert session.token == "tok"
        assert session.email == OUTSIDER


# ------------------------------------------------------------ AC 5: the principal

class TestThePrincipalTheBackendResolves:
    """After the 409 the person signs in at `/login`; the Workspace door then
    resolves that platform session as `is_platform=True` (the team view), where
    a portal session for the same address resolves as an outside client."""

    @staticmethod
    def _resolve(token):
        from client_portal.portal_auth import get_portal_principal
        from starlette.requests import Request
        from starlette.responses import Response

        request = Request({
            "type": "http", "method": "GET", "path": "/api/portal/agents", "root_path": "",
            "query_string": b"", "scheme": "http", "server": ("backend", 8000),
            "headers": [(b"host", b"backend:8000")],
        })
        return asyncio.run(get_portal_principal(request, Response(), token=token))

    def test_a_platform_jwt_resolves_as_platform_and_a_portal_session_does_not(self, monkeypatch):
        import time
        import uuid
        import client_portal.db as portal_db
        from config import ALGORITHM, SECRET_KEY
        from database import db
        from dependencies import PORTAL_MINT_OTP, create_portal_session_token
        from jose import jwt

        monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)
        email = f"member-{uuid.uuid4().hex[:8]}@example.com"
        user = db.get_or_create_email_user(email)   # what `/login` leaves behind
        platform = jwt.encode({"sub": user["username"], "exp": int(time.time()) + 300},
                              SECRET_KEY, algorithm=ALGORITHM)

        principal = self._resolve(platform)
        assert principal.email == email
        assert principal.is_platform is True

        client = self._resolve(create_portal_session_token(email, minted_by=PORTAL_MINT_OTP))
        assert client.email == email
        assert client.is_platform is False

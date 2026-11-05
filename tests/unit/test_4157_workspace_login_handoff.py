"""#4157 — a platform account that starts at the Workspace sign-in needs ONE emailed code.

#3922 (option b) sends a platform account from the Workspace's code page to
`/login`, so 2FA, the allow-list, the login policy, rate limits and audit stay on
the platform door. As built by #3925 the first (correct) code was spent at the
portal door, and `/login` then asked for a second one.

Decision recorded on #4157: a one-time hand-off credential. After a CORRECT
portal code for a platform account the portal still answers 409
`platform_login_required`, and the body now also carries an opaque, single-use,
short-lived token bound to the email (Redis, stored hashed). `/login` redeems it
at `POST /api/auth/email/handoff`, which runs the platform email login's own
checks minus the code check. A wrong code stays the uniform 401 (#186).

Pinned here:
  * the token store: hashed at rest, single-use, email-bound, short TTL;
  * the portal route: 409 + token only after a correct code; 401 unchanged;
    no token for a password-only account the email door would refuse;
  * the hand-off route: allow-list, login policy, rate limits, audit, 2FA;
  * the real flow on a real database: ONE minted code, used once, and neither
    the code nor the token in any log line.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

PLATFORM = "member@example.com"
OUTSIDER = "client@example.com"
ADMIN = "admin@example.com"


@pytest.fixture(autouse=True)
def _clean_gates():
    from services import login_policy_gate, mfa_gate
    login_policy_gate.clear_provider()
    prev = mfa_gate._provider
    mfa_gate._provider = None
    yield
    login_policy_gate.clear_provider()
    mfa_gate._provider = prev


@pytest.fixture()
def redis(monkeypatch):
    import fakeredis
    import routers.auth as oss_auth
    from services import rate_limiter
    # Import the portal router OUTSIDE any `patch("database.db", …)`: a first
    # import inside one would bind the MagicMock as its module-level `db`.
    from client_portal import router as _portal_router  # noqa: F401

    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(oss_auth, "_redis_client", r, raising=False)
    monkeypatch.setattr(oss_auth, "get_redis_client", lambda: r)
    rl = fakeredis.FakeRedis()
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: rl)
    rate_limiter.clear_inprocess()
    yield r
    rate_limiter.clear_inprocess()


def _db(*, whitelisted=(), users=(), passwords=(), email_auth="true", code_ok=True):
    db = MagicMock()
    db.get_setting_value.side_effect = lambda key, default=None: (
        email_auth if key == "email_auth_enabled" else ("true" if key == "setup_completed" else default)
    )
    db.is_email_whitelisted.side_effect = lambda e: e in whitelisted
    db.get_user_by_email.side_effect = lambda e: (
        {"username": e.split("@")[0], "password": "$2b$hash" if e in passwords else None}
        if e in users else None
    )
    db.create_login_code.return_value = {"code": "111111"}
    db.verify_login_code.return_value = {"email": "x"} if code_ok else None
    db.get_or_create_email_user.side_effect = lambda e: {
        "username": e.split("@")[0], "email": e, "role": "user", "name": None, "picture": None,
    }
    return db


# ------------------------------------------------------------ the token store

class TestHandoffStore:
    def test_mint_stores_only_a_hash_bound_to_the_email(self, redis):
        from services import login_handoff_service as h
        token = h.mint(PLATFORM)
        assert token and len(token) >= 32
        keys = redis.keys("*")
        assert keys == [f"login_handoff:{hashlib.sha256(token.encode()).hexdigest()}"]
        assert token not in keys[0]
        assert token not in (redis.get(keys[0]) or "")
        assert 0 < redis.ttl(keys[0]) <= h.HANDOFF_TTL_SECONDS <= 300

    def test_redeem_is_single_use(self, redis):
        from services import login_handoff_service as h
        token = h.mint(PLATFORM)
        assert h.redeem(token, PLATFORM) is True
        assert h.redeem(token, PLATFORM) is False

    def test_redeem_for_a_different_address_is_refused_and_spends_it(self, redis):
        from services import login_handoff_service as h
        token = h.mint(PLATFORM)
        assert h.redeem(token, OUTSIDER) is False
        assert h.redeem(token, PLATFORM) is False

    def test_an_expired_token_is_refused(self, redis, monkeypatch):
        from services import login_handoff_service as h
        monkeypatch.setattr(h, "HANDOFF_TTL_SECONDS", 1)
        token = h.mint(PLATFORM)
        time.sleep(1.2)
        assert h.redeem(token, PLATFORM) is False

    def test_garbage_and_empty_are_refused(self, redis):
        from services import login_handoff_service as h
        assert h.redeem("", PLATFORM) is False
        assert h.redeem("not-a-token", PLATFORM) is False

    def test_no_redis_mints_nothing_and_redeems_nothing(self, monkeypatch):
        import routers.auth as oss_auth
        from services import login_handoff_service as h
        monkeypatch.setattr(oss_auth, "get_redis_client", lambda: None)
        assert h.mint(PLATFORM) is None
        assert h.redeem("anything", PLATFORM) is False


# ------------------------------------------------------------ the portal route

class _PReq:
    client = type("C", (), {"host": "203.0.113.7"})()


def _portal_verify(email, code="111111"):
    from client_portal import router as portal_router
    from client_portal.models import PortalAuthVerify
    return portal_router.portal_auth_verify(PortalAuthVerify(email=email, code=code), _PReq())


class TestPortalRoute:
    def test_correct_code_for_a_platform_account_carries_a_handoff(self, redis):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=True):
            with pytest.raises(HTTPException) as exc:
                _portal_verify(PLATFORM)
        assert exc.value.status_code == 409
        assert exc.value.detail["reason"] == "platform_login_required"
        token = exc.value.detail["handoff"]
        assert isinstance(token, str) and len(token) >= 32
        from services import login_handoff_service as h
        assert h.redeem(token, PLATFORM) is True

    def test_wrong_code_is_the_uniform_401_and_mints_no_token(self, redis):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM}, code_ok=False)), \
             patch.object(service, "email_has_access", return_value=True):
            with pytest.raises(HTTPException) as exc:
                _portal_verify(PLATFORM, code="000000")
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid or expired code"
        assert redis.keys("login_handoff:*") == []

    def test_wrong_portal_code_spends_the_platform_budget_of_every_address(self, redis):
        """A correct portal code now leads to a platform session, so portal
        guesses count where `/login`'s guesses count — for an outsider exactly
        as for an account, or the buckets tell them apart."""
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM}, code_ok=False)), \
             patch.object(service, "email_has_access", return_value=True):
            for who in (PLATFORM, OUTSIDER):
                with pytest.raises(HTTPException):
                    _portal_verify(who, code="000000")
        for who in (PLATFORM, OUTSIDER):
            assert redis.get(f"otp_attempts:{who}") == "1", who
            assert redis.get(f"login_attempts_acct:{who}") == "1", who

    @pytest.mark.parametrize("account", [PLATFORM, ADMIN, "outsider"])
    def test_portal_guesses_are_no_account_oracle_at_the_platform_routes(self, redis, account):
        """The reviewer's probe: exhaust the portal's OTP cap, then ask the
        platform routes. Whatever the address is — an email-login account, a
        password account that is not allow-listed, or nobody — the platform
        OTP and per-account buckets read the same."""
        from client_portal import service
        from routers import auth as oss_auth
        who = OUTSIDER if account == "outsider" else account
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM, ADMIN},
                                      passwords={ADMIN}, code_ok=False)), \
             patch.object(service, "email_has_access", return_value=True):
            for _ in range(oss_auth.OTP_MAX_ATTEMPTS):
                with pytest.raises(HTTPException):
                    _portal_verify(who, code="000000")
        with pytest.raises(HTTPException) as otp:
            oss_auth.check_otp_rate_limit(who)
        assert otp.value.status_code == 429
        assert redis.get(f"login_attempts_acct:{who}") == str(oss_auth.OTP_MAX_ATTEMPTS)

    def test_password_only_account_with_email_login_off_gets_no_token(self, redis):
        """`/login` cannot take this address by email, so a token is useless;
        the person signs in with their password, as before."""
        from client_portal import service
        with patch("database.db", _db(users={ADMIN}, passwords={ADMIN}, email_auth="false")), \
             patch.object(service, "email_has_access", return_value=True):
            with pytest.raises(HTTPException) as exc:
                _portal_verify(ADMIN)
        assert exc.value.status_code == 409
        assert exc.value.detail == {"reason": "platform_login_required"}
        assert redis.keys("login_handoff:*") == []

    def test_outsider_keeps_the_client_session_and_no_token(self, redis):
        from client_portal import service
        with patch("database.db", _db(whitelisted={PLATFORM}, users={PLATFORM})), \
             patch.object(service, "email_has_access", return_value=True), \
             patch("dependencies.create_portal_session_token", return_value="tok"):
            session = _portal_verify(OUTSIDER)
        assert session.token == "tok"
        assert redis.keys("login_handoff:*") == []


# ------------------------------------------------------------ the hand-off route

class _HReq:
    def __init__(self, email, token):
        self._body = {"email": email, "token": token}
        self.client = SimpleNamespace(host="203.0.113.9")
        self.scope = {"path": "/api/auth/email/handoff"}
        self.state = SimpleNamespace(request_id="req-1")

    async def json(self):
        return self._body


@pytest.fixture()
def handoff(redis, monkeypatch):
    import routers.auth as auth
    monkeypatch.setattr(auth, "is_setup_completed", lambda: True)
    calls = SimpleNamespace(rate=[], otp=[], attempts=[], audits=[])
    monkeypatch.setattr(auth, "check_login_rate_limit",
                        lambda ip, account=None: calls.rate.append((ip, account)))
    monkeypatch.setattr(auth, "check_otp_rate_limit", lambda e: calls.otp.append(e))
    monkeypatch.setattr(auth, "record_login_attempt",
                        lambda ip, success, account=None: calls.attempts.append(("login", success, account)))
    monkeypatch.setattr(auth, "record_otp_attempt",
                        lambda e, success: calls.attempts.append(("otp", success, e)))

    async def _log(**kw):
        calls.audits.append(kw)

    monkeypatch.setattr(auth.platform_audit_service, "log", _log)

    def _use(db):
        monkeypatch.setattr(auth, "db", db)
        return db

    calls.use = _use
    return calls


def _redeem(email, token):
    import routers.auth as auth
    return asyncio.run(auth.redeem_email_login_handoff(_HReq(email, token)))


class TestHandoffRoute:
    def test_redeems_into_a_platform_session_with_platform_checks(self, handoff):
        from services import login_handoff_service as h
        handoff.use(_db(whitelisted={PLATFORM}, users={PLATFORM}))
        token = h.mint(PLATFORM)
        resp = _redeem(PLATFORM, token)
        assert resp.access_token
        assert resp.user["email"] == PLATFORM
        assert handoff.rate == [("203.0.113.9", PLATFORM)]
        assert handoff.otp == [PLATFORM]
        assert ("login", True, PLATFORM) in handoff.attempts
        assert [a["event_action"] for a in handoff.audits] == ["login_success"]
        assert handoff.audits[0]["details"]["method"] == "email_handoff"
        assert token not in repr(handoff.audits)

    def test_second_redemption_is_the_uniform_401(self, handoff):
        from services import login_handoff_service as h
        handoff.use(_db(whitelisted={PLATFORM}, users={PLATFORM}))
        token = h.mint(PLATFORM)
        _redeem(PLATFORM, token)
        with pytest.raises(HTTPException) as exc:
            _redeem(PLATFORM, token)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid or expired verification code"
        assert handoff.audits[-1]["event_action"] == "login_failed"
        assert ("otp", False, PLATFORM) in handoff.attempts

    def test_not_allow_listed_is_refused_and_does_not_spend_the_token(self, handoff):
        """The #2381 re-check runs here too, ahead of the redemption."""
        from services import login_handoff_service as h
        db = handoff.use(_db(whitelisted=set(), users={PLATFORM}))
        token = h.mint(PLATFORM)
        with pytest.raises(HTTPException) as exc:
            _redeem(PLATFORM, token)
        assert exc.value.status_code == 401
        db.get_or_create_email_user.assert_not_called()
        assert h.redeem(token, PLATFORM) is True

    def test_email_login_off_is_refused(self, handoff):
        from services import login_handoff_service as h
        handoff.use(_db(whitelisted={PLATFORM}, users={PLATFORM}, email_auth="false"))
        with pytest.raises(HTTPException) as exc:
            _redeem(PLATFORM, h.mint(PLATFORM))
        assert exc.value.status_code == 403

    def test_login_policy_refusing_codes_is_refused(self, handoff):
        from services import login_handoff_service as h
        from services import login_policy_gate

        class _Off:
            def email_code_allowed(self):
                return False

        handoff.use(_db(whitelisted={PLATFORM}, users={PLATFORM}))
        token = h.mint(PLATFORM)
        login_policy_gate.register_provider(_Off())
        with pytest.raises(HTTPException) as exc:
            _redeem(PLATFORM, token)
        assert exc.value.status_code == 403
        assert exc.value.detail == "email_code_disabled"

    def test_rate_limited_account_is_refused_before_redemption(self, handoff, monkeypatch):
        import routers.auth as auth
        from services import login_handoff_service as h
        handoff.use(_db(whitelisted={PLATFORM}, users={PLATFORM}))
        token = h.mint(PLATFORM)

        def _limited(ip, account=None):
            raise HTTPException(status_code=429, detail="Too many login attempts")

        monkeypatch.setattr(auth, "check_login_rate_limit", _limited)
        with pytest.raises(HTTPException) as exc:
            _redeem(PLATFORM, token)
        assert exc.value.status_code == 429
        assert h.redeem(token, PLATFORM) is True

    def test_an_account_with_2fa_still_gets_its_2fa_step(self, handoff):
        from services import login_handoff_service as h
        from services import mfa_gate

        class _TwoFactor:
            def gate_decision(self, user):
                return {"enrolled": True, "required": True}

        mfa_gate._provider = _TwoFactor()
        handoff.use(_db(whitelisted={PLATFORM}, users={PLATFORM}))
        resp = _redeem(PLATFORM, h.mint(PLATFORM))
        assert isinstance(resp, dict) and resp["mfa_required"] is True
        assert "access_token" not in resp
        assert [a["event_action"] for a in handoff.audits] == ["mfa_challenge_issued"]


# ------------------------------------------------------------ the real flow

class TestRealFlowOneCode:
    """Real database, real code mint, real portal route, real hand-off route."""

    def test_one_minted_code_used_once_and_nothing_secret_logged(self, redis, monkeypatch, caplog):
        import uuid
        import routers.auth as auth
        from sqlalchemy import select
        from client_portal import service
        from database import db
        from db.engine import get_engine
        from db.tables import email_login_codes

        email = f"member-{uuid.uuid4().hex[:8]}@example.com"
        db.set_setting("setup_completed", "true")
        db.set_setting("email_auth_enabled", "true")
        member = db.get_or_create_email_user(email)
        db.add_to_whitelist(email, member["username"], "manual", default_role="user")

        async def _log(**kw):
            pass

        monkeypatch.setattr(auth.platform_audit_service, "log", _log)
        monkeypatch.setattr(service, "email_has_access", lambda e: False)

        caplog.set_level(logging.DEBUG)
        code = service.portal_signin_request(email)
        assert code

        with pytest.raises(HTTPException) as exc:
            _portal_verify(email, code)
        assert exc.value.status_code == 409
        token = exc.value.detail["handoff"]

        resp = _redeem(email, token)
        assert resp.access_token
        assert resp.user["email"] == email

        with get_engine().connect() as conn:
            rows = conn.execute(
                select(email_login_codes.c.code, email_login_codes.c.verified)
                .where(email_login_codes.c.email == email)
            ).all()
        assert len(rows) == 1, "exactly ONE code is minted for this path"
        assert rows[0].code == code and rows[0].verified == 1

        with pytest.raises(HTTPException) as again:
            _redeem(email, token)
        assert again.value.status_code == 401

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert code not in logged
        assert token not in logged


# ------------------------------------------------- one code, one verification

class TestVerifyLoginCodeIsSingleUse:
    """Review of #4157: `verify_login_code` read the row, then marked it used
    with no `verified = 0` guard, so two concurrent verifies on PostgreSQL could
    both pass and mint two hand-off tokens from one code. The UPDATE is now
    conditional and its rowcount decides."""

    def _run(self, monkeypatch, rowcount):
        from datetime import datetime, timedelta
        from db import email_auth

        row = {"id": 7, "code": "123456", "verified": 0,
               "expires_at": (datetime.utcnow() + timedelta(minutes=5)).isoformat()}
        statements = []

        class _Result:
            def __init__(self, rc):
                self.rowcount = rc

            def mappings(self):
                return self

            def first(self):
                return row

        class _Conn:
            def execute(self, stmt):
                statements.append(stmt)
                return _Result(rowcount if len(statements) == 2 else None)

        class _Begin:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *exc):
                return False

        class _Engine:
            def begin(self):
                return _Begin()

        monkeypatch.setattr(email_auth, "get_engine", lambda: _Engine())
        ops = email_auth.EmailAuthOperations.__new__(email_auth.EmailAuthOperations)
        return ops.verify_login_code("a@example.com", "123456"), statements

    def test_the_verify_that_loses_the_race_is_an_invalid_code(self, monkeypatch):
        result, _ = self._run(monkeypatch, rowcount=0)
        assert result is None

    def test_the_winner_verifies_and_the_update_is_conditional(self, monkeypatch):
        result, statements = self._run(monkeypatch, rowcount=1)
        assert result and result["verified"] is True
        where = str(statements[1].compile(compile_kwargs={"literal_binds": True}))
        assert "verified = 0" in where, where

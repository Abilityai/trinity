"""Portal sign-in must carry the same brute-force / enumeration protections as
OSS email auth (ent#311, ent#309).

The Client Portal reimplements email-code sign-in and — until this change —
inherited none of `routers/auth.py`'s protections:

  * `/auth/verify` had NO OTP attempt cap. OSS invalidates a 6-digit code after
    `OTP_MAX_ATTEMPTS=5` wrong tries (pentest 3.1.5); the portal let you guess
    forever. Verified live: 60 wrong codes, 60x 401, then the real code still
    minted a session.
  * `/auth/request` had NO throttle. 30 requests minted 30 simultaneously-valid
    codes for one address, shrinking the guess space, and let an attacker sample
    the ent#309 timing oracle without limit.

The fix routes both through the SAME OSS primitives (`check_otp_rate_limit` /
`record_otp_attempt` / `check_login_rate_limit`) plus the shared
`services/rate_limiter` for the request throttle — one source of truth, not a
parallel reimplementation that can drift.

What is pinned here:
  * a run of wrong OTPs stops returning 401 and starts returning 429 at the OSS
    cap — i.e. the code is no longer brute-forceable
  * a correct OTP after some failures still works (the cap must not lock out the
    legitimate client who fat-fingered a digit)
  * success clears the counter (no residual lockout)
  * the request endpoint 429s a flood, per-email and per-IP
  * the timing fix: the access-check + code mint no longer run on the
    synchronous request path (ent#309)
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Portal sign-in wired to fakeredis (both the enterprise `rate_limiter` and
    the OSS `routers.auth` failure-counters), with a stubbed service so no DB or
    email is needed. Real limiter code runs — this is not a mock of the limits."""
    # Keep the OSS import chain off the container's /data path (nothing here
    # touches the DB — the verify service is stubbed).
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "t.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "t.db"))

    import fakeredis
    import routers.auth as oss_auth
    from services import rate_limiter
    from client_portal import router as portal_router
    from client_portal import service

    r = fakeredis.FakeRedis(decode_responses=True)
    # OSS otp/login limiters read their own cached client.
    monkeypatch.setattr(oss_auth, "_redis_client", r, raising=False)
    monkeypatch.setattr(oss_auth, "get_redis_client", lambda: r)
    # Enterprise request-throttle reads the breaker-redis client. ONE instance,
    # not a fresh one per call — a new client each time would give every request
    # an empty window and the throttle would never accumulate.
    rl = fakeredis.FakeRedis()
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: rl)
    rate_limiter.clear_inprocess()

    # The email "bob@example.com" with the right code verifies; everything else
    # fails. No DB, no email dispatch.
    def _verify(email, code):
        return "TOKEN" if (email == "bob@example.com" and code == "424242") else None

    monkeypatch.setattr(service, "portal_signin_verify", _verify)
    yield portal_router, oss_auth
    rate_limiter.clear_inprocess()


class _Req:
    def __init__(self, ip="203.0.113.7"):
        self.client = type("C", (), {"host": ip})()


def _verify(portal_router, code, ip="203.0.113.7", email="bob@example.com"):
    from client_portal.models import PortalAuthVerify
    return portal_router.portal_auth_verify(PortalAuthVerify(email=email, code=code), _Req(ip))


def _request(portal_router, email="bob@example.com", ip="203.0.113.7"):
    from client_portal.models import PortalAuthRequest
    return asyncio.run(
        portal_router.portal_auth_request(PortalAuthRequest(email=email), _Req(ip))
    )


def _status(exc):
    return exc.value.status_code


# ---------------------------------------------------------------------------
# /auth/verify — the OTP brute-force cap (the serious one)
# ---------------------------------------------------------------------------

def test_wrong_otps_stop_being_guessable_at_the_oss_cap(env):
    from fastapi import HTTPException

    portal_router, oss_auth = env
    cap = oss_auth.OTP_MAX_ATTEMPTS

    # The first `cap` wrong guesses are 401 (wrong), then the code is locked: 429.
    for i in range(cap):
        with pytest.raises(HTTPException) as exc:
            _verify(portal_router, f"{i:06d}")
        assert _status(exc) == 401, f"guess {i} should be a plain wrong-code 401"

    with pytest.raises(HTTPException) as exc:
        _verify(portal_router, "999999")
    assert _status(exc) == 429, "past the cap the code must be locked, not endlessly guessable"


def test_the_real_code_is_useless_once_the_cap_is_hit(env):
    """The whole point: an attacker who exhausts the cap cannot then land the
    correct value. Proven by having the REAL code rejected with 429 after the
    lockout, not 200."""
    from fastapi import HTTPException

    portal_router, oss_auth = env
    for i in range(oss_auth.OTP_MAX_ATTEMPTS):
        with pytest.raises(HTTPException):
            _verify(portal_router, f"{i:06d}")

    with pytest.raises(HTTPException) as exc:
        _verify(portal_router, "424242")  # the correct code
    assert _status(exc) == 429, "the code must be locked even for its correct value"


def test_a_correct_code_after_a_few_typos_still_works(env):
    """The cap must not punish the legitimate client who mistyped once or twice."""
    from fastapi import HTTPException

    portal_router, _ = env
    for _ in range(3):
        with pytest.raises(HTTPException):
            _verify(portal_router, "000000")
    out = _verify(portal_router, "424242")
    assert out.token == "TOKEN"


def test_success_clears_the_counter(env):
    """A prior good login must not leave a residual lockout for the next one."""
    from fastapi import HTTPException

    portal_router, oss_auth = env
    with pytest.raises(HTTPException):
        _verify(portal_router, "000000")
    assert _verify(portal_router, "424242").token == "TOKEN"

    # Fresh budget afterwards: cap-1 more failures are still 401, not 429.
    for _ in range(oss_auth.OTP_MAX_ATTEMPTS - 1):
        with pytest.raises(HTTPException) as exc:
            _verify(portal_router, "111111")
        assert _status(exc) == 401


def test_the_cap_is_per_email(env):
    """One victim's exhausted budget must not lock out another client."""
    from fastapi import HTTPException

    portal_router, oss_auth = env
    for i in range(oss_auth.OTP_MAX_ATTEMPTS + 1):
        with pytest.raises(HTTPException):
            _verify(portal_router, f"{i:06d}", email="victim@example.com")

    # A different email is unaffected — still a plain 401, not a 429.
    with pytest.raises(HTTPException) as exc:
        _verify(portal_router, "000000", email="other@example.com")
    assert _status(exc) == 401


def test_portal_otp_failures_do_not_touch_the_platform_login_buckets(env):
    """Regression guard for the cross-surface lockout (review of this PR).

    The OSS `check_otp_rate_limit`/`check_login_rate_limit` build their keys from
    the identifier passed in; the platform email path passes the bare email. If
    the portal reused the bare email, 5 wrong portal codes would lock the victim
    out of PLATFORM sign-in. The portal must write only `portal:`-scoped keys and
    leave `otp_attempts:{email}` / `login_attempts_acct:{email}` untouched.
    """
    from fastapi import HTTPException

    portal_router, oss_auth = env
    r = oss_auth.get_redis_client()
    victim = "victim@example.com"

    # Exhaust the portal OTP cap for the victim.
    for i in range(oss_auth.OTP_MAX_ATTEMPTS + 1):
        with pytest.raises(HTTPException):
            _verify(portal_router, f"{i:06d}", email=victim)

    # The portal bucket exists and is tripped...
    assert r.get(f"otp_attempts:portal:{victim}") is not None
    # ...but the PLATFORM buckets the real email-login path reads are untouched.
    assert r.get(f"otp_attempts:{victim}") is None, (
        "portal OTP failures leaked into the platform OTP bucket — cross-surface lockout"
    )
    assert r.get(f"login_attempts_acct:{victim}") is None, (
        "portal failures leaked into the platform per-account login bucket"
    )


# ---------------------------------------------------------------------------
# /auth/request — throttle + the ent#309 timing fix
# ---------------------------------------------------------------------------

def test_request_flood_is_throttled_per_email(env):
    from fastapi import HTTPException

    portal_router, _ = env
    # The per-email cap is small; a flood at one address trips it.
    tripped = False
    for _ in range(12):
        try:
            _request(portal_router, email="target@example.com")
        except HTTPException as exc:
            assert exc.status_code == 429
            tripped = True
            break
    assert tripped, "an unbounded code-request flood is exactly the ent#311 finding"


def test_request_flood_is_throttled_per_ip_across_emails(env):
    """The per-email cap alone lets one host sweep many addresses; the per-IP cap
    on /auth/request is what stops that (the enumeration / timing-sample vector).
    This bucket is a portal-only `rate_limiter` key, isolated from platform."""
    from fastapi import HTTPException

    portal_router, _ = env
    tripped = False
    for i in range(40):
        try:
            _request(portal_router, email=f"sweep-{i}@example.com", ip="198.51.100.9")
        except HTTPException as exc:
            assert exc.status_code == 429
            tripped = True
            break
    assert tripped, "one IP sweeping distinct addresses must hit the per-IP cap"


def test_verify_has_an_isolated_per_ip_cap(env):
    """The verify per-IP throttle is a portal `rate_limiter` key, not the OSS
    login IP bucket — so a portal brute-forcer cannot lock platform logins from
    a shared egress IP."""
    from fastapi import HTTPException

    portal_router, oss_auth = env
    # Hammer verify from one IP across many emails (each below its own OTP cap).
    tripped = False
    for i in range(80):
        try:
            _verify(portal_router, "000000", email=f"v-{i}@example.com", ip="198.51.100.42")
        except HTTPException as exc:
            if exc.status_code == 429:
                tripped = True
                break
    assert tripped, "verify must have a per-IP abuse cap"
    # And it did not touch the platform per-IP login bucket.
    r = oss_auth.get_redis_client()
    assert r.get("login_attempts_ip:198.51.100.42") is None


def test_request_does_not_touch_the_service_on_the_sync_path(env, monkeypatch):
    """ent#309 timing fix: the access-check + code mint must run in the
    background task, NOT before the response — that DB-write difference was the
    side channel distinguishing a client from a stranger.

    Asserted structurally: `portal_signin_request` is not called during the
    synchronous handler; it is called only when the spawned task runs.
    """
    from client_portal import service

    calls = []
    monkeypatch.setattr(service, "portal_signin_request",
                        lambda addr: calls.append(addr) or None)

    async def _drive():
        from client_portal.models import PortalAuthRequest
        portal_router, _ = env
        await portal_router.portal_auth_request(
            PortalAuthRequest(email="bob@example.com"), _Req())
        # Synchronous handler has returned; the access check must not have run yet.
        assert calls == [], "access check ran on the measured path — the oracle is back"
        # Let the fire-and-forget task run; now it should have.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == ["bob@example.com"]

    asyncio.run(_drive())


def test_request_still_returns_the_generic_body(env):
    """The throttle must not change the anti-enumeration contract (#186)."""
    portal_router, _ = env
    out = _request(portal_router, email="anyone@example.com")
    assert out["success"] is True
    assert "access" in out["message"].lower()

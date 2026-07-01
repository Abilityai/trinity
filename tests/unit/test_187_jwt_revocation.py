"""
JWT revocation on logout (#187 — UnderDefense pentest 3.3.4).

Before #187 a JWT was valid for its full 7-day life with no server-side
revocation: an exfiltrated token kept working through logout / password change.
#187 adds a per-token `jti` and a Redis blacklist keyed on it, stamped by
`POST /api/auth/logout` until the token's own expiry. These tests pin:

  - every minted access token carries a `jti`;
  - revoke → the jti reads back as revoked;
  - the blacklist TTL never outlives the token (past-exp revoke is a no-op);
  - fail-open: no jti (legacy token) and Redis-down both read as NOT revoked,
    so the check can never lock out a legitimate session.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

import dependencies  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: r)
    return r


def _exp_in(seconds: int) -> int:
    return int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())


def test_minted_token_carries_jti():
    from jose import jwt
    from config import SECRET_KEY, ALGORITHM

    token = dependencies.create_access_token({"sub": "alice"}, timedelta(minutes=5))
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert payload.get("jti")  # non-empty per-token id


def test_revoke_then_revoked(fake_redis):
    jti = "tok-abc"
    assert dependencies.is_token_revoked(jti) is False
    dependencies.revoke_token_jti(jti, _exp_in(3600))
    assert dependencies.is_token_revoked(jti) is True


def test_revocation_ttl_bounded_by_token_expiry(fake_redis):
    """The blacklist key self-expires no later than the token would."""
    jti = "tok-ttl"
    dependencies.revoke_token_jti(jti, _exp_in(120))
    ttl = fake_redis.ttl(f"{dependencies._JWT_REVOKED_PREFIX}{jti}")
    assert 0 < ttl <= 120


def test_already_expired_revoke_is_noop(fake_redis):
    """Revoking an already-expired token writes nothing (no unbounded keys)."""
    jti = "tok-expired"
    dependencies.revoke_token_jti(jti, _exp_in(-10))
    assert dependencies.is_token_revoked(jti) is False
    assert fake_redis.exists(f"{dependencies._JWT_REVOKED_PREFIX}{jti}") == 0


def test_missing_jti_is_failopen(fake_redis):
    """A legacy token minted before #187 (no jti) is never treated as revoked."""
    assert dependencies.is_token_revoked(None) is False
    assert dependencies.is_token_revoked("") is False
    dependencies.revoke_token_jti(None, _exp_in(60))  # no crash, no key
    assert fake_redis.keys(f"{dependencies._JWT_REVOKED_PREFIX}*") == []


def test_redis_down_is_failopen(monkeypatch):
    """Redis unreachable → revoke is a no-op and the check returns False, so a
    valid session is never locked out by an infra outage."""
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    dependencies.revoke_token_jti("tok-x", _exp_in(60))  # no crash
    assert dependencies.is_token_revoked("tok-x") is False

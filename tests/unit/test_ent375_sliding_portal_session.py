"""The Workspace session slides — and cannot slide past its own limits (ent#375).

A portal session hard-expired 12 hours after sign-in with no renewal path, so
anyone using the surface on two consecutive days redid the email OTP. It now
renews on use, dies on inactivity, and is still bounded by an absolute cap.

Longer-lived sessions are a security trade, so the tests that matter here are the
ones about what renewal must REFUSE to do:

  * renewal must not outlive the absolute cap, however many rotations happen —
    which is why `sst` (session start) is carried through rotations while `iat`
    moves;
  * renewal must not resurrect a revoked session, and unlike the read path it
    must fail CLOSED when it cannot check;
  * the ent#281 revoke cutoff must outlive the longest-lived token it has to
    kill — the coupling that silently breaks when the session lifetime grows.

That last one is the sharpest edge and is not in the acceptance criteria:
`revoke_portal_sessions_for_email` used `PORTAL_SESSION_EXPIRE_HOURS * 3600` as
its Redis TTL. Extending sessions to 30 days without touching it would have left
the cutoff evaporating after 12 hours while pre-revoke tokens stayed valid — the
next request finds no cutoff and sails through. A revoked session comes back to
life, silently, only for sessions revoked more than 12 hours ago.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent375.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent375-logs")
)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EMAIL = "bob@example.com"
DAY = 86400


@pytest.fixture()
def deps(monkeypatch):
    """`dependencies`, with a fake Redis and the default policy.

    Resolved through `sys.modules` rather than a bare import: `dependencies` is
    on conftest's #762 invariant-restore list, so a re-import can hand back a
    different module object than the one under test, and patches land on an
    object nobody calls (trinity#2094).
    """
    import importlib
    importlib.import_module("dependencies")
    mod = sys.modules["dependencies"]

    import fakeredis
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(mod, "get_breaker_redis", lambda: fake)
    mod._fake = fake
    return mod


@pytest.fixture()
def policy(deps, monkeypatch):
    def _set(idle_days: float, absolute_days: float):
        monkeypatch.setattr(
            deps, "_portal_session_policy",
            lambda: (int(idle_days * DAY), int(absolute_days * DAY)),
        )
    _set(7, 30)
    return _set


def _claims(deps, token: str) -> dict:
    from jose import jwt
    from config import ALGORITHM, SECRET_KEY
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


# ---------------------------------------------------------------------------
# It slides
# ---------------------------------------------------------------------------

def test_a_fresh_session_lives_for_the_idle_window_not_twelve_hours(deps, policy):
    """The reported complaint: 12 hours, every time."""
    tok = deps.create_portal_session_token(EMAIL)
    c = _claims(deps, tok)
    lifetime = c["exp"] - c["iat"]
    assert lifetime > 12 * 3600, "still on the old 12-hour cliff"
    assert abs(lifetime - 7 * DAY) < 120, f"expected ~7d idle window, got {lifetime}s"


def test_renewal_returns_a_different_token_with_a_fresh_jti_and_exp(deps, policy):
    """AC: renewal ROTATES — a new token, not a re-issue of the same one."""
    import time
    now = int(time.time())
    # A session five days into its seven-day idle window.
    old = _reissue_with_iat(
        deps, deps.create_portal_session_token(EMAIL, session_start=now - 5 * DAY),
        now - 5 * DAY,
    )
    oldc = _claims(deps, old)

    new = deps.renew_portal_session(old)
    assert new and new != old
    newc = _claims(deps, new)
    assert newc["jti"] != oldc["jti"], "jti must rotate"
    assert newc["exp"] > oldc["exp"], "exp must move forward"
    assert newc["email"] == EMAIL


def _reissue_with_iat(deps, token: str, iat: int) -> str:
    """Re-sign a token's claims with a chosen `iat`, to simulate an aged session.

    `exp` is recomputed as `iat + idle`, clamped to the cap — the same way the
    mint does it. An earlier version moved only `iat` and kept the original
    `exp`, producing a token that cannot exist (five days old, still seven days
    of validity left) and an assertion that failed for a reason the product
    code had nothing to do with.
    """
    from jose import jwt
    from config import ALGORITHM, SECRET_KEY
    c = _claims(deps, token)
    idle_s, absolute_s = deps._portal_session_policy()
    c["iat"] = iat
    c.setdefault("sst", iat)
    c["exp"] = min(iat + idle_s, int(c["sst"]) + absolute_s)
    return jwt.encode(c, SECRET_KEY, algorithm=ALGORITHM)


# ---------------------------------------------------------------------------
# It cannot slide forever — the absolute cap
# ---------------------------------------------------------------------------

def test_session_start_survives_rotation_so_the_cap_still_bites(deps, policy):
    """`sst` is what makes the cap real.

    `iat` MUST move on rotation (the revoke cutoff dates against it). If the cap
    measured from `iat`, every rotation would reset it and a continuously-used
    session would live forever — exactly what an absolute cap exists to prevent.
    """
    import time
    start = int(time.time()) - 20 * DAY
    tok = _reissue_with_iat(deps, deps.create_portal_session_token(EMAIL, session_start=start), int(time.time()) - 5 * DAY)

    new = deps.renew_portal_session(tok)
    assert new, "a 20-day-old session is inside a 30-day cap and should renew"
    c = _claims(deps, new)
    assert c["sst"] == start, "session start must survive rotation unchanged"
    assert c["iat"] > start, "iat must move forward"
    # …and the new token may not advertise validity beyond the cap.
    assert c["exp"] <= start + 30 * DAY + 5


def test_renewal_refuses_once_the_absolute_cap_is_reached(deps, policy):
    import time
    start = int(time.time()) - 31 * DAY
    tok = _reissue_with_iat(deps, deps.create_portal_session_token(EMAIL, session_start=start), int(time.time()) - 60)
    assert deps.renew_portal_session(tok) is None, "capped-out session must not renew"


def test_a_capped_out_token_stops_decoding_even_if_its_exp_is_still_future(deps, policy, monkeypatch):
    """Shortening the cap takes effect immediately, not only for new sessions.

    A token minted under a 30-day cap carries an `exp` up to 30 days out. If the
    operator narrows the cap to 1 day, the read path must honour the NEW cap —
    otherwise the narrower policy applies to nobody currently signed in, which is
    precisely when an operator narrows it.
    """
    import time
    start = int(time.time()) - 5 * DAY
    tok = _reissue_with_iat(deps, deps.create_portal_session_token(EMAIL, session_start=start), int(time.time()))
    assert deps.decode_portal_session(tok) == EMAIL

    policy(7, 1)          # cap narrowed to 1 day; the session is 5 days old
    assert deps.decode_portal_session(tok) is None


# ---------------------------------------------------------------------------
# It cannot outlive revocation — the sharp edge
# ---------------------------------------------------------------------------

def test_the_revoke_cutoff_outlives_the_longest_session_it_must_kill(deps, policy):
    """The coupling that breaks silently when session lifetime grows.

    The cutoff key's TTL must cover the ABSOLUTE CAP. At the old
    `PORTAL_SESSION_EXPIRE_HOURS * 3600` (12h) with a 30-day cap, the cutoff
    evaporates 29.5 days before the tokens it exists to kill — and a revoked
    session silently comes back.
    """
    policy(7, 30)
    assert deps.revoke_portal_sessions_for_email(EMAIL) is True
    ttl = deps._fake.ttl(deps._portal_revoked_key(EMAIL))
    assert ttl > 29 * DAY, (
        f"revoke cutoff TTL is {ttl}s but sessions can live 30 days — a session "
        "revoked more than that ago would resurrect"
    )
    assert ttl >= 30 * DAY, "TTL must cover the full cap, not merely most of it"


def test_a_revoked_session_cannot_rotate_itself_back_to_life(deps, policy):
    """AC: renewal respects revocation."""
    import time
    tok = _reissue_with_iat(
        deps, deps.create_portal_session_token(EMAIL), int(time.time()) - 5 * DAY
    )
    deps.revoke_portal_sessions_for_email(EMAIL)
    assert deps.renew_portal_session(tok) is None
    assert deps.decode_portal_session(tok) is None


def test_a_jti_revoked_session_cannot_rotate(deps, policy):
    import time
    tok = _reissue_with_iat(
        deps, deps.create_portal_session_token(EMAIL), int(time.time()) - 5 * DAY
    )
    c = _claims(deps, tok)
    deps.revoke_token_jti(c["jti"], c["exp"])
    assert deps.renew_portal_session(tok) is None


def test_renewal_fails_CLOSED_when_the_revocation_store_is_unreadable(deps, policy, monkeypatch):
    """The issue names this explicitly, and it is the one place the read path's
    fail-OPEN posture must not be inherited.

    Failing open here mints a FRESH token for a session an operator just killed —
    and because the new token post-dates the cutoff, the kill does not re-apply
    when Redis recovers. One blip at the wrong moment permanently resurrects it.
    """
    import time
    tok = _reissue_with_iat(
        deps, deps.create_portal_session_token(EMAIL), int(time.time()) - 5 * DAY
    )

    class Broken:
        def exists(self, *a, **k): raise RuntimeError("redis down")
        def get(self, *a, **k): raise RuntimeError("redis down")
        def setex(self, *a, **k): raise RuntimeError("redis down")

    monkeypatch.setattr(deps, "get_breaker_redis", lambda: Broken())
    assert deps.renew_portal_session(tok) is None, "renewal must refuse when it cannot verify"

    monkeypatch.setattr(deps, "get_breaker_redis", lambda: None)
    assert deps.renew_portal_session(tok) is None, "no store at all must also refuse"


def test_the_read_path_still_fails_OPEN(deps, policy, monkeypatch):
    """The counterpart: a Redis blip must NOT sign every client out.

    Renewal and reading deliberately differ, so this pins that the stricter
    renewal policy was not applied to the read path by accident.
    """
    tok = deps.create_portal_session_token(EMAIL)
    monkeypatch.setattr(deps, "get_breaker_redis", lambda: None)
    assert deps.decode_portal_session(tok) == EMAIL


# ---------------------------------------------------------------------------
# Rotation cadence
# ---------------------------------------------------------------------------

def test_a_fresh_token_is_not_rotated_on_every_request(deps, policy):
    """Rotation revokes the token it replaces, so rotating constantly would make
    concurrent in-flight requests race against a token that was just retired."""
    tok = deps.create_portal_session_token(EMAIL)
    assert deps.portal_session_needs_rotation(tok) is False


def test_a_stale_token_is_rotated(deps, policy):
    import time
    tok = _reissue_with_iat(
        deps, deps.create_portal_session_token(EMAIL), int(time.time()) - 5 * DAY
    )
    assert deps.portal_session_needs_rotation(tok) is True


def test_the_superseded_token_is_retired_but_with_a_grace(deps, policy):
    """AC: the old token is not left independently valid for its full lifetime —
    but requests already in flight are still carrying it, so it gets a short
    grace rather than an instant kill."""
    import time
    from config import PORTAL_SESSION_ROTATION_GRACE_SECONDS
    old = _reissue_with_iat(
        deps, deps.create_portal_session_token(EMAIL), int(time.time()) - 5 * DAY
    )
    oldc = _claims(deps, old)
    assert deps.renew_portal_session(old)

    ttl = deps._fake.ttl(f"{deps._JWT_REVOKED_PREFIX}{oldc['jti']}")
    assert 0 < ttl <= PORTAL_SESSION_ROTATION_GRACE_SECONDS + 1, (
        f"superseded jti TTL {ttl}s — expected a short grace, not its full lifetime"
    )


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------

def test_defaults_are_days_not_hours():
    """AC: "an idle window measured in days, not hours"."""
    from config import (
        PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT,
        PORTAL_SESSION_IDLE_DAYS_DEFAULT,
    )
    assert PORTAL_SESSION_IDLE_DAYS_DEFAULT >= 1
    assert PORTAL_SESSION_ABSOLUTE_DAYS_DEFAULT > PORTAL_SESSION_IDLE_DAYS_DEFAULT


@pytest.mark.parametrize("idle_days,abs_days,why", [
    (30, 7, "cap shorter than the idle window"),
    (0, 30, "zero idle window"),
    (7, 9999, "cap beyond the hard ceiling"),
])
def test_a_bad_stored_policy_is_clamped_on_read_not_obeyed(monkeypatch, idle_days, abs_days, why):
    """The entitled setter validates, but a direct DB write or a default
    regression must not be able to widen the window. Clamping on READ is what
    makes `idle <= absolute` hold regardless of who wrote the row."""
    from config import PORTAL_SESSION_MAX_ABSOLUTE_DAYS, PORTAL_SESSION_MIN_IDLE_MINUTES
    from services.settings_service import settings_service

    vals = {
        "portal_session_idle_days": str(idle_days),
        "portal_session_absolute_days": str(abs_days),
    }
    monkeypatch.setattr(settings_service, "get_setting", lambda k, d=None: vals.get(k, d))

    idle_s, abs_s = settings_service.get_portal_session_policy()
    assert idle_s >= PORTAL_SESSION_MIN_IDLE_MINUTES * 60, why
    assert abs_s <= PORTAL_SESSION_MAX_ABSOLUTE_DAYS * DAY, why
    assert abs_s >= idle_s, f"cap must never be shorter than the idle window ({why})"


def test_a_settings_read_failure_degrades_to_the_shipped_policy(monkeypatch):
    """This runs on the auth path — raising here would 500 every Workspace
    request instead of falling back to the defaults."""
    from config import PORTAL_SESSION_IDLE_DAYS_DEFAULT
    from services.settings_service import settings_service

    def boom(*a, **k):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_service, "get_setting", boom)
    idle_s, abs_s = settings_service.get_portal_session_policy()
    assert idle_s == int(PORTAL_SESSION_IDLE_DAYS_DEFAULT * DAY)
    assert abs_s >= idle_s

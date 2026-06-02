"""CSO OBS-1 / OBS-2: in-process rate-limit fallback + token regex tightening.

OBS-1 — without an in-process secondary cap, a Redis outage would let any valid
webhook token holder flood /api/webhooks/{token} unbounded. The fallback caps
blast radius per worker (3x the primary Redis-backed limit) without breaking
the documented fail-open philosophy: legitimate webhooks succeed below the cap.

OBS-2 — the token regex is tightened to {43} (the exact length of
secrets.token_urlsafe(32)) — verified against src/backend/db/schedules.py:524.

Loads `webhooks.py` directly via importlib with stubs for the heavy backend
deps so the unit test stays self-contained (no pytz / fastapi-deep / SQLite).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types
from pathlib import Path

# Issue #589: backend/config.py raises at import if REDIS_URL lacks credentials.
# Set defensively so the dependency-stub flow below works regardless of how
# pytest discovered this file.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_WEBHOOKS_PY = _BACKEND / "routers" / "webhooks.py"


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def webhooks(monkeypatch):
    """Load webhooks.py with stubbed `database` and `services.platform_audit_service`."""

    # Stub `database.db` — webhooks.py does `from database import db` at import time.
    db_stub = types.SimpleNamespace(get_schedule_by_webhook_token=lambda _t: None)
    monkeypatch.setitem(sys.modules, "database", _stub_module("database", db=db_stub))

    # Stub `services.platform_audit_service` — webhooks.py imports it at top level.
    class _AuditEventType:  # mirror just the attrs the module references
        EXECUTION = "execution"

    class _PlatformAudit:
        async def log(self, **_kw):
            return None

    services_pkg = _stub_module("services")
    audit_stub = _stub_module(
        "services.platform_audit_service",
        AuditEventType=_AuditEventType,
        platform_audit_service=_PlatformAudit(),
    )
    monkeypatch.setitem(sys.modules, "services", services_pkg)
    monkeypatch.setitem(sys.modules, "services.platform_audit_service", audit_stub)

    # Stub `services.idempotency_service` — webhooks.py imports it at top level
    # (RELIABILITY-006, #525). These tests exercise rate limiting, not dedup,
    # so begin() returns a disabled/no-replay decision and the rest are no-ops.
    _idem_decision = types.SimpleNamespace(
        enabled=False, replay=False, in_flight=False,
        scope=None, key=None, execution_id=None, snapshot=None,
    )
    idem_stub = _stub_module(
        "services.idempotency_service",
        make_agent_scope=lambda n: f"agent:{n}",
        make_webhook_scope=lambda t: f"webhook:{t}",
        derive_webhook_key=lambda t, b: "stub-key",
        derive_schedule_key=lambda e: f"sched:{e}",
        begin=lambda scope, key: _idem_decision,
        attach_execution=lambda *a, **k: None,
        complete=lambda *a, **k: None,
        fail=lambda *a, **k: None,
    )
    setattr(services_pkg, "idempotency_service", idem_stub)
    monkeypatch.setitem(sys.modules, "services.idempotency_service", idem_stub)

    # Make `from config import REDIS_URL` resolve without re-running config.py
    # in the heavy mode — config.py is fine to load (REDIS_URL is set above)
    # but ensure src/backend is on sys.path for it.
    backend_str = str(_BACKEND)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)

    spec = importlib.util.spec_from_file_location(
        "webhooks_under_test", str(_WEBHOOKS_PY)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _force_no_redis(monkeypatch, webhooks_mod):
    """Pin _get_redis() to None so _check_webhook_rate_limit hits the fallback."""
    monkeypatch.setattr(webhooks_mod, "_get_redis", lambda: None)


# ---------------------------------------------------------------------------
# OBS-1: in-process secondary rate limiter
# ---------------------------------------------------------------------------

def test_inprocess_limiter_caps_when_redis_unavailable(webhooks, monkeypatch):
    """When Redis returns None, the in-process limiter must fire on overflow."""
    _force_no_redis(monkeypatch, webhooks)
    webhooks._inprocess_clear()

    token = "test-token-fallback-cap"

    for _ in range(webhooks.INPROCESS_FALLBACK_LIMIT):
        webhooks._check_webhook_rate_limit(token)  # below cap, all succeed

    with pytest.raises(HTTPException) as excinfo:
        webhooks._check_webhook_rate_limit(token)
    assert excinfo.value.status_code == 429
    assert "in-process fallback" in excinfo.value.detail
    assert "Retry-After" in excinfo.value.headers


def test_inprocess_limiter_window_expires(webhooks, monkeypatch):
    """Once the rolling window passes, calls succeed again."""
    _force_no_redis(monkeypatch, webhooks)
    monkeypatch.setattr(webhooks, "INPROCESS_FALLBACK_WINDOW", 0.05)
    webhooks._inprocess_clear()

    token = "test-token-fallback-window"
    for _ in range(webhooks.INPROCESS_FALLBACK_LIMIT):
        webhooks._check_webhook_rate_limit(token)
    with pytest.raises(HTTPException):
        webhooks._check_webhook_rate_limit(token)

    time.sleep(0.06)
    webhooks._check_webhook_rate_limit(token)  # window expired → must not raise


def test_inprocess_limiter_isolates_tokens(webhooks, monkeypatch):
    """One token hitting its cap must not block a different token."""
    _force_no_redis(monkeypatch, webhooks)
    webhooks._inprocess_clear()

    hot = "test-token-fallback-hot"
    cold = "test-token-fallback-cold"

    for _ in range(webhooks.INPROCESS_FALLBACK_LIMIT):
        webhooks._check_webhook_rate_limit(hot)
    with pytest.raises(HTTPException):
        webhooks._check_webhook_rate_limit(hot)

    webhooks._check_webhook_rate_limit(cold)  # different token still admitted


def test_inprocess_limiter_engages_on_redis_runtime_error(webhooks, monkeypatch):
    """If r.get() raises after a successful _get_redis(), fall back too.

    A connection that succeeded at PING but fails mid-call (e.g. Redis
    restarts between PING and GET) must not silently fail-open.
    """

    class _ExplodingRedis:
        def get(self, *_a, **_kw):
            raise RuntimeError("connection reset")

        def ttl(self, *_a, **_kw):
            return -1

        def pipeline(self):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(webhooks, "_get_redis", lambda: _ExplodingRedis())
    webhooks._inprocess_clear()

    token = "test-token-fallback-runtime-error"

    for _ in range(webhooks.INPROCESS_FALLBACK_LIMIT):
        webhooks._check_webhook_rate_limit(token)
    with pytest.raises(HTTPException) as excinfo:
        webhooks._check_webhook_rate_limit(token)
    assert excinfo.value.status_code == 429


# ---------------------------------------------------------------------------
# OBS-2: token regex tightened to exact 43 chars
# ---------------------------------------------------------------------------

def test_token_regex_requires_exact_43_chars(webhooks):
    """Regex must match exactly 43 url-safe chars (secrets.token_urlsafe(32))."""
    valid = "A" * 43
    assert webhooks._TOKEN_RE.match(valid) is not None
    assert webhooks._TOKEN_RE.match("A" * 42) is None
    assert webhooks._TOKEN_RE.match("A" * 44) is None
    assert webhooks._TOKEN_RE.match("A" * 42 + "$") is None  # bad char
    assert webhooks._TOKEN_RE.match("") is None


# ---------------------------------------------------------------------------
# OBS-1 follow-up: Redis client is cached (avoids connection-per-request flood)
# ---------------------------------------------------------------------------

def test_redis_client_cache_hit(webhooks):
    """_get_redis() returns the cached client without rebuilding.

    Without caching, every webhook call would open a fresh TCP connection
    via redis.from_url(). Under flood, that exhausts Redis maxclients and
    turns the rate limiter into the DoS amplifier it was meant to prevent.
    """
    sentinel = object()
    webhooks._redis_client = sentinel
    try:
        assert webhooks._get_redis() is sentinel
        # Call again — same identity, never goes through the slow path
        assert webhooks._get_redis() is sentinel
    finally:
        webhooks._reset_redis_client()


def test_reset_redis_client_clears_cache(webhooks):
    """_reset_redis_client() drops the cached client so the next call rebuilds."""
    webhooks._redis_client = object()
    webhooks._reset_redis_client()
    assert webhooks._redis_client is None


def test_runtime_failure_resets_client(webhooks, monkeypatch):
    """When a Redis op fails inside _check_webhook_rate_limit, the cache is reset.

    Pairs with _check_webhook_rate_limit's except branch — a cached client
    that's gone stale (server restart, network partition recovered, password
    rotated) must be evicted so the next call rebuilds cleanly. Without the
    reset, the bad client would stay cached and every subsequent call would
    silently fall through to the in-process fallback.
    """

    class _ExplodingRedis:
        def get(self, *_a, **_kw):
            raise RuntimeError("connection reset")

        def ttl(self, *_a, **_kw):
            return -1

        def pipeline(self):
            raise RuntimeError("connection reset")

    bad = _ExplodingRedis()
    webhooks._redis_client = bad
    monkeypatch.setattr(webhooks, "_get_redis", lambda: bad)
    webhooks._inprocess_clear()

    webhooks._check_webhook_rate_limit("test-token-cache-reset")

    # The cache MUST have been cleared by the except branch
    assert webhooks._redis_client is None

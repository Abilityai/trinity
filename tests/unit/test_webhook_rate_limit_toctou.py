"""TOCTOU regression for webhook rate limiter (#644).

Pre-fix code path was:

    count = r.get(key)
    if count and int(count) >= WEBHOOK_RATE_LIMIT:
        raise 429
    pipe.incr(key); pipe.expire(...); pipe.execute()

Concurrent callers could all observe ``count < limit`` between the GET and
the INCR, all skip the 429, and all increment — letting the actual call
rate exceed ``WEBHOOK_RATE_LIMIT`` by the concurrency factor.

Fix: INCR-then-compare. Increment unconditionally (Redis INCR is atomic),
then 429 the caller whose post-increment count crosses the threshold.

These tests pin three properties of the new implementation:

1. Sequential calls past the limit raise 429 — basic behavior.
2. Concurrent calls under the post-fix code don't over-shoot the limit
   (sanity check; in-process timing can mask the pre-fix race).
3. ``r.get()`` is no longer on the hot path — INCR-then-compare. This is
   the reliable structural regression signal: a partial revert that
   re-adds the GET trips this even when timing-based tests don't.

The wide-window concurrency repro against a real Redis lives in
``tests/integration/test_webhook_rate_limit.py`` (run via run-integration.sh).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

# Issue #589: backend/config.py raises at import if REDIS_URL lacks credentials.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


pytestmark = pytest.mark.unit


def _find_backend_root() -> Path:
    """Locate the backend source tree across host and in-container layouts."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "src" / "backend",  # host
        Path("/app"),  # trinity-backend container
    ]
    env_override = os.environ.get("TRINITY_BACKEND_PATH")
    if env_override:
        candidates.insert(0, Path(env_override))
    for c in candidates:
        if (c / "routers" / "webhooks.py").exists():
            return c
    raise RuntimeError(
        "Cannot locate backend source tree (set TRINITY_BACKEND_PATH)"
    )


_BACKEND = _find_backend_root()
_WEBHOOKS_PY = _BACKEND / "routers" / "webhooks.py"


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def webhooks(monkeypatch):
    """Load webhooks.py with the same dep stubs as the in-process unit tests."""
    db_stub = types.SimpleNamespace(get_schedule_by_webhook_token=lambda _t: None)
    monkeypatch.setitem(sys.modules, "database", _stub_module("database", db=db_stub))

    class _AuditEventType:
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
    # (RELIABILITY-006, #525). These tests exercise rate limiting, not dedup.
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

    backend_str = str(_BACKEND)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)

    spec = importlib.util.spec_from_file_location(
        "webhooks_toctou_under_test", str(_WEBHOOKS_PY)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── Atomic-counter Redis stub ────────────────────────────────────────────────
#
# Real Redis INCR is atomic; the stub mirrors that with a Lock so concurrent
# threads can't observe a stale read. The pre-fix code's GET path is an
# explicit get() method — kept on the stub to assert it isn't called by the
# fixed implementation.

class _AtomicRedisStub:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._ttls: dict[str, int] = {}
        self.get_calls = 0
        self.incr_calls = 0
        self.expire_calls = 0

    # GET should not be called by the post-fix implementation.
    def get(self, key):
        self.get_calls += 1
        with self._lock:
            v = self._counts.get(key)
        return None if v is None else str(v).encode()

    def ttl(self, key):
        return self._ttls.get(key, -1)

    def pipeline(self):
        return _PipelineStub(self)


class _PipelineStub:
    def __init__(self, parent: _AtomicRedisStub):
        self._parent = parent
        self._ops: list = []

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        with self._parent._lock:
            for op in self._ops:
                if op[0] == "incr":
                    self._parent.incr_calls += 1
                    self._parent._counts[op[1]] = self._parent._counts.get(op[1], 0) + 1
                    results.append(self._parent._counts[op[1]])
                elif op[0] == "expire":
                    self._parent.expire_calls += 1
                    self._parent._ttls[op[1]] = op[2]
                    results.append(True)
        self._ops.clear()
        return results


# ── Tests ────────────────────────────────────────────────────────────────────

class TestSequentialBoundary:
    """Basic post-fix behavior — N+1 raises 429."""

    def test_first_n_succeed_then_429(self, webhooks, monkeypatch):
        stub = _AtomicRedisStub()
        monkeypatch.setattr(webhooks, "_get_redis", lambda: stub)

        token = "boundary-token"
        limit = webhooks.WEBHOOK_RATE_LIMIT

        for _ in range(limit):
            webhooks._check_webhook_rate_limit(token)

        with pytest.raises(HTTPException) as excinfo:
            webhooks._check_webhook_rate_limit(token)
        assert excinfo.value.status_code == 429
        assert "Retry-After" in excinfo.value.headers


class TestConcurrentLimit:
    """Property #2 — the limit must hold under thread concurrency.

    Sanity check that the post-fix INCR-then-compare path doesn't
    over-shoot the limit when called from multiple threads against an
    atomic-counter stub. This is timing-dependent — the GIL + in-process
    Lock can mask the pre-fix race in unit tests, so this is a happy-path
    pin only. The reliable structural regression signal lives in
    `TestIncrFirstSemantics` below; the wide-window race is exercised
    against a real Redis in `tests/integration/test_webhook_rate_limit.py`.
    """

    @pytest.mark.parametrize("concurrency", [16, 32])
    def test_no_more_than_limit_succeed(self, webhooks, monkeypatch, concurrency):
        stub = _AtomicRedisStub()
        monkeypatch.setattr(webhooks, "_get_redis", lambda: stub)

        token = f"concurrent-{concurrency}"
        limit = webhooks.WEBHOOK_RATE_LIMIT
        n = limit + concurrency

        def call():
            try:
                webhooks._check_webhook_rate_limit(token)
                return "ok"
            except HTTPException as e:
                return e.status_code

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            results = list(ex.map(lambda _: call(), range(n)))

        accepted = sum(1 for r in results if r == "ok")
        rate_limited = sum(1 for r in results if r == 429)

        assert accepted == limit, (
            f"expected exactly {limit} accepted under {concurrency}-way "
            f"concurrency, got {accepted}. Counts: ok={accepted} 429={rate_limited}. "
            f"Pre-fix TOCTOU race regressed."
        )
        assert rate_limited == n - limit


class TestIncrFirstSemantics:
    """Property #3 — INCR is on the hot path, GET is not.

    A partial revert (re-introducing the GET-then-INCR sequence) would
    flip this assertion even if the conditional was kept correct, because
    real-Redis concurrency would still be racy regardless of whether the
    in-process test catches it.
    """

    def test_incr_called_get_not_called(self, webhooks, monkeypatch):
        stub = _AtomicRedisStub()
        monkeypatch.setattr(webhooks, "_get_redis", lambda: stub)

        token = "incr-first"
        for _ in range(3):
            webhooks._check_webhook_rate_limit(token)

        assert stub.incr_calls == 3, (
            f"expected 3 INCRs, got {stub.incr_calls} — INCR must run on every call"
        )
        assert stub.get_calls == 0, (
            f"r.get() called {stub.get_calls} times — pre-fix code path is back. "
            "INCR-then-compare must not GET first."
        )

    def test_pipeline_order_is_incr_then_expire(self, webhooks, monkeypatch):
        """Pipeline must batch the INCR + EXPIRE so TTL is set even on first call."""
        # Use a MagicMock-shaped pipeline to capture call order.
        recorder = MagicMock()
        recorder.execute = MagicMock(return_value=[1, True])

        class _RecordingRedis:
            def __init__(self):
                self.pipeline_calls = 0

            def pipeline(self):
                self.pipeline_calls += 1
                return recorder

            def ttl(self, _key):
                return -1

            def get(self, *_a, **_kw):
                pytest.fail("r.get() must not be called by the post-fix code")

        rec = _RecordingRedis()
        monkeypatch.setattr(webhooks, "_get_redis", lambda: rec)

        webhooks._check_webhook_rate_limit("ordered-token")

        # First two recorded ops on the pipeline are incr then expire.
        method_names = [c[0] for c in recorder.method_calls]
        # Drop the trailing execute() call from comparison.
        ops_before_execute = [m for m in method_names if m != "execute"]
        assert ops_before_execute[:2] == ["incr", "expire"]

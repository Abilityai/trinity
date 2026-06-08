"""
Unit tests for the unified Redis sliding-window rate limiter (#1023).

`services/rate_limiter.py` replaced the bespoke per-endpoint limiters (starting
with the webhook trigger). These tests cover:

- in-process fallback path (Redis unavailable): allow under limit, 429 at
  limit, window roll-off, key isolation;
- Redis path against an atomic ZSET stub: allow under limit, block at limit,
  Retry-After;
- the anti-TOCTOU concurrency property carried over from the old webhook
  limiter (#644): under N concurrent callers, no more than `limit` are allowed
  — the count is taken after an atomic add, never via a read-then-write gap;
- fail-open: a Redis error mid-call drops to the in-process fallback;
- enforce() raises 429 with a Retry-After header.

The full webhook-over-HTTP behaviour is exercised in
tests/integration/test_webhook_rate_limit.py against a real Redis.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException

# Issue #589: backend/config.py raises at import if REDIS_URL lacks credentials.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit


def _find_backend_root() -> Path:
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "src" / "backend",
        Path("/app"),
    ]
    env_override = os.environ.get("TRINITY_BACKEND_PATH")
    if env_override:
        candidates.insert(0, Path(env_override))
    for c in candidates:
        if (c / "services" / "rate_limiter.py").exists():
            return c
    raise RuntimeError("Cannot locate backend source tree (set TRINITY_BACKEND_PATH)")


_BACKEND = _find_backend_root()
_RL_PY = _BACKEND / "services" / "rate_limiter.py"


@pytest.fixture
def rl(monkeypatch):
    """Load services/rate_limiter.py standalone (no heavy services/__init__)."""
    backend_str = str(_BACKEND)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)
    spec = importlib.util.spec_from_file_location("_rate_limiter_under_test", str(_RL_PY))
    module = importlib.util.module_from_spec(spec)
    # monkeypatch.setitem auto-restores (deletes) the entry at teardown so the
    # standalone module doesn't leak into sys.modules (sys.modules pollution lint).
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.clear_inprocess()
    return module


# ── In-process fallback path (Redis unavailable) ─────────────────────────────

def _force_no_redis(monkeypatch, rl):
    monkeypatch.setattr(rl, "_get_redis", lambda: None)


def test_inprocess_allows_under_limit(rl, monkeypatch):
    _force_no_redis(monkeypatch, rl)
    for _ in range(5):
        assert rl.check("k", limit=5, window_seconds=60).allowed


def test_inprocess_blocks_at_limit(rl, monkeypatch):
    _force_no_redis(monkeypatch, rl)
    for _ in range(5):
        rl.check("k", 5, 60)
    res = rl.check("k", 5, 60)
    assert res.allowed is False
    assert res.retry_after >= 1


def test_inprocess_window_rolls_off(rl, monkeypatch):
    _force_no_redis(monkeypatch, rl)
    for _ in range(3):
        rl.check("k", 3, 1)
    assert rl.check("k", 3, 1).allowed is False
    time.sleep(1.05)
    assert rl.check("k", 3, 1).allowed is True  # window expired


def test_inprocess_key_isolation(rl, monkeypatch):
    _force_no_redis(monkeypatch, rl)
    for _ in range(3):
        rl.check("a", 3, 60)
    assert rl.check("a", 3, 60).allowed is False
    assert rl.check("b", 3, 60).allowed is True  # different key unaffected


# ── Redis ZSET path against an atomic stub ───────────────────────────────────

class _FakeRedis:
    """Minimal atomic ZSET stub: pipeline ops applied under a lock in execute()."""

    def __init__(self):
        self._lock = threading.Lock()
        self.z: dict[str, dict[str, float]] = {}

    def pipeline(self):
        return _FakePipe(self)

    def zrem(self, key, member):
        with self._lock:
            self.z.get(key, {}).pop(member, None)

    def zrange(self, key, start, stop, withscores=False):
        with self._lock:
            items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1])
        sel = items[start:] if stop == -1 else items[start:stop + 1]
        return [(m, s) for m, s in sel] if withscores else [m for m, _ in sel]


class _FakePipe:
    def __init__(self, parent: _FakeRedis):
        self.p = parent
        self.ops: list = []

    def zremrangebyscore(self, key, lo, hi):
        self.ops.append(("zrem", key, lo, hi)); return self

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping)); return self

    def zcard(self, key):
        self.ops.append(("zcard", key)); return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl)); return self

    def execute(self):
        res = []
        with self.p._lock:
            for op in self.ops:
                if op[0] == "zrem":
                    _, key, lo, hi = op
                    d = self.p.z.setdefault(key, {})
                    for m in [m for m, s in list(d.items()) if lo <= s <= hi]:
                        d.pop(m)
                    res.append(None)
                elif op[0] == "zadd":
                    _, key, mapping = op
                    self.p.z.setdefault(key, {}).update(mapping)
                    res.append(len(mapping))
                elif op[0] == "zcard":
                    res.append(len(self.p.z.get(op[1], {})))
                elif op[0] == "expire":
                    res.append(True)
        self.ops = []
        return res


def test_redis_allows_under_then_blocks_at_limit(rl, monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: fake)
    for _ in range(10):
        assert rl.check("tok", 10, 60).allowed
    res = rl.check("tok", 10, 60)
    assert res.allowed is False
    assert res.retry_after >= 1


def test_redis_concurrency_never_exceeds_limit(rl, monkeypatch):
    """#644 anti-TOCTOU property: under concurrency, at most `limit` allowed.

    The old fixed-window GET-then-INCR could let N callers all observe
    count<limit and all pass. The ZSET add-then-count under an atomic pipeline
    cannot — exactly `limit` succeed regardless of thread interleaving.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: fake)
    limit = 10
    concurrency = 40

    def call():
        return rl.check("flood", limit, 60).allowed

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = list(ex.map(lambda _: call(), range(concurrency)))

    assert sum(1 for ok in results if ok) == limit


def test_redis_error_falls_back_inprocess(rl, monkeypatch):
    class _BoomRedis:
        def pipeline(self):
            raise RuntimeError("redis down mid-call")

    monkeypatch.setattr(rl, "_get_redis", lambda: _BoomRedis())
    monkeypatch.setattr(rl, "reset_redis_client", lambda: None)
    # Should not raise — falls back to in-process and allows under limit.
    assert rl.check("k", 5, 60).allowed is True


# ── enforce() ────────────────────────────────────────────────────────────────

def test_enforce_raises_429_with_retry_after(rl, monkeypatch):
    _force_no_redis(monkeypatch, rl)
    for _ in range(3):
        rl.enforce("k", 3, 60)
    with pytest.raises(HTTPException) as exc:
        rl.enforce("k", 3, 60)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_enforce_allows_under_limit(rl, monkeypatch):
    _force_no_redis(monkeypatch, rl)
    res = rl.enforce("k", 3, 60)
    assert res.allowed is True

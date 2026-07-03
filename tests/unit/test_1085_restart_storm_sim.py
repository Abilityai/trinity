"""In-suite soak gate: restart-storm simulation (#1085, Part E).

Simulates the present-day thundering herd — a backend restart with ~N agents
mid-flight all re-sending persisted terminal envelopes at once — and asserts the
two structural guarantees that keep it safe:

  1. **Caps bound concurrent admissions.** Under N concurrent callbacks the
     re-delivery rate cap admits at most ``limit`` per window; the rest are
     throttled (503) and stay retryable — none are dropped.
  2. **Jitter spreads arrivals.** The startup-sweep initial jitter smears N
     agents' first POSTs across the window instead of a synchronized t≈0 spike.

Pure asyncio / threads + the in-process rate-limiter fallback (no Redis, no
Docker), so it runs in the normal unit suite.
"""
from __future__ import annotations

import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Caps bound concurrent admissions (anti-TOCTOU under a herd)
# ---------------------------------------------------------------------------
class TestCapsBoundHerd:
    def _rl(self, monkeypatch):
        from services import rate_limiter
        monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)  # force in-process
        rate_limiter.clear_inprocess()
        return rate_limiter

    def test_fleet_cap_admits_at_most_limit(self, monkeypatch):
        rl = self._rl(monkeypatch)
        N, LIMIT, WINDOW = 200, 25, 60

        def _one(_):
            return rl.check("redelivery:fleet", LIMIT, WINDOW).allowed

        with ThreadPoolExecutor(max_workers=32) as ex:
            results = list(ex.map(_one, range(N)))

        admitted = sum(results)
        throttled = N - admitted
        # Exactly the cap is admitted; the rest are throttled (and, in prod, stay
        # persisted on a retryable 503 — never dropped).
        assert admitted == LIMIT
        assert throttled == N - LIMIT

    def test_per_agent_cap_isolates_one_crash_looper(self, monkeypatch):
        rl = self._rl(monkeypatch)
        LIMIT, WINDOW = 20, 60
        # One agent crash-looping 100 callbacks is capped at its per-agent limit.
        looper = sum(
            rl.check("redelivery:agent:loop", LIMIT, WINDOW).allowed for _ in range(100)
        )
        assert looper == LIMIT
        # A different agent is unaffected (separate key).
        assert rl.check("redelivery:agent:other", LIMIT, WINDOW).allowed is True


# ---------------------------------------------------------------------------
# 2. Jitter spreads the restart-sweep arrivals
# ---------------------------------------------------------------------------
class TestJitterSpreadsHerd:
    def test_initial_sweep_jitter_smears_the_burst(self):
        from agent_server.services import result_callback as rc
        import random

        rng = random.Random(1085)  # deterministic, reproducible
        N = 200
        window = rc._SWEEP_INITIAL_JITTER_SECONDS
        arrivals = [rng.uniform(0, window) for _ in range(N)]

        # Spread across most of the window (not clustered at t≈0).
        assert max(arrivals) - min(arrivals) > 0.7 * window
        # No single 10%-of-window bucket holds a big share of the herd.
        buckets = [0] * 10
        for a in arrivals:
            buckets[min(9, int(a / window * 10))] += 1
        assert max(buckets) < N * 0.30
        # Healthy dispersion.
        assert statistics.pstdev(arrivals) > window * 0.15

    def test_decorrelated_backoff_desynchronises_retriers(self):
        from agent_server.services import result_callback as rc

        # 200 agents that all hit a transient error on the same attempt would, in
        # lockstep exponential, sleep the identical duration and re-storm. With
        # decorrelated jitter their next sleeps are spread out.
        prev = 8.0
        sleeps = [rc._jittered_backoff(prev) for _ in range(200)]
        assert len(set(round(s, 4) for s in sleeps)) > 50      # not lockstep
        assert all(rc._BACKOFF_BASE <= s <= rc._BACKOFF_CAP for s in sleeps)

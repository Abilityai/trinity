"""/edge-cases 2026-10-06 — Hypothesis properties for the dispatch breaker (#526)
and the correlated-failure re-delivery governor (#1085).

Boundary rows live in ``test_ec_dispatch_breaker_edges.py``; this file holds the
invariants that a handful of examples cannot pin:

* **Routing totality** — ``record_outcome`` reaches ``record_success`` iff the
  code is ``None`` and ``record_failure`` iff its (``.value``-unwrapped) string
  case-folds to ``"auth"``; everything else is a no-op. Never raises.
* **Read-path totality** — ``to_dict`` / ``retry_after_seconds`` never raise and
  never report a negative wait for ANY stored hash contents.
* **Governor oracle** — the distinct-failing count equals a naive model
  (agents whose latest correlated failure is strictly newer than ``now - window``)
  for any monotone sequence of failures, and the pause is armed iff the model
  ever crossed the threshold.
* **Lua tier (needs lupa; skipped otherwise — see the edges file docstring):**
  the cooldown formula ``min(base * 2**min(k-1, 20), max)`` holds for every
  ``k`` / ``base`` / ``max``; and a model-based oracle drives random sequences of
  fail / success / allow / clock-step (including BACKWARDS) / lock-lapse through
  the production Lua and compares every transition, allow verdict and read.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

import fakeredis  # noqa: E402

import services.dispatch_breaker as DB  # noqa: E402
import services.redelivery_governor as RG  # noqa: E402
from services.dispatch_breaker import DispatchBreaker, Transition  # noqa: E402

pytestmark = pytest.mark.unit

T0 = 1_800_000_000.0
_FIXTURE_OK = [HealthCheck.function_scoped_fixture]


class _Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def time(self) -> float:
        return self.t


def _lua_capable() -> bool:
    try:
        return fakeredis.FakeRedis(decode_responses=True).eval("return 1", 0) == 1
    except Exception:
        return False


needs_lua = pytest.mark.skipif(not _lua_capable(), reason="fakeredis has no Lua runtime (lupa)")


# ---------------------------------------------------------------------------
# Routing totality (pure)
# ---------------------------------------------------------------------------

codes = st.one_of(
    st.none(),
    st.text(max_size=12),
    st.sampled_from(["auth", "AUTH", "Auth", " auth", "auth\n", "billing", "timeout"]),
    st.integers(),
    st.booleans(),
    st.binary(max_size=6),
    st.text(max_size=8).map(lambda s: SimpleNamespace(value=s)),
)


@settings(max_examples=300, deadline=None)
@given(codes)
@example(None)
@example(0)
@example("")
@example("AUTH")
@example("İ")  # İ — lower() is 'i̇', not an ascii match
def test_record_outcome_routing_is_total(code):
    b = DispatchBreaker("x", redis_client=MagicMock())
    b.record_success = MagicMock(return_value=Transition("closed", "closed"))
    b.record_failure = MagicMock(return_value=Transition("closed", "closed"))
    b.record_outcome(code)
    unwrapped = getattr(code, "value", code)
    is_auth = isinstance(unwrapped, str) and unwrapped.lower() == "auth"
    assert b.record_success.called is (code is None)
    assert b.record_failure.called is is_auth
    assert not (b.record_success.called and b.record_failure.called)


# ---------------------------------------------------------------------------
# Read-path totality (pure — any stored hash contents)
# ---------------------------------------------------------------------------

field_values = st.one_of(
    st.text(max_size=20),
    st.floats(allow_nan=True, allow_infinity=True).map(repr),
    st.integers(min_value=-(10**30), max_value=10**30).map(str),
    st.sampled_from(["nan", "inf", "-inf", "1e400", "", "0", "open", "closed"]),
)


@settings(max_examples=300, deadline=None)
@given(
    state=st.one_of(st.just("open"), st.just("closed"), st.text(max_size=8)),
    failures=field_values,
    npa=field_values,
)
@example(state="open", failures="3", npa="nan")
@example(state="open", failures="3", npa="inf")
@example(state="open", failures="3", npa="-inf")
@example(state="open", failures="1e400", npa="1e400")
def test_read_path_never_raises_never_negative(state, failures, npa):
    r = fakeredis.FakeRedis(decode_responses=True)
    r.hset("agent:dispatch:p", mapping={"state": state, "failures": failures, "next_probe_at": npa})
    b = DispatchBreaker("p", redis_client=r)
    d = b.to_dict()
    assert isinstance(d["retry_after_seconds"], int) and d["retry_after_seconds"] >= 0
    assert isinstance(d["failure_count"], int)
    ra = b.retry_after_seconds()
    assert isinstance(ra, int) and ra >= 0


# ---------------------------------------------------------------------------
# Governor oracle (pure — real fakeredis ZSET, frozen clock)
# ---------------------------------------------------------------------------

gov_ops = st.lists(
    st.tuples(
        st.sampled_from(["a", "b", "c", "d", "e", "f"]),
        st.floats(min_value=0, max_value=200, allow_nan=False),
        st.sampled_from(["auth", "billing", "AUTH", "timeout", None]),
    ),
    max_size=30,
)


@settings(max_examples=150, deadline=None, suppress_health_check=_FIXTURE_OK)
@given(ops=gov_ops, threshold=st.integers(1, 6), window=st.integers(1, 150))
@example(ops=[("a", 0.0, "auth"), ("b", 10.0, "auth")], threshold=2, window=10)  # exact cutoff
def test_governor_matches_naive_model(monkeypatch, ops, threshold, window):
    import config

    clock = _Clock()
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(RG, "time", clock)
    monkeypatch.setattr(RG, "get_breaker_redis", lambda: r)
    monkeypatch.setattr(RG, "reset_breaker_redis_client", lambda: None)
    monkeypatch.setattr(config, "CORRELATED_FAILURE_THRESHOLD", threshold, raising=False)
    monkeypatch.setattr(config, "CORRELATED_FAILURE_WINDOW_SECONDS", window, raising=False)
    monkeypatch.setattr(config, "CORRELATED_PAUSE_TTL_SECONDS", 300, raising=False)
    g = RG.RedeliveryGovernor()

    last: dict = {}
    ever_armed = False
    for agent, dt, code in ops:
        clock.t += dt
        g.record_terminal_failure(agent, code)
        if code and code.lower() in ("auth", "billing"):
            last[agent] = clock.t
            live = sum(1 for s in last.values() if s > clock.t - window)
            ever_armed = ever_armed or live >= threshold
        assert g.distinct_failing_count() == sum(
            1 for s in last.values() if s > clock.t - window
        )
    assert g.is_paused() is ever_armed


# ---------------------------------------------------------------------------
# Lua tier
# ---------------------------------------------------------------------------


@pytest.fixture
def lua_env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(DB, "time", clock)
    DB._DISPATCH_SCRIPTS.reset()
    yield SimpleNamespace(clock=clock, mp=monkeypatch)
    DB._DISPATCH_SCRIPTS.reset()


@needs_lua
@settings(max_examples=200, deadline=None, suppress_health_check=_FIXTURE_OK)
@given(
    k=st.integers(1, 40),
    base=st.floats(min_value=0.0, max_value=1e4, allow_nan=False),
    max_cd=st.floats(min_value=0.0, max_value=1e9, allow_nan=False),
)
@example(k=21, base=1.0, max_cd=1e9)   # exp reaches the clamp
@example(k=22, base=1.0, max_cd=1e9)   # one past the clamp
@example(k=1, base=30.0, max_cd=30.0)  # cap == base
def test_cooldown_formula(lua_env, k, base, max_cd):
    mp = lua_env.mp
    mp.setattr(DB, "DISPATCH_FAILURE_THRESHOLD", 1)
    mp.setattr(DB, "DISPATCH_BASE_COOLDOWN_SECONDS", base)
    mp.setattr(DB, "DISPATCH_MAX_COOLDOWN_SECONDS", max_cd)
    r = fakeredis.FakeRedis(decode_responses=True)
    b = DispatchBreaker("c", redis_client=r)
    for _ in range(k):
        b.record_failure()
    expected = min(base * 2 ** min(k - 1, 20), max_cd)
    got = float(r.hget("agent:dispatch:c", "next_probe_at")) - T0
    assert got == pytest.approx(expected, rel=1e-9, abs=1e-3)


class _Model:
    """Reference model of the three Lua scripts (the documented D9 machine)."""

    def __init__(self, threshold, base, max_cd):
        self.T, self.base, self.max = threshold, base, max_cd
        self.state, self.failures, self.pc, self.npa, self.lock = "closed", 0, 0, 0.0, False

    def allow(self, now):
        if self.state == "closed":
            return True
        if now < self.npa:
            return False
        if self.lock:
            return False
        self.lock = True
        return True

    def fail(self, now):
        prior = self.state
        self.failures += 1
        if prior == "closed" and self.failures < self.T:
            return Transition("closed", "closed")
        self.pc += 1
        self.npa = now + min(self.base * 2 ** min(self.pc - 1, 20), self.max)
        self.state, self.lock = "open", False
        return Transition(prior, "open")

    def success(self):
        prior = self.state
        if prior == "closed" and self.failures == 0:
            return Transition(prior, "closed")
        self.state, self.failures, self.pc, self.npa, self.lock = "closed", 0, 0, 0.0, False
        return Transition(prior, "closed")

    def retry_after(self, now):
        return max(0, math.ceil(self.npa - now)) if self.state == "open" else 0


machine_ops = st.lists(
    st.one_of(
        st.just(("fail",)),
        st.just(("success",)),
        st.just(("allow",)),
        st.just(("lapse",)),  # probe-lock TTL expiry
        st.tuples(st.just("tick"), st.integers(-120, 400)),  # negative = clock backwards
    ),
    max_size=40,
)


@needs_lua
@settings(max_examples=200, deadline=None, suppress_health_check=_FIXTURE_OK)
@given(ops=machine_ops, threshold=st.integers(1, 4))
@example(ops=[("fail",)] * 3 + [("tick", 30), ("allow",), ("allow",), ("fail",),
              ("tick", 59), ("allow",), ("tick", 1), ("allow",), ("success",)], threshold=3)
@example(ops=[("fail",)] * 3 + [("tick", -100), ("allow",)], threshold=3)
def test_lua_matches_reference_model(lua_env, ops, threshold):
    mp = lua_env.mp
    mp.setattr(DB, "DISPATCH_FAILURE_THRESHOLD", threshold)
    mp.setattr(DB, "DISPATCH_BASE_COOLDOWN_SECONDS", 30.0)
    mp.setattr(DB, "DISPATCH_MAX_COOLDOWN_SECONDS", 300.0)
    r = fakeredis.FakeRedis(decode_responses=True)
    b = DispatchBreaker("m", redis_client=r)
    m = _Model(threshold, 30.0, 300.0)
    clock = lua_env.clock
    for op in ops:
        if op[0] == "fail":
            assert b.record_failure("auth") == m.fail(clock.t)
        elif op[0] == "success":
            assert b.record_success() == m.success()
        elif op[0] == "allow":
            assert b.allow_dispatch() is m.allow(clock.t)
        elif op[0] == "lapse":
            r.delete("agent:dispatch:m:probe-lock")
            m.lock = False
        else:
            clock.t += op[1]
        d = b.to_dict()
        assert d["state"] == m.state
        assert d["failure_count"] == m.failures
        assert d["retry_after_seconds"] == m.retry_after(clock.t)

"""/edge-cases 2026-10-06 — boundary cases for the dispatch breaker (#526) and the
correlated-failure re-delivery governor (#1085).

Companion to ``test_dispatch_breaker.py`` (Transition semantics, record_outcome
routing, fail-open with no Lua) and ``test_1085_correlated_pause.py`` (distinct-agent
arming against a hand-rolled fake). Neither suite can execute the breaker's Lua
state machine: stock fakeredis has no EVAL, so the only coverage of the
thresholds / cooldowns / probe-lock lives in ``tests/integration/test_dispatch_breaker.py``
behind a real Redis, and even that file probes the threshold only at N-1/N and
the backoff only as ``second >= first``.

This file works the BOUNDARIES, in two tiers:

* **Pure tier (always runs).** ``_dispatch_state_dict`` parsing + the ``ceil``
  boundary of ``retry_after_seconds``; ``record_outcome`` on falsy-but-not-None
  codes (``0`` / ``""`` / ``False`` must NOT read as a success and reset the
  counter); decode of a malformed Lua reply; ``fail_open`` resetting the script
  cache; the module-level operator hooks; and the governor against a REAL
  fakeredis ZSET (window cutoff inclusivity, threshold N-1/N/N+1, re-arm TTL
  refresh, case folding, a TTL that Redis rejects, every fail-open edge).

* **Lua tier (needs ``lupa``).** Executes the PRODUCTION Lua source through
  fakeredis' Lua 5.1 runtime under a frozen clock: threshold at exactly N and
  N±1 (parametrised over N), the full cooldown ladder including the ``exp > 20``
  clamp and the max-cooldown cap, the ``now < next_probe_at`` boundary instant,
  probe-lock exclusivity / expiry, the clock running backwards, straggler
  failures while open, success-resets-backoff, and threaded concurrent trip /
  probe races (exactly one ``opened``, exactly one probe).
  Skipped (with a reason) when fakeredis cannot run Lua — tests/requirements-test.txt
  does not ship ``lupa`` today. To run it locally:
  ``pip install --target <dir> lupa`` and put ``<dir>`` on ``PYTHONPATH`` for this
  file only (NOT globally: ``test_dispatch_breaker.py::TestFailOpen::
  test_fakeredis_no_lua_fails_open`` assumes fakeredis has no Lua and fails when
  lupa is importable).

Nothing here pins a tunable's VALUE as the requirement; thresholds/cooldowns are
monkeypatched per test and assertions are on the RELATION to them.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

import fakeredis  # noqa: E402

import services.dispatch_breaker as DB  # noqa: E402
import services.redelivery_governor as RG  # noqa: E402
from services.dispatch_breaker import DispatchBreaker, Transition  # noqa: E402

pytestmark = pytest.mark.unit

T0 = 1_800_000_000.0  # a fixed, realistic epoch for the frozen clock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def time(self) -> float:
        return self.t


def _lua_capable() -> bool:
    try:
        r = fakeredis.FakeRedis(decode_responses=True)
        return r.eval("return 1", 0) == 1
    except Exception:
        return False


_LUA = _lua_capable()
needs_lua = pytest.mark.skipif(
    not _LUA, reason="fakeredis has no Lua runtime (lupa not installed) — see module docstring"
)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(DB, "time", c)
    monkeypatch.setattr(RG, "time", c)
    return c


@pytest.fixture
def lua_redis(monkeypatch):
    """A fresh Lua-capable fakeredis; the process-wide script cache is reset so
    the scripts register against THIS server."""
    if not _LUA:
        pytest.skip("no Lua runtime")
    r = fakeredis.FakeRedis(decode_responses=True)
    DB._DISPATCH_SCRIPTS.reset()
    monkeypatch.setattr(DB, "get_breaker_redis", lambda: r)
    monkeypatch.setattr(DB, "reset_breaker_redis_client", lambda: None)
    yield r
    DB._DISPATCH_SCRIPTS.reset()


@pytest.fixture
def tunables(monkeypatch):
    def _set(threshold=3, base=30.0, max_cd=300.0, lock_ttl=10):
        monkeypatch.setattr(DB, "DISPATCH_FAILURE_THRESHOLD", threshold)
        monkeypatch.setattr(DB, "DISPATCH_BASE_COOLDOWN_SECONDS", base)
        monkeypatch.setattr(DB, "DISPATCH_MAX_COOLDOWN_SECONDS", max_cd)
        monkeypatch.setattr(DB, "DISPATCH_PROBE_LOCK_TTL_SECONDS", lock_ttl)

    _set()
    return _set


def _hash(r, name="a"):
    return r.hgetall(f"agent:dispatch:{name}")


def _lock_exists(r, name="a") -> bool:
    return bool(r.exists(f"agent:dispatch:{name}:probe-lock"))


def _open(b: DispatchBreaker, n: int) -> list:
    return [b.record_failure("auth") for _ in range(n)]


# ===========================================================================
# PURE TIER
# ===========================================================================


class TestStateDictParsing:
    """_dispatch_state_dict: the read path every dashboard / admission check uses."""

    @pytest.mark.parametrize(
        "raw, expect_state, expect_failures",
        [
            pytest.param({}, "closed", 0, id="m1-empty-hash"),
            pytest.param({"state": ""}, "closed", 0, id="m2-empty-state"),
            pytest.param({"failures": "abc"}, "closed", 0, id="m3-garbage-failures"),
            pytest.param({"failures": "1.5"}, "closed", 0, id="m4-float-failures"),
            pytest.param({"failures": None}, "closed", 0, id="m5-none-failures"),
            pytest.param({"failures": "7"}, "closed", 7, id="m6-int-failures"),
            pytest.param({"state": "half-open"}, "half-open", 0, id="m7-unknown-state-passthrough"),
        ],
    )
    def test_parse_tolerates_garbage(self, clock, raw, expect_state, expect_failures):
        d = DB._dispatch_state_dict(raw)
        assert d["state"] == expect_state
        assert d["failure_count"] == expect_failures
        assert d["retry_after_seconds"] >= 0

    @pytest.mark.parametrize(
        "delta, expected",
        [
            pytest.param(29.2, 30, id="m8-fractional-ceils-up"),
            pytest.param(30.0, 30, id="m9-exact-integer-no-bump"),
            pytest.param(0.0001, 1, id="m10-epsilon-future-is-1"),
            pytest.param(0.0, 0, id="m11-exactly-now-is-0"),
            pytest.param(-5.0, 0, id="m12-past-clamped-to-0"),
        ],
    )
    def test_retry_after_ceil_boundary(self, clock, delta, expected):
        d = DB._dispatch_state_dict({"state": "open", "next_probe_at": repr(T0 + delta)})
        assert d["retry_after_seconds"] == expected

    def test_unknown_state_advertises_no_wait(self, clock):
        # m7b: only "open" advertises a wait — an unknown state with a future
        # next_probe_at must not (kills `state != "closed"`).
        d = DB._dispatch_state_dict({"state": "half-open", "next_probe_at": repr(T0 + 50)})
        assert d["retry_after_seconds"] == 0

    def test_retry_after_zero_when_closed_even_with_future_probe(self, clock):
        # m13: a stale next_probe_at on a closed hash must not advertise a wait.
        d = DB._dispatch_state_dict({"state": "closed", "next_probe_at": repr(T0 + 999)})
        assert d["retry_after_seconds"] == 0

    @pytest.mark.parametrize("bad", ["abc", "", None], ids=["m14-garbage", "m15-empty", "m16-none"])
    def test_retry_after_garbage_next_probe_is_zero(self, clock, bad):
        d = DB._dispatch_state_dict({"state": "open", "next_probe_at": bad})
        assert d["retry_after_seconds"] == 0

    @pytest.mark.parametrize("bad", ["nan", "inf"], ids=["m17-nan", "m18-inf"])
    def test_non_finite_next_probe_never_raises_through_to_dict(self, clock, bad):
        """m17/m18: `float('nan')`/`float('inf')` parse fine, then `math.ceil`
        raises. Unreachable from the Lua writer (finite tunables), but the public
        read must still never raise — `to_dict` fails OPEN to the closed default."""
        r = fakeredis.FakeRedis(decode_responses=True)
        r.hset("agent:dispatch:a", mapping={"state": "open", "next_probe_at": bad})
        b = DispatchBreaker("a", redis_client=r)
        assert b.to_dict()["retry_after_seconds"] == 0
        assert b.retry_after_seconds() == 0


class TestRecordOutcomeFalsyCodes:
    """m19-m25: the CALLER CONTRACT says only `None` is success. A falsy
    non-None code must never reset the counter."""

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(0, id="m19-zero"),
            pytest.param("", id="m20-empty-str"),
            pytest.param(False, id="m21-false"),
            pytest.param(SimpleNamespace(value=None), id="m22-enum-with-none-value"),
            pytest.param(b"auth", id="m23-bytes-auth"),
            pytest.param(" auth", id="m24-padded-auth"),
        ],
    )
    def test_falsy_or_odd_codes_are_noop_not_success(self, code):
        b = DispatchBreaker("x", redis_client=MagicMock())
        b.record_success = MagicMock(return_value=Transition("open", "closed"))
        b.record_failure = MagicMock(return_value=Transition("closed", "open"))
        assert b.record_outcome(code) == Transition.noop()
        b.record_success.assert_not_called()
        b.record_failure.assert_not_called()

    @pytest.mark.parametrize("code", ["AUTH", "Auth", "aUtH"], ids=["m25a", "m25b", "m25c"])
    def test_auth_any_case_is_failure(self, code):
        b = DispatchBreaker("x", redis_client=MagicMock())
        b.record_failure = MagicMock(return_value=Transition("closed", "closed"))
        b.record_outcome(code)
        b.record_failure.assert_called_once_with(reason="auth")


class TestMalformedLuaReplyAndFailOpen:
    def _breaker_with_script(self, monkeypatch, reply=None, raises=None):
        script = MagicMock(side_effect=raises) if raises else MagicMock(return_value=reply)
        scripts = {"allow": script, "record_failure": script, "record_success": script}
        monkeypatch.setattr(DB._DISPATCH_SCRIPTS, "ensure", lambda client: scripts)
        return DispatchBreaker("x", redis_client=MagicMock())

    @pytest.mark.parametrize(
        "reply",
        [None, [], ["open"], ["closed", "open", "x"]],
        ids=["m26-none", "m27-empty", "m28-one-elem", "m29-three-elem"],
    )
    def test_malformed_failure_reply_is_noop(self, monkeypatch, reply):
        b = self._breaker_with_script(monkeypatch, reply)
        assert b.record_failure() == Transition.noop()

    def test_bytes_reply_is_decoded(self, monkeypatch):
        # m30: a client without decode_responses returns bytes.
        b = self._breaker_with_script(monkeypatch, [b"closed", b"open"])
        t = b.record_failure()
        assert t == Transition("closed", "open") and t.opened

    def test_success_reply_none_maps_to_closed(self, monkeypatch):
        # m31: `prior or "closed"` — an empty Lua reply is not a recovery.
        b = self._breaker_with_script(monkeypatch, None)
        t = b.record_success()
        assert t == Transition("closed", "closed") and not t.closed

    @pytest.mark.parametrize(
        "unknown", ["probe", "", None], ids=["m32-probe-verdict", "m33-empty", "m34-nil"]
    )
    def test_allow_verdicts(self, monkeypatch, unknown):
        b = self._breaker_with_script(monkeypatch, unknown)
        # 'probe' admits; any other unrecognised verdict denies (fail-closed on
        # an UNKNOWN verdict is the safe direction only for a real Lua reply —
        # an exception is the fail-open path, tested below).
        assert b.allow_dispatch() is (unknown == "probe")

    @pytest.mark.parametrize("op", ["allow_dispatch", "record_failure", "record_success"])
    def test_exception_fails_open_and_resets_script_cache(self, monkeypatch, op):
        """m35: an EVALSHA error fails open (allow=True / noop) AND drops the
        script cache so the next call re-registers after a reconnect."""
        resets = []
        monkeypatch.setattr(DB, "_reset_dispatch_redis", lambda: resets.append(1))
        b = self._breaker_with_script(monkeypatch, raises=RuntimeError("NOSCRIPT"))
        out = getattr(b, op)()
        assert out in (True, Transition.noop())
        assert resets == [1]


class TestNoClientAndRecoveryLog:
    def test_record_success_no_client_is_noop(self, monkeypatch):
        # m42: Redis unreachable → success is a noop, never a phantom recovery.
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: None)
        assert DispatchBreaker("n").record_success() == Transition.noop()

    def test_recovery_from_open_is_logged(self, monkeypatch, caplog):
        # m43: open→closed is a recovery (Transition.closed) and is logged once.
        scripts = {"record_success": MagicMock(return_value="open")}
        monkeypatch.setattr(DB._DISPATCH_SCRIPTS, "ensure", lambda client: scripts)
        with caplog.at_level(logging.INFO, logger=DB.logger.name):
            t = DispatchBreaker("r", redis_client=MagicMock()).record_success()
        assert t == Transition("open", "closed") and t.closed and t.changed
        assert sum("CLOSED for r" in rec.getMessage() for rec in caplog.records) == 1


class TestOperatorHooksPure:
    def test_states_for_empty_list_is_empty_even_without_redis(self, monkeypatch):
        # m36
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: None)
        assert DB.get_dispatch_states_for([]) == {}

    def test_states_for_no_redis_is_all_closed(self, monkeypatch):
        # m37
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: None)
        out = DB.get_dispatch_states_for(["a", "b"])
        assert set(out) == {"a", "b"}
        assert all(v["state"] == "closed" for v in out.values())

    def test_states_for_duplicates_collapse(self, monkeypatch, clock):
        # m38: duplicate names in the request — one entry, no IndexError.
        r = fakeredis.FakeRedis(decode_responses=True)
        r.hset("agent:dispatch:a", mapping={"state": "open", "failures": "3",
                                            "next_probe_at": repr(T0 + 10)})
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: r)
        out = DB.get_dispatch_states_for(["a", "a", "b"])
        assert out["a"]["state"] == "open" and out["a"]["retry_after_seconds"] == 10
        assert out["b"]["state"] == "closed"

    def test_get_all_skips_probe_locks(self, monkeypatch, clock):
        # m39 (pure twin of the integration test).
        r = fakeredis.FakeRedis(decode_responses=True)
        r.hset("agent:dispatch:a", mapping={"state": "open", "failures": "3"})
        r.set("agent:dispatch:a:probe-lock", "1")
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: r)
        assert set(DB.get_all_dispatch_states()) == {"a"}

    def test_get_all_no_redis_is_empty(self, monkeypatch):
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: None)
        assert DB.get_all_dispatch_states() == {}

    def test_reset_dispatch_deletes_hash_and_lock(self, monkeypatch):
        # m40
        r = fakeredis.FakeRedis(decode_responses=True)
        r.hset("agent:dispatch:a", mapping={"state": "open"})
        r.set("agent:dispatch:a:probe-lock", "1")
        r.hset("agent:dispatch:ab", mapping={"state": "open"})  # prefix sibling untouched
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: r)
        DB.reset_dispatch("a")
        assert not r.exists("agent:dispatch:a")
        assert not r.exists("agent:dispatch:a:probe-lock")
        assert r.exists("agent:dispatch:ab")

    def test_reset_dispatch_never_raises(self, monkeypatch):
        # m41
        boom = MagicMock()
        boom.delete.side_effect = RuntimeError("down")
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: boom)
        monkeypatch.setattr(DB, "_reset_dispatch_redis", lambda: None)
        DB.reset_dispatch("a")  # no raise
        monkeypatch.setattr(DB, "get_breaker_redis", lambda: None)
        DB.reset_dispatch("a")  # no raise


# ---------------------------------------------------------------------------
# Governor against a REAL fakeredis ZSET (no Lua needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def gov(monkeypatch, clock):
    import config

    r = fakeredis.FakeRedis(decode_responses=True)
    resets = []
    monkeypatch.setattr(RG, "get_breaker_redis", lambda: r)
    monkeypatch.setattr(RG, "reset_breaker_redis_client", lambda: resets.append(1))
    monkeypatch.setattr(config, "CORRELATED_FAILURE_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(config, "CORRELATED_FAILURE_WINDOW_SECONDS", 120, raising=False)
    monkeypatch.setattr(config, "CORRELATED_PAUSE_TTL_SECONDS", 300, raising=False)
    return SimpleNamespace(g=RG.RedeliveryGovernor(), r=r, clock=clock, cfg=config, resets=resets)


class TestGovernorBoundaries:
    @pytest.mark.parametrize("n, armed", [(2, False), (3, True), (4, True)],
                             ids=["g1-N-1", "g2-N", "g3-N+1"])
    def test_threshold_n_and_neighbours(self, gov, n, armed):
        for i in range(n):
            gov.g.record_terminal_failure(f"agent-{i}", "auth")
        assert gov.g.is_paused() is armed
        assert gov.g.distinct_failing_count() == n

    def test_rearm_refreshes_ttl(self, gov):
        # g4: storm persists → each distinct failure past N re-sets the TTL.
        for i in range(3):
            gov.g.record_terminal_failure(f"a{i}", "auth")
        gov.r.expire(RG._PAUSE_KEY, 5)  # pretend most of the TTL elapsed
        gov.g.record_terminal_failure("a3", "billing")
        assert gov.r.ttl(RG._PAUSE_KEY) > 5

    @pytest.mark.parametrize(
        "age, counted",
        [
            pytest.param(120.0, False, id="g5-exactly-window-old-is-evicted"),
            pytest.param(119.999, True, id="g6-just-inside-window"),
            pytest.param(120.001, False, id="g7-just-outside-window"),
        ],
    )
    def test_window_cutoff_is_inclusive(self, gov, age, counted):
        gov.g.record_terminal_failure("old", "auth")
        gov.clock.t = T0 + age
        gov.g.record_terminal_failure("new", "auth")
        assert gov.g.distinct_failing_count() == (2 if counted else 1)

    def test_same_agent_refresh_keeps_it_alive(self, gov):
        # g8: a re-failing agent's score is refreshed, so it survives the window.
        gov.g.record_terminal_failure("loop", "auth")
        gov.clock.t = T0 + 100
        gov.g.record_terminal_failure("loop", "auth")
        gov.clock.t = T0 + 200  # first failure 200s old, second 100s old
        assert gov.g.distinct_failing_count() == 1

    def test_clock_backwards_keeps_future_scores(self, gov):
        """g9: time going backwards. Entries scored in the 'future' are never
        evicted by the `0..cutoff` range until the clock catches up — they are
        counted for (window + skew) seconds. Pinned as current behaviour; only
        reachable via a host clock step (all writers share one host clock)."""
        gov.g.record_terminal_failure("a", "auth")
        gov.clock.t = T0 - 1000
        gov.g.record_terminal_failure("b", "auth")
        assert gov.g.distinct_failing_count() == 2

    @pytest.mark.parametrize(
        "code, counted",
        [
            ("AUTH", True), ("Billing", True), ("auth", True),
            (" auth", False), ("authx", False), ("", False), (None, False),
            ("timeout", False), ("circuit_open", False),
        ],
        ids=["g10-upper", "g11-mixed", "g12-lower", "g13-padded", "g14-prefix",
             "g15-empty", "g16-none", "g17-timeout", "g18-circuit-open"],
    )
    def test_code_folding(self, gov, code, counted):
        gov.g.record_terminal_failure("a", code)
        assert gov.g.distinct_failing_count() == (1 if counted else 0)

    @pytest.mark.parametrize("threshold", [1, 0, -1], ids=["g19-one", "g20-zero", "g21-negative"])
    def test_degenerate_threshold_arms_on_first(self, gov, threshold):
        """A threshold <= 1 arms on the FIRST correlated failure (no validation
        on CORRELATED_FAILURE_THRESHOLD). Pinned so a change is deliberate."""
        gov.cfg.CORRELATED_FAILURE_THRESHOLD = threshold
        gov.g.record_terminal_failure("solo", "auth")
        assert gov.g.is_paused() is True

    def test_zero_window_counts_only_the_current_instant(self, gov):
        # g22: window=0 → cutoff == now removes every prior score <= now, then
        # adds the current agent → distinct is always 1 at a frozen instant.
        gov.cfg.CORRELATED_FAILURE_WINDOW_SECONDS = 0
        for i in range(5):
            gov.g.record_terminal_failure(f"a{i}", "auth")
        assert gov.g.is_paused() is False

    def test_rejected_pause_ttl_fails_open(self, gov):
        """g23: PAUSE_TTL=0 → Redis rejects `SET ... EX 0`. The arm fails OPEN
        (no pause, no raise) and the client is reset."""
        gov.cfg.CORRELATED_PAUSE_TTL_SECONDS = 0
        for i in range(3):
            gov.g.record_terminal_failure(f"a{i}", "auth")
        assert gov.g.is_paused() is False
        assert gov.resets  # reset_breaker_redis_client was called

    def test_pipeline_error_fails_open_and_resets(self, gov, monkeypatch):
        # g24
        boom = MagicMock()
        boom.pipeline.return_value.execute.side_effect = RuntimeError("down")
        monkeypatch.setattr(RG, "get_breaker_redis", lambda: boom)
        gov.g.record_terminal_failure("a", "auth")
        boom.set.assert_not_called()
        assert gov.resets == [1]

    def test_count_error_is_zero(self, gov, monkeypatch):
        # g25
        boom = MagicMock()
        boom.zremrangebyscore.side_effect = RuntimeError("down")
        monkeypatch.setattr(RG, "get_breaker_redis", lambda: boom)
        assert gov.g.distinct_failing_count() == 0
        assert gov.resets == [1]

    def test_count_no_redis_is_zero(self, gov, monkeypatch):
        monkeypatch.setattr(RG, "get_breaker_redis", lambda: None)
        assert gov.g.distinct_failing_count() == 0

    def test_is_paused_error_resets_and_false(self, gov, monkeypatch):
        # g26
        boom = MagicMock()
        boom.get.side_effect = RuntimeError("down")
        monkeypatch.setattr(RG, "get_breaker_redis", lambda: boom)
        assert gov.g.is_paused() is False
        assert gov.g.should_hold_reaper() is False
        assert gov.resets == [1, 1]

    def test_pause_key_ttl_is_configured_value(self, gov):
        # g27: the arm writes the configured TTL (relation, not a pinned number).
        gov.cfg.CORRELATED_PAUSE_TTL_SECONDS = 77
        for i in range(3):
            gov.g.record_terminal_failure(f"a{i}", "auth")
        assert 0 < gov.r.ttl(RG._PAUSE_KEY) <= 77

    def test_corr_key_gets_window_plus_one_expiry(self, gov):
        # g28: the ZSET outlives the window (self-expires one second after it),
        # so a member is evicted by the range-remove, never by key expiry first.
        gov.g.record_terminal_failure("a", "auth")
        assert 120 < gov.r.ttl(RG._CORR_KEY) <= 121

    def test_should_hold_reaper_tracks_is_paused(self, gov):
        # g29
        assert gov.g.should_hold_reaper() is False
        for i in range(3):
            gov.g.record_terminal_failure(f"a{i}", "auth")
        assert gov.g.should_hold_reaper() is True

    def test_singleton_identity(self, monkeypatch):
        # g30
        monkeypatch.setattr(RG, "_governor", None)
        assert RG.get_redelivery_governor() is RG.get_redelivery_governor()

    def test_concurrent_distinct_recorders_arm_once_stable(self, gov):
        """g31: N threads recording N distinct agents concurrently — the pause is
        armed and the distinct count is exact (ZADD is per-member idempotent)."""
        gov.cfg.CORRELATED_FAILURE_THRESHOLD = 8
        threads = [threading.Thread(target=gov.g.record_terminal_failure, args=(f"t{i}", "auth"))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert gov.g.distinct_failing_count() == 8
        assert gov.g.is_paused() is True


# ===========================================================================
# LUA TIER — the production state machine, executed
# ===========================================================================


@needs_lua
class TestLuaThreshold:
    @pytest.mark.parametrize("n", [1, 2, 3, 5], ids=lambda n: f"b1-threshold-{n}")
    def test_opens_at_exactly_n(self, lua_redis, clock, tunables, n):
        tunables(threshold=n)
        b = DispatchBreaker("a", redis_client=lua_redis)
        ts = _open(b, n + 1)
        # N-1 failures: closed, no transition.
        assert all(t == Transition("closed", "closed") for t in ts[: n - 1])
        # Exactly the N-th opens (fresh open → drain fires once).
        assert ts[n - 1] == Transition("closed", "open") and ts[n - 1].opened
        # N+1: open→open — NOT a second drain.
        assert ts[n] == Transition("open", "open") and not ts[n].opened
        assert sum(t.opened for t in ts) == 1

    @pytest.mark.parametrize("n", [0, -3], ids=["b2-threshold-zero", "b3-threshold-negative"])
    def test_degenerate_threshold_opens_on_first(self, lua_redis, clock, tunables, n):
        tunables(threshold=n)
        b = DispatchBreaker("a", redis_client=lua_redis)
        assert b.record_failure().opened

    def test_success_between_failures_restarts_count(self, lua_redis, clock, tunables):
        # b4: consecutive model — N-1, success, N-1 never opens.
        b = DispatchBreaker("a", redis_client=lua_redis)
        _open(b, 2)
        b.record_success()
        assert not any(t.opened for t in _open(b, 2))
        assert b.to_dict()["state"] == "closed"


@needs_lua
class TestLuaCooldownLadder:
    def test_ladder_doubles_then_caps(self, lua_redis, clock, tunables):
        # b5: 30, 60, 120, 240, 300(cap), 300.
        b = DispatchBreaker("a", redis_client=lua_redis)
        _open(b, 2)
        gaps = []
        for _ in range(6):
            b.record_failure()
            gaps.append(float(_hash(lua_redis)["next_probe_at"]) - T0)
        assert gaps == pytest.approx([30, 60, 120, 240, 300, 300])

    def test_exponent_clamped_at_20(self, lua_redis, clock, tunables):
        # b6: with an effectively-infinite cap, probe_count 25 → base * 2**20.
        tunables(threshold=1, base=1.0, max_cd=1e12)
        b = DispatchBreaker("a", redis_client=lua_redis)
        for _ in range(25):
            b.record_failure()
        assert int(_hash(lua_redis)["probe_count_since_open"]) == 25
        assert float(_hash(lua_redis)["next_probe_at"]) - T0 == pytest.approx(2 ** 20)

    def test_max_below_base_caps_first_cooldown(self, lua_redis, clock, tunables):
        # b7: misconfig max < base → max wins from the very first open.
        tunables(threshold=1, base=30.0, max_cd=5.0)
        b = DispatchBreaker("a", redis_client=lua_redis)
        b.record_failure()
        assert float(_hash(lua_redis)["next_probe_at"]) - T0 == pytest.approx(5.0)

    def test_zero_base_probes_immediately(self, lua_redis, clock, tunables):
        # b8: base=0 → next_probe_at == now → the very next allow is a probe.
        tunables(threshold=1, base=0.0)
        b = DispatchBreaker("a", redis_client=lua_redis)
        b.record_failure()
        assert b.allow_dispatch() is True and _lock_exists(lua_redis)

    def test_success_resets_backoff_ladder(self, lua_redis, clock, tunables):
        # b9: after recovery, the next open starts again at base, not where it left off.
        b = DispatchBreaker("a", redis_client=lua_redis)
        _open(b, 6)  # deep into the ladder
        assert b.record_success().closed
        h = _hash(lua_redis)
        assert h["state"] == "closed" and h["failures"] == "0"
        assert h["probe_count_since_open"] == "0" and float(h["next_probe_at"]) == 0
        _open(b, 3)
        assert float(_hash(lua_redis)["next_probe_at"]) - T0 == pytest.approx(30)


@needs_lua
class TestLuaProbeBoundary:
    def _opened(self, r):
        b = DispatchBreaker("a", redis_client=r)
        _open(b, 3)
        return b, float(_hash(r)["next_probe_at"])

    def test_closed_allow_never_takes_lock(self, lua_redis, clock, tunables):
        # b10
        b = DispatchBreaker("a", redis_client=lua_redis)
        assert b.allow_dispatch() is True
        assert not _lock_exists(lua_redis)

    def test_boundary_instant(self, lua_redis, clock, tunables):
        """b11/b12: `now < next_probe_at` → deny at npa-ε, probe at exactly npa."""
        b, npa = self._opened(lua_redis)
        clock.t = npa - 0.001
        assert b.allow_dispatch() is False
        assert not _lock_exists(lua_redis)  # a denied dispatch takes no lock
        clock.t = npa
        assert b.allow_dispatch() is True
        assert _lock_exists(lua_redis)

    def test_probe_is_exclusive_until_lock_lapses(self, lua_redis, clock, tunables):
        # b13/b14: one probe per lock; lock lapse (TTL) admits exactly one more.
        b, npa = self._opened(lua_redis)
        clock.t = npa + 1
        assert b.allow_dispatch() is True
        assert b.allow_dispatch() is False
        assert DispatchBreaker("a", redis_client=lua_redis).allow_dispatch() is False
        ttl = lua_redis.ttl("agent:dispatch:a:probe-lock")
        assert 0 < ttl <= DB.DISPATCH_PROBE_LOCK_TTL_SECONDS
        lua_redis.delete("agent:dispatch:a:probe-lock")  # simulate the TTL lapse
        assert b.allow_dispatch() is True

    def test_probe_failure_releases_lock_and_grows_backoff(self, lua_redis, clock, tunables):
        # b15
        b, npa = self._opened(lua_redis)
        clock.t = npa
        assert b.allow_dispatch() is True
        t = b.record_failure("auth")
        assert t == Transition("open", "open") and not t.opened
        assert not _lock_exists(lua_redis)
        assert float(_hash(lua_redis)["next_probe_at"]) - npa == pytest.approx(60)
        assert b.allow_dispatch() is False  # back inside a (longer) cooldown

    def test_probe_success_closes_and_releases_lock(self, lua_redis, clock, tunables):
        # b16
        b, npa = self._opened(lua_redis)
        clock.t = npa
        b.allow_dispatch()
        t = b.record_success()
        assert t.closed and not _lock_exists(lua_redis)
        assert b.allow_dispatch() is True

    def test_clock_backwards_extends_open(self, lua_redis, clock, tunables):
        """b17: time going backwards after an open. The cooldown is an absolute
        `next_probe_at` from the recorder's clock, so a backward step lengthens
        the deny window by the step size and retry_after reports it honestly.
        Never admits early; never raises."""
        b, npa = self._opened(lua_redis)
        clock.t = T0 - 3600
        assert b.allow_dispatch() is False
        assert b.retry_after_seconds() == math.ceil(npa - clock.t)

    def test_straggler_failure_while_open_extends_and_drops_probe_lock(
        self, lua_redis, clock, tunables
    ):
        """b18: an in-flight execution dispatched BEFORE the open that lands an
        AUTH terminal while open is indistinguishable from a failed probe: it
        climbs the backoff ladder and DELs a live probe-lock. Pinned as current
        behaviour (see UNSPECIFIED in the report)."""
        b, npa = self._opened(lua_redis)
        clock.t = npa
        assert b.allow_dispatch() is True          # probe in flight
        b.record_failure("auth")                   # straggler, not the probe
        assert not _lock_exists(lua_redis)
        assert int(_hash(lua_redis)["probe_count_since_open"]) == 2

    def test_denied_during_inflight_probe_advertises_zero_wait(self, lua_redis, clock, tunables):
        """b27: cooldown elapsed + a sibling holds the probe-lock → allow is
        DENIED but retry_after_seconds is 0, so the CircuitOpen 503 carries
        `Retry-After: 0` (capacity_manager.py:354 → routers/chat.py:137) for a
        retry that is guaranteed to fail until the probe resolves. Pinned as
        current behaviour — flagged UNSPECIFIED in the report, not a bug: the
        docstring ("seconds until the next half-open probe is allowed") is met
        literally."""
        b, npa = self._opened(lua_redis)
        clock.t = npa + 5
        assert DispatchBreaker("a", redis_client=lua_redis).allow_dispatch() is True
        assert b.allow_dispatch() is False
        assert b.retry_after_seconds() == 0

    def test_retry_after_matches_ladder(self, lua_redis, clock, tunables):
        # b19
        b, _ = self._opened(lua_redis)
        assert b.retry_after_seconds() == 30
        assert b.to_dict() == {"state": "open", "failure_count": 3, "retry_after_seconds": 30}

    def test_success_on_clean_breaker_writes_nothing(self, lua_redis, clock, tunables):
        # b20 (unit twin of the integration-only test)
        b = DispatchBreaker("a", redis_client=lua_redis)
        assert b.record_success() == Transition("closed", "closed")
        assert not lua_redis.exists("agent:dispatch:a")

    def test_noop_success_does_not_touch_a_foreign_lock(self, lua_redis, clock, tunables):
        # b21: the closed/0 early return also skips the DEL of the probe-lock.
        lua_redis.set("agent:dispatch:a:probe-lock", "1")
        DispatchBreaker("a", redis_client=lua_redis).record_success()
        assert _lock_exists(lua_redis)

    def test_failure_records_last_failure_ts(self, lua_redis, clock, tunables):
        # b22
        b = DispatchBreaker("a", redis_client=lua_redis)
        b.record_failure()
        assert float(_hash(lua_redis)["last_failure_ts"]) == pytest.approx(T0)

    def test_agents_are_isolated(self, lua_redis, clock, tunables):
        # b23: prefix-sibling names never share a counter.
        _open(DispatchBreaker("a", redis_client=lua_redis), 3)
        assert DispatchBreaker("ab", redis_client=lua_redis).allow_dispatch() is True
        assert DispatchBreaker("ab", redis_client=lua_redis).to_dict()["failure_count"] == 0


@needs_lua
class TestLuaConcurrency:
    def test_concurrent_failures_open_exactly_once(self, lua_redis, clock, tunables):
        """b24: 16 threads race AUTH failures across the threshold — the atomic
        Lua yields exactly one fresh `opened` (one drain + one audit)."""
        b = DispatchBreaker("a", redis_client=lua_redis)
        out: list = []
        lock = threading.Lock()

        def worker():
            t = b.record_failure("auth")
            with lock:
                out.append(t)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(t.opened for t in out) == 1
        assert int(_hash(lua_redis)["failures"]) == 16

    def test_concurrent_probe_admits_exactly_one(self, lua_redis, clock, tunables):
        # b25
        b = DispatchBreaker("a", redis_client=lua_redis)
        _open(b, 3)
        clock.t = T0 + 31
        out: list = []
        lock = threading.Lock()

        def worker():
            v = DispatchBreaker("a", redis_client=lua_redis).allow_dispatch()
            with lock:
                out.append(v)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert out.count(True) == 1

    def test_concurrent_trip_and_reset_ends_consistent(self, lua_redis, clock, tunables):
        """b26: failures racing successes. Whatever the interleaving, the final
        hash is internally consistent: `open` always carries a future
        next_probe_at and a positive probe count; `closed` always carries a
        sub-threshold failure count (never a closed breaker that should be open)."""
        b = DispatchBreaker("a", redis_client=lua_redis)

        def fail():
            for _ in range(20):
                b.record_failure("auth")

        def succeed():
            for _ in range(20):
                b.record_success()

        ts = [threading.Thread(target=fail), threading.Thread(target=succeed)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        h = _hash(lua_redis)
        if h.get("state") == "open":
            assert int(h["probe_count_since_open"]) >= 1
            assert float(h["next_probe_at"]) > T0
        else:
            assert int(h.get("failures", 0)) < DB.DISPATCH_FAILURE_THRESHOLD

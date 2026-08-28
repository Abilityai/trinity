"""
#2409 — auto-switch ranks alternative subscriptions by cached headroom.
Related flow: docs/memory/feature-flows/subscription-auto-switch.md

`select_best_alternative_subscription` used to return the FIRST survivor of the
2h failure filter in `agent_count ASC` order and read no headroom at all, so
SUB-003 could relocate an agent onto a subscription at 99% of its weekly window
— and an *unused dead-token* subscription (no agents ⇒ no failure rows) sorted
first. These tests pin the agreed design (issue comment, 2026-08-27):

  1. the recent-failure filter is unchanged and runs FIRST (db layer, SQL);
  2. survivors are ranked by the cached provider snapshot — ONE MGET, never a
     probe — furthest from the NEAREST wall first (the fuller of the two
     windows), banded so load-balance still spreads a storm within a band;
  3. a FRESH provider refusal (probe 429 / blocking window / rejected token)
     is filtered out; a stale one is merely unknown;
  4. missing / stale / unreadable readings never block a switch — they sort
     after measured ones in today's order, and any ranking failure degrades
     to today's order with a WARNING (never silently);
  5. `classify_headroom` (ent#434) keeps every verdict byte-for-byte — it is
     now policy over the same gate the ranker consumes, pinned by a
     differential test against a frozen copy of the pre-#2409 function.

Real modules, attributes monkeypatched (the ent#434 harness shape). Redis is
fakeredis or None — never the network. Time is injected via
`snapshot_age_seconds` / `fetched_at`, never read from the clock, except in the
end-to-end fakeredis cases where `fetched_at` is stamped relative to now.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _headroom(**kw):
    from db_models import HeadroomWindow, SubscriptionHeadroom
    five = kw.pop("five_hour", None)
    seven = kw.pop("seven_day", None)
    return SubscriptionHeadroom(
        five_hour=HeadroomWindow(**five) if isinstance(five, dict) else five,
        seven_day=HeadroomWindow(**seven) if isinstance(seven, dict) else seven,
        **kw,
    )


def _fresh(seven=None, five=None, *, age=30, status="ok"):
    """A snapshot model. `seven`/`five` are dicts (HeadroomWindow kwargs) or None."""
    return _headroom(seven_day=seven, five_hour=five, snapshot_age_seconds=age, status=status)


def _sub(sid, *, agents=0, name=None):
    return SimpleNamespace(id=sid, name=name or sid, agent_count=agents)


def _svc():
    import services.subscription_headroom_service as headroom
    return headroom


def _auto_switch():
    import services.subscription_auto_switch as auto_switch
    return auto_switch


# =============================================================================
# A — the gate: `headroom_reading` decides what is USABLE at all
# =============================================================================

class TestGate:

    def test_none_snapshot_is_unusable(self):
        assert _svc().headroom_reading(None) is None

    def test_missing_age_is_unusable(self):
        h = _fresh({"utilization_pct": 10.0, "status": "allowed"})
        h = h.model_copy(update={"snapshot_age_seconds": None})
        assert _svc().headroom_reading(h) is None

    def test_stale_is_unusable_at_the_default_bound(self):
        svc = _svc()
        h = _fresh({"utilization_pct": 10.0, "status": "allowed"}, age=svc.FRESHNESS_SECONDS + 1)
        assert svc.headroom_reading(h) is None
        assert svc.headroom_reading(h, max_age_seconds=svc.FRESHNESS_SECONDS + 2) is not None

    @pytest.mark.parametrize("status", ["error", "no_windows", "something_new"])
    def test_non_answers_are_unusable(self, status):
        h = _fresh({"utilization_pct": 10.0, "status": "allowed"}, status=status)
        assert _svc().headroom_reading(h) is None

    @pytest.mark.parametrize("status", ["rate_limited", "invalid_token"])
    def test_refusals_pass_the_gate_as_readings(self, status):
        """The probe LEARNED something — the policy layers decide what it means."""
        r = _svc().headroom_reading(_fresh(None, status=status))
        assert r is not None
        assert r.provider_status == status
        assert r.refusing is True

    def test_ok_reading_carries_both_windows_and_resets(self):
        r = _svc().headroom_reading(_fresh(
            {"utilization_pct": 39.0, "status": "allowed", "resets_at": "2026-09-01T00:00:00Z"},
            {"utilization_pct": 21.0, "status": "allowed_warning", "resets_at": "2026-08-27T16:10:00Z"},
            age=12,
        ))
        assert r.age_seconds == 12
        assert r.provider_status == "ok"
        assert r.seven_day.utilization_pct == 39.0 and r.seven_day.blocked is False
        assert r.seven_day.resets_at == "2026-09-01T00:00:00Z"
        assert r.five_hour.utilization_pct == 21.0 and r.five_hour.blocked is False
        assert r.refusing is False

    def test_allowed_warning_is_not_blocking(self):
        """#2396's rule, inherited: the provider is still serving."""
        r = _svc().headroom_reading(_fresh({"utilization_pct": 90.0, "status": "allowed_warning"}))
        assert r.seven_day.blocked is False and r.refusing is False

    @pytest.mark.parametrize("status", ["rejected", "blocked", "brand_new_status"])
    def test_any_status_outside_the_allowlist_is_blocking(self, status):
        """An ALLOWLIST: an unrecognised status reads as blocking (the #848 inverse)."""
        r = _svc().headroom_reading(_fresh({"utilization_pct": 50.0, "status": status}))
        assert r.seven_day.blocked is True and r.refusing is True

    def test_none_window_status_is_not_blocking(self):
        r = _svc().headroom_reading(_fresh({"utilization_pct": 50.0, "status": None}))
        assert r.seven_day.blocked is False

    def test_blocking_five_hour_window_alone_is_refusing(self):
        r = _svc().headroom_reading(_fresh(
            {"utilization_pct": 20.0, "status": "allowed"},
            {"utilization_pct": 100.0, "status": "rejected"},
        ))
        assert r.refusing is True


# =============================================================================
# B — `classify_headroom` is byte-for-byte the pre-#2409 function
# =============================================================================

_NON_BLOCKING = frozenset({"allowed", "allowed_warning"})


def _classify_headroom_frozen(headroom, *, threshold_pct, max_age_seconds=None):
    """VERBATIM copy of `classify_headroom` as merged in 908f8713 (ent#434),
    with the module constants inlined. Never edit — it is the oracle."""
    svc = _svc()
    if headroom is None:
        return "unassessable"
    age = headroom.snapshot_age_seconds
    limit = svc.FRESHNESS_SECONDS if max_age_seconds is None else max_age_seconds
    if age is None or age > limit:
        return "unassessable"
    if headroom.status == "rate_limited":
        return "saturated"
    if headroom.status != "ok":
        return "unassessable"
    week = headroom.seven_day
    if week is None:
        return "unassessable"
    if week.status is not None and week.status not in _NON_BLOCKING:
        return "saturated"
    if week.utilization_pct is None:
        return "unassessable"
    return "saturated" if week.utilization_pct >= threshold_pct else "has_headroom"


_AGES = [None, 0, 1799, 1801, 7199, 7201]
_STATUSES = ["ok", "rate_limited", "invalid_token", "error", "no_windows", "weird"]
_WEEKS = [
    None,
    {"utilization_pct": None, "status": None},
    {"utilization_pct": None, "status": "allowed"},
    {"utilization_pct": None, "status": "rejected"},
    {"utilization_pct": 5.0, "status": "rejected"},
    {"utilization_pct": 74.9, "status": "allowed"},
    {"utilization_pct": 75.0, "status": "allowed"},
    {"utilization_pct": 90.0, "status": "allowed_warning"},
    {"utilization_pct": 100.0, "status": None},
    {"utilization_pct": float("nan"), "status": "allowed"},
]
_FIVES = [None, {"utilization_pct": 99.0, "status": "rejected"}]
_THRESHOLDS = [50, 75, 90, 99]
_MAX_AGES = [None, 7200]


class TestClassifierParity:

    def test_full_case_product_matches_the_frozen_oracle(self):
        svc = _svc()
        checked = 0
        for age in _AGES:
            for status in _STATUSES:
                for week in _WEEKS:
                    for five in _FIVES:
                        h = _fresh(week, five, age=age, status=status)
                        for t in _THRESHOLDS:
                            for ma in _MAX_AGES:
                                got = svc.classify_headroom(h, threshold_pct=t, max_age_seconds=ma)
                                want = _classify_headroom_frozen(h, threshold_pct=t, max_age_seconds=ma)
                                assert got == want, (age, status, week, five, t, ma, got, want)
                                checked += 1
        assert svc.classify_headroom(None, threshold_pct=75) == "unassessable"
        assert checked == len(_AGES) * len(_STATUSES) * len(_WEEKS) * len(_FIVES) * len(_THRESHOLDS) * len(_MAX_AGES)

    def test_classifier_is_policy_over_the_shared_gate(self):
        """No second freshness/status rule: the classifier calls the gate."""
        src = (_BACKEND / "services" / "subscription_headroom_service.py").read_text()
        body = src[src.index("def classify_headroom"):src.index("def cached_headroom_readings")]
        assert "headroom_reading(" in body


# =============================================================================
# C — the ranker: tiers, keys, bands, stability
# =============================================================================

class TestSelectionVerdict:

    def test_no_reading_is_unknown(self):
        assert _svc().selection_verdict(None)[0] == _svc().SELECTION_UNKNOWN

    def test_nearest_wall_is_the_fuller_window(self):
        svc = _svc()
        r = svc.headroom_reading(_fresh({"utilization_pct": 20.0, "status": "allowed"},
                                        {"utilization_pct": 98.0, "status": "allowed"}))
        tier, primary, other = svc.selection_verdict(r)
        assert (tier, primary, other) == (svc.SELECTION_MEASURED, 98.0, 20.0)

    def test_weekly_only_reading_is_measured(self):
        svc = _svc()
        r = svc.headroom_reading(_fresh({"utilization_pct": 39.0, "status": "allowed"}))
        assert svc.selection_verdict(r) == (svc.SELECTION_MEASURED, 39.0, None)

    def test_five_hour_only_reading_is_unknown(self):
        """The weekly figure is the AC's key; without it nothing is ranked."""
        svc = _svc()
        r = svc.headroom_reading(_fresh(None, {"utilization_pct": 5.0, "status": "allowed"}))
        assert svc.selection_verdict(r)[0] == svc.SELECTION_UNKNOWN

    def test_stale_five_hour_figure_is_dropped_but_weekly_kept(self):
        """The 5h window can fully reset inside the 2h weekly bound — a 5h figure
        older than the display bound is not evidence about the next minute."""
        svc = _svc()
        r = svc.headroom_reading(
            _fresh({"utilization_pct": 30.0, "status": "allowed"},
                   {"utilization_pct": 99.0, "status": "allowed"},
                   age=svc.FAST_WINDOW_FRESHNESS_SECONDS + 1),
            max_age_seconds=svc.MAX_READING_AGE_SECONDS,
        )
        assert svc.selection_verdict(r) == (svc.SELECTION_MEASURED, 30.0, None)

    @pytest.mark.parametrize("status", ["rate_limited", "invalid_token"])
    def test_fresh_provider_refusal_is_refused(self, status):
        svc = _svc()
        r = svc.headroom_reading(_fresh({"utilization_pct": 5.0, "status": "allowed"}, status=status))
        assert svc.selection_verdict(r)[0] == svc.SELECTION_REFUSED

    def test_fresh_blocking_window_is_refused(self):
        svc = _svc()
        r = svc.headroom_reading(_fresh({"utilization_pct": 5.0, "status": "allowed"},
                                        {"utilization_pct": 100.0, "status": "rejected"}))
        assert svc.selection_verdict(r)[0] == svc.SELECTION_REFUSED

    def test_stale_refusal_is_merely_unknown(self):
        """A refusal is a point-in-time verdict: past the LIMIT-badge bound it
        neither blocks nor ranks."""
        svc = _svc()
        r = svc.headroom_reading(
            _fresh({"utilization_pct": 5.0, "status": "allowed"},
                   status="rate_limited", age=svc.REFUSAL_FRESHNESS_SECONDS + 1),
            max_age_seconds=svc.MAX_READING_AGE_SECONDS,
        )
        assert svc.selection_verdict(r)[0] == svc.SELECTION_UNKNOWN

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_figures_are_no_figure(self, bad):
        svc = _svc()
        r = svc.headroom_reading(_fresh({"utilization_pct": bad, "status": "allowed"},
                                        {"utilization_pct": bad, "status": "allowed"}))
        assert svc.selection_verdict(r)[0] == svc.SELECTION_UNKNOWN

    @pytest.mark.parametrize("bad", [-0.1, -5.0, -100.0])
    def test_a_negative_figure_is_no_figure(self, bad):
        """The sanitiser is two-sided (PR #2422 review): a negative utilization
        is malformed provider data, not a very empty subscription. Unbounded, it
        would band to -1 and sort AHEAD of a genuinely empty one."""
        svc = _svc()
        r = svc.headroom_reading(_fresh({"utilization_pct": bad, "status": "allowed"},
                                        {"utilization_pct": bad, "status": "allowed"}))
        assert svc.selection_verdict(r)[0] == svc.SELECTION_UNKNOWN


class TestRankSubscriptions:

    def _readings(self, svc, **per_id):
        return {sid: (svc.headroom_reading(h) if h is not None else None) for sid, h in per_id.items()}

    def test_lowest_nearest_wall_wins(self):
        svc = _svc()
        cands = [_sub("a"), _sub("b"), _sub("c")]
        readings = self._readings(
            svc,
            a=_fresh({"utilization_pct": 61.0, "status": "allowed"}),
            b=_fresh({"utilization_pct": 12.0, "status": "allowed"}),
            c=_fresh({"utilization_pct": 45.0, "status": "allowed"}),
        )
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["b", "c", "a"]

    def test_a_negative_figure_never_outranks_an_empty_subscription(self):
        """A malformed negative reading must not beat a real 0% one — it is
        UNKNOWN, so it sorts after every measured candidate."""
        svc = _svc()
        cands = [_sub("neg"), _sub("empty")]
        readings = self._readings(
            svc,
            neg=_fresh({"utilization_pct": -5.0, "status": "allowed"}),
            empty=_fresh({"utilization_pct": 0.0, "status": "allowed"}),
        )
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["empty", "neg"]

    def test_the_reviewers_example_a_full_five_hour_window_loses(self):
        """7d 20% / 5h 98% fails the #792 retry within the minute; 7d 25% / 5h 10% does not."""
        svc = _svc()
        cands = [_sub("a"), _sub("b")]
        readings = self._readings(
            svc,
            a=_fresh({"utilization_pct": 20.0, "status": "allowed"}, {"utilization_pct": 98.0, "status": "allowed"}),
            b=_fresh({"utilization_pct": 25.0, "status": "allowed"}, {"utilization_pct": 10.0, "status": "allowed"}),
        )
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["b", "a"]

    def test_measured_before_unknown(self):
        svc = _svc()
        cands = [_sub("unknown", agents=0), _sub("measured", agents=5)]
        readings = self._readings(svc, unknown=None,
                                  measured=_fresh({"utilization_pct": 88.0, "status": "allowed"}))
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["measured", "unknown"]

    def test_fresh_refusals_are_filtered_out(self):
        svc = _svc()
        cands = [_sub("dead", agents=0), _sub("limited", agents=0), _sub("blocked", agents=0), _sub("ok", agents=9)]
        readings = self._readings(
            svc,
            dead=_fresh({"utilization_pct": 1.0, "status": "allowed"}, status="invalid_token"),
            limited=_fresh({"utilization_pct": 1.0, "status": "allowed"}, status="rate_limited"),
            blocked=_fresh({"utilization_pct": 1.0, "status": "allowed"}, {"utilization_pct": 100.0, "status": "rejected"}),
            ok=_fresh({"utilization_pct": 97.0, "status": "allowed_warning"}),
        )
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["ok"]

    def test_all_refused_yields_nothing(self):
        svc = _svc()
        cands = [_sub("a"), _sub("b")]
        readings = self._readings(svc, a=_fresh(None, status="rate_limited"), b=_fresh(None, status="invalid_token"))
        assert svc.rank_subscriptions(cands, readings) == []

    def test_all_saturated_but_serving_picks_the_least_saturated(self):
        """AC #3: nearly full beats stuck. Serving readings are never filtered."""
        svc = _svc()
        cands = [_sub("a"), _sub("b")]
        readings = self._readings(svc, a=_fresh({"utilization_pct": 99.0, "status": "allowed_warning"}),
                                  b=_fresh({"utilization_pct": 96.0, "status": "allowed_warning"}))
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["b", "a"]

    def test_all_unknown_is_todays_order_regardless_of_input_order(self):
        """agent_count ASC, name ASC — deterministic even when the input is shuffled."""
        svc = _svc()
        cands = [_sub("z", agents=3), _sub("m", agents=0), _sub("a", agents=0), _sub("k", agents=1)]
        readings = {c.id: None for c in cands}
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["a", "m", "k", "z"]

    def test_bands_let_load_balance_spread_a_storm(self):
        """Snapshots do not move during a storm; within a 10-point band the
        agent count — the only key that DOES move as switches land — decides."""
        svc = _svc()
        cands = [_sub("busy", agents=20), _sub("idle", agents=0)]
        readings = self._readings(svc, busy=_fresh({"utilization_pct": 39.0, "status": "allowed"}),
                                  idle=_fresh({"utilization_pct": 39.4, "status": "allowed"}))
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["idle", "busy"]

    def test_a_full_band_of_headroom_beats_load_balance(self):
        svc = _svc()
        cands = [_sub("busy", agents=20), _sub("idle", agents=0)]
        readings = self._readings(svc, busy=_fresh({"utilization_pct": 29.0, "status": "allowed"}),
                                  idle=_fresh({"utilization_pct": 41.0, "status": "allowed"}))
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["busy", "idle"]

    def test_within_a_band_and_equal_load_the_exact_figure_then_the_other_window_decide(self):
        svc = _svc()
        cands = [_sub("a"), _sub("b"), _sub("c")]
        readings = self._readings(
            svc,
            a=_fresh({"utilization_pct": 33.0, "status": "allowed"}, {"utilization_pct": 30.0, "status": "allowed"}),
            b=_fresh({"utilization_pct": 33.0, "status": "allowed"}, {"utilization_pct": 5.0, "status": "allowed"}),
            c=_fresh({"utilization_pct": 31.0, "status": "allowed"}, {"utilization_pct": 30.0, "status": "allowed"}),
        )
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["c", "b", "a"]

    def test_missing_other_window_sorts_after_a_known_one(self):
        """Same nearest wall (7d 33%), same load: a known, emptier 5h beats no
        5h figure at all — unknown sorts after known at every level."""
        svc = _svc()
        cands = [_sub("a"), _sub("b")]
        readings = self._readings(svc, a=_fresh({"utilization_pct": 33.0, "status": "allowed"}),
                                  b=_fresh({"utilization_pct": 33.0, "status": "allowed"}, {"utilization_pct": 20.0, "status": "allowed"}))
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["b", "a"]

    def test_a_fuller_five_hour_window_is_the_nearest_wall(self):
        """The inverse: 7d 33% / 5h 90% is a 90% wall, and loses to a bare 7d 33%."""
        svc = _svc()
        cands = [_sub("a"), _sub("b")]
        readings = self._readings(svc, a=_fresh({"utilization_pct": 33.0, "status": "allowed"}),
                                  b=_fresh({"utilization_pct": 33.0, "status": "allowed"}, {"utilization_pct": 90.0, "status": "allowed"}))
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["a", "b"]

    def test_ranking_only_reorders_serving_survivors(self):
        """Never fewer candidates than the input, minus fresh refusals."""
        svc = _svc()
        cands = [_sub(str(i), agents=i) for i in range(6)]
        readings = {c.id: None for c in cands}
        readings["2"] = svc.headroom_reading(_fresh({"utilization_pct": 50.0, "status": "allowed"}))
        assert sorted(s.id for s in svc.rank_subscriptions(cands, readings)) == [c.id for c in cands]


# =============================================================================
# D — the reader: one MGET, never a probe, tri-state on Redis
# =============================================================================

def _snapshot(*, age, status="ok", seven=None, five=None, now=None):
    now = now or datetime.now(timezone.utc)
    snap = {"fetched_at": _iso(now - timedelta(seconds=age)), "status": status}
    if seven is not None:
        snap["seven_day"] = seven
    if five is not None:
        snap["five_hour"] = five
    return snap


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_svc(), "get_breaker_redis", lambda: r)
    return r


class TestCachedReadings:

    def test_one_mget_for_the_whole_candidate_set(self, fake_redis, monkeypatch):
        svc = _svc()
        fake_redis.set(svc._SNAPSHOT_KEY.format(sid="a"),
                       json.dumps(_snapshot(age=10, seven={"utilization_pct": 30.0, "status": "allowed"})))
        calls = []
        real_mget = fake_redis.mget

        def spy(keys, *a, **k):
            calls.append(list(keys))
            return real_mget(keys, *a, **k)

        monkeypatch.setattr(fake_redis, "mget", spy)
        monkeypatch.setattr(fake_redis, "get", lambda *a, **k: pytest.fail("per-key GET issued"))
        readings = svc.cached_headroom_readings(["a", "b", "c"])
        assert len(calls) == 1 and len(calls[0]) == 3
        assert readings["a"].seven_day.utilization_pct == 30.0
        assert readings["b"] is None and readings["c"] is None

    def test_redis_unreachable_reads_every_candidate_as_unknown_in_one_attempt(self, monkeypatch):
        svc = _svc()
        attempts = []
        monkeypatch.setattr(svc, "get_breaker_redis", lambda: attempts.append(1) or None)
        assert svc.cached_headroom_readings(["a", "b"]) == {"a": None, "b": None}
        assert attempts == [1]

    def test_redis_raising_mid_read_is_unknown_not_an_error(self, fake_redis, monkeypatch):
        svc = _svc()

        def boom(*a, **k):
            raise ConnectionError("socket timeout")

        monkeypatch.setattr(fake_redis, "mget", boom)
        assert svc.cached_headroom_readings(["a"]) == {"a": None}

    def test_a_malformed_snapshot_blinds_only_its_own_candidate(self, fake_redis):
        svc = _svc()
        fake_redis.set(svc._SNAPSHOT_KEY.format(sid="bad"), "{not json")
        fake_redis.set(svc._SNAPSHOT_KEY.format(sid="shape"), json.dumps({"fetched_at": "x", "seven_day": "not-a-window"}))
        fake_redis.set(svc._SNAPSHOT_KEY.format(sid="good"),
                       json.dumps(_snapshot(age=10, seven={"utilization_pct": 30.0, "status": "allowed"})))
        readings = svc.cached_headroom_readings(["bad", "shape", "good"])
        assert readings["bad"] is None and readings["shape"] is None
        assert readings["good"].seven_day.utilization_pct == 30.0

    def test_empty_candidate_set_touches_nothing(self, monkeypatch):
        svc = _svc()
        monkeypatch.setattr(svc, "get_breaker_redis", lambda: pytest.fail("Redis touched for no candidates"))
        assert svc.cached_headroom_readings([]) == {}

    def test_reader_never_probes(self, fake_redis, monkeypatch):
        svc = _svc()
        for name in ("_probe", "_locked_probe", "_probe_and_store", "get_headroom", "ensure_reading"):
            monkeypatch.setattr(svc, name, lambda *a, **k: pytest.fail(f"{name} called from the selector"))
        assert svc.cached_headroom_readings(["a"]) == {"a": None}

    def test_default_bound_is_the_selection_bound(self, fake_redis):
        svc = _svc()
        fake_redis.set(svc._SNAPSHOT_KEY.format(sid="a"),
                       json.dumps(_snapshot(age=svc.FRESHNESS_SECONDS + 60, seven={"utilization_pct": 30.0, "status": "allowed"})))
        assert svc.cached_headroom_readings(["a"])["a"] is not None
        fake_redis.set(svc._SNAPSHOT_KEY.format(sid="a"),
                       json.dumps(_snapshot(age=svc.MAX_READING_AGE_SECONDS + 60, seven={"utilization_pct": 30.0, "status": "allowed"})))
        assert svc.cached_headroom_readings(["a"])["a"] is None


# =============================================================================
# E — constants that keep the ranker from being silently inert
# =============================================================================

class TestConstants:

    def test_selection_bound_outlives_the_sampler_cadence(self):
        """Any bound under the sampler interval makes most candidates UNKNOWN on
        an unwatched instance — the feature would ship inert."""
        svc = _svc()
        assert svc.MAX_READING_AGE_SECONDS > svc.SAMPLE_INTERVAL_SECONDS

    def test_the_alert_module_reads_the_same_constant(self):
        import services.subscription_headroom_alerts as alerts
        assert alerts.MAX_READING_AGE_SECONDS is _svc().MAX_READING_AGE_SECONDS

    def test_fast_bounds_are_the_display_bound(self):
        svc = _svc()
        assert svc.REFUSAL_FRESHNESS_SECONDS == svc.FRESHNESS_SECONDS
        assert svc.FAST_WINDOW_FRESHNESS_SECONDS == svc.FRESHNESS_SECONDS
        assert svc.HEADROOM_BAND_PCT == 10


# =============================================================================
# F — the db layer: filter ONLY, deterministic order, no service import
# =============================================================================

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
        "email TEXT, role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE subscription_credentials (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
        "encrypted_credentials TEXT NOT NULL, subscription_type TEXT, rate_limit_tier TEXT, "
        "owner_id INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE agent_ownership (agent_name TEXT PRIMARY KEY, owner_id INTEGER, "
        "subscription_id TEXT, deleted_at TEXT, use_platform_api_key INTEGER DEFAULT 1)"
    )
    cur.execute(
        "CREATE TABLE subscription_rate_limit_events (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, "
        "subscription_id TEXT NOT NULL, error_message TEXT, failure_kind TEXT, occurred_at TEXT NOT NULL)"
    )
    now = _iso(datetime.now(timezone.utc))
    cur.execute("INSERT INTO users (id, username, email, role, created_at, updated_at) "
                "VALUES (1, 'tester', 'tester@example.com', 'admin', ?, ?)", (now, now))
    for sid, name in (("sub-a", "sub-A"), ("sub-b", "sub-B"), ("sub-c", "sub-C"), ("sub-d", "sub-D")):
        cur.execute("INSERT INTO subscription_credentials (id, name, encrypted_credentials, owner_id, created_at, updated_at) "
                    "VALUES (?, ?, 'enc', 1, ?, ?)", (sid, name, now, now))
    # agent-x on sub-a; two agents on sub-c; sub-b and sub-d unused
    cur.execute("INSERT INTO agent_ownership (agent_name, owner_id, subscription_id) VALUES ('agent-x', 1, 'sub-a')")
    cur.execute("INSERT INTO agent_ownership (agent_name, owner_id, subscription_id) VALUES ('agent-y', 1, 'sub-c')")
    cur.execute("INSERT INTO agent_ownership (agent_name, owner_id, subscription_id) VALUES ('agent-z', 1, 'sub-c')")
    conn.commit()
    conn.close()
    for mod in ("db.connection", "db.subscriptions"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    yield db_path


@pytest.fixture
def sub_ops(tmp_db):
    from db.subscriptions import SubscriptionOperations
    return SubscriptionOperations(encryption_service=MagicMock())


def _event(db_path, subscription_id, *, failure_kind="rate_limit", minutes_ago=30):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO subscription_rate_limit_events (id, agent_name, subscription_id, error_message, failure_kind, occurred_at) "
        "VALUES (?, 'agent-x', ?, '', ?, ?)",
        (uuid.uuid4().hex, subscription_id, failure_kind,
         _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))),
    )
    conn.commit()
    conn.close()


class TestDbListing:

    def test_excludes_current_and_orders_by_load_then_name(self, sub_ops):
        ids = [s.id for s in sub_ops.list_viable_alternative_subscriptions("sub-a")]
        assert ids == ["sub-b", "sub-d", "sub-c"]

    @pytest.mark.parametrize("kind", ["rate_limit", "auth", None])
    def test_the_failure_filter_is_kind_blind_and_runs_here(self, sub_ops, tmp_db, kind):
        """#444 / #2352: a candidate that failed for ANY reason is gone before
        any ranking can see it."""
        _event(tmp_db, "sub-b", failure_kind=kind)
        ids = [s.id for s in sub_ops.list_viable_alternative_subscriptions("sub-a")]
        assert ids == ["sub-d", "sub-c"]

    def test_everything_failed_is_an_empty_list(self, sub_ops, tmp_db):
        for sid in ("sub-b", "sub-c", "sub-d"):
            _event(tmp_db, sid)
        assert sub_ops.list_viable_alternative_subscriptions("sub-a") == []

    def test_assignable_listing_includes_every_unfailed_subscription(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-d", failure_kind="auth")
        ids = [s.id for s in sub_ops.list_assignable_subscriptions()]
        # load order: sub-b (0 agents), sub-a (1), sub-c (2); sub-d failed
        assert ids == ["sub-b", "sub-a", "sub-c"]

    def test_the_db_layer_never_reads_headroom(self):
        """The ranking is the service's job; the db layer holds no Redis edge
        (Invariant #1) and the old first-match selectors are gone."""
        import re
        src = (_BACKEND / "db" / "subscriptions.py").read_text()
        # No IMPORT edge to the headroom service (docstrings may name it as a caller).
        assert not re.search(
            r"(from services\.subscription_headroom_service|"
            r"import services\.subscription_headroom_service|"
            r"import_module\(\s*[\"']services\.subscription_headroom)", src
        )
        assert "get_breaker_redis" not in src and "mget(" not in src
        assert "def select_best_alternative_subscription" not in src
        assert "def get_least_used_subscription" not in src


# =============================================================================
# G — the service selector, end to end (real modules, fakeredis)
# =============================================================================

@pytest.fixture
def selector(monkeypatch, fake_redis):
    """Real `subscription_auto_switch` + real headroom service; `db` and the
    auto-refresh flag monkeypatched as ATTRIBUTES (never sys.modules stubs)."""
    auto_switch = _auto_switch()
    svc = _svc()
    db = MagicMock(name="db")
    monkeypatch.setattr(auto_switch, "db", db)
    monkeypatch.setattr(svc, "is_auto_refresh_enabled", lambda: True)
    return auto_switch, svc, db, fake_redis


def _seed(fake_redis, svc, sid, **kw):
    fake_redis.set(svc._SNAPSHOT_KEY.format(sid=sid), json.dumps(_snapshot(**kw)))


class TestServiceSelector:

    def test_picks_the_survivor_furthest_from_its_nearest_wall(self, selector):
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("b", agents=0), _sub("c", agents=0), _sub("d", agents=0)]
        _seed(r, svc, "b", age=10, seven={"utilization_pct": 61.0, "status": "allowed"})
        _seed(r, svc, "c", age=10, seven={"utilization_pct": 12.0, "status": "allowed"}, five={"utilization_pct": 97.0, "status": "allowed_warning"})
        _seed(r, svc, "d", age=10, seven={"utilization_pct": 45.0, "status": "allowed"}, five={"utilization_pct": 8.0, "status": "allowed"})
        picked = auto_switch.select_best_alternative_subscription("a")
        assert picked is not None
        sub, why = picked
        assert sub.id == "d"
        assert why["tier"] == svc.SELECTION_MEASURED
        assert why["seven_day_pct"] == 45.0 and why["five_hour_pct"] == 8.0
        assert why["candidates"] == 3
        db.list_viable_alternative_subscriptions.assert_called_once_with("a")

    def test_only_survivors_are_ever_read(self, selector, monkeypatch):
        """The failure filter ran in the db; the selector must not even look at
        a subscription the filter dropped."""
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("c"), _sub("d")]
        seen = []
        real = svc.cached_headroom_readings
        monkeypatch.setattr(svc, "cached_headroom_readings",
                            lambda ids, **k: seen.append(list(ids)) or real(ids, **k))
        auto_switch.select_best_alternative_subscription("a")
        assert seen == [["c", "d"]]

    def test_no_survivors_is_none_as_before(self, selector):
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = []
        assert auto_switch.select_best_alternative_subscription("a") is None

    def test_no_readings_degrades_to_todays_order(self, selector):
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("b", agents=0), _sub("c", agents=2)]
        sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "b"
        assert why["tier"] == svc.SELECTION_UNKNOWN
        assert why["auto_refresh_enabled"] is True

    def test_redis_down_degrades_to_todays_order(self, selector, monkeypatch):
        auto_switch, svc, db, r = selector
        monkeypatch.setattr(svc, "get_breaker_redis", lambda: None)
        db.list_viable_alternative_subscriptions.return_value = [_sub("b", agents=0), _sub("c", agents=2)]
        sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "b" and why["tier"] == svc.SELECTION_UNKNOWN

    def test_every_survivor_refused_is_none_and_says_why(self, selector, caplog):
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("b"), _sub("c")]
        _seed(r, svc, "b", age=10, status="rate_limited")
        _seed(r, svc, "c", age=10, status="invalid_token")
        with caplog.at_level(logging.WARNING):
            assert auto_switch.select_best_alternative_subscription("a") is None
        assert any("refus" in rec.getMessage() for rec in caplog.records)

    def test_a_stale_refusal_does_not_block(self, selector):
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("b")]
        _seed(r, svc, "b", age=svc.REFUSAL_FRESHNESS_SECONDS + 5, status="rate_limited")
        sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "b" and why["tier"] == svc.SELECTION_UNKNOWN

    def test_selection_never_probes(self, selector, monkeypatch):
        auto_switch, svc, db, r = selector
        for name in ("_probe", "_locked_probe", "_probe_and_store", "get_headroom", "ensure_reading"):
            monkeypatch.setattr(svc, name, lambda *a, **k: pytest.fail(f"{name} called from the selector"))
        db.list_viable_alternative_subscriptions.return_value = [_sub("b")]
        _seed(r, svc, "b", age=svc.MAX_READING_AGE_SECONDS + 5, seven={"utilization_pct": 1.0, "status": "allowed"})
        sub, _ = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "b"

    def test_all_unknown_with_ambient_refresh_off_warns_loudly(self, selector, monkeypatch, caplog):
        """An inert ranker must not look like a working one (learnings 2026-08-06)."""
        auto_switch, svc, db, r = selector
        monkeypatch.setattr(svc, "is_auto_refresh_enabled", lambda: False)
        db.list_viable_alternative_subscriptions.return_value = [_sub("b"), _sub("c")]
        with caplog.at_level(logging.WARNING):
            sub, why = auto_switch.select_best_alternative_subscription("a")
        assert why["auto_refresh_enabled"] is False
        assert any("refresh" in rec.getMessage().lower() for rec in caplog.records)

    def test_a_poisoned_headroom_module_fails_open_and_is_logged(self, selector, monkeypatch, caplog):
        """learnings 2026-08-12: a foreign stub under the lazy import must not
        become a silent policy flip — the fallback is today's order AND a WARNING."""
        auto_switch, svc, db, r = selector
        poison = types.ModuleType("services.subscription_headroom_service")
        monkeypatch.setitem(sys.modules, "services.subscription_headroom_service", poison)
        db.list_viable_alternative_subscriptions.return_value = [_sub("first"), _sub("second")]
        with caplog.at_level(logging.WARNING):
            sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "first"
        assert why["tier"] == "unranked"
        assert any("2409" in rec.getMessage() for rec in caplog.records)

    def test_the_real_module_resolves_and_the_ranking_engages(self, selector):
        """The positive proof the poisoned-order pin needs beside it."""
        auto_switch, svc, db, r = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("first", agents=0), _sub("second", agents=5)]
        _seed(r, svc, "first", age=10, seven={"utilization_pct": 95.0, "status": "allowed_warning"})
        _seed(r, svc, "second", age=10, seven={"utilization_pct": 5.0, "status": "allowed"})
        sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "second" and why["tier"] == svc.SELECTION_MEASURED


class TestHandleFailureWiring:

    @pytest.mark.asyncio
    async def test_the_pick_and_its_reason_reach_perform_auto_switch(self, selector, monkeypatch):
        auto_switch, svc, db, r = selector
        auto_switch._reset_locks_for_test()
        db.get_agent_subscription_id.return_value = "a"
        db.record_rate_limit_event.return_value = 1
        db.get_setting_value.return_value = "true"
        current = MagicMock(); current.name = "sub-a"
        db.get_subscription.return_value = current
        db.list_viable_alternative_subscriptions.return_value = [_sub("b", name="sub-b")]
        _seed(r, svc, "b", age=10, seven={"utilization_pct": 33.0, "status": "allowed"})
        captured = {}

        async def spy(**kw):
            captured.update(kw)
            return {"switched": True}

        monkeypatch.setattr(auto_switch, "_perform_auto_switch", spy)
        result = await auto_switch.handle_subscription_failure("agent-x", "429", "rate_limit")
        assert result == {"switched": True}
        assert captured["new_subscription"].id == "b"
        assert captured["destination_headroom"]["tier"] == svc.SELECTION_MEASURED
        assert captured["destination_headroom"]["seven_day_pct"] == 33.0

    @pytest.mark.asyncio
    async def test_selection_runs_off_the_event_loop(self, selector, monkeypatch):
        """The db + Redis reads are synchronous; under the per-agent lock they
        must not stall the loop (the eng-review HIGH)."""
        import threading
        auto_switch, svc, db, r = selector
        auto_switch._reset_locks_for_test()
        db.get_agent_subscription_id.return_value = "a"
        db.record_rate_limit_event.return_value = 1
        db.get_setting_value.return_value = "true"
        threads = []

        def listing(_cur):
            threads.append(threading.current_thread())
            return []

        db.list_viable_alternative_subscriptions.side_effect = listing
        assert await auto_switch.handle_subscription_failure("agent-x", "429", "rate_limit") is None
        assert threads and threads[0] is not threading.main_thread()


# =============================================================================
# H — the switch surfaces WHY the destination was chosen
# =============================================================================

@pytest.fixture
def perform_env(monkeypatch):
    auto_switch = _auto_switch()
    db = MagicMock(name="db")
    monkeypatch.setattr(auto_switch, "db", db)

    async def _reload(_agent):
        return "hot_reloaded"

    monkeypatch.setattr(auto_switch, "_hot_reload_subscription_token", _reload)
    activity = MagicMock()

    async def _track(**kw):
        activity.tracked = kw
        return "act-1"

    async def _complete(**kw):
        activity.completed = kw

    activity.track_activity = _track
    activity.complete_activity = _complete
    act_mod = types.ModuleType("services.activity_service")
    act_mod.activity_service = activity
    monkeypatch.setitem(sys.modules, "services.activity_service", act_mod)
    return auto_switch, db, activity


def _notification(db):
    db.create_notification.assert_called_once()
    return db.create_notification.call_args.kwargs["data"]


class TestPerformAutoSwitchSurfacesWhy:

    @pytest.mark.asyncio
    async def test_measured_destination_is_explained(self, perform_env):
        auto_switch, db, activity = perform_env
        why = {"tier": "measured", "seven_day_pct": 39.0, "five_hour_pct": 21.0,
               "seven_day_resets_at": "2026-09-01T00:00:00Z", "five_hour_resets_at": None,
               "reading_age_seconds": 12, "candidates": 3, "auto_refresh_enabled": True}
        result = await auto_switch._perform_auto_switch(
            agent_name="agent-x", old_subscription_id="a", old_subscription_name="sub-a",
            new_subscription=_sub("b", name="sub-b"), failure_kind="rate_limit", event_count=1,
            destination_headroom=why,
        )
        assert result["destination_headroom"] == why
        assert activity.tracked["details"]["destination_headroom"] == why
        note = _notification(db)
        assert note.metadata["destination_headroom"] == why
        assert "39%" in note.message and "21%" in note.message
        assert "most headroom" in note.message

    @pytest.mark.asyncio
    async def test_unknown_destination_says_so_and_names_the_off_switch(self, perform_env):
        auto_switch, db, activity = perform_env
        why = {"tier": "unknown", "seven_day_pct": None, "five_hour_pct": None,
               "seven_day_resets_at": None, "five_hour_resets_at": None,
               "reading_age_seconds": None, "candidates": 2, "auto_refresh_enabled": False}
        await auto_switch._perform_auto_switch(
            agent_name="agent-x", old_subscription_id="a", old_subscription_name="sub-a",
            new_subscription=_sub("b", name="sub-b"), failure_kind="auth", event_count=1,
            destination_headroom=why,
        )
        msg = _notification(db).message
        assert "no fresh headroom reading" in msg.lower()
        assert "refresh is off" in msg.lower()

    @pytest.mark.asyncio
    async def test_no_reason_at_all_keeps_the_old_message(self, perform_env):
        """Callers that pass nothing (and the fail-open path) get the pre-#2409 text."""
        auto_switch, db, activity = perform_env
        result = await auto_switch._perform_auto_switch(
            agent_name="agent-x", old_subscription_id="a", old_subscription_name="sub-a",
            new_subscription=_sub("b", name="sub-b"), failure_kind="rate_limit", event_count=1,
        )
        msg = _notification(db).message
        assert msg.endswith("after a rate-limit error.")
        assert result["destination_headroom"] is None

    def test_the_clause_reacts_to_the_tiers_the_service_actually_emits(self):
        """`_destination_clause` compares literal strings (the headroom module is a
        lazy import there); pin them to the service's constants so a renamed tier
        cannot silently make every clause vanish."""
        auto_switch, svc = _auto_switch(), _svc()
        measured = svc.describe_reading(svc.headroom_reading(_fresh({"utilization_pct": 39.0, "status": "allowed"})))
        measured.update({"candidates": 2, "auto_refresh_enabled": True})
        assert "most headroom" in auto_switch._destination_clause(measured)
        unknown = svc.describe_reading(None)
        unknown.update({"candidates": 2, "auto_refresh_enabled": True})
        assert "No fresh headroom reading" in auto_switch._destination_clause(unknown)
        assert auto_switch._destination_clause({"tier": auto_switch.SELECTION_UNRANKED}) == ""

    @pytest.mark.asyncio
    async def test_a_broken_reason_never_breaks_the_switch(self, perform_env):
        auto_switch, db, activity = perform_env
        result = await auto_switch._perform_auto_switch(
            agent_name="agent-x", old_subscription_id="a", old_subscription_name="sub-a",
            new_subscription=_sub("b", name="sub-b"), failure_kind="rate_limit", event_count=1,
            destination_headroom={"tier": "measured", "seven_day_pct": "garbage"},
        )
        assert result["switched"] is True
        db.assign_subscription_to_agent.assert_called_once_with("agent-x", "b")


# =============================================================================
# I — new-agent auto-assign rides the same ranker
# =============================================================================

@pytest.fixture
def assigner(monkeypatch, fake_redis):
    import services.subscription_service as subscription_service
    svc = _svc()
    db = MagicMock(name="db")
    dbmod = types.ModuleType("database")
    dbmod.db = db
    monkeypatch.setitem(sys.modules, "database", dbmod)
    monkeypatch.setattr(svc, "is_auto_refresh_enabled", lambda: True)
    return subscription_service, svc, db, fake_redis


class TestNewAgentAssignment:

    def test_ranks_then_takes_the_first_decryptable_token(self, assigner):
        subscription_service, svc, db, r = assigner
        db.list_assignable_subscriptions.return_value = [_sub("a", agents=0), _sub("b", agents=1), _sub("c", agents=2)]
        _seed(r, svc, "a", age=10, seven={"utilization_pct": 80.0, "status": "allowed_warning"})
        _seed(r, svc, "b", age=10, seven={"utilization_pct": 10.0, "status": "allowed"})
        _seed(r, svc, "c", age=10, seven={"utilization_pct": 20.0, "status": "allowed"})
        db.get_subscription_token.side_effect = lambda sid: None if sid == "b" else "tok"
        chosen = subscription_service.select_subscription_for_new_agent()
        assert chosen.id == "c"
        assert [c.args[0] for c in db.get_subscription_token.call_args_list] == ["b", "c"]

    def test_one_decrypt_when_the_top_pick_is_valid(self, assigner):
        subscription_service, svc, db, r = assigner
        db.list_assignable_subscriptions.return_value = [_sub("a"), _sub("b")]
        db.get_subscription_token.return_value = "tok"
        chosen = subscription_service.select_subscription_for_new_agent()
        assert chosen.id == "a"
        assert db.get_subscription_token.call_count == 1

    def test_no_readings_is_the_old_round_robin(self, assigner):
        subscription_service, svc, db, r = assigner
        db.list_assignable_subscriptions.return_value = [_sub("busy", agents=3), _sub("idle", agents=0)]
        db.get_subscription_token.return_value = "tok"
        assert subscription_service.select_subscription_for_new_agent().id == "idle"

    def test_nothing_assignable_is_none(self, assigner):
        subscription_service, svc, db, r = assigner
        db.list_assignable_subscriptions.return_value = []
        assert subscription_service.select_subscription_for_new_agent() is None

    def test_a_poisoned_headroom_module_falls_back_to_load_order_loudly(self, assigner, monkeypatch, caplog):
        subscription_service, svc, db, r = assigner
        monkeypatch.setitem(sys.modules, "services.subscription_headroom_service",
                            types.ModuleType("services.subscription_headroom_service"))
        db.list_assignable_subscriptions.return_value = [_sub("idle", agents=0), _sub("busy", agents=3)]
        db.get_subscription_token.return_value = "tok"
        with caplog.at_level(logging.WARNING):
            assert subscription_service.select_subscription_for_new_agent().id == "idle"
        assert any("2409" in rec.getMessage() for rec in caplog.records)

    def test_a_fresh_dead_token_is_never_assigned(self, assigner):
        subscription_service, svc, db, r = assigner
        db.list_assignable_subscriptions.return_value = [_sub("dead", agents=0), _sub("live", agents=4)]
        _seed(r, svc, "dead", age=10, status="invalid_token")
        db.get_subscription_token.return_value = "tok"
        assert subscription_service.select_subscription_for_new_agent().id == "live"

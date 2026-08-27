"""
ent#434 — alert the operator before a subscription's WEEKLY window runs out.

What this suite pins, and why each part exists:

**A. A three-state classifier, not the binary one that already exists.**
   `_headroom_indicates_healthy` returns `False` for a stale snapshot, a
   rejected token, a transport error AND a genuinely saturated subscription —
   so it cannot tell "no evidence" from "no headroom". Used as an
   assessability gate it would block the fleet claim on precisely the most
   saturated fleet. #2396's docstring said ent#434 would consume it; that was
   wrong and is corrected here.

**B. The alert id IS the state machine.** The weekly window was measured
   fixed-with-reset (see the module docstring of
   `services/subscription_headroom_alerts.py`), so utilization is monotonic
   inside a window and the only real re-arm is the reset. Putting the reset
   day in the id gives edge-triggering, cross-worker dedup and re-arm for
   free, with no durable memo to leak or race.

**C. The empty fleet.** `all([])` is `True`, so a fleet-saturation verdict
   over zero subscriptions is vacuously "everything is saturated".

**D. Isolation.** A bug in this feature must never stop #447's recovery
   detection, which is the only mechanism that clears a stale `LIMIT` badge.

Test hygiene per learnings 2026-08-12/13 (#2114 stub-leak class): no bare
`sys.modules` stubs — attributes are monkeypatched on the real imported module.
Time is injected, never read from the clock (learnings 2026-08-03).
"""

from __future__ import annotations

import ast
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _headroom(**kw):
    from db_models import HeadroomWindow, SubscriptionHeadroom
    five = kw.pop("five_hour", None)
    seven = kw.pop("seven_day", None)
    return SubscriptionHeadroom(
        five_hour=HeadroomWindow(**five) if isinstance(five, dict) else five,
        seven_day=HeadroomWindow(**seven) if isinstance(seven, dict) else seven,
        **kw,
    )


def _fresh(**seven):
    """A fresh, provider-ok snapshot carrying only a 7d window."""
    return _headroom(seven_day=seven, snapshot_age_seconds=30, status="ok")


# =============================================================================
# A — the three-state classifier
# =============================================================================

class TestClassifyHeadroom:

    def test_blocking_window_with_no_number_is_saturated(self):
        """The reading this feature exists for, and the one the old gate lost.

        A 429 window carries a status and no `utilization_pct`.
        `_headroom_indicates_healthy` returns False for it — the same False it
        returns for an unreachable provider — so using that predicate as the
        assessability gate would file the most saturated subscription in the
        fleet as "no evidence" and block the escalation.
        """
        from services.subscription_headroom_service import (
            SATURATED, classify_headroom,
        )
        h = _fresh(utilization_pct=None, status="blocked")
        assert classify_headroom(h, threshold_pct=75) == SATURATED

    def test_probe_429_is_saturated(self):
        """The provider refusing outright is the strongest evidence there is."""
        from services.subscription_headroom_service import (
            SATURATED, classify_headroom,
        )
        h = _headroom(snapshot_age_seconds=30, status="rate_limited")
        assert classify_headroom(h, threshold_pct=75) == SATURATED

    @pytest.mark.parametrize("status", ["invalid_token", "error", "no_windows", "weird"])
    def test_non_ok_non_429_status_is_unassessable(self, status):
        """A rejected token or a transport error taught us nothing about quota.

        Never `has_headroom`: that would let a broken credential satisfy the
        fleet claim's "positive evidence" requirement.
        """
        from services.subscription_headroom_service import (
            UNASSESSABLE, classify_headroom,
        )
        h = _headroom(snapshot_age_seconds=30, status=status)
        assert classify_headroom(h, threshold_pct=75) == UNASSESSABLE

    def test_stale_but_healthy_reading_is_unassessable(self):
        from services.subscription_headroom_service import (
            FRESHNESS_SECONDS, UNASSESSABLE, classify_headroom,
        )
        h = _headroom(
            seven_day={"utilization_pct": 10.0, "status": "allowed"},
            snapshot_age_seconds=FRESHNESS_SECONDS + 1,
            status="ok",
        )
        assert classify_headroom(h, threshold_pct=75) == UNASSESSABLE

    def test_missing_seven_day_window_is_unassessable(self):
        """`parse_unified_headers` admits a 5h-only snapshot: its top guard
        passes on `5h-utilization` alone. A gate written as "ok status +
        non-blocking windows" would claim WEEKLY headroom on zero weekly
        evidence."""
        from services.subscription_headroom_service import (
            UNASSESSABLE, classify_headroom,
        )
        h = _headroom(
            five_hour={"utilization_pct": 12.0, "status": "allowed"},
            seven_day=None, snapshot_age_seconds=30, status="ok",
        )
        assert classify_headroom(h, threshold_pct=75) == UNASSESSABLE

    def test_null_utilization_is_never_coerced(self):
        """`utilization_pct` is Optional INDEPENDENTLY of status. Coercing a
        missing number to 0 or 100 invents a reading in the direction of the
        coercion."""
        from services.subscription_headroom_service import (
            UNASSESSABLE, classify_headroom,
        )
        h = _fresh(utilization_pct=None, status="allowed")
        assert classify_headroom(h, threshold_pct=75) == UNASSESSABLE

    def test_allowed_warning_is_classified_by_NUMBER_not_status(self):
        """#2396 made `allowed_warning` non-blocking, which is right for the
        badge and would be catastrophic here: a status-driven classifier would
        file a live 90% warning-tier reading as `has_headroom` — the exact
        reading this feature exists to alarm on."""
        from services.subscription_headroom_service import (
            SATURATED, classify_headroom,
        )
        h = _fresh(utilization_pct=90.0, status="allowed_warning")
        assert classify_headroom(h, threshold_pct=75) == SATURATED

    def test_below_threshold_has_headroom(self):
        from services.subscription_headroom_service import (
            HAS_HEADROOM, classify_headroom,
        )
        h = _fresh(utilization_pct=25.0, status="allowed")
        assert classify_headroom(h, threshold_pct=75) == HAS_HEADROOM

    def test_none_reading_is_unassessable(self):
        from services.subscription_headroom_service import (
            UNASSESSABLE, classify_headroom,
        )
        assert classify_headroom(None, threshold_pct=75) == UNASSESSABLE


# =============================================================================
# B — the projection, and the boundary
# =============================================================================

class TestProjection:

    def test_early_in_the_week_projects_over_the_wall(self):
        """75% three days into a seven-day window is an emergency."""
        from services.subscription_headroom_alerts import project_end_utilization
        resets = (NOW + timedelta(days=4)).isoformat().replace("+00:00", "Z")
        projected = project_end_utilization(75.0, resets, now=NOW)
        assert projected is not None and projected > 160

    def test_late_in_the_week_projects_under_the_wall(self):
        """75% with hours left is a normal week — the case that must not be
        treated as urgent. No time constant decides this; the pace does."""
        from services.subscription_headroom_alerts import project_end_utilization
        resets = (NOW + timedelta(hours=4)).isoformat().replace("+00:00", "Z")
        projected = project_end_utilization(75.0, resets, now=NOW)
        assert projected is not None and projected < 80

    @pytest.mark.parametrize("resets", [None, "", "not-a-date"])
    def test_unknowable_projection_returns_none(self, resets):
        from services.subscription_headroom_alerts import project_end_utilization
        assert project_end_utilization(75.0, resets, now=NOW) is None

    def test_lapsed_reset_returns_none_rather_than_dividing_by_zero(self):
        from services.subscription_headroom_alerts import project_end_utilization
        resets = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        assert project_end_utilization(75.0, resets, now=NOW) is None

    def test_missing_utilization_projects_nothing(self):
        from services.subscription_headroom_alerts import project_end_utilization
        resets = (NOW + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        assert project_end_utilization(None, resets, now=NOW) is None


class TestTierAndPriority:

    def test_boundary_74_9_silent_75_0_fires(self):
        """`_parse_utilization` rounds to 1dp before we ever see it, so the
        effective threshold is threshold-0.05. Pinned so a later refactor
        cannot quietly move it."""
        from services.subscription_headroom_alerts import TIER_WARN, decide_tier
        assert decide_tier(74.9, 75, 90) is None
        assert decide_tier(75.0, 75, 90) == TIER_WARN

    def test_a_jump_past_both_tiers_yields_one_tier_not_two(self):
        """Going straight from below-threshold to 95 satisfies both
        conditions; emitting both would be two operator items for one
        crossing."""
        from services.subscription_headroom_alerts import TIER_CRIT, decide_tier
        assert decide_tier(95.0, 75, 90) == TIER_CRIT

    def test_escalation_is_derived_so_a_low_threshold_cannot_invert_it(self):
        """A fixed 90 escalation under a threshold of 95 would fire BELOW the
        warning; a fixed floor above the threshold would re-arm every cycle.
        One knob, derived siblings, no cross-field invariant to violate."""
        from services.subscription_headroom_alerts import decide_tier, escalation_pct
        assert escalation_pct(95) == 95
        assert escalation_pct(60) == 90
        # threshold 60: 85 is a warning, not an escalation.
        assert decide_tier(85.0, 60, escalation_pct(60)) == "warn"

    def test_priority_reflects_pace_but_the_alert_is_never_withheld(self):
        """Operator ruling: always tell me at the threshold; let the
        projection say how much it matters."""
        from services.subscription_headroom_alerts import TIER_WARN, priority_for
        assert priority_for(TIER_WARN, 130.0) == "high"
        assert priority_for(TIER_WARN, 78.0) == "low"

    def test_unknown_projection_is_not_treated_as_an_emergency(self):
        from services.subscription_headroom_alerts import TIER_WARN, priority_for
        assert priority_for(TIER_WARN, None) == "low"


# =============================================================================
# C — the id IS the state machine
# =============================================================================

class TestEpisodeIdentity:

    def test_same_window_same_id_so_a_re_emit_is_an_on_conflict_noop(self):
        """Three cycles at 80/81/82 inside one window produce ONE id, so the
        sink's ON CONFLICT DO NOTHING makes it one row. This is the whole
        edge-trigger, with no durable memo."""
        from services.subscription_headroom_alerts import alert_id, episode_key
        resets = "2026-09-01T00:00:00Z"
        ids = {alert_id("s1", episode_key(resets, now=NOW), "warn") for _ in range(3)}
        assert len(ids) == 1

    def test_window_reset_mints_a_new_id_so_the_alert_re_arms(self):
        from services.subscription_headroom_alerts import alert_id, episode_key
        first = alert_id("s1", episode_key("2026-09-01T00:00:00Z", now=NOW), "warn")
        after = alert_id("s1", episode_key("2026-09-08T00:00:00Z", now=NOW), "warn")
        assert first != after

    def test_escalation_carries_its_own_id(self):
        from services.subscription_headroom_alerts import alert_id, episode_key
        ep = episode_key("2026-09-01T00:00:00Z", now=NOW)
        assert alert_id("s1", ep, "warn") != alert_id("s1", ep, "crit")

    def test_a_moving_reset_degrades_to_one_id_per_day_not_one_per_probe(self):
        """The belt against the measurement being wrong elsewhere. If some
        provider plan did behave as a rolling window, quantising to the day
        bounds the blast radius at one alert per day instead of one per
        probe."""
        from services.subscription_headroom_alerts import episode_key
        a = episode_key("2026-09-01T00:00:00Z", now=NOW)
        b = episode_key("2026-09-01T18:30:00Z", now=NOW)
        assert a == b

    def test_id_carries_the_reserved_prefix(self):
        from services.subscription_headroom_alerts import (
            ALARM_ID_PREFIX, alert_id, fleet_alert_id,
        )
        assert alert_id("s1", "2026-09-01", "warn").startswith(ALARM_ID_PREFIX)
        assert fleet_alert_id("2026-09-01").startswith(ALARM_ID_PREFIX)


# =============================================================================
# D — the fleet claim
# =============================================================================

class TestFleetVerdict:

    def test_empty_fleet_never_claims_saturation(self):
        """`all([])` is True. Without an explicit guard a fresh install with no
        subscriptions would be told every subscription is full."""
        from services.subscription_headroom_alerts import fleet_verdict
        v = fleet_verdict({})
        assert v["saturated"] is False
        assert v["blocked_reason"] == "no_subscriptions"

    def test_single_subscription_does_not_get_two_alerts_for_one_fact(self):
        from services.subscription_headroom_alerts import SATURATED, fleet_verdict
        v = fleet_verdict({"s1": SATURATED})
        assert v["saturated"] is False
        assert v["blocked_reason"] == "single_subscription"

    def test_one_unassessable_member_blocks_the_claim_and_is_named(self):
        """A positive fleet-wide claim needs positive evidence from every
        member (the ent#100 rule)."""
        from services.subscription_headroom_alerts import (
            SATURATED, UNASSESSABLE, fleet_verdict,
        )
        v = fleet_verdict({"s1": SATURATED, "s2": SATURATED, "s3": UNASSESSABLE})
        assert v["saturated"] is False
        assert v["blocked_reason"] == "unassessable_members"
        assert v["unassessable_ids"] == ["s3"]

    def test_any_headroom_blocks_the_claim(self):
        from services.subscription_headroom_alerts import (
            HAS_HEADROOM, SATURATED, fleet_verdict,
        )
        v = fleet_verdict({"s1": SATURATED, "s2": HAS_HEADROOM})
        assert v["saturated"] is False
        assert v["blocked_reason"] == "headroom_available"

    def test_all_saturated_and_assessable_fires(self):
        from services.subscription_headroom_alerts import SATURATED, fleet_verdict
        v = fleet_verdict({"s1": SATURATED, "s2": SATURATED})
        assert v["saturated"] is True
        assert v["blocked_reason"] is None
        assert v["saturated_ids"] == ["s1", "s2"]


# =============================================================================
# E — the shared read path
# =============================================================================

class TestEnsureReading:

    def test_fails_closed_when_redis_did_not_answer(self, monkeypatch):
        """A client object existing is not Redis being reachable (learnings
        2026-08-19). Without a readable cache a probe's result could not be
        stored for any surface to see, so it would be quota spent for
        nothing."""
        import services.subscription_headroom_service as mod
        probed = []
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (False, None))
        monkeypatch.setattr(
            mod, "_locked_probe",
            lambda sid: probed.append(sid) or asyncio.sleep(0),
        )
        reading, did_probe = asyncio.run(mod.ensure_reading("s1", max_age_seconds=3600))
        assert reading is None and did_probe is False
        assert probed == [], "a Redis-down sampler must spend no quota"

    def test_a_fresh_snapshot_is_served_without_a_second_probe(self, monkeypatch):
        """The #447-ordering property: recovery probes first, so the sampler
        must reuse that zero-age snapshot rather than being floored out."""
        import services.subscription_headroom_service as mod
        snap = {"status": "ok", "seven_day": {"utilization_pct": 40.0},
                "fetched_at": "2026-08-26T12:00:00Z"}
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (True, snap))
        monkeypatch.setattr(mod, "_snapshot_age_seconds", lambda s: 5)

        async def _boom(sid):  # pragma: no cover - must not run
            raise AssertionError("probed a snapshot that was already fresh")
        monkeypatch.setattr(mod, "_locked_probe", _boom)

        reading, did_probe = asyncio.run(mod.ensure_reading("s1", max_age_seconds=3600))
        assert did_probe is False and reading is not None


# =============================================================================
# F — isolation: this feature must not be able to break #447
# =============================================================================

class TestSweepIsolation:

    def test_an_alert_bug_does_not_stop_recovery_detection(self, monkeypatch):
        """Recovery is the ONLY mechanism that clears a stale LIMIT badge
        (nothing clears a failure row on success). If the ent#434 evaluation
        shared its try/except, a `None` utilization or a malformed reading
        would take that mechanism down with it."""
        import services.subscription_recovery_service as svc

        async def _recovered(sid):
            return "recovered"

        async def _explode(sid, **kw):
            raise RuntimeError("alert evaluation is broken")

        monkeypatch.setattr(svc, "recover_probe", _recovered)
        monkeypatch.setattr(svc, "ensure_reading", _explode)

        class _Sub:
            id = "s1"

        service = svc.SubscriptionRecoveryService()
        result = asyncio.run(
            service._sweep_one(_Sub(), threshold=75, alerting=True)
        )
        assert result["outcome"] == "recovered"
        assert result["classification"] is None

    def test_no_subscriptions_short_circuits_before_any_fleet_verdict(self, monkeypatch):
        import services.subscription_recovery_service as svc
        monkeypatch.setattr(svc.db, "list_subscriptions", lambda: [])

        async def _boom(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("evaluated alerts on an empty fleet")
        monkeypatch.setattr(
            svc.SubscriptionRecoveryService, "_evaluate_alerts", _boom
        )
        out = asyncio.run(svc.SubscriptionRecoveryService().run_cycle())
        assert out["subscriptions"] == 0 and out["probed"] == 0

    def test_cycle_return_shape_is_still_the_asserted_contract(self, monkeypatch):
        """#447's test asserts `{"probed", "outcomes"}`; new counters are
        ADDITIONAL keys, never a reshape."""
        import services.subscription_recovery_service as svc
        monkeypatch.setattr(svc.db, "list_subscriptions", lambda: [])
        out = asyncio.run(svc.SubscriptionRecoveryService().run_cycle())
        assert {"probed", "outcomes"} <= set(out)


# =============================================================================
# G — configuration
# =============================================================================

class TestThresholdSetting:

    @pytest.mark.parametrize("raw", ["abc", "-40", "150", "  ", None])
    def test_garbage_coerces_to_the_default_not_to_zero_or_a_hundred(
        self, monkeypatch, raw
    ):
        """The two failure directions are not symmetric: 0 would alarm on every
        subscription forever and 100 would silently disable the feature. Both
        are worse than the default."""
        import services.subscription_headroom_alerts as alerts
        monkeypatch.setattr(alerts.db, "get_setting_value", lambda k, default=None: raw)
        assert alerts.effective_threshold_pct() == alerts.DEFAULT_THRESHOLD_PCT

    def test_explicit_zero_disables(self, monkeypatch):
        import services.subscription_headroom_alerts as alerts
        monkeypatch.setattr(alerts.db, "get_setting_value", lambda k, default=None: "0")
        assert alerts.effective_threshold_pct() == 0

    def test_a_settings_read_failure_does_not_kill_the_sweep(self, monkeypatch):
        import services.subscription_headroom_alerts as alerts

        def _raise(k, default=None):
            raise RuntimeError("db down")
        monkeypatch.setattr(alerts.db, "get_setting_value", _raise)
        assert alerts.effective_threshold_pct() == alerts.DEFAULT_THRESHOLD_PCT


# =============================================================================
# H — registry parity (three places a platform emitter must be registered)
# =============================================================================

class TestRegistryParity:

    def test_alarm_id_prefix_is_reserved_against_agent_pre_creation(self):
        """Unreserved, an agent could pre-create the id and the sink's ON
        CONFLICT DO NOTHING would silence its own alarm."""
        from services.operator_queue_service import _RESERVED_ID_PREFIXES
        from services.subscription_headroom_alerts import ALARM_ID_PREFIX
        assert ALARM_ID_PREFIX in _RESERVED_ID_PREFIXES

    def test_alarm_host_is_exempt_from_the_canary_orphan_scan(self):
        """Without the exemption every alert is a permanent, un-fixable L-03
        orphan violation — the sentinel has no `agent_ownership` row and never
        can, because `sanitize_agent_name` strips the leading underscore."""
        from canary.snapshot import _PLATFORM_ALARM_SENTINELS
        from services.subscription_headroom_alerts import ALARM_AGENT_NAME
        assert ALARM_AGENT_NAME in _PLATFORM_ALARM_SENTINELS

    def test_alarm_host_is_uncreatable_as_a_real_agent(self):
        from utils.helpers import sanitize_agent_name
        from services.subscription_headroom_alerts import ALARM_AGENT_NAME
        assert sanitize_agent_name(ALARM_AGENT_NAME) != ALARM_AGENT_NAME

    def test_emitter_is_on_the_1677_platform_only_allowlist(self):
        """A new direct `create_operator_queue_item` call site reds that guard
        until it is classified. This asserts the classification exists rather
        than re-running the guard."""
        src = (_REPO / "tests" / "unit"
               / "test_1677_operator_alert_emitters.py").read_text()
        assert "services/subscription_headroom_alerts.py" in src

    def test_alerts_emit_never_sets_expires_at(self):
        """`mark_operator_queue_expired` flips any pending row past
        `expires_at` to expired fleet-wide every 5s."""
        src = (_BACKEND / "services" / "subscription_headroom_alerts.py").read_text()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "expires_at":
                        found = True
                        assert isinstance(v, ast.Constant) and v.value is None
        assert found, "the emitted item must set expires_at explicitly"


# =============================================================================
# I — the settings write paths
# =============================================================================

class TestSettingsWritePaths:

    def test_threshold_key_is_blocked_on_the_generic_catch_all(self):
        """The catch-all takes an unvalidated string. A small VALID integer is
        the dangerous input here, not garbage — "5" would be stored verbatim
        and alarm on every subscription forever (#1644's lesson)."""
        src = (_BACKEND / "routers" / "settings.py").read_text()
        assert "HEADROOM_ALERT_THRESHOLD_KEY" in src
        assert "headroom-alert-threshold" in src

    def test_catch_all_now_validates_every_ops_key(self):
        """T1: ops keys were range-validated on /ops/config and NOWHERE on the
        catch-all, so an ops key reachable there accepted "abc" or "-40"
        verbatim. Closed generally rather than with a twelfth `if key == ...`."""
        src = (_BACKEND / "routers" / "settings.py").read_text()
        put_key = src.index('@router.put("/{key}"')
        ops_config = src.index('@router.put("/ops/config")')
        catch_all = src[put_key:ops_config]
        assert "OPS_SETTINGS_VALIDATION" in catch_all
        assert "validate_ops_setting" in catch_all


# =============================================================================
# J — the corrected claims
# =============================================================================

class TestCorrectedClaims:

    def test_2396_docstring_no_longer_names_this_feature_as_its_consumer(self):
        """#2396 asserted ent#434 would consume `_headroom_indicates_healthy`
        as its assessability gate. It cannot: that predicate returns False for
        a saturated subscription too."""
        src = (_BACKEND / "services" / "subscription_headroom_service.py").read_text()
        start = src.index("def _headroom_indicates_healthy")
        body = src[start:src.index("def classify_headroom")]
        assert "consumes this predicate as its" not in body
        assert "classify_headroom" in body

    def test_the_rolling_window_claim_is_corrected(self):
        """Measured on a live instance: `seven_day_resets_at` held constant for
        five days of probes while utilization climbed, then stepped exactly +7
        days. That is a fixed window with a scheduled reset."""
        doc = (_REPO / "docs" / "memory" / "feature-flows"
               / "dashboard-grid-view.md").read_text()
        assert "they are rolling windows" not in doc

    def test_the_toggle_no_longer_claims_dashboard_only_probing(self):
        """False since #447, and more false with the sampler."""
        vue = (_REPO / "src" / "frontend" / "src" / "components" / "settings"
               / "SubscriptionsPanel.vue").read_text()
        assert "only while a dashboard is open" not in vue


# =============================================================================
# K — regressions caught in /review of this PR
# =============================================================================

class TestReviewRegressions:

    def test_cycle_summary_alerts_key_has_one_shape(self, monkeypatch):
        """Caught in review: the disabled branch set `alerts` to the bare
        string "disabled" while both alerting branches set a dict, so any
        consumer reading `summary["alerts"]["fleet"]` broke the moment an
        operator set the threshold to 0."""
        import services.subscription_recovery_service as svc
        import services.subscription_headroom_alerts as alerts

        class _Sub:
            id = "s1"
            name = "primary"

        monkeypatch.setattr(svc.db, "list_subscriptions", lambda: [_Sub()])
        monkeypatch.setattr(alerts.db, "get_setting_value",
                            lambda k, default=None: "0")

        async def _recovered(sid):
            return "not_limited"
        monkeypatch.setattr(svc, "recover_probe", _recovered)
        monkeypatch.setattr(
            svc.SubscriptionRecoveryService, "_try_acquire_leadership",
            lambda self, ttl: True,
        )

        out = asyncio.run(svc.SubscriptionRecoveryService().run_cycle())
        assert isinstance(out["alerts"], dict)
        assert out["alerts"]["fleet"] is False
        assert out["alerts"]["disabled"] is True

    def test_threshold_zero_spends_no_probe(self, monkeypatch):
        """Disabling the alert must disable the sampling it exists for —
        otherwise an operator who turned it off still pays the quota."""
        import services.subscription_recovery_service as svc

        async def _recovered(sid):
            return "not_limited"

        async def _must_not_run(*a, **k):  # pragma: no cover
            raise AssertionError("sampled with alerts disabled")

        monkeypatch.setattr(svc, "recover_probe", _recovered)
        monkeypatch.setattr(svc, "ensure_reading", _must_not_run)

        class _Sub:
            id = "s1"

        out = asyncio.run(
            svc.SubscriptionRecoveryService()._sweep_one(
                _Sub(), threshold=0, alerting=False
            )
        )
        assert out["outcome"] == "not_limited"

    def test_reading_age_bound_is_named_for_what_it_bounds(self):
        """The constant bounds EVERY classification, not just the fleet arm.
        It was named FLEET_* while being passed as the age bound for the
        per-subscription path too."""
        import services.subscription_headroom_alerts as alerts
        assert hasattr(alerts, "MAX_READING_AGE_SECONDS")
        assert not hasattr(alerts, "FLEET_MAX_READING_AGE_SECONDS")

    def test_sample_interval_is_a_constant_not_an_inert_env_read(self):
        """Caught by /validate-pr's config-packaging gate.

        `SAMPLE_INTERVAL_SECONDS` documents itself as a constant, not an
        operator knob (#1644) — but it was reading
        `SUBSCRIPTION_SAMPLE_INTERVAL_SECONDS` from the environment, which
        neither compose forwards. Since neither compose uses `env_file`, an
        unlisted var never reaches the container: the read was inert AND it
        contradicted its own comment, which is the combination that invites a
        later "packaging fix" creating exactly the knob the comment argues
        against. Either it is a constant or it is a wired lever; it cannot be
        a third thing.

        Pattern note: this asserts on the CODE shape (`os.getenv("<name>"`),
        not the bare name. An unanchored name match would also hit the comment
        explaining the bug — the `debt:2026-08-24-source-regex-guard-governs-prose`
        trap, where the previous fix degraded the prose to satisfy the regex.
        The pattern must be as specific as the code it forbids.
        """
        src = (_BACKEND / "services" / "subscription_headroom_service.py").read_text()
        assert 'os.getenv("SUBSCRIPTION_SAMPLE_INTERVAL_SECONDS"' not in src
        # the explanatory comment must survive — it is why the constant is a constant
        assert "SUBSCRIPTION_SAMPLE_INTERVAL_SECONDS" in src

    def test_sweep_concurrency_lever_reaches_every_deployment_compose(self):
        """The other half of the packaging gate: this one IS a real operator
        lever (fleet-size dependent), so it must reach every container that
        runs the sweep.

        There are THREE deployment composes, not two. #2280 added
        `docker-compose.hosted.yml` — the pull-only twin of prod — while this
        branch was open, and its parity guard caught the omission on CI. My own
        packaging check had verified two files because the third did not exist
        when I ran it, which is precisely why this is enumerated rather than
        asserted from memory.

        The list is the three STANDALONE targets. It deliberately excludes the
        other five `docker-compose*.yml` files: `gitea` and the two `override`
        files are overlays merged onto the base (`-f base -f overlay`), so they
        inherit its environment; `sibling` is the verify-local harness; and
        `prod.enterprise` declares no backend environment block. Requiring the
        var in an overlay would be requiring it twice.
        """
        targets = [
            "docker-compose.yml",          # dev / base
            "docker-compose.prod.yml",     # standalone: no base merge, no env_file
            "docker-compose.hosted.yml",   # standalone pull-only twin of prod (#2280)
            ".env.example",                # documented for the operator
        ]
        for name in targets:
            path = _REPO / name
            assert path.exists(), f"{name} is gone — has the deployment set changed?"
            assert "SUBSCRIPTION_SWEEP_CONCURRENCY" in path.read_text(), name

    def test_no_fourth_deployment_compose_has_appeared_unnoticed(self):
        """A guard on the guard: if a new standalone compose lands, the list
        above silently stops being complete — the exact way #2280 caught me.
        Overlays and harnesses are excluded by name WITH a reason, so a genuinely
        new deployment target fails here and has to be classified deliberately.
        """
        known = {
            "docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml",
            "docker-compose.gitea.yml",            # dev-only overlay onto the base
            "docker-compose.override.yml",         # local Docker Desktop log source
            "docker-compose.override.example.yml", # its committed template
            "docker-compose.sibling.yml",          # /verify-local isolated stack
            "docker-compose.prod.enterprise.yml",  # overlay; no backend environment block
        }
        found = {p.name for p in _REPO.glob("docker-compose*.yml")}
        assert found <= known, (
            f"unclassified compose file(s): {sorted(found - known)} — decide whether "
            "each is a standalone deployment target (add it to the target list above) "
            "or an overlay (add it here with the reason)."
        )


# =============================================================================
# L — findings from the PR review (dolho, PR #2410)
# =============================================================================

class _Sub:
    def __init__(self, sid, name):
        self.id, self.name = sid, name


def _wire_sweep(monkeypatch, subs, *, raises_for=(), classification=None):
    """Common wiring: every subscription saturated except those in `raises_for`,
    whose SAMPLING blows up (which `_sweep_one` swallows by design)."""
    import services.subscription_recovery_service as svc
    import services.subscription_headroom_alerts as alerts
    emitted = []

    async def _recovered(sid):
        return "not_limited"

    async def _ensure(sid, *, max_age_seconds):
        if sid in raises_for:
            raise RuntimeError("sampling blew up")

        class W:
            utilization_pct, resets_at, status = 88.0, "2026-09-01T00:00:00Z", "allowed_warning"

        class H:
            seven_day, five_hour = W(), None
            snapshot_age_seconds, status = 10, "ok"

        return H(), True

    monkeypatch.setattr(svc, "recover_probe", _recovered)
    monkeypatch.setattr(svc, "ensure_reading", _ensure)
    monkeypatch.setattr(
        svc, "classify_headroom",
        lambda h, **kw: classification or alerts.SATURATED,
    )
    monkeypatch.setattr(svc.db, "list_subscriptions", lambda: subs)
    monkeypatch.setattr(svc.db, "get_agents_by_subscription", lambda sid: [])
    monkeypatch.setattr(alerts.db, "get_setting_value", lambda k, default=None: "75")
    monkeypatch.setattr(
        alerts, "_emit",
        lambda item_id, **kw: (emitted.append((item_id, kw)), True)[1],
    )
    monkeypatch.setattr(
        svc.SubscriptionRecoveryService, "_try_acquire_leadership",
        lambda self, ttl: True,
    )
    return svc, emitted


class TestFleetClaimDenominator:
    """[C1] The pure predicate was right; the CALLER had narrowed its input."""

    def test_an_unclassified_member_blocks_the_fleet_claim(self, monkeypatch):
        """THE bug. Three subscriptions registered, `s3`'s sampling raises (which
        `_sweep_one` swallows BY DESIGN to protect #447), and the claim used to
        read "All 2 registered subscriptions" — asserting fleet-wide saturation
        over two thirds of the evidence, with the per-subscription alerts
        suppressed so the wrong claim was the only thing emitted.

        `fleet_verdict` always honoured the ent#100 rule; the caller filtered
        the blocking member out before handing it over.
        """
        subs = [_Sub("s1", "one"), _Sub("s2", "two"), _Sub("s3", "three")]
        svc, emitted = _wire_sweep(monkeypatch, subs, raises_for={"s3"})
        out = asyncio.run(svc.SubscriptionRecoveryService().run_cycle())

        assert out["alerts"]["fleet"] is False
        assert out["fleet_blocked_reason"] == "unassessable_members"
        assert not [i for i, _ in emitted if "fleet" in i]

    def test_a_fully_assessed_saturated_fleet_still_fires(self, monkeypatch):
        """The fix must not make the alert unreachable — the failure direction
        that would be invisible, because a silent alert looks like a calm fleet."""
        subs = [_Sub("s1", "one"), _Sub("s2", "two")]
        svc, emitted = _wire_sweep(monkeypatch, subs)
        out = asyncio.run(svc.SubscriptionRecoveryService().run_cycle())

        assert out["alerts"]["fleet"] is True
        assert [i for i, _ in emitted if "fleet" in i]

    def test_a_partial_sweep_cannot_produce_a_positive_fleet_claim(self, monkeypatch):
        """The second reachable path: the chunk loop `break`s on a mid-cycle
        lease yield, so `results` is shorter than `subs` while `_evaluate_alerts`
        still runs. Building the map from `subs` makes that self-correcting."""
        subs = [_Sub(f"s{i}", f"n{i}") for i in range(1, 7)]
        svc, emitted = _wire_sweep(monkeypatch, subs)
        # Yield on the FIRST re-assert, i.e. before the final chunk. Yielding
        # after the last chunk drops nothing (every subscription is already in
        # `results`), so a test that does that proves nothing — it passed
        # against the unfixed code.
        monkeypatch.setattr(
            svc.SubscriptionRecoveryService, "_try_acquire_leadership",
            lambda self, ttl: False,
        )
        out = asyncio.run(svc.SubscriptionRecoveryService().run_cycle())

        assert out["alerts"]["fleet"] is False
        assert not [i for i, _ in emitted if "fleet" in i]


class TestTierCollapse:

    def test_warning_tier_is_unreachable_at_or_above_ninety(self):
        """[I2] `escalation_pct` returns `max(threshold, 90)` and `decide_tier`
        tests `>= escalate_at` first, so from a threshold of 90 up the two
        bounds coincide and every crossing files as `crit`. Correct behaviour —
        there is no gentle tier left at 90% of a weekly window — but a real
        change across that boundary, pinned so it is a decision and not a
        surprise."""
        from services.subscription_headroom_alerts import (
            TIER_CRIT, TIER_WARN, decide_tier, escalation_pct,
        )
        assert decide_tier(88.0, 75, escalation_pct(75)) == TIER_WARN
        for threshold in (90, 95, 99):
            reachable = {
                decide_tier(u, threshold, escalation_pct(threshold))
                for u in (threshold, threshold + 0.5, 100.0)
            }
            assert reachable == {TIER_CRIT}, threshold


class TestStatusHonesty:

    def test_an_unreadable_subscription_list_is_not_reported_as_active(self, monkeypatch):
        """[I6] `None == 0` is False, so a failed read fell through every reason
        arm and returned `active: true` — the one path in a function whose whole
        job is naming why it is off that claimed health from a failure."""
        import routers.subscriptions as rs

        def _boom():
            raise RuntimeError("db unreadable")

        monkeypatch.setattr(rs.db, "list_subscriptions", _boom)
        status = rs._weekly_alert_status_blocking()
        assert status["active"] is False
        assert status["inactive_reason"] == "count_unavailable"
        assert status["subscription_count"] is None


class TestEnvIntGuard:

    def test_garbage_concurrency_degrades_instead_of_crashing_import(self):
        """[I4] These constants resolve at MODULE SCOPE, so a bare `int()` on a
        non-numeric value raises during import and crash-loops the backend
        rather than degrading. The var is now advertised in `.env.example`, so
        a human types it (#1197's shape)."""
        from services.subscription_recovery_service import _env_int
        assert _env_int("ENT434_DEFINITELY_UNSET_VAR", 4) == 4
        import os
        os.environ["ENT434_TEST_GARBAGE"] = "eight"
        try:
            assert _env_int("ENT434_TEST_GARBAGE", 4) == 4
        finally:
            del os.environ["ENT434_TEST_GARBAGE"]

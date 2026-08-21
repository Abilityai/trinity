"""
#447 — a recovered subscription must stop claiming it is rate-limited, and the
platform must find out on its own.

Two backend halves (the third, the reset on the tile row face, is pure frontend
and lives in `src/frontend/tests/unit/subscriptionPressureTile.spec.js`):

**A. `resolve_rate_limited_now` is three-state, not an OR.**
   `rate_limited_now` used to be `db_2h_predicate OR fresh_provider_verdict`.
   Nothing clears a failure row on success (`clear_rate_limit_events` has had
   zero production callers since #444), so the db half only decays with the
   clock — and being OR'd, a fresh, authoritative "allowed, 32% used" from the
   provider was structurally powerless to clear the badge. Observed live: two
   subscriptions wearing `LIMIT` while their agents answered normally.

**B. `recover_probe` re-asks the provider for subscriptions believed limited.**
   The ambient refresh is demand-driven, so an unwatched instance never
   re-checks at all. This is the only thing that can produce the fresh verdict
   arm A depends on.

Test hygiene per learnings 2026-08-12/13 (#2114 stub-leak class): no bare
`sys.modules` stubs — attributes are monkeypatched on the real imported module.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Make src/backend importable (mirrors test_471_subscription_usage_observability.py).
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _headroom(**kw):
    from db_models import HeadroomWindow, SubscriptionHeadroom

    win = kw.pop("five_hour", None)
    seven = kw.pop("seven_day", None)
    return SubscriptionHeadroom(
        five_hour=HeadroomWindow(**win) if isinstance(win, dict) else win,
        seven_day=HeadroomWindow(**seven) if isinstance(seven, dict) else seven,
        **kw,
    )


# =============================================================================
# A — the three-state derivation
# =============================================================================

class TestResolveRateLimitedNow:
    """The one-gate `rate_limited_now` derivation (#2157 rule, #447 fix)."""

    def test_fresh_allowed_verdict_clears_a_stale_db_predicate(self):
        """THE bug: ground truth must beat a 2h-old inference.

        Before #447 this returned True — the OR meant a subscription that had
        recovered kept its `LIMIT` badge for up to two hours while every agent
        on it answered normally.
        """
        from services.subscription_headroom_service import resolve_rate_limited_now

        healthy = _headroom(
            five_hour={"utilization_pct": 32.0, "resets_at": "2026-08-20T19:10:00Z",
                       "status": "allowed"},
            seven_day={"utilization_pct": 32.0, "resets_at": "2026-08-26T05:00:00Z",
                       "status": "allowed"},
            snapshot_age_seconds=30,
            status="ok",
        )
        assert resolve_rate_limited_now(db_says_limited=True, headroom=healthy) is False

    def test_fresh_limited_verdict_still_wins(self):
        from services.subscription_headroom_service import resolve_rate_limited_now

        limited = _headroom(snapshot_age_seconds=30, status="rate_limited")
        assert resolve_rate_limited_now(db_says_limited=False, headroom=limited) is True

    def test_stale_snapshot_falls_back_to_the_db_predicate(self):
        """A stale reading is not evidence in EITHER direction."""
        from services.subscription_headroom_service import (
            FRESHNESS_SECONDS,
            resolve_rate_limited_now,
        )

        stale = _headroom(
            five_hour={"utilization_pct": 5.0, "status": "allowed"},
            snapshot_age_seconds=FRESHNESS_SECONDS + 1,
            status="ok",
        )
        assert resolve_rate_limited_now(db_says_limited=True, headroom=stale) is True
        assert resolve_rate_limited_now(db_says_limited=False, headroom=stale) is False

    def test_no_snapshot_falls_back_to_the_db_predicate(self):
        from services.subscription_headroom_service import resolve_rate_limited_now

        assert resolve_rate_limited_now(db_says_limited=True, headroom=None) is True
        assert resolve_rate_limited_now(db_says_limited=False, headroom=None) is False

    @pytest.mark.parametrize("status", ["invalid_token", "error"])
    def test_a_probe_that_never_reached_the_quota_clears_nothing(self, status):
        """#2353's rule, kept: an auth failure or a transport error is no
        evidence about the quota, so it must not be read as "healthy" and
        silently clear a real limit."""
        from services.subscription_headroom_service import resolve_rate_limited_now

        h = _headroom(
            five_hour={"utilization_pct": 1.0, "status": "allowed"},
            snapshot_age_seconds=10,
            status=status,
        )
        assert resolve_rate_limited_now(db_says_limited=True, headroom=h) is True

    def test_ok_status_with_no_windows_is_not_proof_of_health(self):
        """A 200 carrying no unified headers tells us nothing about the quota."""
        from services.subscription_headroom_service import resolve_rate_limited_now

        h = _headroom(snapshot_age_seconds=10, status="ok")
        assert resolve_rate_limited_now(db_says_limited=True, headroom=h) is True

    def test_a_blocked_window_under_an_ok_status_still_reads_as_limited(self):
        from services.subscription_headroom_service import resolve_rate_limited_now

        h = _headroom(
            five_hour={"utilization_pct": 100.0, "status": "blocked"},
            snapshot_age_seconds=10,
            status="ok",
        )
        assert resolve_rate_limited_now(db_says_limited=False, headroom=h) is True

    def test_healthy_arm_is_not_the_negation_of_the_limited_arm(self):
        """The two predicates must not partition the space: everything that is
        neither positive proof of a limit nor positive proof of headroom has to
        fall through to the db predicate."""
        from services.subscription_headroom_service import (
            _headroom_indicates_healthy,
            _headroom_indicates_limited,
        )

        unknown = _headroom(snapshot_age_seconds=10, status="error")
        assert _headroom_indicates_limited(unknown) is False
        assert _headroom_indicates_healthy(unknown) is False


class TestBothSurfacesShareTheDerivation:
    """The tile and the per-agent chip must never disagree (#2157 one gate)."""

    def test_pressure_states_uses_the_resolver(self):
        src = (_BACKEND / "services" / "subscription_headroom_service.py").read_text()
        body = src.split("async def pressure_states", 1)[1]
        assert "resolve_rate_limited_now(" in body, (
            "pressure_states must go through the one resolver, not re-derive"
        )
        assert " or _headroom_indicates_limited(" not in body, (
            "the OR is the #447 bug — a fresh provider verdict must be able to "
            "clear the badge, not just set it"
        )

    def test_decorate_usage_uses_the_resolver(self):
        src = (_BACKEND / "services" / "subscription_headroom_service.py").read_text()
        body = src.split("async def decorate_usage", 1)[1].split("async def ", 1)[0]
        assert "resolve_rate_limited_now(" in body
        assert "usage.rate_limited_now or " not in body


# =============================================================================
# B — the recovery probe
# =============================================================================

class TestRecoverProbe:
    """Only probes subscriptions believed limited; never fabricates a signal."""

    def _svc(self):
        import services.subscription_headroom_service as mod
        return mod

    def test_skips_a_subscription_nobody_thinks_is_limited(self, monkeypatch):
        mod = self._svc()
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (True, {"status": "ok"}))
        monkeypatch.setattr(mod.db, "is_subscription_rate_limited", lambda sid: False)
        probed = []
        monkeypatch.setattr(mod, "_locked_probe", lambda sid: probed.append(sid))

        assert asyncio.run(mod.recover_probe("s1")) == "not_limited"
        assert probed == []

    def test_fails_closed_when_redis_cannot_be_read(self, monkeypatch):
        """Same rule as the ambient path: without a readable cache the result
        could not be stored for anyone to see, so the quota would be spent for
        nothing."""
        mod = self._svc()
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (False, None))
        probed = []
        monkeypatch.setattr(mod, "_locked_probe", lambda sid: probed.append(sid))

        assert asyncio.run(mod.recover_probe("s1")) == "redis_unavailable"
        assert probed == []

    def test_probes_when_the_db_predicate_alone_says_limited(self, monkeypatch):
        """The common case: the badge comes from the stale 2h predicate and no
        provider snapshot disagrees with it yet. This is exactly the set that
        can never clear itself without a probe."""
        mod = self._svc()
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (True, None))
        monkeypatch.setattr(mod.db, "is_subscription_rate_limited", lambda sid: True)
        monkeypatch.setattr(mod, "_probe_floor_ok", lambda sid, snap: True)

        async def _fake(sid):
            return {"status": "ok"}
        monkeypatch.setattr(mod, "_locked_probe", _fake)

        assert asyncio.run(mod.recover_probe("s1")) == "recovered"

    def test_reports_a_still_limited_subscription_without_recovering_it(self, monkeypatch):
        mod = self._svc()
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (True, {"status": "rate_limited"}))
        monkeypatch.setattr(mod, "_probe_floor_ok", lambda sid, snap: True)

        async def _fake(sid):
            return {"status": "rate_limited"}
        monkeypatch.setattr(mod, "_locked_probe", _fake)

        assert asyncio.run(mod.recover_probe("s1")) == "still_rate_limited"

    def test_honours_the_recovery_interval(self, monkeypatch):
        mod = self._svc()
        monkeypatch.setattr(
            mod, "_read_snapshot", lambda sid: (True, {"status": "rate_limited"})
        )
        monkeypatch.setattr(mod, "_snapshot_age_seconds", lambda snap: 5)
        probed = []
        monkeypatch.setattr(mod, "_locked_probe", lambda sid: probed.append(sid))

        assert asyncio.run(mod.recover_probe("s1")) == "too_soon"
        assert probed == []

    def test_a_db_error_does_not_stall_the_sweep(self, monkeypatch):
        mod = self._svc()

        def _boom(sid):
            raise RuntimeError("db down")
        monkeypatch.setattr(mod, "_read_snapshot", lambda sid: (True, None))
        monkeypatch.setattr(mod.db, "is_subscription_rate_limited", _boom)

        assert asyncio.run(mod.recover_probe("s1")) == "not_limited"

    def test_probing_cannot_feed_the_predicate_it_exists_to_clear(self):
        """`_probe` must record a 429 into the SNAPSHOT only. If it ever wrote a
        `subscription_rate_limit_events` row, this loop would manufacture the
        very db failure rows that keep a subscription marked limited."""
        src = (_BACKEND / "services" / "subscription_headroom_service.py").read_text()
        probe_body = src.split("async def _probe(", 1)[1].split("\ndef ", 1)[0]
        assert "record_rate_limit_event" not in probe_body


class TestRecoveryServiceWiring:
    def test_cycle_surveys_every_subscription_and_survives_a_bad_one(self, monkeypatch):
        import services.subscription_recovery_service as mod

        class _Sub:
            def __init__(self, sid):
                self.id = sid

        monkeypatch.setattr(
            mod.db, "list_subscriptions", lambda: [_Sub("a"), _Sub("b"), _Sub("c")]
        )

        async def _probe(sid):
            if sid == "b":
                raise RuntimeError("boom")
            return "recovered" if sid == "a" else "not_limited"

        monkeypatch.setattr(mod, "recover_probe", _probe)

        result = asyncio.run(mod.subscription_recovery_service.run_cycle())
        assert result["probed"] == 3
        assert result["outcomes"] == {"recovered": 1, "error": 1, "not_limited": 1}

    def test_leader_lease_is_registered_and_not_agent_keyed(self):
        """Fleet-scoped like `skills:sync:leader`, so the #1560 name-keyed
        agent-runtime registry deliberately does not apply."""
        import services.subscription_recovery_service as mod

        assert mod._LEADER_KEY == "subscription:recovery:leader"
        assert not mod._LEADER_KEY.startswith("agent:")

    def test_started_and_stopped_in_the_lifespan(self):
        src = (_BACKEND / "main.py").read_text()
        assert "subscription_recovery_service.start()" in src
        assert "subscription_recovery_service.stop()" in src

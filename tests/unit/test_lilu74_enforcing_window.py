"""
Tests for the enforcing-limit block on the headroom payload (Abilityai/lilu#74).

Related flow: docs/memory/feature-flows/subscription-usage-tracking.md

The reported defect: `GET /api/subscriptions/{name}/usage` and the runtime that
actually rejects turns were describing DIFFERENT windows with DIFFERENT resets,
so the endpoint said `allowed` at 16% (`resets_at 2026-09-15T23:00Z`) while
every turn was being refused on a weekly limit resetting 2026-09-13T02:00Z.

Root cause: `parse_unified_headers` read exactly two hardcoded window prefixes.
A window family the provider reports but this code has never heard of was
parsed into nothing — so it could not appear in the payload, could not move a
badge, and could not be the thing a dispatch gate reasoned about.
"""

import sys
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the backend at a scratch DB path.

    Every test below is over pure functions — header parsing, window selection,
    model conversion, the two predicates — so no tables are needed. The fixture
    exists only so importing `database` at service-import time cannot touch a
    real file.
    """
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "trinity.db"))
    yield tmp_path


# The exact header set the 2026-08-19 spike captured from a real probe — the
# shape every install reports today. Used here as the "nothing changed" baseline.
SPIKE_HEADERS = {
    "anthropic-ratelimit-unified-5h-reset": "1787155200",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.15",
    "anthropic-ratelimit-unified-7d-reset": "1787720400",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.06",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-overage-status": "rejected",
    "anthropic-ratelimit-unified-status": "allowed",
}

# The 2026-09-09 incident, reconstructed: a general weekly window with room and
# a LATER reset, beside a second weekly bucket that is refusing and resets
# EARLIER. The old parser saw only the first and reported `allowed`.
INCIDENT_HEADERS = {
    "anthropic-ratelimit-unified-5h-reset": "1788951600",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.04",
    # 16%, resets 2026-09-15T23:00:00Z — what the endpoint reported.
    "anthropic-ratelimit-unified-7d-reset": "1789513200",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.16",
    # The limit that was actually enforcing: resets 2026-09-13T02:00:00Z.
    "anthropic-ratelimit-unified-7d-opus-reset": "1789264800",
    "anthropic-ratelimit-unified-7d-opus-status": "rejected",
    "anthropic-ratelimit-unified-7d-opus-utilization": "1.0",
    "anthropic-ratelimit-unified-representative-claim": "seven_day",
    "anthropic-ratelimit-unified-status": "allowed",
}


# =============================================================================
# 1. Canonical naming
# =============================================================================

class TestCanonicalWindowName:
    @pytest.mark.parametrize("segment,expected", [
        ("5h", "five_hour"),
        ("7d", "seven_day"),
        ("7d-opus", "seven_day_opus"),
        ("5h-sonnet", "five_hour_sonnet"),
        # A family whose head we cannot translate is carried through verbatim
        # rather than dropped — an unnameable window is still a window.
        ("30d", "30d"),
        ("monthly-opus", "monthly_opus"),
    ])
    def test_names(self, tmp_db, segment, expected):
        from services.subscription_headroom_service import canonical_window_name

        assert canonical_window_name(segment) == expected


# =============================================================================
# 2. Generic discovery — the root cause
# =============================================================================

class TestWindowDiscovery:
    def test_known_pair_unchanged(self, tmp_db):
        """The fixed pair keeps meaning exactly what it meant (the contract)."""
        from services.subscription_headroom_service import parse_unified_headers

        snap = parse_unified_headers(SPIKE_HEADERS)
        assert snap["five_hour"] == {
            "utilization_pct": 15.0,
            "resets_at": "2026-08-19T16:00:00Z",
            "status": "allowed",
        }
        assert snap["seven_day"]["utilization_pct"] == 6.0
        assert snap["representative_claim"] == "five_hour"
        assert snap["overage_status"] == "rejected"
        # ...and the new map holds exactly those two, nothing invented.
        assert set(snap["windows"]) == {"five_hour", "seven_day"}

    def test_third_family_is_no_longer_invisible(self, tmp_db):
        """THE regression. A window the code has never heard of is reported."""
        from services.subscription_headroom_service import parse_unified_headers

        snap = parse_unified_headers(INCIDENT_HEADERS)
        assert "seven_day_opus" in snap["windows"]
        assert snap["windows"]["seven_day_opus"] == {
            "utilization_pct": 100.0,
            "resets_at": "2026-09-13T02:00:00Z",
            "status": "rejected",
        }

    def test_non_window_families_are_not_windows(self, tmp_db):
        """`overage-status` shares the `<segment>-<field>` shape and must not
        become a window called 'overage' — it would then be eligible to be
        named as the enforcing limit."""
        from services.subscription_headroom_service import parse_unified_headers

        snap = parse_unified_headers({
            **SPIKE_HEADERS,
            "anthropic-ratelimit-unified-overage-reason": "plan",
            "anthropic-ratelimit-unified-fallback-status": "rejected",
        })
        assert set(snap["windows"]) == {"five_hour", "seven_day"}

    def test_unnamed_status_only_family_excluded(self, tmp_db):
        """A family carrying only a `-status` and no figure is some other flag."""
        from services.subscription_headroom_service import parse_unified_headers

        snap = parse_unified_headers({
            **SPIKE_HEADERS,
            "anthropic-ratelimit-unified-something-status": "weird",
        })
        assert "something" not in snap["windows"]

    def test_known_window_survives_a_status_only_429(self, tmp_db):
        """A 429 reports a window status with NO figure. Dropping it would
        delete the very signal `_headroom_indicates_limited` reads."""
        from services.subscription_headroom_service import parse_unified_headers

        snap = parse_unified_headers({
            "anthropic-ratelimit-unified-status": "rejected",
            "anthropic-ratelimit-unified-7d-status": "rejected",
        })
        assert snap["seven_day"] == {
            "utilization_pct": None, "resets_at": None, "status": "rejected",
        }
        assert snap["windows"]["seven_day"]["status"] == "rejected"

    def test_absent_family_still_returns_none(self, tmp_db):
        from services.subscription_headroom_service import parse_unified_headers

        assert parse_unified_headers({"content-type": "application/json"}) is None


# =============================================================================
# 3. Which limit binds
# =============================================================================

class TestSelectEnforcingWindow:
    def test_refusing_window_outranks_the_providers_nomination(self, tmp_db):
        """The incident, end to end: the claim says `seven_day` (16%, resets
        Sep 15) and a different weekly bucket is refusing (resets Sep 13).
        The one that is refusing is the enforcement."""
        from services.subscription_headroom_service import parse_unified_headers

        enf = parse_unified_headers(INCIDENT_HEADERS)["enforcing"]
        assert enf["window"] == "seven_day_opus"
        assert enf["basis"] == "blocking_status"
        assert enf["resets_at"] == "2026-09-13T02:00:00Z"
        assert enf["utilization_pct"] == 100.0
        assert enf["remaining_pct"] == 0.0
        assert enf["status"] == "rejected"

    def test_representative_claim_used_when_nothing_blocks(self, tmp_db):
        from services.subscription_headroom_service import parse_unified_headers

        enf = parse_unified_headers(SPIKE_HEADERS)["enforcing"]
        assert enf["window"] == "five_hour"
        assert enf["basis"] == "representative_claim"
        assert enf["resets_at"] == "2026-08-19T16:00:00Z"
        assert enf["remaining_pct"] == 85.0

    @pytest.mark.parametrize("claim", ["5h", "five_hour", "FIVE_HOUR", " five_hour "])
    def test_claim_spellings_resolve(self, tmp_db, claim):
        from services.subscription_headroom_service import select_enforcing_window

        windows = {
            "five_hour": {"utilization_pct": 10.0, "resets_at": "a", "status": "allowed"},
            "seven_day": {"utilization_pct": 90.0, "resets_at": "b", "status": "allowed"},
        }
        enf = select_enforcing_window(windows, claim)
        assert enf["window"] == "five_hour"
        assert enf["basis"] == "representative_claim"

    def test_falls_back_to_the_fullest_window(self, tmp_db):
        from services.subscription_headroom_service import select_enforcing_window

        windows = {
            "five_hour": {"utilization_pct": 10.0, "resets_at": "a", "status": "allowed"},
            "seven_day": {"utilization_pct": 90.0, "resets_at": "b", "status": "allowed"},
        }
        enf = select_enforcing_window(windows, None)
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "highest_utilization"
        assert enf["remaining_pct"] == 10.0

    def test_unresolvable_claim_falls_through(self, tmp_db):
        from services.subscription_headroom_service import select_enforcing_window

        windows = {"seven_day": {"utilization_pct": 5.0, "resets_at": "b", "status": "allowed"}}
        enf = select_enforcing_window(windows, "some_window_we_never_got")
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "highest_utilization"

    def test_fullest_blocker_is_the_headline(self, tmp_db):
        from services.subscription_headroom_service import select_enforcing_window

        windows = {
            "five_hour": {"utilization_pct": 30.0, "resets_at": "a", "status": "rejected"},
            "seven_day": {"utilization_pct": 99.0, "resets_at": "b", "status": "rejected"},
        }
        assert select_enforcing_window(windows, None)["window"] == "seven_day"

    def test_blocker_without_a_figure_still_names_the_limit(self, tmp_db):
        """The 429 case: status but no utilization. `remaining_pct` stays None
        rather than becoming a fabricated 0 — but the limit IS named."""
        from services.subscription_headroom_service import select_enforcing_window

        windows = {"seven_day": {"utilization_pct": None, "resets_at": "b", "status": "rejected"}}
        enf = select_enforcing_window(windows, None)
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "blocking_status"
        assert enf["utilization_pct"] is None
        assert enf["remaining_pct"] is None

    def test_allowed_warning_does_not_block(self, tmp_db):
        """#2396: the provider's near-the-limit tier means requests ARE being
        served. It must not be reported as the thing refusing them."""
        from services.subscription_headroom_service import select_enforcing_window

        windows = {
            "five_hour": {"utilization_pct": 5.0, "resets_at": "a", "status": "allowed"},
            "seven_day": {"utilization_pct": 90.0, "resets_at": "b", "status": "allowed_warning"},
        }
        enf = select_enforcing_window(windows, None)
        assert enf["basis"] == "highest_utilization"

    def test_absent_not_fabricated(self, tmp_db):
        """The issue's explicit contract: no enforcing figure means the field is
        absent, never a `0`."""
        from services.subscription_headroom_service import select_enforcing_window

        assert select_enforcing_window({}, "five_hour") is None
        # Windows present but none blocking, claimed, or carrying a number.
        assert select_enforcing_window(
            {"five_hour": {"utilization_pct": None, "resets_at": "a", "status": "allowed"}},
            None,
        ) is None

    def test_selection_is_deterministic_across_probes(self, tmp_db):
        """A gate reading this must not see the named limit flap between two
        equally-full windows on consecutive polls."""
        from services.subscription_headroom_service import select_enforcing_window

        windows = {
            "seven_day": {"utilization_pct": 50.0, "resets_at": "b", "status": "allowed"},
            "five_hour": {"utilization_pct": 50.0, "resets_at": "a", "status": "allowed"},
        }
        picks = {select_enforcing_window(dict(windows), None)["window"] for _ in range(5)}
        assert len(picks) == 1


# =============================================================================
# 4. Model conversion + Redis back-compat
# =============================================================================

class TestToModel:
    def test_enforcing_reaches_the_payload_model(self, tmp_db):
        from services.subscription_headroom_service import (
            _to_model, parse_unified_headers,
        )
        from utils.helpers import utc_now_iso

        snap = {**parse_unified_headers(INCIDENT_HEADERS),
                "fetched_at": utc_now_iso(), "status": "ok"}
        model = _to_model(snap)
        assert model.enforcing.window == "seven_day_opus"
        assert model.enforcing.resets_at == "2026-09-13T02:00:00Z"
        assert model.enforcing.remaining_pct == 0.0
        assert set(model.windows) == {"five_hour", "seven_day", "seven_day_opus"}
        # Additive: the pre-existing fields are untouched.
        assert model.seven_day.utilization_pct == 16.0
        assert model.five_hour.utilization_pct == 4.0

    def test_pre_change_redis_snapshot_still_works(self, tmp_db):
        """Snapshots live in Redis for 7 DAYS. For a week after this ships most
        reads are of dicts written by the old parser, with no `windows` key.
        An empty map would make every one read as 'no windows reported' —
        `_headroom_indicates_healthy` returns False on that, re-pinning a stale
        LIMIT badge on every subscription (the #447 bug) until the cache turns
        over."""
        from services.subscription_headroom_service import (
            _headroom_indicates_healthy, _to_model,
        )
        from utils.helpers import utc_now_iso

        legacy = {
            "five_hour": {"utilization_pct": 15.0, "resets_at": "a", "status": "allowed"},
            "seven_day": {"utilization_pct": 6.0, "resets_at": "b", "status": "allowed"},
            "representative_claim": "five_hour",
            "fetched_at": utc_now_iso(),
            "status": "ok",
        }
        model = _to_model(legacy)
        assert set(model.windows) == {"five_hour", "seven_day"}
        assert model.enforcing.window == "five_hour"
        assert model.enforcing.basis == "representative_claim"
        assert _headroom_indicates_healthy(model) is True

    def test_windowless_snapshot_has_no_enforcing(self, tmp_db):
        from services.subscription_headroom_service import _to_model
        from utils.helpers import utc_now_iso

        model = _to_model({"fetched_at": utc_now_iso(), "status": "error"})
        assert model.enforcing is None
        assert model.windows == {}


# =============================================================================
# 5. The display predicates see the complete set
# =============================================================================

class TestPredicatesAreWindowComplete:
    def _model(self, headers, status="ok"):
        from services.subscription_headroom_service import (
            _to_model, parse_unified_headers,
        )
        from utils.helpers import utc_now_iso

        return _to_model({**parse_unified_headers(headers),
                          "fetched_at": utc_now_iso(), "status": status})

    def test_known_pair_behaviour_is_unchanged(self, tmp_db):
        """Every snapshot on every install today reports only the two known
        windows; for those, nothing about either predicate moves."""
        from services.subscription_headroom_service import (
            _headroom_indicates_healthy, _headroom_indicates_limited,
        )

        model = self._model(SPIKE_HEADERS)
        assert _headroom_indicates_limited(model) is False
        assert _headroom_indicates_healthy(model) is True

    def test_third_family_refusing_is_reported_as_limited(self, tmp_db):
        """`allowed` at 16% while every turn is rejected — the symptom."""
        from services.subscription_headroom_service import (
            _headroom_indicates_healthy, _headroom_indicates_limited,
            resolve_rate_limited_now,
        )

        model = self._model(INCIDENT_HEADERS)
        assert _headroom_indicates_limited(model) is True
        assert _headroom_indicates_healthy(model) is False
        assert resolve_rate_limited_now(db_says_limited=False, headroom=model) is True

    def test_stale_snapshot_still_drives_nothing(self, tmp_db):
        """The freshness rule outranks window completeness."""
        from services.subscription_headroom_service import (
            FRESHNESS_SECONDS, _headroom_indicates_limited, _to_model,
            parse_unified_headers,
        )
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(seconds=FRESHNESS_SECONDS + 60))
        model = _to_model({
            **parse_unified_headers(INCIDENT_HEADERS),
            "fetched_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "ok",
        })
        assert _headroom_indicates_limited(model) is False


# =============================================================================
# 6. The endpoint contract
# =============================================================================

class TestUsagePayloadContract:
    def test_decorate_usage_carries_enforcing_through(self, tmp_db, monkeypatch):
        from db_models import SubscriptionUsage, SubscriptionUsageWindow
        import services.subscription_headroom_service as svc
        from utils.helpers import utc_now_iso
        import asyncio

        snap = {**svc.parse_unified_headers(INCIDENT_HEADERS),
                "fetched_at": utc_now_iso(), "status": "ok"}

        async def fake_get_headroom(sid, **kw):
            return svc._to_model(snap)

        monkeypatch.setattr(svc, "get_headroom", fake_get_headroom)

        usage = SubscriptionUsage(
            subscription_id="sub-1",
            window_5h=SubscriptionUsageWindow(),
            window_7d=SubscriptionUsageWindow(),
        )
        out = asyncio.run(svc.decorate_usage(usage))

        assert out.source == "anthropic"
        assert out.headroom.enforcing.window == "seven_day_opus"
        assert out.headroom.enforcing.resets_at == "2026-09-13T02:00:00Z"
        # The gate that was built on this number now gets the right verdict.
        assert out.rate_limited_now is True

    def test_response_model_serializes_the_new_fields(self, tmp_db):
        """Additive on the wire: existing keys present and unchanged, new keys
        alongside them."""
        from db_models import SubscriptionUsage, SubscriptionUsageWindow
        import services.subscription_headroom_service as svc
        from utils.helpers import utc_now_iso

        snap = {**svc.parse_unified_headers(INCIDENT_HEADERS),
                "fetched_at": utc_now_iso(), "status": "ok"}
        usage = SubscriptionUsage(
            subscription_id="sub-1",
            window_5h=SubscriptionUsageWindow(),
            window_7d=SubscriptionUsageWindow(),
            headroom=svc._to_model(snap),
            source="anthropic",
        )
        body = usage.model_dump()
        hr = body["headroom"]
        for legacy_key in (
            "five_hour", "seven_day", "representative_claim",
            "overage_status", "fetched_at", "snapshot_age_seconds", "status",
        ):
            assert legacy_key in hr
        assert hr["five_hour"]["utilization_pct"] == 4.0
        assert hr["enforcing"]["window"] == "seven_day_opus"
        assert hr["enforcing"]["basis"] == "blocking_status"
        assert set(hr["windows"]) == {"five_hour", "seven_day", "seven_day_opus"}

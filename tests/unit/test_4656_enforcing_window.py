"""
#4656 — name the enforcing limit in the subscription headroom payload.

`GET /api/subscriptions/<sub>/usage` now carries an `enforcing` field that
names which window is currently binding and why, so a dispatch gate can read
the correct `resets_at` for back-off without guessing.

Covers:
  A. `parse_unified_headers` — generic window discovery + enforcing derivation
  B. `_derive_enforcing` — the selection logic in isolation
  C. `_to_model` — snapshot dict → SubscriptionHeadroom with windows/enforcing
  D. Backward compatibility — `five_hour`/`seven_day` unchanged at top level

Reference: closed PR #2858, Abilityai/lilu-trinity@lilu commit 718546057.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _svc():
    import services.subscription_headroom_service as svc
    return svc


def _build_headers(**kw) -> dict:
    """Build a plain dict of ``anthropic-ratelimit-unified-*`` headers."""
    prefix = "anthropic-ratelimit-unified-"
    out = {}
    for k, v in kw.items():
        if k == "status":
            out[prefix + "status"] = v
        elif k == "overage_status":
            out[prefix + "overage-status"] = v
        elif k == "representative_claim":
            out[prefix + "representative-claim"] = v
        else:
            # e.g. five_hour_utilization -> 5h-utilization
            part = k.replace("five_hour", "5h").replace("seven_day", "7d").replace("_", "-")
            out[prefix + part] = v
    return out


# ============================================================
# A — parse_unified_headers
# ============================================================

class TestParseUnifiedHeaders:

    def test_absent_family_returns_none(self):
        assert _svc().parse_unified_headers({}) is None
        assert _svc().parse_unified_headers({"x-other": "v"}) is None

    def test_top_level_status_alone_is_sufficient(self):
        h = _build_headers(status="allowed")
        result = _svc().parse_unified_headers(h)
        assert result is not None

    def test_five_hour_window_parsed(self):
        h = _build_headers(
            five_hour_utilization="0.39",
            five_hour_reset="1757001600",   # some unix ts
            five_hour_status="allowed",
        )
        result = _svc().parse_unified_headers(h)
        assert result is not None
        fh = result["five_hour"]
        assert fh is not None
        assert fh["utilization_pct"] == 39.0
        assert fh["status"] == "allowed"

    def test_seven_day_window_parsed(self):
        h = _build_headers(
            seven_day_utilization="1.0",
            seven_day_reset="1757001600",
            seven_day_status="rejected",
        )
        result = _svc().parse_unified_headers(h)
        assert result is not None
        sd = result["seven_day"]
        assert sd is not None
        assert sd["utilization_pct"] == 100.0
        assert sd["status"] == "rejected"

    def test_windows_dict_matches_top_level(self):
        h = _build_headers(
            five_hour_utilization="0.21",
            five_hour_status="allowed",
            seven_day_utilization="0.90",
            seven_day_status="allowed_warning",
        )
        result = _svc().parse_unified_headers(h)
        assert result["windows"]["five_hour"] == result["five_hour"]
        assert result["windows"]["seven_day"] == result["seven_day"]

    def test_enforcing_absent_when_no_windows(self):
        h = _build_headers(status="allowed")
        result = _svc().parse_unified_headers(h)
        assert result["enforcing"] is None
        assert result["windows"] == {}

    def test_enforcing_selects_blocking_window(self):
        """The 2026-09-09 incident case: 7d exhausted, 5h fine."""
        h = _build_headers(
            five_hour_utilization="0.0",
            five_hour_reset="1757001600",
            five_hour_status="allowed",
            seven_day_utilization="1.0",
            seven_day_reset="1757088000",
            seven_day_status="rejected",
            status="rate_limited",
        )
        result = _svc().parse_unified_headers(h)
        enf = result["enforcing"]
        assert enf is not None
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "blocking_status"
        assert enf["utilization_pct"] == 100.0
        assert enf["remaining_pct"] == 0.0
        assert enf["status"] == "rejected"

    def test_enforcing_absent_never_fabricated(self):
        """When no window data is available, enforcing must be null, not a zero."""
        h = _build_headers(status="allowed")
        result = _svc().parse_unified_headers(h)
        assert result["enforcing"] is None

    def test_enforcing_uses_highest_utilization_when_no_blocking(self):
        h = _build_headers(
            five_hour_utilization="0.21",
            five_hour_status="allowed",
            seven_day_utilization="0.39",
            seven_day_status="allowed",
        )
        result = _svc().parse_unified_headers(h)
        enf = result["enforcing"]
        assert enf is not None
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "highest_utilization"
        assert enf["remaining_pct"] == round(100.0 - 39.0, 1)

    def test_enforcing_representative_claim(self):
        """Provider names the binding window explicitly."""
        h = _build_headers(
            five_hour_utilization="0.21",
            five_hour_status="allowed",
            seven_day_utilization="0.39",
            seven_day_status="allowed_warning",
            representative_claim="7d",
            status="allowed",
        )
        result = _svc().parse_unified_headers(h)
        # representative_claim is normalised to the Python name
        assert result["representative_claim"] == "seven_day"
        enf = result["enforcing"]
        # 7d is non-blocking, so representative_claim is the basis
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "representative_claim"

    def test_remaining_pct_is_clamped_to_zero_on_overage(self):
        """A plan that read 120 % (overage) has remaining_pct 0, not negative."""
        h = _build_headers(
            seven_day_utilization="1.2",   # 120 %
            seven_day_status="rejected",
        )
        result = _svc().parse_unified_headers(h)
        enf = result["enforcing"]
        assert enf is not None
        assert enf["remaining_pct"] == 0.0

    def test_additive_existing_top_level_fields_unchanged(self):
        """New fields are additive; old consumers still find five_hour/seven_day."""
        h = _build_headers(
            five_hour_utilization="0.10",
            five_hour_status="allowed",
            seven_day_utilization="0.50",
            seven_day_status="allowed",
        )
        result = _svc().parse_unified_headers(h)
        assert "five_hour" in result
        assert "seven_day" in result
        assert "windows" in result
        assert "enforcing" in result


# ============================================================
# B — _derive_enforcing in isolation
# ============================================================

class TestDeriveEnforcing:

    def _derive(self, windows, claim=None):
        return _svc()._derive_enforcing(windows, claim)

    def test_empty_windows_returns_none(self):
        assert self._derive({}) is None

    def test_no_utilization_figures_returns_none(self):
        windows = {"five_hour": {"utilization_pct": None, "status": "allowed", "resets_at": None}}
        assert self._derive(windows) is None

    def test_blocking_beats_representative_claim(self):
        windows = {
            "five_hour": {"utilization_pct": 50.0, "status": "rejected", "resets_at": None},
            "seven_day": {"utilization_pct": 10.0, "status": "allowed", "resets_at": None},
        }
        enf = self._derive(windows, claim="seven_day")
        assert enf["window"] == "five_hour"
        assert enf["basis"] == "blocking_status"

    def test_among_blocked_picks_highest_utilization(self):
        windows = {
            "five_hour": {"utilization_pct": 70.0, "status": "rejected", "resets_at": None},
            "seven_day": {"utilization_pct": 100.0, "status": "rejected", "resets_at": None},
        }
        enf = self._derive(windows)
        assert enf["window"] == "seven_day"

    def test_representative_claim_beats_highest_utilization(self):
        windows = {
            "five_hour": {"utilization_pct": 90.0, "status": "allowed", "resets_at": None},
            "seven_day": {"utilization_pct": 30.0, "status": "allowed", "resets_at": None},
        }
        enf = self._derive(windows, claim="seven_day")
        assert enf["window"] == "seven_day"
        assert enf["basis"] == "representative_claim"

    def test_remaining_pct_calculation(self):
        windows = {"seven_day": {"utilization_pct": 60.0, "status": "allowed", "resets_at": None}}
        enf = self._derive(windows)
        assert enf["remaining_pct"] == 40.0

    def test_remaining_pct_none_when_no_utilization(self):
        windows = {
            "five_hour": {"utilization_pct": None, "status": "rejected", "resets_at": None}
        }
        enf = self._derive(windows)
        # blocked but no utilization figure
        assert enf is not None
        assert enf["basis"] == "blocking_status"
        assert enf["remaining_pct"] is None

    def test_allowed_warning_is_not_blocking(self):
        windows = {
            "seven_day": {"utilization_pct": 90.0, "status": "allowed_warning", "resets_at": None}
        }
        enf = self._derive(windows)
        assert enf["basis"] == "highest_utilization"

    def test_unrecognised_status_is_blocking(self):
        windows = {
            "seven_day": {"utilization_pct": 50.0, "status": "future_unknown_status", "resets_at": None}
        }
        enf = self._derive(windows)
        assert enf["basis"] == "blocking_status"


# ============================================================
# C — _to_model passes windows / enforcing into SubscriptionHeadroom
# ============================================================

class TestToModel:

    def _to_model(self, snapshot):
        return _svc()._to_model(snapshot)

    def test_none_snapshot_returns_none(self):
        assert self._to_model(None) is None

    def test_windows_and_enforcing_passed_through(self):
        snapshot = {
            "fetched_at": "2026-11-19T11:00:00.000000Z",
            "status": "rate_limited",
            "five_hour": {"utilization_pct": 0.0, "resets_at": "2026-11-19T16:00:00Z", "status": "allowed"},
            "seven_day": {"utilization_pct": 100.0, "resets_at": "2026-11-20T00:00:00Z", "status": "rejected"},
            "windows": {
                "five_hour": {"utilization_pct": 0.0, "resets_at": "2026-11-19T16:00:00Z", "status": "allowed"},
                "seven_day": {"utilization_pct": 100.0, "resets_at": "2026-11-20T00:00:00Z", "status": "rejected"},
            },
            "enforcing": {
                "window": "seven_day", "basis": "blocking_status",
                "utilization_pct": 100.0, "remaining_pct": 0.0,
                "resets_at": "2026-11-20T00:00:00Z", "status": "rejected",
            },
            "representative_claim": "seven_day",
            "overage_status": None,
            "unified_status": "rate_limited",
        }
        model = self._to_model(snapshot)
        assert model is not None
        assert model.windows["five_hour"]["utilization_pct"] == 0.0
        assert model.windows["seven_day"]["utilization_pct"] == 100.0
        assert model.enforcing is not None
        assert model.enforcing.window == "seven_day"
        assert model.enforcing.basis == "blocking_status"
        assert model.enforcing.remaining_pct == 0.0
        assert model.enforcing.resets_at == "2026-11-20T00:00:00Z"

    def test_legacy_snapshot_without_new_fields_still_deserialises(self):
        """Pre-#4656 snapshots stored in Redis lack 'windows'/'enforcing'."""
        snapshot = {
            "fetched_at": "2026-11-01T10:00:00.000000Z",
            "status": "ok",
            "five_hour": {"utilization_pct": 10.0, "resets_at": None, "status": "allowed"},
            "seven_day": {"utilization_pct": 30.0, "resets_at": None, "status": "allowed"},
        }
        model = self._to_model(snapshot)
        assert model is not None
        assert model.five_hour.utilization_pct == 10.0
        assert model.seven_day.utilization_pct == 30.0
        assert model.windows == {}
        assert model.enforcing is None

    def test_enforcing_null_in_model_when_snapshot_has_none(self):
        snapshot = {
            "fetched_at": "2026-11-19T11:00:00.000000Z",
            "status": "ok",
            "windows": {},
            "enforcing": None,
        }
        model = self._to_model(snapshot)
        assert model.enforcing is None
        assert model.windows == {}

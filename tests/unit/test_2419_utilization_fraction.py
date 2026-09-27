"""
#2419 — the utilization header is a fraction, and it is honoured past 1.0.

`anthropic-ratelimit-unified-{5h,7d}-utilization` carries a FRACTION of the
window's cap (`0.39` = 39%). `_parse_utilization` used to multiply by 100 only
when the value was <= 1.0 and pass anything larger through as an
"already-percent" reading, so an overage subscription at `1.2` (120%) displayed,
alerted and ranked as 1.2%: the ent#434 weekly alert never fired, and the #2409
ranker rated the exhausted subscription the BEST destination in the fleet.

Evidence for the contract — no real header set past 1.0 has been captured
(every local probe row carries `overage_status = rejected`: these plans block
at 100%):
- the 2026-08-19 live spike headers are fractions (`0.15`, `0.06`), and 1333
  local probe rows are consistent with x100;
- the provider's own client (Claude Code 2.1.274) reads the header as
  `Number(value)` behind `Number.isFinite`, has no `<= 1` branch and no clamp
  on 5h/7d (it DOES clamp a sibling header, `-slow-budget-utilization`), and
  displays `Math.round(u * 100)`.
The overage header set below is therefore SYNTHETIC — modelled on that parser,
not on a capture. Whether the provider ever emits a value past 1.0 is open; the
fix makes Trinity agree with the provider's client for every input either way,
and the `[#2419]` arrival log in `_probe` records the raw strings the first time
one arrives.

Related flow: docs/memory/feature-flows/subscription-usage-tracking.md
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _svc():
    import services.subscription_headroom_service as headroom
    return headroom


def _alerts():
    import services.subscription_headroom_alerts as alerts
    return alerts


_H = "anthropic-ratelimit-unified-"

# The 2026-08-19 spike payload's shape with the WEEKLY window past its cap.
# Synthetic (see the module docstring); statuses are passed through verbatim
# and are not the subject here — the number is.
OVERAGE_HEADERS = {
    _H + "5h-reset": "1787155200",
    _H + "5h-status": "allowed",
    _H + "5h-utilization": "0.34",
    _H + "7d-reset": "1787720400",
    _H + "7d-status": "allowed",
    _H + "7d-utilization": "1.2",
    _H + "representative-claim": "seven_day",
    _H + "overage-status": "allowed",
    _H + "status": "allowed",
}

WITHIN_CAP_HEADERS = {**OVERAGE_HEADERS, _H + "7d-utilization": "0.39"}


# =============================================================================
# A — the parser: fraction in, percent out, never a "tolerated" percent
# =============================================================================

class TestParser:

    @pytest.mark.parametrize("raw, expected", [
        ("1.2", 120.0),        # the issue's case: 120%, not 1.2%
        ("0.39", 39.0),
        ("1.0", 100.0),        # exactly at the cap
        ("0.15", 15.0),
        ("0", 0.0),
        ("1.15", 115.0),       # 114.99999999999999 rounds up
        ("0.018416969696969696", 1.8),   # 1-decimal rounding is kept
        (" 1.2 ", 120.0),      # any float() literal
        ("42.5", 4250.0),      # a percent-shaped input is NOT tolerated: loud-wrong,
                               # the same 4250% the provider's client would show
    ])
    def test_fraction_scales_to_percent(self, raw, expected):
        assert _svc()._parse_utilization(raw) == expected

    @pytest.mark.parametrize("raw", [
        "nan", "NaN", "-nan", "inf", "-inf", "Infinity", "1e400",
        "1e307",   # finite, but overflows to inf only AFTER the multiply
        "-0.1",    # malformed provider data, not a very empty subscription
        "", "garbage", "0x10", "1,2", None,
    ])
    def test_non_finite_negative_or_unparseable_is_none(self, raw):
        assert _svc()._parse_utilization(raw) is None

    def test_negative_zero_is_plain_zero(self):
        """`-0.0` survives `< 0` and would render as "-0%"."""
        v = _svc()._parse_utilization("-0.0")
        assert v == 0.0 and math.copysign(1.0, v) == 1.0


# =============================================================================
# B — the header set → snapshot dict
# =============================================================================

class TestHeaders:

    def test_overage_window_parses_past_100(self):
        snap = _svc().parse_unified_headers(OVERAGE_HEADERS)
        assert snap["seven_day"]["utilization_pct"] == 120.0
        assert snap["five_hour"]["utilization_pct"] == 34.0
        assert snap["seven_day"]["status"] == "allowed"
        assert snap["overage_status"] == "allowed"

    def test_history_row_keeps_the_figure(self):
        svc = _svc()
        snap = {**svc.parse_unified_headers(OVERAGE_HEADERS), "status": "ok"}
        assert svc._history_row(snap)["seven_day"]["utilization_pct"] == 120.0


# =============================================================================
# C — the filed bug, end to end through the REAL path:
#     headers → snapshot → model → classifier / ranker verdict
# =============================================================================

def _model_from(headers):
    svc = _svc()
    from utils.helpers import utc_now_iso
    snap = {**svc.parse_unified_headers(headers), "fetched_at": utc_now_iso(), "status": "ok"}
    return svc._to_model(snap)


class TestChain:

    def test_an_overage_week_is_saturated_for_the_weekly_alert(self):
        """ent#434: the classifier keys on the weekly figure. 1.2 used to
        arrive here as 1.2 → `has_headroom` — the exact reading the alert
        exists for, silently filed as fine."""
        svc = _svc()
        assert svc.classify_headroom(_model_from(OVERAGE_HEADERS), threshold_pct=75) == svc.SATURATED
        assert svc.classify_headroom(_model_from(WITHIN_CAP_HEADERS), threshold_pct=75) == svc.HAS_HEADROOM

    def test_an_overage_week_is_measured_at_120_for_the_ranker(self):
        """#2409: the ranker's verdict is the fuller window's percentage."""
        svc = _svc()
        reading = svc.headroom_reading(_model_from(OVERAGE_HEADERS))
        assert svc.selection_verdict(reading)[:2] == (svc.SELECTION_MEASURED, 120.0)

    def test_decide_tier_escalates_at_120(self):
        alerts = _alerts()
        assert alerts.decide_tier(120.0, 75, 90) == alerts.TIER_CRIT

    def test_ranker_sorts_the_overage_subscription_last(self):
        """Today's highest pinned rank value is 100.0; past the cap must sort
        AFTER a subscription with real headroom, never ahead of it."""
        svc = _svc()
        cands = [SimpleNamespace(id="overage", name="overage", agent_count=0),
                 SimpleNamespace(id="cool", name="cool", agent_count=0)]
        readings = {
            "overage": svc.headroom_reading(_model_from(OVERAGE_HEADERS)),
            "cool": svc.headroom_reading(_model_from(WITHIN_CAP_HEADERS)),
        }
        assert [s.id for s in svc.rank_subscriptions(cands, readings)] == ["cool", "overage"]


# =============================================================================
# D — the arrival signal: the only way AC #1 can ever close
# =============================================================================

class TestArrivalSignal:

    def _run_probe(self, monkeypatch, caplog, headers):
        svc = _svc()
        monkeypatch.setattr(svc.db, "get_subscription_token", lambda sid: "tok")

        async def _fake_post(token):
            return SimpleNamespace(status_code=200, headers=headers)

        monkeypatch.setattr(svc, "_post_probe", _fake_post)
        with caplog.at_level(logging.INFO, logger=svc.__name__):
            snap = asyncio.run(svc._probe("sub-1"))
        return snap, [r for r in caplog.records if "[#2419]" in r.getMessage()]

    def test_a_window_past_its_cap_logs_the_raw_headers(self, monkeypatch, caplog):
        snap, hits = self._run_probe(monkeypatch, caplog, OVERAGE_HEADERS)
        assert snap["seven_day"]["utilization_pct"] == 120.0
        assert len(hits) == 1
        msg = hits[0].getMessage()
        assert "sub-1" in msg
        assert "'1.2'" in msg and "'0.34'" in msg          # the RAW strings — the capture
        assert "overage-status='allowed'" in msg
        assert hits[0].levelno == logging.INFO

    def test_within_the_cap_is_silent(self, monkeypatch, caplog):
        snap, hits = self._run_probe(monkeypatch, caplog, WITHIN_CAP_HEADERS)
        assert snap["seven_day"]["utilization_pct"] == 39.0
        assert hits == []

"""The ONE stale rule (trinity-enterprise#479, C2).

`freshness()` is the platform's single definition of "is this number still
true?" — the tile, the bound widget, the health block, the MCP tool and
#2927's role card all call it. These are the boundary cases that decide what
the whole platform means by "stale", so they are pinned as a table rather than
left to the route tests that happen to exercise one arm each.

The function is PURE and imported without `database`: the first test proves
that property directly, because the moment it needs the store, ent#666 and the
role card will copy the rule instead of importing it — which is exactly how a
platform ends up with three thresholds.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

from services.metric_read_service import (  # noqa: E402
    STALE_RULE,
    MetricReadError,
    fold,
    freshness,
    resolve_window,
)

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
HOUR = 3600


def _ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

def test_a_point_inside_one_cadence_is_fresh():
    verdict = freshness(HOUR, _ago(600), NOW)
    assert verdict["stale"] is False
    assert verdict["freshness"] == "fresh"
    # The deadline is stated even while fresh: a tile can say WHEN it will go
    # stale, which is the difference between a status and a countdown.
    assert verdict["stale_after"].startswith("2026-09-22T13:50")


def test_exactly_two_cadences_is_not_yet_stale():
    """Strict `>`, per the issue AC. One second decides this, so it is pinned
    at both sides rather than at a comfortable middle."""
    assert freshness(HOUR, _ago(2 * HOUR), NOW)["stale"] is False


def test_one_second_past_two_cadences_is_stale():
    verdict = freshness(HOUR, _ago(2 * HOUR + 1), NOW)
    assert verdict["stale"] is True
    assert verdict["freshness"] == "stale"


def test_no_declared_cadence_is_never_stale():
    """`None`, not `False`: "cannot be late" and "is not late" are different
    answers, and the tile renders them with different copy."""
    for cadence in (None, 0, ""):
        verdict = freshness(cadence, _ago(400 * 24 * HOUR), NOW)
        assert verdict["stale"] is None, cadence
        assert verdict["freshness"] == "no_cadence"
        assert verdict["stale_after"] is None


def test_a_metric_with_no_points_is_empty_not_stale():
    verdict = freshness(HOUR, None, NOW)
    assert verdict["stale"] is False
    assert verdict["freshness"] == "no_points"
    assert verdict["stale_after"] is None


def test_a_future_point_clamps_to_fresh_rather_than_a_negative_age():
    """The write path accepts up to 300 s of clock skew, so a future `ts` is
    legal input — it must not read as an enormous age or a negative one."""
    ahead = (NOW + timedelta(seconds=200)).isoformat().replace("+00:00", "Z")
    verdict = freshness(HOUR, ahead, NOW)
    assert verdict["age_seconds"] == 0
    assert verdict["stale"] is False


def test_an_unparseable_stamp_reads_as_no_points_not_as_a_crash():
    assert freshness(HOUR, "not-a-timestamp", NOW)["freshness"] == "no_points"


def test_a_naive_stamp_is_read_as_utc():
    """Historical rows predate the Z-suffix normalisation; reading one as
    local time would shift a fresh metric by the host's offset (#1474)."""
    naive = (NOW - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
    assert freshness(HOUR, naive, NOW)["stale"] is False


def test_a_naive_now_is_read_as_utc_too():
    assert freshness(HOUR, _ago(60), NOW.replace(tzinfo=None))["stale"] is False


def test_the_rule_is_named_once_for_every_consumer():
    assert STALE_RULE == "2x cadence"


def test_freshness_does_not_need_the_database():
    """The property that keeps the rule importable by #2927's role card and
    ent#666 without dragging the store in. If this fails, the next consumer
    copies the threshold instead of importing it."""
    import services.metric_read_service as mod

    sys.modules.pop("database", None)
    assert mod.freshness(HOUR, _ago(10), NOW)["stale"] is False
    assert "database" not in sys.modules


# ---------------------------------------------------------------------------
# The fold (TD-2) — which dimension series count toward the tile value
# ---------------------------------------------------------------------------

def test_sum_folds_every_series_including_one_that_stopped_reporting():
    """A region that stopped keeps contributing its last value, and its own
    `stale` flag is what says so — dropping it would make a total FALL with no
    event behind the drop."""
    assert fold([10, 5, 2], "sum") == 17


def test_avg_folds_the_mean():
    assert fold([10, 20], "avg") == 15


def test_last_and_unknown_aggregations_take_the_newest():
    assert fold([7, 1, 2], "last") == 7
    assert fold([7, 1, 2], None) == 7
    assert fold([7, 1, 2], "p99") == 7


def test_a_status_label_never_sums():
    """Non-numeric values fall back to the newest whatever the declaration
    says, because summing labels is not an answer."""
    assert fold(["healthy", "degraded"], "sum") == "healthy"


def test_an_empty_fold_is_none_not_zero():
    assert fold([], "sum") is None


# ---------------------------------------------------------------------------
# Window resolution (TD-8)
# ---------------------------------------------------------------------------

def test_the_enum_windows_resolve_to_their_spans():
    for name, hours in (("24h", 24), ("7d", 168), ("30d", 720), ("90d", 2160)):
        assert resolve_window(name, now=NOW)["hours"] == hours


def test_auto_is_at_least_a_day_for_a_fast_metric():
    window = resolve_window(
        "auto", definitions=[{"cadence_seconds": 300}], now=NOW)
    assert window["hours"] == 24
    assert window["kind"] == "auto"


def test_auto_widens_for_a_weekly_metric():
    """The reason `auto` exists: twelve weekly points do not fit in 24h, and a
    tile with four points is not a chart."""
    weekly = [{"cadence_seconds": 7 * 24 * HOUR}]
    assert resolve_window("auto", definitions=weekly, now=NOW)["hours"] == 2016


def test_auto_is_capped_at_ninety_days():
    yearly = [{"cadence_seconds": 366 * 24 * HOUR}]
    assert resolve_window("auto", definitions=yearly, now=NOW)["hours"] == 2160


def test_since_overrides_the_enum():
    window = resolve_window(
        "24h", since="2026-09-01T00:00:00Z", until="2026-09-08T00:00:00Z",
        now=NOW)
    assert window["kind"] == "custom"
    assert window["since"].startswith("2026-09-01")
    assert window["hours"] == 168


def test_an_unknown_window_name_is_a_named_refusal():
    with pytest.raises(MetricReadError) as exc:
        resolve_window("last-tuesday", now=NOW)
    assert exc.value.reason == "window_invalid"
    assert "auto" in exc.value.message


def test_a_span_beyond_retention_is_refused_rather_than_answered_empty():
    with pytest.raises(MetricReadError) as exc:
        resolve_window(None, since="2020-01-01T00:00:00Z", now=NOW,
                       retention_days=30)
    assert exc.value.reason == "window_invalid"
    assert "retained" in exc.value.message


def test_until_before_since_is_refused():
    with pytest.raises(MetricReadError) as exc:
        resolve_window(None, since="2026-09-08T00:00:00Z",
                       until="2026-09-01T00:00:00Z", now=NOW)
    assert exc.value.reason == "window_invalid"


def test_a_malformed_since_is_named_not_ignored():
    with pytest.raises(MetricReadError) as exc:
        resolve_window(None, since="yesterday", now=NOW)
    assert exc.value.reason == "window_invalid"

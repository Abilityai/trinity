"""A `dims:` tile finds its series by itself, not among the busy ones (#3293).

A bound widget's `dims:` selector used to be matched against the metric's
shared read: the 200 newest points across ALL of its series. A channel that
reports once a day beside one that reports every minute fell out of that
slice, and its tile said "no recent data" although its own last point was
hours old.

Every test here seeds the REAL store (through `insert_metric_points`) with a
busy series holding MORE than `LATEST_POINTS_PER_METRIC` points that are all
newer than the quiet series' points, then binds through
`bind_dashboard_widgets`. Definitions come from the test; points, ordering and
the per-series lookup go through the real SQL on every available backend.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _ROOT / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

from db_harness import db_backend  # noqa: E402,F401

import database as database_mod  # noqa: E402
from services import metric_read_service as mrs  # noqa: E402
from services.metric_points_service import point_identity  # noqa: E402

AGENT = "quiet-agent"
HOUR = 3600
DAY = 24 * HOUR
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

#: More than the shared read keeps, so the quiet series is always outside it.
BUSY_POINTS = mrs.LATEST_POINTS_PER_METRIC + 50


def _ts(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _definition(**overrides):
    d = {
        "name": "ad_spend",
        "type": "gauge",
        "label": "Spend",
        "unit": "USD",
        "status": "active",
        "cadence_seconds": HOUR,
        "aggregation": "last",
        "dimensions": ["channel"],
        "values": None,
        "retired_at": None,
        "type_conflict": None,
        "direction": "down_good",
        "warning_threshold": None,
        "critical_threshold": None,
        "description": None,
        "cadence": "1h",
    }
    d.update(overrides)
    return d


def _point(value, seconds_ago, dims):
    ts = _ts(seconds_ago)
    return {
        "metric": "ad_spend",
        "ts": ts,
        "value_numeric": value,
        "value_text": None,
        "dims": dims,
        "idempotency_key": point_identity("ad_spend", ts, dims),
        "execution_id": None,
        "created_at": ts,
    }


def _busy(dims, count=BUSY_POINTS):
    """One point a minute, newest a minute ago: every one newer than 5 h."""
    return [_point(600.0 + i, 60 + i * 60, dims) for i in range(count)]


class _Store:
    """Definitions from the test; every point read is the REAL store's SQL."""

    def __init__(self, real, definitions):
        self._real = real
        self.definitions = definitions
        self.fail_series_lookup = False

    def list_metric_definitions(self, name, include_retired=False):
        return list(self.definitions)

    def metric_series_points_for_dims(self, *a, **k):
        if self.fail_series_lookup:
            raise RuntimeError("store down")
        return self._real.metric_series_points_for_dims(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def seeded(db_backend, monkeypatch):
    """`seed(points, definitions=None)` -> the store the binding reads."""
    real = database_mod.db

    def seed(points, definitions=None):
        real.insert_metric_points(AGENT, points)
        store = _Store(real, definitions or [_definition()])
        monkeypatch.setattr(database_mod, "db", store)
        return store

    return seed


def _bind(*widgets):
    config = {"sections": [{"widgets": [dict(w) for w in widgets]}]}
    mrs.bind_dashboard_widgets(config, AGENT, now=NOW)
    return config["sections"][0]["widgets"]


def _w(dims=None, **extra):
    widget = {"type": "metric", "label": "Spend", "metric": "ad_spend"}
    if dims is not None:
        widget["dims"] = dims
    widget.update(extra)
    return widget


def _history_values(widget):
    return [p["v"] for p in widget["history"]["values"]]


# ---------------------------------------------------------------------------
# AC1 / AC4: the quiet series binds with its own latest point and history
# ---------------------------------------------------------------------------


def test_a_quiet_series_beside_a_busy_one_binds_with_its_own_points(seeded):
    quiet = [
        _point(410.0, 5 * HOUR, {"channel": "google"}),
        _point(390.0, 10 * HOUR, {"channel": "google"}),
        _point(370.0, 20 * HOUR, {"channel": "google"}),
    ]
    seeded(_busy({"channel": "meta"}) + quiet)

    # The premise: the shared read holds no google point at all.
    shared = database_mod.db.latest_metric_points(
        AGENT, ["ad_spend"], mrs.LATEST_POINTS_PER_METRIC)
    assert len(shared) == mrs.LATEST_POINTS_PER_METRIC
    assert {r["dims"]["channel"] for r in shared} == {"meta"}

    (google,) = _bind(_w({"channel": "google"}))
    assert google["bound"] is True
    assert "binding_error_code" not in google
    assert google["value"] == 410.0
    assert google["last_point_at"] == _ts(5 * HOUR)
    # 5 h old on a 1 h cadence: its OWN freshness, not the busy series'.
    assert google["stale"] is True and google["freshness"] == "stale"
    assert _history_values(google) == [370.0, 390.0, 410.0]
    assert google["bound_series"]["basis"] == "selected"
    assert google["bound_series"]["dims"] == {"channel": "google"}


def test_the_busy_series_tile_is_its_own_series_too(seeded):
    seeded(_busy({"channel": "meta"})
           + [_point(410.0, 5 * HOUR, {"channel": "google"})])
    (meta,) = _bind(_w({"channel": "meta"}))
    assert meta["bound"] is True
    assert meta["value"] == 600.0
    assert meta["last_point_at"] == _ts(60)
    assert meta["stale"] is False


def test_a_quiet_series_older_than_the_tile_window_still_shows_its_number(seeded):
    """Three days old: outside the 24 h the sparkline covers, so there is no
    history to draw, but the series reports and the tile says what it last
    said (stale), exactly like a slow series inside the shared read."""
    seeded(_busy({"channel": "meta"})
           + [_point(410.0, 3 * DAY, {"channel": "google"})])
    (google,) = _bind(_w({"channel": "google"}))
    assert google["bound"] is True
    assert google["value"] == 410.0
    assert google["last_point_at"] == _ts(3 * DAY)
    assert google["stale"] is True
    assert _history_values(google) == []


def test_key_order_in_the_stored_dims_does_not_hide_the_series(seeded):
    """`dims` is stored in caller key order; identity is canonical."""
    quiet = [_point(410.0, 5 * HOUR, {"geo": "us", "channel": "google"})]
    seeded(_busy({"channel": "meta", "geo": "us"}) + quiet,
           [_definition(dimensions=["channel", "geo"])])
    (widget,) = _bind(_w({"channel": "google", "geo": "us"}))
    assert widget["bound"] is True and widget["value"] == 410.0


def test_a_busy_wider_series_does_not_crowd_out_the_exact_one(seeded):
    """Exact match only: `channel=google, geo=us` is another series, however
    many points it holds, and must neither answer for `channel=google` nor
    push it out of the lookup."""
    seeded(_busy({"channel": "google", "geo": "us"})
           + [_point(410.0, 5 * HOUR, {"channel": "google"})],
           [_definition(dimensions=["channel", "geo"])])
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["bound"] is True
    assert widget["value"] == 410.0
    assert widget["bound_series"]["dims"] == {"channel": "google"}


# ---------------------------------------------------------------------------
# AC2: true absence still refuses, and never falls back to the fold
# ---------------------------------------------------------------------------


def test_a_series_that_never_reported_is_refused_never_the_fold(seeded):
    seeded(_busy({"channel": "meta"})
           + [_point(410.0, 5 * HOUR, {"channel": "google"})])
    (widget,) = _bind(_w({"channel": "tiktok"}, value=0, color="red"))
    assert widget["bound"] is False
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert widget["binding_error"] == (
        "metric 'ad_spend': no recent data for channel=tiktok")
    for key in ("value", "history", "color", "bound_series", "last_point_at"):
        assert key not in widget, key
    detail = widget["binding_detail"]
    assert detail["selector"] == {"channel": "tiktok"}
    assert detail["recent_series"] == [{"channel": "meta"}]
    # The refusal states the bound it applied, and no longer blames a
    # 200-point window the selected series is not read through.
    assert detail["lookback_days"] == mrs.SELECTED_SERIES_LOOKBACK_HOURS // 24
    assert "window_points" not in detail and "series_cap" not in detail


def test_a_partial_selector_is_refused_with_the_wider_series_as_near(seeded):
    seeded(_busy({"channel": "google", "geo": "us"}),
           [_definition(dimensions=["channel", "geo"])])
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert "value" not in widget
    assert widget["binding_detail"]["near"] == [
        {"channel": "google", "geo": "us"}]


def test_a_series_carrying_a_since_removed_dimension_is_not_the_selected_one(
    seeded,
):
    """`geo` is no longer declared, so the store cannot be told to exclude it.
    Identity is still `canonical_dims`: `channel=google, geo=us` must not
    answer for `channel=google`."""
    seeded(_busy({"channel": "meta"})
           + [_point(410.0, 5 * HOUR, {"channel": "google", "geo": "us"})])
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["bound"] is False
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert "value" not in widget


def test_a_series_silent_for_longer_than_the_lookback_has_no_recent_point(seeded):
    old = mrs.SELECTED_SERIES_LOOKBACK_HOURS * HOUR + DAY
    seeded(_busy({"channel": "meta"})
           + [_point(410.0, old, {"channel": "google"})])
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["bound"] is False
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert "value" not in widget


def test_a_failed_series_lookup_refuses_that_tile_only(seeded):
    """The lookup runs per widget, outside the bind's store `try`: a store
    fault there must cost one tile its number, never the dashboard read."""
    store = seeded(_busy({"channel": "meta"})
                   + [_point(410.0, 5 * HOUR, {"channel": "google"})])
    store.fail_series_lookup = True
    google, fold = _bind(_w({"channel": "google"}, value=0), _w())
    assert google["bound"] is False
    assert google["binding_error_code"] == "metric_store_unavailable"
    assert "value" not in google
    assert fold["bound"] is True and fold["value"] == 600.0


# ---------------------------------------------------------------------------
# AC3: the unselected tile and the objective join read the same fold
# ---------------------------------------------------------------------------


def test_the_unselected_tile_still_equals_the_objective_joins_fold(seeded):
    seeded(_busy({"channel": "meta"})
           + [_point(410.0, 5 * HOUR, {"channel": "google"})],
           [_definition(aggregation="sum")])
    (fold,) = _bind(_w())
    joined = mrs.latest_by_metric(AGENT, now=NOW)["ad_spend"]
    assert fold["bound"] is True
    assert fold["value"] == joined["latest"]["value"] == 600.0
    assert fold["last_point_at"] == joined["last_point_at"]
    assert fold["stale"] == joined["stale"]
    # The fold is over the shared read, as before: google is not in it.
    assert joined["series_count"] == 1


# ---------------------------------------------------------------------------
# The store read the binding rests on
# ---------------------------------------------------------------------------


def test_the_store_reads_one_series_newest_first_and_bounded(db_backend):
    real = database_mod.db
    google = [_point(float(i), HOUR + i * HOUR, {"channel": "google"})
              for i in range(6)]
    real.insert_metric_points(
        AGENT,
        _busy({"channel": "meta"}, 20) + google
        + [_point(9.0, 30, {"channel": "google", "geo": "us"}),
           _point(8.0, 40, None)])

    rows = real.metric_series_points_for_dims(
        AGENT, "ad_spend", {"channel": "google"}, ["geo"], _ts(DAY), 4)
    assert [r["value_numeric"] for r in rows] == [0.0, 1.0, 2.0, 3.0]
    assert all(r["dims"] == {"channel": "google"} for r in rows)

    since = real.metric_series_points_for_dims(
        AGENT, "ad_spend", {"channel": "google"}, ["geo"], _ts(2 * HOUR), 50)
    assert [r["value_numeric"] for r in since] == [0.0, 1.0]

    assert real.metric_series_points_for_dims(
        AGENT, "ad_spend", {"channel": "tiktok"}, ["geo"], _ts(DAY), 50) == []
    assert real.metric_series_points_for_dims(
        "other-agent", "ad_spend", {"channel": "google"}, ["geo"],
        _ts(DAY), 50) == []

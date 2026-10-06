"""A bound `dashboard.yaml` widget names ONE series with `dims:` (ent#730).

Before ent#730 a widget bound to a dimensioned metric (one series per
channel) could only show the cross-series fold, so three "per-channel" tiles
showed the same number with no label. `bind_dashboard_widgets` now picks a
SOURCE (the one selected series, or today's fold) and fills the widget once,
so value, freshness, colour and sparkline come from the same place on both
paths, and a selector that cannot match is a named refusal, never the fold.

The fixture is three `channel` series with distinct values: meta 623.88
(newest), google 410.0, linkedin 95.5 (3 h old on a 1 h cadence, so stale).
Assertions pin google and linkedin, never meta, because meta is exactly what
the bug returned under `last`.

The fake store honours the SQL contract of `latest_metric_points` (per metric,
newest-first by `ts` then `idempotency_key`, sliced to `per_metric_limit`);
the ent#479 fake ignores the limit, which would make the 200-point window
untestable. `test_the_fake_store_matches_the_real_store` pins that.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _ROOT / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401

import database as database_mod  # noqa: E402
from services import metric_read_service as mrs  # noqa: E402
from services.metric_points_service import point_identity  # noqa: E402

AGENT = "dims-agent"
HOUR = 3600
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

NEW_CODES = {
    "metric_series_not_found",
    "metric_dimension_undeclared",
    "metric_dimension_invalid",
}
FILLED_KEYS = (
    "value",
    "history",
    "color",
    "bound_series",
    "last_point_at",
    "stale",
    "freshness",
    "trend",
    "trend_value",
)


def _ts(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _definition(name="ad_spend", **overrides):
    d = {
        "name": name,
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
        "critical_threshold": 500,
        "description": None,
        "cadence": "1h",
    }
    d.update(overrides)
    return d


def _point(value, seconds_ago, dims=None, metric="ad_spend"):
    ts = _ts(seconds_ago)
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    return {
        "metric": metric,
        "ts": ts,
        "value_numeric": value if numeric else None,
        "value_text": None if numeric else value,
        "dims": dims,
        "idempotency_key": point_identity(metric, ts, dims),
    }


def _three_channels(metric="ad_spend"):
    return [
        _point(623.88, 60, {"channel": "meta"}, metric),
        _point(410.0, 120, {"channel": "google"}, metric),
        _point(95.5, 3 * HOUR, {"channel": "linkedin"}, metric),
    ]


class _Db:
    """The store, honouring `latest_metric_points`' SQL contract."""

    def __init__(self, definitions=None, points=None):
        self.definitions = definitions if definitions is not None else [_definition()]
        self.points = points if points is not None else _three_channels()

    def list_metric_definitions(self, name, include_retired=False):
        return list(self.definitions)

    def latest_metric_points(self, name, metric_names, per_metric_limit=200):
        if not metric_names or per_metric_limit <= 0:
            return []
        out = []
        for metric in metric_names:
            rows = [p for p in self.points if p["metric"] == metric]
            rows.sort(key=lambda p: (p["ts"], p["idempotency_key"]), reverse=True)
            out.extend(dict(r) for r in rows[:per_metric_limit])
        return out

    def metric_series_points(self, *a, **k):
        return []

    def calculate_widget_stats(self, values):
        nums = [v["v"] for v in values if isinstance(v.get("v"), (int, float))]
        if not nums:
            return None
        return {
            "min": min(nums),
            "max": max(nums),
            "avg": sum(nums) / len(nums),
            "trend": "stable",
            "trend_percent": 0,
        }


@pytest.fixture
def store(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(database_mod, "db", fake)
    return fake


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
# AC1 / AC2: one series' number, freshness and sparkline
# ---------------------------------------------------------------------------


def test_T1_each_dims_tile_shows_its_own_series(store):
    google, linkedin = _bind(_w({"channel": "google"}), _w({"channel": "linkedin"}))
    assert (google["value"], linkedin["value"]) == (410.0, 95.5)
    assert google["last_point_at"] == _ts(120)
    assert linkedin["last_point_at"] == _ts(3 * HOUR)
    assert google["stale"] is False and google["freshness"] == "fresh"
    assert linkedin["stale"] is True and linkedin["freshness"] == "stale"
    assert _history_values(google) == [410.0]
    assert _history_values(linkedin) == [95.5]
    assert google["bound"] is True and linkedin["bound"] is True


def test_T2_a_sum_metric_still_shows_the_one_series(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition(aggregation="sum")]))
    (google,) = _bind(_w({"channel": "google"}))
    assert google["value"] == 410.0  # not the 1129.38 total


def test_T3_matching_is_canonical_so_key_order_does_not_matter(monkeypatch):
    monkeypatch.setattr(
        database_mod,
        "db",
        _Db(
            [_definition(dimensions=["channel", "geo"])],
            [
                _point(623.88, 60, {"channel": "meta", "geo": "us"}),
                _point(410.0, 120, {"channel": "google", "geo": "us"}),
            ],
        ),
    )
    (widget,) = _bind(_w({"geo": "us", "channel": "google"}))
    assert widget["value"] == 410.0
    assert widget["bound_series"]["dims"] == {"channel": "google", "geo": "us"}


# ---------------------------------------------------------------------------
# AC3: a selector that cannot match is a named refusal, never the fold
# ---------------------------------------------------------------------------


def test_T4_no_matching_series_refuses_by_name(store):
    (widget,) = _bind(_w({"channel": "tiktok"}))
    assert widget["bound"] is False
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert widget["binding_error"] == (
        "metric 'ad_spend': no recent data for channel=tiktok"
    )
    for key in FILLED_KEYS:
        assert key not in widget, key
    assert widget["binding_detail"] == {
        "selector": {"channel": "tiktok"},
        "recent_series": [
            {"channel": "meta"},
            {"channel": "google"},
            {"channel": "linkedin"},
        ],
        "more": 0,
        "window_points": mrs.LATEST_POINTS_PER_METRIC,
        "series_cap": None,
        "near": [],
    }


def test_T5_a_refusal_drops_the_authors_placeholder_value_and_colour(store):
    (widget,) = _bind(_w({"channel": "tiktok"}, value=0, color="green"))
    assert "value" not in widget and "color" not in widget


def test_T6_a_partial_selector_is_refused_not_folded(monkeypatch):
    monkeypatch.setattr(
        database_mod,
        "db",
        _Db(
            [_definition(dimensions=["channel", "geo"])],
            [
                _point(623.88, 60, {"channel": "meta", "geo": "us"}),
                _point(410.0, 120, {"channel": "google", "geo": "us"}),
            ],
        ),
    )
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert "value" not in widget
    assert widget["binding_detail"]["near"] == [{"channel": "google", "geo": "us"}]


# ---------------------------------------------------------------------------
# AC1 / AC5: selection is a singleton fold, and history is the same series
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("aggregation", ["last", "sum", "avg"])
@pytest.mark.parametrize("channel", ["google", "linkedin"])
def test_T7_a_selected_tile_equals_the_fold_over_that_series_alone(
    monkeypatch, aggregation, channel
):
    definition = _definition(aggregation=aggregation)
    monkeypatch.setattr(database_mod, "db", _Db([definition]))
    (widget,) = _bind(_w({"channel": channel}))

    alone = [p for p in _three_channels() if p["dims"]["channel"] == channel]
    monkeypatch.setattr(database_mod, "db", _Db([definition], alone))
    tile = mrs.latest_by_metric(AGENT, now=NOW)["ad_spend"]

    assert widget["value"] == tile["latest"]["value"]
    assert widget["last_point_at"] == tile["last_point_at"]
    assert widget["stale"] == tile["stale"]
    assert widget["freshness"] == tile["freshness"]


def test_T8_a_selected_sum_tile_draws_its_own_series_not_the_total(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition(aggregation="sum")]))
    (google,) = _bind(_w({"channel": "google"}))
    assert google["history"]["values"] == [{"t": _ts(120), "v": 410.0}]


@pytest.mark.parametrize("meta_points,google_seen", [(197, 3), (198, 2)])
def test_T8_the_window_is_the_200_newest_points_of_the_metric(
    monkeypatch, meta_points, google_seen
):
    """Three google points older than every meta point. With 200 points in
    all, google draws all three; one more meta point pushes google's oldest
    out of the read (the documented 200-point limit)."""
    google = [
        _point(400.0 + i, 20 * HOUR + i * HOUR, {"channel": "google"}) for i in range(3)
    ]
    meta = [
        _point(600.0, 60 + i * 300, {"channel": "meta"}) for i in range(meta_points)
    ]
    monkeypatch.setattr(
        database_mod, "db", _Db([_definition(aggregation="sum")], meta + google)
    )
    (widget,) = _bind(_w({"channel": "google"}))
    assert len(widget["history"]["values"]) == google_seen


def test_T8_same_series_does_not_mean_equal_endpoints(monkeypatch):
    """AC5 is "the number and the sparkline describe the same series": a
    `sum` series recorded 10 then 20 inside one bucket shows 20 (its latest
    point) over a last bucket of 30 (the bucket's declared fold)."""
    monkeypatch.setattr(
        database_mod,
        "db",
        _Db(
            [_definition(aggregation="sum")],
            [
                _point(10.0, 120, {"channel": "google"}),
                _point(20.0, 60, {"channel": "google"}),
            ],
        ),
    )
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["value"] == 20.0
    assert widget["history"]["values"][-1]["v"] == 30.0


# ---------------------------------------------------------------------------
# AC4: no `dims` keeps today's number and says what it is
# ---------------------------------------------------------------------------


def test_T9_a_folded_sum_tile_says_it_is_a_fold(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition(aggregation="sum")]))
    (widget,) = _bind(_w())
    assert widget["value"] == pytest.approx(1129.38)
    assert widget["bound_series"] == {
        "basis": "folded",
        "aggregation": "sum",
        "series_count": 3,
        "dims": None,
        "dimensions": ["channel"],
        "stale_count": 1,
    }


def test_T9_a_last_tile_over_several_series_names_the_newest(store):
    (widget,) = _bind(_w())
    assert widget["value"] == 623.88
    assert widget["bound_series"] == {
        "basis": "series",
        "aggregation": "last",
        "series_count": 3,
        "dims": {"channel": "meta"},
        "dimensions": ["channel"],
    }


def test_T9_one_series_under_sum_is_a_series_basis(monkeypatch):
    monkeypatch.setattr(
        database_mod,
        "db",
        _Db(
            [_definition(aggregation="sum")],
            [_point(410.0, 120, {"channel": "google"})],
        ),
    )
    (widget,) = _bind(_w())
    assert widget["bound_series"] == {
        "basis": "series",
        "aggregation": "sum",
        "series_count": 1,
        "dims": {"channel": "google"},
        "dimensions": ["channel"],
    }


def test_T9_a_status_metric_declaring_sum_is_never_captioned_a_fold(monkeypatch):
    status = _definition(
        name="pipeline",
        type="status",
        aggregation="sum",
        dimensions=["region"],
        direction="neutral",
        critical_threshold=None,
        values=[{"value": "ok", "color": "green"}, {"value": "down", "color": "red"}],
    )
    monkeypatch.setattr(
        database_mod,
        "db",
        _Db(
            [status],
            [
                _point("ok", 60, {"region": "eu"}, "pipeline"),
                _point("down", 120, {"region": "us"}, "pipeline"),
                _point("ok", 180, {"region": "ap"}, "pipeline"),
            ],
        ),
    )
    (widget,) = _bind({"type": "status", "label": "P", "metric": "pipeline"})
    assert widget["value"] == "ok"
    assert widget["bound_series"] == {
        "basis": "series",
        "aggregation": "sum",
        "series_count": 3,
        "dims": {"region": "eu"},
        "dimensions": ["region"],
    }


def test_T10_the_unselected_tile_still_equals_the_objective_join(monkeypatch):
    """Green on dev by design: pins that the fold path did not move."""
    monkeypatch.setattr(database_mod, "db", _Db([_definition(aggregation="sum")]))
    (widget,) = _bind(_w())
    tile = mrs.latest_by_metric(AGENT, now=NOW)["ad_spend"]
    assert widget["value"] == tile["latest"]["value"]


# ---------------------------------------------------------------------------
# AC6: an undeclared key refuses
# ---------------------------------------------------------------------------


def test_T11_an_undeclared_key_refuses_and_names_the_declared_ones(store):
    (widget,) = _bind(_w({"region": "eu"}))
    assert widget["binding_error_code"] == "metric_dimension_undeclared"
    assert widget["bound"] is False
    assert "channel" in widget["binding_error"]
    assert widget["binding_error"].startswith("metric 'ad_spend': ")
    assert "value" not in widget


def test_T11_on_a_metric_with_no_dimensions_the_sentence_says_so(monkeypatch):
    monkeypatch.setattr(
        database_mod, "db", _Db([_definition(dimensions=[])], [_point(5.0, 60)])
    )
    (widget,) = _bind(_w({"region": "eu"}))
    assert widget["binding_error_code"] == "metric_dimension_undeclared"
    assert "declares no dimensions" in widget["binding_error"]


def test_T11_an_undeclared_key_refuses_even_with_zero_points(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition()], []))
    (widget,) = _bind(_w({"region": "eu"}))
    assert widget["binding_error_code"] == "metric_dimension_undeclared"


# ---------------------------------------------------------------------------
# AC8: colour comes from the resolved value
# ---------------------------------------------------------------------------


def test_T12_only_the_breaching_channel_is_red(store):
    meta, google, linkedin = _bind(
        _w({"channel": "meta"}), _w({"channel": "google"}), _w({"channel": "linkedin"})
    )
    assert meta["color"] == "red"
    assert "color" not in google and "color" not in linkedin


def test_T12_a_status_metric_takes_each_series_own_colour(monkeypatch):
    status = _definition(
        name="pipeline",
        type="status",
        dimensions=["region"],
        direction="neutral",
        critical_threshold=None,
        values=[{"value": "ok", "color": "green"}, {"value": "down", "color": "red"}],
    )
    monkeypatch.setattr(
        database_mod,
        "db",
        _Db(
            [status],
            [
                _point("ok", 60, {"region": "eu"}, "pipeline"),
                _point("down", 120, {"region": "us"}, "pipeline"),
            ],
        ),
    )
    eu, us = _bind(
        {
            "type": "status",
            "label": "EU",
            "metric": "pipeline",
            "dims": {"region": "eu"},
        },
        {
            "type": "status",
            "label": "US",
            "metric": "pipeline",
            "dims": {"region": "us"},
        },
    )
    assert (eu["value"], eu["color"]) == ("ok", "green")
    assert (us["value"], us["color"]) == ("down", "red")


def test_T12_a_selected_tile_clears_the_authors_colour_and_trend(store):
    (widget,) = _bind(
        _w({"channel": "google"}, color="red", trend="up", trend_value="+12%")
    )
    assert "color" not in widget
    assert "trend" not in widget and "trend_value" not in widget


def test_T12_a_selected_zero_point_status_tile_drops_the_authors_colour(monkeypatch):
    status = _definition(
        name="pipeline",
        type="status",
        dimensions=["region"],
        direction="neutral",
        critical_threshold=None,
        values=[{"value": "ok", "color": "green"}],
    )
    monkeypatch.setattr(database_mod, "db", _Db([status], []))
    (widget,) = _bind(
        {
            "type": "status",
            "label": "EU",
            "metric": "pipeline",
            "dims": {"region": "eu"},
            "color": "green",
        }
    )
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert "color" not in widget


# ---------------------------------------------------------------------------
# T13: robustness — the per-widget loop runs OUTSIDE the store `try`, so a
# bad `dims` must never raise out of it and take the whole dashboard down.
# ---------------------------------------------------------------------------

_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=140)
    | st.sampled_from(["meta", "google", "linkedin", "tiktok", "Google", ""])
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)
_selectors = (
    _json_values
    | st.dictionaries(
        st.sampled_from(["channel", "region", "x"]), _json_values, max_size=3
    )
    | st.fixed_dictionaries(
        {"channel": st.sampled_from(["meta", "google", "linkedin", "tiktok"])}
    )
)


@settings(deadline=None, max_examples=200)
@given(dims=_selectors, zero_points=st.booleans())
def test_T13_any_dims_value_binds_or_refuses_by_name_never_raises(dims, zero_points):
    fake = _Db(points=[] if zero_points else None)
    stored = {p["value_numeric"] for p in fake.points}
    # Author-typed keys the registry owns: a refusal must not keep them.
    authored = {"threshold_verdict": {"level": "critical"}, "direction": "up_good"}
    with patch.object(database_mod, "db", fake):
        (widget,) = _bind(_w(dims, **authored))
    no_selector = dims is None or dims == {}
    if widget["bound"] is True:
        assert "binding_error_code" not in widget
        if "value" in widget:
            if no_selector:
                assert widget["value"] == 623.88  # the fold under `last`
            else:
                assert widget["value"] in stored
                assert widget["bound_series"]["basis"] == "selected"
        else:
            assert zero_points and no_selector
            assert "bound_series" not in widget
    else:
        assert not no_selector
        assert widget["binding_error_code"] in NEW_CODES
        for key in FILLED_KEYS:
            assert key not in widget, key
        # A refused tile carries no verdict and no direction either (D3).
        assert "threshold_verdict" not in widget
        assert "direction" not in widget


# ---------------------------------------------------------------------------
# T14: every invalid shape, its code, and its hint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dims,code,fragment,hint",
    [
        (
            {"channel": 2024},
            "metric_dimension_invalid",
            "dims value for 'channel' must be text",
            "quote it",
        ),
        (
            {"channel": True},
            "metric_dimension_invalid",
            "dims value for 'channel' must be text",
            "quote it",
        ),
        (
            {"channel": ["meta", "google"]},
            "metric_dimension_invalid",
            "dims value for 'channel' must be text",
            "one value per tile",
        ),
        (
            {"channel": None},
            "metric_dimension_invalid",
            "dims value for 'channel' must be text",
            "the value is missing",
        ),
        ({"channel": ""}, "metric_dimension_invalid", "must be 1..128", None),
        ({"channel": "x" * 129}, "metric_dimension_invalid", "must be 1..128", None),
        (
            {"channel": "a\x07b"},
            "metric_dimension_invalid",
            "contains control characters",
            None,
        ),
        (
            "meta",
            "metric_dimension_invalid",
            "dims must be a mapping of dimension: value",
            None,
        ),
        (
            ["channel"],
            "metric_dimension_invalid",
            "dims must be a mapping of dimension: value",
            None,
        ),
        (
            {"region": 2024},
            "metric_dimension_undeclared",
            "dimension 'region' is not declared",
            None,
        ),
    ],
)
def test_T14_invalid_selectors_refuse_with_a_code_and_the_right_hint(
    store, dims, code, fragment, hint
):
    (widget,) = _bind(_w(dims))
    assert widget["binding_error_code"] == code
    assert widget["binding_error"].startswith("metric 'ad_spend': ")
    assert fragment in widget["binding_error"]
    for any_hint in ("quote it", "one value per tile", "the value is missing"):
        if any_hint == hint:
            assert any_hint in widget["binding_error"]
        else:
            assert any_hint not in widget["binding_error"]
    assert "`" not in widget["binding_error"]
    assert "binding_detail" not in widget


def test_T14_the_first_failing_key_is_the_one_reported(monkeypatch):
    monkeypatch.setattr(
        database_mod, "db", _Db([_definition(dimensions=["a", "b"])], [])
    )
    (widget,) = _bind(_w({"a": "", "b": 2024}))
    assert widget["binding_error_code"] == "metric_dimension_invalid"
    assert "'a'" in widget["binding_error"]
    assert "quote it" not in widget["binding_error"]


def test_T14_more_than_ten_keys_is_invalid_with_no_hint(monkeypatch):
    keys = [f"k{i}" for i in range(11)]
    monkeypatch.setattr(database_mod, "db", _Db([_definition(dimensions=keys)], []))
    (widget,) = _bind(_w({k: "v" for k in keys}))
    assert widget["binding_error_code"] == "metric_dimension_invalid"
    assert widget["binding_error"] == (
        "metric 'ad_spend': dims names more than 10 dimensions"
    )


@pytest.mark.parametrize("dims", [{}, None])
def test_T14_an_empty_selector_is_no_selector(store, dims):
    (widget,) = _bind({**_w(), "dims": dims})
    assert widget["bound"] is True
    assert widget["value"] == 623.88
    assert widget["bound_series"]["basis"] == "series"


def test_parse_dims_selector_is_the_one_answer(store):
    """The public, pure parser X-009 shares: no `metric '<name>'` prefix."""
    assert mrs.parse_dims_selector(None, ["channel"]) == (None, None)
    assert mrs.parse_dims_selector({}, ["channel"]) == (None, None)
    assert mrs.parse_dims_selector({"channel": "meta"}, ["channel"]) == (
        {"channel": "meta"},
        None,
    )
    clean, err = mrs.parse_dims_selector({"channel": 2024}, ["channel"])
    assert clean is None
    assert err[0] == "metric_dimension_invalid"
    assert err[1].startswith("dims value for 'channel' must be text")


# ---------------------------------------------------------------------------
# T15 – T18: the bounds, stale state, and no points
# ---------------------------------------------------------------------------


def test_T15_a_series_outside_the_50_listed_is_refused_with_the_cap(monkeypatch):
    points = [
        _point(float(i), 60 + i * 10, {"channel": f"c{i:02d}"}) for i in range(55)
    ]
    monkeypatch.setattr(database_mod, "db", _Db([_definition()], points))
    (widget,) = _bind(_w({"channel": "c54"}))  # the oldest: 55th of 55
    assert widget["binding_error_code"] == "metric_series_not_found"
    detail = widget["binding_detail"]
    assert detail["series_cap"] == mrs.MAX_SERIES_PER_METRIC
    assert len(detail["recent_series"]) == 5
    assert detail["more"] == 50


def test_T16_a_refusal_clears_a_previous_success_and_a_success_clears_it(store):
    widget = _w(
        {"channel": "tiktok"},
        value=410.0,
        history={"values": [{"t": "x", "v": 1}]},
        color="red",
        bound_series={"basis": "selected"},
        last_point_at=_ts(60),
        stale=False,
        freshness="fresh",
        trend="up",
        trend_value="+12%",
    )
    config = {"sections": [{"widgets": [widget]}]}
    mrs.bind_dashboard_widgets(config, AGENT, now=NOW)
    for key in FILLED_KEYS:
        assert key not in widget, key
    assert widget["binding_error_code"] == "metric_series_not_found"

    widget["dims"] = {"channel": "google"}
    mrs.bind_dashboard_widgets(config, AGENT, now=NOW)
    assert widget["value"] == 410.0
    for key in ("binding_error", "binding_error_code", "binding_detail"):
        assert key not in widget, key


def test_T17_a_selector_on_a_metric_with_no_points_refuses(monkeypatch):
    fake = _Db([_definition()], [])
    monkeypatch.setattr(database_mod, "db", fake)
    widget = _w({"channel": "google"}, history={"values": [{"t": "x", "v": 1}]})
    config = {"sections": [{"widgets": [widget]}]}
    mrs.bind_dashboard_widgets(config, AGENT, now=NOW)
    assert widget["bound"] is False
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert widget["binding_detail"]["recent_series"] == []
    assert widget["binding_detail"]["more"] == 0
    for key in ("value", "history", "color", "trend"):
        assert key not in widget, key

    fake.points = [_point(410.0, 120, {"channel": "google"})]
    mrs.bind_dashboard_widgets(config, AGENT, now=NOW)
    assert widget["bound"] is True and widget["value"] == 410.0
    for key in ("binding_error", "binding_error_code", "binding_detail"):
        assert key not in widget, key


def test_T17_an_unselected_zero_point_widget_is_todays_path(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition()], []))
    (widget,) = _bind(_w())
    assert widget["bound"] is True
    assert "value" not in widget and "bound_series" not in widget
    assert widget["freshness"] == "no_points"


def _crowded_out():
    """200 meta points, every one newer than the only google point."""
    google = [_point(410.0, 20 * HOUR, {"channel": "google"})]
    meta = [
        _point(600.0, 60 + i * 60, {"channel": "meta"})
        for i in range(mrs.LATEST_POINTS_PER_METRIC)
    ]
    return meta + google


def test_T18_a_series_crowded_out_of_the_window_refuses_and_says_so(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition()], _crowded_out()))
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert widget["binding_detail"]["window_points"] == (mrs.LATEST_POINTS_PER_METRIC)
    assert "does not exist" not in widget["binding_error"]


class _RealPoints:
    """Definitions from the test, points through the REAL store's SQL."""

    def __init__(self, real, definitions):
        self._real = real
        self.definitions = definitions

    def list_metric_definitions(self, name, include_retired=False):
        return list(self.definitions)

    def latest_metric_points(self, *a, **k):
        return self._real.latest_metric_points(*a, **k)

    def metric_series_points(self, *a, **k):
        return []

    def calculate_widget_stats(self, values):
        return None


def _seed_real(real, points):
    real.insert_metric_points(
        AGENT, [{**p, "execution_id": None, "created_at": p["ts"]} for p in points]
    )


def test_T18_against_the_real_store(db_backend, monkeypatch):
    real = database_mod.db
    _seed_real(real, _crowded_out())
    monkeypatch.setattr(database_mod, "db", _RealPoints(real, [_definition()]))
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["binding_error_code"] == "metric_series_not_found"
    assert widget["binding_detail"]["window_points"] == (mrs.LATEST_POINTS_PER_METRIC)


def test_the_fake_store_matches_the_real_store(db_backend):
    """The fake's `latest_metric_points` is what T8's window variant and T18
    rest on, so pin it to the real SQL: same rows, same order, same slice,
    including a `ts` tie broken by `idempotency_key`."""
    real = database_mod.db
    tie = _ts(500)
    points = _crowded_out()[:20] + [
        _point(1.0, 500, {"channel": "a"}),
        _point(2.0, 500, {"channel": "b"}),
        _point(3.0, 500, {"channel": "c"}),
    ]
    assert sum(p["ts"] == tie for p in points) >= 3
    _seed_real(real, points)
    fake = _Db([_definition()], points)
    for limit in (5, 21, 200):
        got = real.latest_metric_points(AGENT, ["ad_spend"], limit)
        want = fake.latest_metric_points(AGENT, ["ad_spend"], limit)
        assert [r["idempotency_key"] for r in got] == [
            r["idempotency_key"] for r in want
        ]


# ---------------------------------------------------------------------------
# The user doc's recipe (docs/user-docs/advanced/dynamic-dashboards.md,
# "One series per tile: dims:") — the same YAML, so it cannot silently rot.
# ---------------------------------------------------------------------------

RECIPE_DASHBOARD_YAML = """\
title: "Spend"
sections:
  - title: "Spend by channel"
    widgets:
      - type: metric
        label: "Meta"
        metric: ad_spend
        dims: {channel: meta}
        value: 0             # placeholder, only for an older base image
      - type: metric
        label: "Google"
        metric: ad_spend
        dims: {channel: google}
        value: 0
      - type: metric
        label: "LinkedIn"
        metric: ad_spend
        dims: {channel: linkedin}
        value: 0
"""


def _validate_widget():
    """The REAL agent-server `validate_widget` (the ent#479 harness)."""
    import importlib

    base = str(_ROOT / "docker" / "base-image")
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        module = importlib.import_module("agent_server.routers.dashboard")
    except Exception as e:  # noqa: BLE001 — the agent image has its own deps
        pytest.skip(f"agent server module not importable here: {e}")
    return module.validate_widget


def test_the_user_doc_recipe_shows_three_different_numbers(monkeypatch):
    import yaml

    doc = (
        _ROOT / "docs" / "user-docs" / "advanced" / "dynamic-dashboards.md"
    ).read_text()
    for line in RECIPE_DASHBOARD_YAML.splitlines():
        assert line.strip() in doc, line
    monkeypatch.setattr(database_mod, "db", _Db([_definition(aggregation="sum")]))
    config = yaml.safe_load(RECIPE_DASHBOARD_YAML)
    validate = _validate_widget()
    for index, widget in enumerate(config["sections"][0]["widgets"]):
        assert validate(widget, index) is None
    bound = copy.deepcopy(config)
    mrs.bind_dashboard_widgets(bound, AGENT, now=NOW)
    values = [w["value"] for w in bound["sections"][0]["widgets"]]
    assert values == [623.88, 410.0, 95.5]
    assert [w["bound_series"]["dims"] for w in bound["sections"][0]["widgets"]] == [
        {"channel": "meta"},
        {"channel": "google"},
        {"channel": "linkedin"},
    ]


# ---------------------------------------------------------------------------
# D3: the typed threshold verdict and the declared direction (T20 – T22)
# ---------------------------------------------------------------------------

def _verdict_db(**overrides):
    definition = _definition(warning_threshold=400, critical_threshold=500)
    definition.update(overrides)
    return _Db([definition])


def test_T20_each_channel_is_judged_on_its_own_value(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _verdict_db())
    meta, google, linkedin = _bind(_w({"channel": "meta"}),
                                   _w({"channel": "google"}),
                                   _w({"channel": "linkedin"}))
    assert meta["threshold_verdict"] == {"level": "critical", "threshold": 500}
    assert google["threshold_verdict"] == {"level": "warning", "threshold": 400}
    assert linkedin["threshold_verdict"] == {"level": "ok", "threshold": None}
    assert (meta["color"], google["color"]) == ("red", "yellow")


def test_T20_an_unselected_fold_tile_is_judged_on_the_fold(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _verdict_db(aggregation="sum"))
    (widget,) = _bind(_w())
    assert widget["value"] == pytest.approx(1129.38)
    assert widget["threshold_verdict"] == {"level": "critical",
                                           "threshold": 500}


def test_T20_up_good_breaches_at_exactly_the_threshold(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db(
        [_definition(direction="up_good", critical_threshold=410.0)]))
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["threshold_verdict"] == {"level": "critical",
                                           "threshold": 410.0}


@pytest.mark.parametrize("overrides,points", [
    ({"direction": "neutral"}, None),
    ({"warning_threshold": None, "critical_threshold": None}, None),
    ({"type": "status", "direction": "down_good",
      "values": [{"value": "ok", "color": "green"}]},
     [_point("ok", 60, {"channel": "google"})]),
    ({}, [_point("lots", 60, {"channel": "google"})]),
    ({}, [_point(True, 60, {"channel": "google"})]),
])
def test_T20_no_verdict_where_the_metric_cannot_be_judged(
        monkeypatch, overrides, points):
    definition = _definition(warning_threshold=400, critical_threshold=500)
    definition.update(overrides)
    monkeypatch.setattr(database_mod, "db", _Db([definition], points))
    (widget,) = _bind(_w({"channel": "google"},
                         threshold_verdict={"level": "critical"}))
    assert widget["bound"] is True
    assert "threshold_verdict" not in widget


def test_T20_an_unselected_zero_point_widget_has_no_verdict(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition()], []))
    (widget,) = _bind(_w(threshold_verdict={"level": "critical"}))
    assert widget["bound"] is True
    assert "threshold_verdict" not in widget


def test_T20_an_author_typed_verdict_never_survives(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _verdict_db())
    below, refused = _bind(
        _w({"channel": "linkedin"}, threshold_verdict={"level": "critical"}),
        _w({"channel": "tiktok"}, threshold_verdict={"level": "critical"}))
    assert below["threshold_verdict"] == {"level": "ok", "threshold": None}
    assert "threshold_verdict" not in refused


@pytest.mark.parametrize("direction,warn,crit,value,verdict,color", [
    ("down_good", 400, 500, 500, ("critical", 500), "red"),
    ("down_good", 400, 500, 499.99, ("warning", 400), "yellow"),
    ("down_good", 400, 500, 400, ("warning", 400), "yellow"),
    ("down_good", 400, 500, 399.99, None, None),
    ("down_good", None, 500, 450, None, None),
    ("up_good", 20, 10, 10, ("critical", 10), "red"),
    ("up_good", 20, 10, 10.01, ("warning", 20), "yellow"),
    ("up_good", 20, 10, 20, ("warning", 20), "yellow"),
    ("up_good", 20, 10, 20.01, None, None),
    ("up_good", None, 10, True, None, None),
    ("down_good", 400, 500, False, None, None),
    ("down_good", 400, 500, "600", None, None),
    ("down_good", 400, 500, None, None, None),
    ("neutral", 400, 500, 900, None, None),
    (None, 400, 500, 900, None, None),
])
def test_T21_one_rule_behind_the_verdict_and_the_colour(
        direction, warn, crit, value, verdict, color):
    """Each row states its verdict AND its colour independently: comparing
    one function with a mapping of the other would pass a bug they share."""
    definition = {"direction": direction, "warning_threshold": warn,
                  "critical_threshold": crit}
    expected = (None if verdict is None
                else {"level": verdict[0], "threshold": verdict[1]})
    assert mrs._threshold_verdict(definition, value) == expected
    assert mrs._threshold_color(definition, value) == color


def test_T22_a_bind_carries_the_registrys_direction(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db())
    selected, folded = _bind(_w({"channel": "google"}, direction="up_good"),
                             _w(direction="up_good"))
    assert selected["direction"] == "down_good"
    assert folded["direction"] == "down_good"


def test_T22_no_declared_direction_is_neutral(monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition(direction=None)]))
    (widget,) = _bind(_w({"channel": "google"}))
    assert widget["direction"] == "neutral"


def test_T22_a_zero_point_bind_still_carries_it_and_a_refusal_drops_it(
        monkeypatch):
    monkeypatch.setattr(database_mod, "db", _Db([_definition()], []))
    unselected, refused = _bind(_w(), _w({"channel": "google"},
                                         direction="down_good"))
    assert unselected["direction"] == "down_good"
    assert "direction" not in refused

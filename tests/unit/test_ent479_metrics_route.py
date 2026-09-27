"""`GET /api/agents/{name}/metrics`, re-backed (trinity-enterprise#479, C3).

The route owns transport (gate order, status codes, the rate limit, the D-010
echo); `services/metric_read_service.py` owns the composition. Both are
exercised here through a real `TestClient` over the real router with only the
STORE stubbed, because the thing worth proving is that the two halves agree on
the shape a consumer binds to.

`read_agent_metrics` resolves `database.db` at CALL time (so `freshness` stays
importable without the store), which is what lets these tests hand it a fake
store without a database file.
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

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import database as database_mod  # noqa: E402
import routers.agent_files as route_mod  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402

AGENT = "read-agent"
HOUR = 3600


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds: float) -> str:
    """A stamp relative to the WALL clock.

    The route takes its `now` from the clock (a request has no injected one —
    that parameter exists for `freshness`'s own table test), so a fixture
    pinned to a literal date would drift into and out of every window as the
    calendar moves. `freshness` is proved against a frozen clock in
    `test_ent479_freshness_rule.py`; here the stamps move with it.
    """
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _definition(**overrides):
    d = {
        "name": "revenue", "type": "gauge", "label": "Revenue",
        "description": None, "unit": "USD", "direction": "up",
        "aggregation": "last", "cadence": "1h", "cadence_seconds": HOUR,
        "warning_threshold": None, "critical_threshold": None,
        "values": None, "dimensions": [], "status": "active",
        "retired_at": None, "type_conflict": None,
    }
    d.update(overrides)
    return d


def _point(metric="revenue", ts=None, value=10.0, dims=None, text=None):
    return {
        "metric": metric,
        "ts": ts or _ago(60),
        "value_numeric": value,
        "value_text": text,
        "dims": dims,
        "idempotency_key": f"{metric}{ts}{dims}",
    }


class _Db:
    """A store that answers from lists, and can be told to fail."""

    def __init__(self):
        self.definitions = [_definition()]
        self.points = [_point()]
        self.series = None
        self.compat = None
        self.raise_on_read = None

    def list_metric_definitions(self, name, include_retired=False):
        if self.raise_on_read:
            raise self.raise_on_read
        if include_retired:
            return list(self.definitions)
        return [d for d in self.definitions if d.get("status") == "active"]

    def latest_metric_points(self, name, metric_names, per_metric_limit=200):
        wanted = set(metric_names)
        rows = [p for p in self.points if p["metric"] in wanted]
        return sorted(rows, key=lambda r: r["ts"], reverse=True)

    def metric_series_points(self, name, metric, since, until=None, limit=2000):
        rows = self.series if self.series is not None else [
            p for p in self.points if p["metric"] == metric]
        return sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit + 1]

    def calculate_widget_stats(self, values):
        numbers = [v["v"] for v in values if isinstance(v["v"], (int, float))]
        if not numbers:
            return {"min": None, "max": None, "avg": None, "trend": "stable"}
        return {"min": min(numbers), "max": max(numbers),
                "avg": sum(numbers) / len(numbers), "trend": "stable"}

    def get_compatibility_result(self, name):
        return self.compat


@pytest.fixture
def ctx(monkeypatch):
    fake_db = _Db()
    monkeypatch.setattr(database_mod, "db", fake_db)
    monkeypatch.setattr(route_mod, "db", fake_db)
    monkeypatch.setattr(route_mod.rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(
        route_mod, "_metric_policy",
        lambda: {"retention_days": 365, "daily_point_cap": 100000,
                 "enforced": True})

    app = FastAPI()
    principal = User(id=1, username="operator", email="op@example.com",
                     role="user")

    from dependencies import get_authorized_agent_by_name

    app.include_router(route_mod.router)
    holder = {"user": principal}

    # The access dependency is overridden to "every agent in the path is
    # accessible", so these tests exercise the handler's OWN gates. The uniform
    # 404 it produces for an unknown or inaccessible agent is proven by its own
    # suite (#186) and is not re-proved here. Keyed off the callable the ROUTE
    # resolved, not a re-import: the unit island restores backend modules
    # between tests, so a second import is not guaranteed to be the same
    # object FastAPI will look up.
    def _override(dependant):
        for dep in getattr(dependant, "dependencies", []):
            name = getattr(dep.call, "__name__", "")
            if name == "get_authorized_agent_by_name":
                app.dependency_overrides[dep.call] = lambda agent_name: agent_name
            if name == "get_current_user":
                app.dependency_overrides[dep.call] = lambda: holder["user"]
            _override(dep)

    for _route in app.routes:
        _override(getattr(_route, "dependant", None))
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    app.dependency_overrides[get_authorized_agent_by_name] = (
        lambda agent_name: agent_name)

    class Ctx:
        client = TestClient(app, raise_server_exceptions=False)
        db = fake_db
        users = holder
    return Ctx()


def _get(ctx, **params):
    return ctx.client.get(f"/api/agents/{AGENT}/metrics", params=params)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_an_agent_key_reads_its_own_metrics(ctx):
    ctx.users["user"] = User(id=2, username=AGENT, email=f"{AGENT}@agents.local",
                             role="user", agent_name=AGENT)
    assert _get(ctx).status_code == 200


def test_an_agent_key_may_not_read_another_agents_numbers(ctx):
    """Cross-agent reads are ent#80's grant, not an oversight here. 403 and
    not 404: the caller already knows this agent exists (the dependency let it
    through), so the uniform-404 reasoning does not apply to the self-gate."""
    ctx.users["user"] = User(id=3, username="other", email="o@agents.local",
                             role="user", agent_name="other-agent")
    response = _get(ctx)
    assert response.status_code == 403
    assert "own metrics" in response.json()["detail"]


def test_the_rate_limit_is_enforced_per_agent(monkeypatch, ctx):
    """TD-6: the read is polled by every consumer, so the ceiling clears
    normal traffic and stops a loop — but it is enforced, not decorative."""
    seen = []
    monkeypatch.setattr(route_mod.rate_limiter, "enforce",
                        lambda key, *a, **k: seen.append((key, a)))
    _get(ctx)
    assert seen[0][0] == f"agent_metrics_read:{AGENT}"
    assert seen[0][1][0] == route_mod.METRICS_READ_RATE_LIMIT == 240


# ---------------------------------------------------------------------------
# Named refusals
# ---------------------------------------------------------------------------

def test_an_unknown_window_is_a_named_422(ctx):
    response = _get(ctx, window="last-tuesday")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "window_invalid"


def test_an_undeclared_metric_is_422_not_404(ctx):
    """404 would be read by the MCP classifier as `not_authorized` (the #186
    uniform-404 convention), telling an agent it lacks access to its own
    agent."""
    response = _get(ctx, metric="nope")
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["reason"] == "metric_undeclared"
    assert "template.yaml" in body["message"]


def test_a_retired_metric_names_the_include_retired_hint(ctx):
    """TD-10: never "never existed" for a name that has history."""
    ctx.db.definitions = [_definition(status="retired",
                                      retired_at="2026-09-01T00:00:00Z")]
    response = _get(ctx, metric="revenue")
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["reason"] == "metric_undeclared"
    assert "include_retired" in body["message"]
    assert body["retired_at"] == "2026-09-01T00:00:00Z"

    assert _get(ctx, metric="revenue", include_retired="true").status_code == 200


def test_a_store_outage_is_a_retryable_503(ctx):
    ctx.db.raise_on_read = OperationalError("SELECT 1", {}, Exception("down"))
    response = _get(ctx)
    assert response.status_code == 503
    assert response.json()["detail"] == "metric_store_unavailable"
    assert response.headers["Retry-After"] == "30"


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_the_response_carries_one_object_per_metric_with_its_freshness(ctx):
    body = _get(ctx).json()
    assert body["agent_name"] == AGENT
    assert body["declared"] is True
    assert body["stale_rule"] == "2x cadence"
    assert body["window"]["kind"] == "auto"

    metric = body["metrics"][0]
    assert metric["name"] == "revenue"
    assert metric["label"] == "Revenue"
    assert metric["unit"] == "USD"
    assert metric["latest"]["value"] == 10.0
    assert metric["freshness"] == "fresh"
    assert metric["stale"] is False
    assert metric["series_count"] == 1


def test_a_stopped_agent_answers_exactly_like_a_running_one(ctx):
    """The whole reason the URL was re-backed: the proxy this replaces said
    "Agent must be running to read metrics", so every number vanished at the
    moment an operator most wanted to see what it had been. Nothing in this
    path can contact a container — there is no docker stub in this suite and
    the read still answers."""
    assert _get(ctx).json()["metrics"][0]["latest"]["value"] == 10.0


def test_a_metric_past_two_cadences_is_marked_stale(ctx):
    ctx.db.points = [_point(ts=_ago(3 * HOUR))]
    metric = _get(ctx).json()["metrics"][0]
    assert metric["stale"] is True
    assert metric["freshness"] == "stale"
    assert metric["stale_after"] is not None


def test_a_declared_metric_with_no_points_teaches_the_next_action(ctx):
    """Both halves of the ruled copy, on one sentence.

    The first version branched on a `has_playbook` flag NO CALLER PASSED, so
    the `/update-dashboard` half was dead copy — a route test that only read
    the default branch could never have noticed. This read is store-only and
    the playbook catalog is a container probe, so the copy names both actions
    instead of branching on a fact it cannot learn. The two assertions are the
    two branches that used to exist; a re-introduced branch that drops either
    action from the default answer fails here.
    """
    ctx.db.points = []
    metric = _get(ctx).json()["metrics"][0]
    assert metric["latest"] is None
    assert metric["freshness"] == "no_points"
    assert metric["stale"] is False
    assert "record_metrics" in metric["message"]
    assert "/update-dashboard" in metric["message"]


def test_a_metric_WITH_points_carries_no_empty_copy(ctx):
    """The other arm of the same branch: the empty-state sentence must not
    ride along under a number that exists."""
    metric = _get(ctx).json()["metrics"][0]
    assert metric["latest"] is not None
    assert metric["message"] is None


def test_the_empty_copy_takes_no_flag_the_route_cannot_compute(ctx):
    """@signature-pin. `has_playbook` was reachable only by its default, which
    is how half a documented sentence shipped unrenderable. If a future change
    wants the branch back, it has to give `read_agent_metrics` a value the
    ROUTE can produce store-only — and updating this pin is the moment to
    prove it, rather than adding a parameter nobody passes again.
    """
    import inspect

    from services import metric_read_service

    params = inspect.signature(metric_read_service.read_agent_metrics).parameters
    assert "has_playbook" not in params


def test_an_agent_with_no_metrics_block_names_the_next_action(ctx):
    ctx.db.definitions = []
    body = _get(ctx).json()
    assert body["declared"] is False
    assert body["metrics"] == []
    assert "template.yaml" in body["message"]


def test_dimension_series_are_grouped_and_folded_by_the_declared_aggregation(ctx):
    """TD-2 / S-F3: one series per dimension tuple, and the tile value is the
    declared fold across them — an interleaved series is noise for a chart and
    a single "latest" is ill-defined across regions."""
    ctx.db.definitions = [_definition(aggregation="sum", dimensions=["region"])]
    ctx.db.points = [
        _point(ts=_ago(60), value=10.0, dims={"region": "eu"}),
        _point(ts=_ago(90), value=5.0, dims={"region": "us"}),
        _point(ts=_ago(3600), value=9.0, dims={"region": "eu"}),
    ]
    metric = _get(ctx).json()["metrics"][0]
    assert metric["series_count"] == 2
    assert metric["latest"]["value"] == 15.0
    assert metric["latest"]["dims"] is None  # an aggregate belongs to no tuple
    assert {s["dims"]["region"] for s in metric["latest_by_series"]} == {"eu", "us"}
    assert len(metric["series"]) == 2


def test_freshness_is_the_newest_point_across_every_dimension_series(ctx):
    """One region still reporting keeps the metric fresh; the per-series flags
    are what say which stopped."""
    ctx.db.definitions = [_definition(aggregation="sum", dimensions=["region"])]
    ctx.db.points = [
        _point(ts=_ago(60), value=10.0, dims={"region": "eu"}),
        _point(ts=_ago(10 * HOUR), value=5.0, dims={"region": "us"}),
    ]
    metric = _get(ctx).json()["metrics"][0]
    assert metric["stale"] is False
    stale_series = [s for s in metric["latest_by_series"] if s["stale"]]
    assert [s["dims"]["region"] for s in stale_series] == ["us"]


def test_a_point_OLDER_than_the_window_is_dropped_not_folded_into_bucket_0(ctx):
    """I1 — the defect this whole assertion set exists for.

    `latest_metric_points` returns the newest N points for the metric, which
    is a "newest N" slice and NOT the window. The bucket index of a point
    older than `since` is negative, and the first version clamped it with
    `max(index, 0)` instead of dropping it: the read opened with a fabricated
    spike — the fold of every excluded point — stamped BEFORE `since`, and
    `stats` was computed from it.
    """
    ctx.db.definitions = [_definition(aggregation="sum")]
    ctx.db.points = [
        _point(ts=_ago(10 * 86400), value=500.0),   # ten days old
        _point(ts=_ago(9 * 86400), value=500.0),
        _point(ts=_ago(60), value=7.0),             # the only in-window point
    ]
    body = _get(ctx, window="24h").json()
    metric = body["metrics"][0]
    since = body["window"]["since"]

    buckets = metric["series"][0]["buckets"]
    assert buckets, "the in-window point should still be charted"
    assert all(b["ts"] >= since for b in buckets), (
        f"a bucket is stamped before the window opened: {buckets}")
    assert [b["value"] for b in buckets] == [7.0], (
        "an excluded point contributed to a fold")

    # The same points must not reach `stats` by the back door — min/max/trend
    # and the tile's trend arrow are computed from these buckets.
    assert metric["stats"]["max"] == 7.0
    assert metric["stats"]["min"] == 7.0
    assert metric["chart"]["buckets"] == buckets


def test_a_point_in_the_window_at_its_very_END_still_lands_in_a_bucket(ctx):
    """The other side of the filter: the `SERIES_BUCKETS - 1` clamp survives
    so `ts == until` is the last bucket rather than one past the end. A filter
    written as `ts >= end: continue` would silently drop the newest point,
    which is the one an operator is actually looking at."""
    metric = _get(ctx, window="24h").json()["metrics"][0]
    assert [b["value"] for b in metric["series"][0]["buckets"]] == [10.0]


def test_the_chart_and_the_trend_describe_the_SAME_thing_as_the_value(ctx):
    """I6: `latest.value` is the fold across every dimension series, so for a
    foldable aggregation the chart must be that same fold. Charting
    `series[0]` drew one region's history under a total."""
    ctx.db.definitions = [_definition(aggregation="sum", dimensions=["region"])]
    ctx.db.points = [
        _point(ts=_ago(60), value=10.0, dims={"region": "eu"}),
        _point(ts=_ago(90), value=5.0, dims={"region": "us"}),
    ]
    metric = _get(ctx, window="24h").json()["metrics"][0]

    assert metric["latest"]["value"] == 15.0
    assert metric["chart"]["basis"] == "folded"
    assert [b["value"] for b in metric["chart"]["buckets"]] == [15.0]
    assert metric["stats"]["max"] == 15.0
    # Per-series buckets are still there for anyone who wants the breakdown.
    assert sorted(s["buckets"][0]["value"] for s in metric["series"]) == [5.0, 10.0]


def test_a_last_metric_over_several_series_SAYS_which_series_it_charts(ctx):
    """A cross-series fold is undefined for `last` — the last value of two
    regions is not one number. Rather than invent one, the payload names the
    series it drew so the tile can label the chart; silently charting
    `series[0]` under a multi-series metric was the ambiguity."""
    ctx.db.definitions = [_definition(aggregation="last", dimensions=["region"])]
    ctx.db.points = [
        _point(ts=_ago(60), value=10.0, dims={"region": "eu"}),
        _point(ts=_ago(90), value=5.0, dims={"region": "us"}),
    ]
    metric = _get(ctx, window="24h").json()["metrics"][0]

    assert metric["chart"]["basis"] == "series"
    assert metric["chart"]["series_count"] == 2
    assert metric["chart"]["dims"] == {"region": "eu"}  # the newest series
    # …and it is the same series the `last` fold took its value from.
    assert metric["latest"]["value"] == 10.0
    assert [b["value"] for b in metric["chart"]["buckets"]] == [10.0]


def test_a_single_series_metric_needs_no_chart_caveat(ctx):
    """`basis: "series"` with one series is the whole metric — the tile's
    label is gated on `series_count > 1`, so this is what keeps the ordinary
    tile free of a caveat that would mean nothing."""
    metric = _get(ctx).json()["metrics"][0]
    assert metric["chart"]["series_count"] == 1


def test_the_default_read_ships_buckets_and_no_raw_points(ctx):
    """E-E1: 50 metrics x 2000 points is a ten-megabyte "read"."""
    series = _get(ctx).json()["metrics"][0]["series"][0]
    assert "buckets" in series
    assert "points" not in series


def test_naming_one_metric_switches_the_series_to_raw_points(ctx):
    body = _get(ctx, metric="revenue").json()
    assert len(body["metrics"]) == 1
    series = body["metrics"][0]["series"][0]
    assert series["points"] and series["truncated"] is False


def test_a_truncated_raw_series_keeps_the_NEWEST_points_and_says_so(ctx):
    """S-F2: `ASC + LIMIT` would keep the OLDEST points of a busy window —
    a sparkline of last month while today is missing."""
    ctx.db.series = [_point(ts=_ago(i * 60), value=float(i)) for i in range(20)]
    series = _get(ctx, metric="revenue", series_limit=5).json()[
        "metrics"][0]["series"][0]
    assert series["truncated"] is True
    assert len(series["points"]) == 5
    # Oldest-first for charting, and the newest five are what survived.
    assert [p["value"] for p in series["points"]] == [4.0, 3.0, 2.0, 1.0, 0.0]


def test_a_status_metric_has_no_stats_and_keeps_its_label(ctx):
    ctx.db.definitions = [_definition(name="pipeline", type="status",
                                      values=[{"value": "healthy",
                                               "color": "green"}])]
    ctx.db.points = [_point(metric="pipeline", value=None, text="healthy")]
    metric = _get(ctx).json()["metrics"][0]
    assert metric["latest"]["value"] == "healthy"
    assert metric["stats"] is None


def test_series_limit_is_bounded_by_the_route_not_by_the_caller(ctx):
    assert _get(ctx, metric="revenue", series_limit=99999).status_code == 422
    assert _get(ctx, metric="revenue", series_limit=0).status_code == 422


# ---------------------------------------------------------------------------
# D-010 echo (TD-1)
# ---------------------------------------------------------------------------

def test_a_persisted_d010_finding_is_echoed_onto_the_read(ctx):
    ctx.db.compat = {
        "checked_at": "2026-09-22T10:00:00Z",
        "checks": [{"id": "D-010", "status": "fail",
                    "message": "metrics.json is superseded",
                    "detail": {"keys": ["revenue"]}}],
    }
    body = _get(ctx).json()
    assert body["findings"][0]["code"] == "metrics_json_superseded"
    assert body["findings"][0]["detail"]["keys"] == ["revenue"]
    assert body["findings_evaluated_at"] == "2026-09-22T10:00:00Z"


def test_no_compat_run_yet_is_told_apart_from_no_finding(ctx):
    """S-F8: an empty `findings` with a null `findings_evaluated_at` means the
    collector has not run since the upgrade, not that the agent is clean."""
    body = _get(ctx).json()
    assert body["findings"] == []
    assert body["findings_evaluated_at"] is None


def test_a_passing_d010_is_not_echoed_as_a_finding(ctx):
    ctx.db.compat = {"checked_at": "2026-09-22T10:00:00Z",
                     "checks": [{"id": "D-010", "status": "pass",
                                 "message": "no metrics.json"}]}
    body = _get(ctx).json()
    assert body["findings"] == []
    assert body["findings_evaluated_at"] == "2026-09-22T10:00:00Z"


def test_the_live_policy_travels_with_the_read(ctx):
    assert _get(ctx).json()["policy"]["retention_days"] == 365


# ---------------------------------------------------------------------------
# `latest_by_metric` parity (ent#666 C1)
# ---------------------------------------------------------------------------
# The objective join reads `actual` through `latest_by_metric` rather than
# through this route, so the two must be the SAME computation and not two that
# happen to agree. Both go through `_latest_entry`; these cases are what says
# so out loud, including the case the extraction could plausibly break — a
# dimensioned metric, where the tile value is a cross-series fold.


def _latest_map(ctx, **kwargs):
    from services import metric_read_service
    return metric_read_service.latest_by_metric(AGENT, **kwargs)


def test_the_tile_and_the_join_read_the_same_number(ctx):
    body = _get(ctx).json()
    folded = _latest_map(ctx)

    for entry in body["metrics"]:
        mirror = folded[entry["name"]]
        assert mirror["latest"] == entry["latest"]
        assert mirror["last_point_at"] == entry["last_point_at"]
        assert mirror["stale"] == entry["stale"]
        assert mirror["freshness"] == entry["freshness"]
        assert mirror["stale_after"] == entry["stale_after"]
        assert mirror["series_count"] == entry["series_count"]


def test_parity_holds_for_a_dimensioned_sum_metric(ctx):
    """Sum of per-region latest IS the total — the number on the tile. A join
    that re-implemented the fold would be a second answer to one question."""
    ctx.db.definitions = [_definition(name="signups", type="counter",
                                      aggregation="sum",
                                      dimensions=["region"])]
    ctx.db.points = [
        _point("signups", _ago(30), 7.0, {"region": "emea"}),
        _point("signups", _ago(90), 3.0, {"region": "emea"}),
        _point("signups", _ago(45), 5.0, {"region": "us"}),
    ]
    entry = _get(ctx).json()["metrics"][0]
    mirror = _latest_map(ctx)["signups"]

    assert entry["latest"]["value"] == 12.0        # 7 + 5, not 7 and not 15
    assert mirror["latest"] == entry["latest"]
    assert mirror["series_count"] == entry["series_count"] == 2


def test_parity_holds_when_a_metric_has_no_points(ctx):
    ctx.db.points = []
    entry = _get(ctx).json()["metrics"][0]
    mirror = _latest_map(ctx)["revenue"]

    assert entry["latest"] is None and mirror["latest"] is None
    assert mirror["freshness"] == entry["freshness"] == "no_points"


def test_the_join_can_narrow_to_the_names_it_needs(ctx):
    ctx.db.definitions = [_definition(), _definition(name="unrelated")]
    asked = []
    original = ctx.db.latest_metric_points

    def _spy(name, metric_names, per_metric_limit=200):
        asked.append(tuple(metric_names))
        return original(name, metric_names, per_metric_limit)

    ctx.db.latest_metric_points = _spy
    folded = _latest_map(ctx, names=["revenue"])

    assert set(folded) == {"revenue"}
    assert asked == [("revenue",)]


def test_an_empty_name_list_performs_no_store_read_at_all(ctx):
    """The zero-config path: an agent with nothing to join costs no query."""
    def _boom(*a, **k):
        raise AssertionError("the store must not be touched")

    ctx.db.list_metric_definitions = _boom
    ctx.db.latest_metric_points = _boom
    assert _latest_map(ctx, names=[]) == {}


def test_the_join_may_hand_in_registry_rows_it_already_holds(ctx):
    """One registry read per join, not one per consumer of it."""
    def _boom(*a, **k):
        raise AssertionError("definitions were handed in")

    ctx.db.list_metric_definitions = _boom
    folded = _latest_map(ctx, definitions=[_definition()])
    assert folded["revenue"]["latest"]["value"] == 10.0

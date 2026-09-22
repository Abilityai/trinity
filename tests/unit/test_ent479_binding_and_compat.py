"""D-010, the `metric:` widget binding, and the relaxed validator (ent#479).

Three small surfaces that together retire `metrics.json`:

* **D-010** names the superseded file instead of the read silently omitting
  the numbers it holds.
* **The binding** lets a `dashboard.yaml` widget carry `metric: <name>` and be
  filled from the registry — and, critically, be SKIPPED by the snapshot
  writer, so the one number does not acquire a second source.
* **The agent-server validator** stops demanding a `value` an author would
  have to invent (Invariant #5 — the mirror is the agent's own module, which
  this suite imports directly).
"""

from __future__ import annotations

import importlib.util
import json
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

import database as database_mod  # noqa: E402
from services import metric_read_service  # noqa: E402
from services.compatibility import spec, static_checks  # noqa: E402
from services.compatibility.collector import _FIXED_FILES  # noqa: E402

AGENT = "bind-agent"
HOUR = 3600


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# D-010
# ---------------------------------------------------------------------------

def _snap(metrics_json=None, template=None):
    files = {}
    if metrics_json is not None:
        files["metrics.json"] = {"exists": True,
                                 "content": json.dumps(metrics_json)}
    else:
        files["metrics.json"] = {"exists": False}
    if template is not None:
        files["template.yaml"] = {"exists": True, "content": template}
    return {"files": files}


def test_d010_is_registered_as_a_soft_static_check():
    entry = next(c for c in spec.CHECKS if c.id == "D-010")
    assert entry.severity == "soft"
    assert entry.type == "static"
    assert static_checks.STATIC_CHECKS["D-010"] is static_checks.c_d010


def test_the_collector_reads_metrics_json_with_its_content():
    """A check that only knew the file EXISTS could not name which of its keys
    have no registry entry — which is the actionable half of the finding."""
    assert ("metrics.json", True) in _FIXED_FILES


def test_no_metrics_json_passes():
    status, message, _detail = static_checks.c_d010(_snap())
    assert status == "pass"
    assert "no superseded" in message


def test_a_present_metrics_json_fails_and_names_the_remedy():
    status, message, detail = static_checks.c_d010(
        _snap({"revenue": 100, "leads": 3}))
    assert status == "fail"
    assert "record_metrics" in message
    assert sorted(detail["keys"]) == ["leads", "revenue"]


def test_keys_with_no_registry_entry_are_named_separately():
    """"You still write this file" is advice; "these numbers are declared
    nowhere" is a fix."""
    template = "name: x\nmetrics:\n  revenue:\n    type: gauge\n"
    _status, _message, detail = static_checks.c_d010(
        _snap({"revenue": 100, "orphan": 3}, template=template))
    assert detail["undeclared"] == ["orphan"]


def test_the_nested_legacy_spelling_is_read_too():
    """Both shapes shipped — `{metrics: {...}, last_updated: ...}` and a flat
    map — so both are read rather than one being guessed at."""
    _status, _message, detail = static_checks.c_d010(
        _snap({"metrics": {"revenue": 1}, "last_updated": "2026-01-01"}))
    assert detail["keys"] == ["revenue"]


def test_unparseable_content_still_reports_the_file():
    snap = {"files": {"metrics.json": {"exists": True, "content": "{not json"}}}
    status, _message, detail = static_checks.c_d010(snap)
    assert status == "fail"
    assert detail["keys"] == []


def test_key_names_are_charset_bounded_before_they_are_persisted():
    """`checks_json` is persisted AND rendered in the UI, and these keys are
    author-controlled text."""
    _status, _message, detail = static_checks.c_d010(
        _snap({"<script>alert(1)</script>": 1}))
    assert "<" not in detail["keys"][0] and ">" not in detail["keys"][0]


# ---------------------------------------------------------------------------
# The binding
# ---------------------------------------------------------------------------

def _definition(name="revenue", **overrides):
    d = {"name": name, "type": "gauge", "label": "Revenue", "unit": "USD",
         "status": "active", "cadence_seconds": HOUR, "aggregation": "last",
         "dimensions": [], "values": None, "retired_at": None,
         "type_conflict": None, "direction": "up",
         "warning_threshold": None, "critical_threshold": None,
         "description": None, "cadence": "1h"}
    d.update(overrides)
    return d


class _Db:
    def __init__(self):
        self.definitions = [_definition()]
        self.points = [{"metric": "revenue", "ts": _ago(60),
                        "value_numeric": 42.0, "value_text": None,
                        "dims": None, "idempotency_key": "k"}]
        self.raise_on_read = None

    def list_metric_definitions(self, name, include_retired=False):
        if self.raise_on_read:
            raise self.raise_on_read
        return list(self.definitions)

    def latest_metric_points(self, name, metric_names, per_metric_limit=200):
        wanted = set(metric_names)
        return [p for p in self.points if p["metric"] in wanted]

    def metric_series_points(self, *a, **k):
        return []

    def calculate_widget_stats(self, values):
        return {"min": None, "max": None, "avg": None, "trend": "stable"}


@pytest.fixture
def store(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(database_mod, "db", fake)
    return fake


def _config(*widgets):
    return {"sections": [{"widgets": list(widgets)}]}


def test_a_bound_widget_gets_its_value_from_the_registry(store):
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    widget = config["sections"][0]["widgets"][0]
    assert widget["value"] == 42.0
    assert widget["bound"] is True
    assert widget["stale"] is False
    assert widget["last_point_at"]


def test_a_placeholder_value_is_OVERWRITTEN_not_preferred(store):
    """E-H3: an author on an older base image keeps `value:` so the agent-side
    validator accepts the file. That placeholder must never be what an
    operator sees once the binding resolves."""
    config = _config({"type": "metric", "label": "Rev", "value": 0,
                      "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)
    assert config["sections"][0]["widgets"][0]["value"] == 42.0


def test_an_undeclared_binding_shows_the_error_and_NO_number(store):
    config = _config({"type": "metric", "label": "X", "value": 7,
                      "metric": "nope"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    widget = config["sections"][0]["widgets"][0]
    assert "not declared in template.yaml" in widget["binding_error"]
    assert "value" not in widget  # a wrong number is worse than no number


def test_an_unbound_widget_is_left_completely_alone(store):
    config = _config({"type": "metric", "label": "Manual", "value": 5})
    before = dict(config["sections"][0]["widgets"][0])
    metric_read_service.bind_dashboard_widgets(config, AGENT)
    assert config["sections"][0]["widgets"][0] == before


def test_a_store_outage_degrades_per_widget_and_never_raises(store):
    store.raise_on_read = RuntimeError("store down")
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"},
                     {"type": "metric", "label": "Manual", "value": 5})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    bound, unbound = config["sections"][0]["widgets"]
    assert bound["binding_error"] == "metric store unavailable"
    assert unbound["value"] == 5


def test_a_bound_status_widget_takes_its_colour_from_the_declaration(store):
    """S-F9 / E-E5: the author already chose colours per status value in
    `template.yaml`; a bound widget must not make them repeat it."""
    store.definitions = [_definition("pipeline", type="status",
                                     values=[{"value": "healthy",
                                              "color": "green"}])]
    store.points = [{"metric": "pipeline", "ts": _ago(60),
                     "value_numeric": None, "value_text": "healthy",
                     "dims": None, "idempotency_key": "k"}]
    config = _config({"type": "status", "label": "Pipeline",
                      "metric": "pipeline"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    widget = config["sections"][0]["widgets"][0]
    assert widget["value"] == "healthy"
    assert widget["color"] == "green"


def test_a_numeric_bound_widget_colours_from_its_thresholds(store):
    store.definitions = [_definition(critical_threshold=40.0, direction="up")]
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)
    assert config["sections"][0]["widgets"][0]["color"] == "red"


def test_a_bound_widget_carries_no_top_level_stale_flag(store):
    """P8 / E-H2: the dashboard payload's top-level `stale` means
    served-from-cache. Conflating the two flips a banner for the wrong
    reason."""
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)
    assert "stale" not in config


def test_a_bound_widget_history_matches_the_shape_the_panel_reads(store):
    """One `history` shape, not two.

    `_enrich_widgets_with_history` writes `{values, trend, trend_percent, min,
    max, avg}` and `DashboardPanel.vue` reads `widget.history.values` and
    `widget.history.trend`. A bound widget handed a bare LIST would render no
    sparkline and no trend arrow, silently, with every backend assertion still
    green — the bind is the only writer the panel's contract is not spelled
    next to.
    """
    store.points = [
        {"metric": "revenue", "ts": _ago(7200), "value_numeric": 10.0,
         "value_text": None, "dims": None, "idempotency_key": "a"},
        {"metric": "revenue", "ts": _ago(60), "value_numeric": 42.0,
         "value_text": None, "dims": None, "idempotency_key": "b"},
    ]
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    history = config["sections"][0]["widgets"][0]["history"]
    assert isinstance(history, dict), "the panel reads history.values, not history[i]"
    assert set(history) == {"values", "trend", "trend_percent", "min", "max", "avg"}
    assert all(set(v) == {"t", "v"} for v in history["values"])


def test_a_retired_metric_is_REFUSED_not_rendered_as_current(store):
    """I5 / TD-10 applied to the binding.

    `bind_dashboard_widgets` reads with `include_retired=True` — it has to, or
    a retired name would be indistinguishable from one that was never declared
    — and the first version then copied the retired metric's last value onto
    the widget like any other. TD-10 refuses exactly this on the route
    (`metric=<retired>` is a 422, not a 200 carrying the stale number) *so
    that a retired metric never silently reads as current*, and a widget is
    that same read with nobody there to pass `include_retired`. Refusing is
    the consistent answer; the widget says why rather than vanishing.
    """
    store.definitions = [_definition(status="retired",
                                     retired_at="2026-09-01T00:00:00Z")]
    config = _config({"type": "metric", "label": "Rev", "value": 7,
                      "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    widget = config["sections"][0]["widgets"][0]
    assert widget["binding_error_code"] == "metric_retired"
    assert "retired at 2026-09-01T00:00:00Z" in widget["binding_error"]
    assert widget["retired_at"] == "2026-09-01T00:00:00Z"
    assert widget["bound"] is False
    # The placeholder goes too: a number the author wrote is not a substitute
    # for the one the registry refuses to serve.
    assert "value" not in widget
    assert "history" not in widget


def test_every_binding_refusal_carries_a_MACHINE_code(store):
    """The route answers `{reason, message}`; a widget must not answer with a
    sentence alone, or a consumer telling "store down" from "you named a
    retired metric" has to substring-match English."""
    store.raise_on_read = RuntimeError("store down")
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)
    assert (config["sections"][0]["widgets"][0]["binding_error_code"]
            == "metric_store_unavailable")

    store.raise_on_read = None
    config = _config({"type": "metric", "label": "X", "metric": "nope"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)
    assert (config["sections"][0]["widgets"][0]["binding_error_code"]
            == "metric_undeclared")


def test_a_resolved_binding_clears_a_PREVIOUS_refusal(store):
    """The error keys are written onto the caller's dict, so a config that
    carried a stale refusal (a retry after the store came back, the same dict
    re-bound) must come out clean rather than bound-and-erroring at once."""
    config = _config({"type": "metric", "label": "Rev", "metric": "revenue",
                      "binding_error": "metric store unavailable",
                      "binding_error_code": "metric_store_unavailable"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    widget = config["sections"][0]["widgets"][0]
    assert widget["bound"] is True
    assert "binding_error" not in widget
    assert "binding_error_code" not in widget


def test_a_bound_widgets_history_is_the_FOLD_its_value_came_from(store):
    """I6: the trend arrow must describe the number beside it.

    A `sum` metric over two dimension series shows the TOTAL as its value, so
    charting `series[0]` drew one region's history under a total — the arrow
    and the number described different things. The history is built from
    `chart`, which for a foldable aggregation is the cross-series fold.
    """
    store.definitions = [_definition(aggregation="sum",
                                     dimensions=["region"])]
    store.points = [
        {"metric": "revenue", "ts": _ago(60), "value_numeric": 30.0,
         "value_text": None, "dims": {"region": "eu"},
         "idempotency_key": "eu-new"},
        {"metric": "revenue", "ts": _ago(90), "value_numeric": 12.0,
         "value_text": None, "dims": {"region": "us"},
         "idempotency_key": "us-new"},
    ]
    store.calculate_widget_stats = lambda values: {
        "min": min(v["v"] for v in values), "max": max(v["v"] for v in values),
        "avg": 0, "trend": "stable"}

    config = _config({"type": "metric", "label": "Rev", "metric": "revenue"})
    metric_read_service.bind_dashboard_widgets(config, AGENT)

    widget = config["sections"][0]["widgets"][0]
    assert widget["value"] == 42.0  # 30 + 12, the cross-series fold
    charted = [v["v"] for v in widget["history"]["values"]]
    assert charted == [42.0], (
        f"the chart shows {charted}, which is one region of a total of 42")


def test_is_bound_is_the_one_spelling_the_skippers_share():
    assert metric_read_service.is_bound({"metric": "revenue"}) is True
    assert metric_read_service.is_bound({"value": 1}) is False
    assert metric_read_service.is_bound(None) is False


def test_the_snapshot_writer_skips_a_bound_widget(store, monkeypatch):
    """The load-bearing one: a bound number in `agent_dashboard_values` would
    be a SECOND source for a value the registry already owns, and the two
    would disagree the moment the poll and the recording cadence drift."""
    import db.dashboard_history as history_mod
    import inspect

    source = inspect.getsource(history_mod.DashboardHistoryOperations
                               .capture_dashboard_snapshot)
    # @source-text-pin: the skip is a `continue` inside a nested loop that
    # writes through a live engine; a behavioural test would need a real DB
    # fixture, which this file (a service/unit suite) deliberately does not
    # carry. The store suite covers the write; this pins that the guard is
    # present and reads the same key `is_bound` does.
    assert 'widget.get("metric")' in source


def test_history_enrichment_skips_a_bound_widget(store, monkeypatch):
    """A bound widget's history came from the point store; the snapshot
    table's values would chart what the dashboard POLLED, not what the agent
    measured."""
    from services.agent_service import dashboard as dashboard_mod

    monkeypatch.setattr(
        dashboard_mod.db, "get_all_widget_history",
        lambda agent, hours: {"w1": [{"t": "2026-09-22T00:00:00Z", "v": 1}]},
        raising=False)
    config = _config({"type": "metric", "id": "w1", "label": "Rev",
                      "metric": "revenue", "history": "from-the-store"})
    dashboard_mod._enrich_widgets_with_history(config, AGENT, 24)
    assert config["sections"][0]["widgets"][0]["history"] == "from-the-store"


# ---------------------------------------------------------------------------
# The agent-server validator (Invariant #5 mirror)
# ---------------------------------------------------------------------------

def _validate_widget():
    """The REAL `validate_widget` out of the agent-server image's module.

    Imported as `agent_server.routers.dashboard` (its relative imports need a
    package), from `docker/base-image/` — the Invariant #5 mirror is only
    meaningfully tested against the file the agent container actually runs, not
    against a copy of the rule restated here.
    """
    base = str(_ROOT / "docker" / "base-image")
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        module = importlib.import_module("agent_server.routers.dashboard")
    except Exception as e:  # noqa: BLE001 — the agent image has its own deps
        pytest.skip(f"agent server module not importable here: {e}")
    return module.validate_widget


def test_a_bound_widget_needs_no_value():
    """TD-3: without this an author has to write a FAKE number into every
    bound widget forever."""
    validate = _validate_widget()
    assert validate({"type": "metric", "label": "Rev", "metric": "revenue"}, 0) is None


def test_a_bound_status_widget_needs_no_value_and_no_color():
    validate = _validate_widget()
    assert validate({"type": "status", "label": "P", "metric": "pipeline"}, 0) is None


def test_an_UNBOUND_widget_still_requires_its_value():
    """The relaxation is scoped to `metric:` — a hand-written widget with no
    number is still an authoring mistake and still says so."""
    validate = _validate_widget()
    assert "value" in (validate({"type": "metric", "label": "Rev"}, 0) or "")
    assert "value" in (validate({"type": "progress", "label": "P"}, 0) or "")
    assert "color" in (validate({"type": "status", "label": "S",
                                 "value": "ok"}, 0) or "")


def test_a_bound_widget_still_needs_its_label():
    """`label` is the widget's own copy, not a measurement — nothing binds
    it."""
    validate = _validate_widget()
    assert "label" in (validate({"type": "metric", "metric": "revenue"}, 0) or "")

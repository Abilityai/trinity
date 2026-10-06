"""Compat X-009: `dashboard.yaml` bindings that can never resolve (ent#730).

A `dims:` refusal renders only in the browser, and the dashboard route is
`# mcp: none`, so an agent never sees its own selector mistakes. X-009 rides
the compatibility report (MCP `get_agent_compatibility_report`) instead. It
validates each widget's `dims:` against the dimensions its metric declares in
`template.yaml`, with the binding's OWN parser (`parse_dims_selector`, which
wraps the write path's `validate_dims`), so the report and the tile give the
same code and the same sentence. It also reports a `metric:` that is not text,
which the binding refuses per widget.

It never consults recorded points: a valid selector with no data yet passes.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_STR = str(_ROOT / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

from services import metric_read_service as mrs  # noqa: E402
from services.compatibility import spec, static_checks  # noqa: E402
from services.compatibility.static_checks import run_static  # noqa: E402

TEMPLATE = """\
name: spend-agent
metrics:
  - name: ad_spend
    type: gauge
    label: "Ad spend"
    unit: "USD"
    cadence: 1h
    direction: down_good
    aggregation: sum
    critical_threshold: 500
    dimensions: [channel]
  - name: pipeline
    type: status
    label: "Pipeline"
    values:
      - {value: ok, color: green}
"""


def _snap(dashboard=None, template=TEMPLATE):
    files = {}
    files["dashboard.yaml"] = ({"exists": True, "content": dashboard}
                               if dashboard is not None else {"exists": False})
    files["template.yaml"] = ({"exists": True, "content": template}
                              if template is not None else {"exists": False})
    return {"files": files}


def _dashboard(*widget_yaml):
    body = "".join(f"      - {w}\n" for w in widget_yaml)
    return f'title: "Spend"\nsections:\n  - title: "S"\n    widgets:\n{body}'


def _x009(snap):
    return run_static(snap, ["X-009"])["X-009"]


def _findings(result):
    status, _message, detail = result
    assert status == "fail", result
    return detail["widgets"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_x009_is_a_soft_static_cross_file_check():
    entry = next(c for c in spec.CHECKS if c.id == "X-009")
    assert (entry.severity, entry.type, entry.category) == ("soft", "static", "X")
    assert static_checks.STATIC_CHECKS["X-009"] is static_checks.c_x009


# ---------------------------------------------------------------------------
# C1 – C9
# ---------------------------------------------------------------------------

def test_C1_a_valid_selector_passes_without_any_points():
    """The fixture has no store at all: the check never consults points."""
    status, _m, _d = _x009(_snap(_dashboard(
        '{type: metric, label: G, metric: ad_spend, dims: {channel: google}}')))
    assert status == "pass"


def test_C2_an_undeclared_key_is_a_soft_finding_naming_the_declared_one():
    (finding,) = _findings(_x009(_snap(_dashboard(
        '{type: metric, label: EU, metric: ad_spend, dims: {region: eu}}'))))
    assert finding["code"] == "metric_dimension_undeclared"
    assert "channel" in finding["problem"]
    assert finding["metric"] == "ad_spend" and finding["label"] == "EU"


def test_C3_a_number_value_is_invalid_with_the_quote_hint():
    (finding,) = _findings(_x009(_snap(_dashboard(
        '{type: metric, label: Y, metric: ad_spend, dims: {channel: 2024}}'))))
    assert finding["code"] == "metric_dimension_invalid"
    assert "quote it" in finding["problem"]


@pytest.mark.parametrize("dims", ["[meta]", '"meta"'])
def test_C4_a_non_mapping_selector_is_invalid(dims):
    (finding,) = _findings(_x009(_snap(_dashboard(
        f'{{type: metric, label: M, metric: ad_spend, dims: {dims}}}'))))
    assert finding["code"] == "metric_dimension_invalid"
    assert finding["problem"] == "dims must be a mapping of dimension: value"


def test_C5_dims_without_metric_does_nothing_and_says_so():
    (finding,) = _findings(_x009(_snap(_dashboard(
        '{type: metric, label: Lone, value: 3, dims: {channel: meta}}'))))
    assert finding["code"] == "dims_without_metric"


@pytest.mark.parametrize("dims", ["{}", "null"])
def test_C6_an_empty_selector_is_no_selector(dims):
    status, _m, _d = _x009(_snap(_dashboard(
        f'{{type: metric, label: T, metric: ad_spend, dims: {dims}}}')))
    assert status == "pass"


@pytest.mark.parametrize("widget", [
    '{type: metric, label: U, metric: nope, dims: {channel: meta}}',
    '{type: metric, label: B, metric: broken, dims: {channel: meta}}',
])
def test_C7_an_undeclared_or_malformed_metric_is_not_x009s_finding(widget):
    template = TEMPLATE + "  - name: broken\n    type: nonsense\n"
    status, _m, _d = _x009(_snap(_dashboard(widget), template=template))
    assert status == "pass"


@pytest.mark.parametrize("metric,kind", [
    ("[ad_spend]", "a list"), ("{a: b}", "a mapping"), ("5", "a number")])
def test_C7_a_non_text_metric_is_reported_with_the_binding_sentence(
        metric, kind):
    """Round 2: the binding refuses such a widget by name, so the report
    says the same thing before an operator opens the dashboard."""
    (finding,) = _findings(_x009(_snap(_dashboard(
        f'{{type: metric, label: Bad, metric: {metric}}}'))))
    assert finding["code"] == "metric_name_invalid"
    assert kind in finding["problem"]
    assert finding["problem"] == mrs.invalid_metric_name(
        {"[ad_spend]": ["ad_spend"], "{a: b}": {"a": "b"}, "5": 5}[metric])


def test_C8_no_dashboard_skips():
    status, _m, detail = _x009(_snap(None))
    assert status == "skipped"


@pytest.mark.parametrize("template", [None, "name: [unclosed"])
def test_C8_a_missing_or_invalid_template_skips_the_selector_check(template):
    status, _m, _d = _x009(_snap(_dashboard(
        '{type: metric, label: EU, metric: ad_spend, dims: {region: eu}}'),
        template=template))
    assert status == "skipped"


def test_C9_a_raise_fails_closed_with_the_type_name_only():
    secret = "token-in-the-template-do-not-echo"
    with patch.object(mrs, "parse_dims_selector",
                      side_effect=ValueError(secret)):
        status, message, detail = _x009(_snap(_dashboard(
            '{type: metric, label: G, metric: ad_spend, dims: {channel: google}}')))
    assert status == "fail"
    assert detail == {"error_type": "ValueError"}
    assert secret not in message


# ---------------------------------------------------------------------------
# C10: parity with the binding, echo safety, and the user doc's recipe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dims", [
    {"region": "eu"}, {"channel": 2024}, {"channel": ["meta", "google"]},
    {"channel": None}, ["meta"], "meta", {"channel": "google"}])
def test_C10_the_report_and_the_tile_say_the_same_thing(dims, monkeypatch):
    import yaml

    import database as database_mod

    widget = {"type": "metric", "label": "P", "metric": "ad_spend", "dims": dims}
    result = _x009(_snap(yaml.safe_dump(
        {"title": "T", "sections": [{"title": "S", "widgets": [widget]}]})))
    expected = mrs.parse_dims_selector(dims, ["channel"])

    class _Db:
        def list_metric_definitions(self, *a, **k):
            return [{"name": "ad_spend", "type": "gauge", "status": "active",
                     "dimensions": ["channel"], "aggregation": "sum"}]

        def latest_metric_points(self, *a, **k):
            return []

        def metric_series_points(self, *a, **k):
            return []

        def calculate_widget_stats(self, values):
            return None

    monkeypatch.setattr(database_mod, "db", _Db())
    bound = {"sections": [{"widgets": [dict(widget)]}]}
    mrs.bind_dashboard_widgets(bound, "parity-agent")
    tile = bound["sections"][0]["widgets"][0]

    if expected[1] is None:
        assert result[0] == "pass"
        return
    (finding,) = _findings(result)
    assert (finding["code"], finding["problem"]) == expected[1]
    assert tile["binding_error_code"] == expected[1][0]
    assert tile["binding_error"] == f"metric 'ad_spend': {expected[1][1]}"


def _has_control(text):
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def test_C10_nothing_raw_from_the_author_reaches_the_persisted_detail():
    """`checks_json` is persisted and rendered. The leaf interpolates the
    DECLARED keys raw, and the registry's name regex accepts a trailing
    newline, so a declared `"channel\\n"` must not reach the detail."""
    template = TEMPLATE.replace("dimensions: [channel]",
                                'dimensions: ["channel\\n"]')
    status, message, detail = _x009(_snap(_dashboard(
        '{type: metric, label: "L\\u202e\\u0007", metric: ad_spend, '
        'dims: {region: eu}}'), template=template))
    assert status == "fail"
    strings = [message] + [v for w in detail["widgets"] for v in w.values()]
    for text in strings:
        assert not _has_control(text), repr(text)
        assert "‮" not in text


def test_C10_the_user_docs_recipe_passes_x009_and_d003():
    import importlib.util

    path = Path(__file__).with_name("test_ent730_bound_widget_dims.py")
    loader = importlib.util.spec_from_file_location("_ent730_recipe", path)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    results = run_static(_snap(module.RECIPE_DASHBOARD_YAML), ["X-009", "D-003"])
    assert results["X-009"][0] == "pass"
    assert results["D-003"][0] == "pass"

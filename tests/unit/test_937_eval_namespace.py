"""The `eval.` metric namespace is the platform's (trinity-enterprise#937, C4).

The scorecard writer stores `eval.*` points under `platform:eval`; no caller
of `record_metrics` — agent key, user key or admin — may write into that
namespace. `validate_batch` is the one validator of the one write route, and
it takes no principal, so refusing there refuses every principal. The refusal
is named (`eval_namespace_reserved`) and comes BEFORE the name rule, so a
dotted name does not read as a typo (`metric_name_invalid`).
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import metric_points_service as svc  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


def _definition(name):
    return {"name": name, "type": "counter", "status": "active", "dimensions": [],
            "values": None, "type_conflict": None}


@pytest.mark.parametrize("metric", ["eval.checks_passed_rate", "eval.graded", "eval.anything"])
def test_an_eval_dot_metric_is_refused_by_name(metric):
    rows, errors = svc.validate_batch([_definition(metric)], [{"metric": metric, "value": 1}],
                                      now=NOW)
    assert rows == []
    assert [e["code"] for e in errors] == ["eval_namespace_reserved"]
    assert "eval." in errors[0]["message"] and errors[0].get("hint")


def test_the_refusal_is_per_point_and_names_the_index():
    rows, errors = svc.validate_batch(
        [_definition("cycles")],
        [{"metric": "cycles", "value": 1}, {"metric": "eval.passed", "value": 1}], now=NOW)
    assert [(e["index"], e["code"]) for e in errors] == [(1, "eval_namespace_reserved")]


def test_eval_underscore_still_follows_the_normal_rules():
    rows, errors = svc.validate_batch([_definition("eval_score")],
                                      [{"metric": "eval_score", "value": 2}], now=NOW)
    assert errors == [] and rows[0]["metric"] == "eval_score"
    _, errors = svc.validate_batch([], [{"metric": "eval_score", "value": 2}], now=NOW)
    assert [e["code"] for e in errors] == ["metric_undeclared"]


def test_the_write_route_validates_without_a_principal_argument():
    """Every principal reaches the same refusal: the route's one call into the
    validator passes no caller identity the rule could branch on."""
    tree = ast.parse((_BACKEND / "routers" / "metric_points.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "validate_batch"]
    assert len(calls) == 1
    assert {k.arg for k in calls[0].keywords} <= {"now", "retention_days", "execution_id"}
    params = svc.validate_batch.__code__.co_varnames[:svc.validate_batch.__code__.co_argcount
                                                     + svc.validate_batch.__code__.co_kwonlyargcount]
    assert not {"current_user", "principal", "user", "agent_name"} & set(params)

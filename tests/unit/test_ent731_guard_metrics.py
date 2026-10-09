"""Guard metrics in the objective join (trinity-enterprise#731).

An objective's `guard_metrics:` are numbers that must not move while its own
metrics are pursued. They ride the same `metrics` list as the primaries, tagged
`role: "guard"`, so they are fetched, served through a grant and judged by the
same code; what differs is that a guard is ALWAYS a hold, and is counted in its
own `summary.guards` block.

Driven at `parse_objective` / `join_objectives` (pure) and once through
`read_objective_join` to prove a guard's number is actually fetched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Sibling import — the unit dir is not implicitly importable, and the fakes the
# objective-join suite already owns are the ones to reuse.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent666_objective_join import (  # noqa: E402
    AGENT, NOW, ROLE, TEMPLATE, _FakeClient, _definition, _join, _latest,
    running, store, svc,
)

pytestmark = pytest.mark.unit

PATH = "canon/objectives/q4-marketing-demand.yaml"


def _parse(**overrides):
    doc = {
        "id": "q4-marketing-demand",
        "owner": f"role:{ROLE}",
        "status": "active",
        "metrics": [{"name": "close_rate", "direction": "up", "target": 35}],
        **overrides,
    }
    return svc.parse_objective(doc, path=PATH)


def _codes(findings):
    return [f["code"] for f in findings]


def _guard_row(result, name="spend"):
    rows = [m for m in result["objectives"][0]["metrics"] if m["name"] == name]
    assert len(rows) == 1, rows
    return rows[0]


# ===========================================================================
# parse_objective
# ===========================================================================

def test_guards_are_read_into_the_same_list_tagged_guard():
    """Canon's real shape: descriptive keys beside the guard are ignored."""
    obj, findings = _parse(guard_metrics=[{
        "name": "spend", "direction": "hold", "served_by": "tom",
        "definition": "Paid spend per month.", "rationale": "...",
        "instrument": "ads console", "tolerance": 0.15, "target": 4000,
    }])
    assert findings == []
    assert [(m["name"], m["role"]) for m in obj["metrics"]] == [
        ("close_rate", "primary"), ("spend", "guard")]
    guard = obj["metrics"][1]
    assert (guard["target"], guard["tolerance"]) == (4000, 0.15)


@pytest.mark.parametrize("value", [None, []])
def test_no_guards_adds_nothing(value):
    obj, findings = _parse(guard_metrics=value)
    assert findings == []
    assert [m["role"] for m in obj["metrics"]] == ["primary"]


@pytest.mark.parametrize("value", ["spend", {"name": "spend"}, 3])
def test_a_guard_section_that_is_not_a_list_is_named(value):
    obj, findings = _parse(guard_metrics=value)
    assert _codes(findings) == ["guard_metrics_invalid"]
    assert [m["role"] for m in obj["metrics"]] == ["primary"]


def test_malformed_guard_entries_are_named():
    obj, findings = _parse(guard_metrics=[
        "spend", {"name": "bad name!"}, {True: "yes"}, {"name": "spend"},
        {"name": "spend"}])
    assert _codes(findings) == ["metric_name_invalid", "metric_name_invalid",
                                "metric_name_invalid", "metric_duplicate"]
    assert [m["name"] for m in obj["metrics"]] == ["close_rate", "spend"]


def test_a_name_in_both_lists_keeps_the_primary_and_says_the_guard_is_ignored():
    obj, findings = _parse(guard_metrics=[{"name": "close_rate"}])
    assert [m["role"] for m in obj["metrics"]] == ["primary"]
    assert _codes(findings) == ["metric_duplicate"]
    assert "guard" in findings[0]["message"]


def test_guards_have_their_own_cap():
    cap = svc.MAX_METRICS_PER_OBJECTIVE
    primaries = [{"name": f"p{i}", "target": 1} for i in range(cap)]
    guards = [{"name": f"g{i}"} for i in range(cap + 1)]
    obj, _ = _parse(metrics=primaries, guard_metrics=guards)
    roles = [m["role"] for m in obj["metrics"]]
    assert roles.count("primary") == cap
    assert roles.count("guard") == cap
    assert obj["metrics_truncated"] is True


def test_metrics_truncated_stays_false_within_both_caps():
    obj, _ = _parse(guard_metrics=[{"name": "spend"}])
    assert obj["metrics_truncated"] is False


# ===========================================================================
# resolve_direction
# ===========================================================================

@pytest.mark.parametrize("registry", ["up_good", "down_good", "neutral", None])
@pytest.mark.parametrize("declared", ["hold", None, "up", "down"])
def test_a_guard_is_always_a_hold(registry, declared):
    assert svc.resolve_direction(registry, declared, guard=True) == (
        "neutral", "objective", False)


def test_a_primary_disagreement_is_still_a_mismatch():
    assert svc.resolve_direction("up_good", "hold") == (
        "up_good", "registry", True)


# ===========================================================================
# join_objectives
# ===========================================================================

def _joined(guard, *, definitions=None, latest=None, primary_value=30.0,
            served=None, role_id=ROLE, owner=f"role:{ROLE}"):
    obj, _ = _parse(guard_metrics=[guard], owner=owner)
    definitions = definitions if definitions is not None else [
        _definition(), _definition(name="spend", direction="up_good")]
    latest = latest if latest is not None else {
        **_latest(value=primary_value), **_latest(name="spend", value=4300.0)}
    return svc.join_objectives([obj], definitions, latest, agent_name=AGENT,
                               role_id=role_id, served=served)


def test_a_guard_over_an_up_good_registry_is_judged_as_a_hold():
    result = _joined({"name": "spend", "target": 4000, "tolerance": 500})
    row = _guard_row(result)
    assert row["role"] == "guard"
    assert (row["direction"], row["direction_source"]) == ("neutral", "objective")
    assert row["objective_direction"] == "hold"
    assert row["gap"]["status"] == "on_target"
    assert row["finding"] is None
    assert _codes(result["findings"]) == []


@pytest.mark.parametrize("value", [3000.0, 5000.0])
def test_a_guard_off_either_side_is_off_target_never_behind_or_ahead(value):
    result = _joined({"name": "spend", "target": 4000, "tolerance": 500},
                     latest={**_latest(), **_latest(name="spend", value=value)})
    assert _guard_row(result)["gap"]["status"] == "off_target"


def test_a_guard_without_tolerance_holds_exactly():
    result = _joined({"name": "spend", "target": 4300})
    assert _guard_row(result)["gap"]["status"] == "on_target"
    result = _joined({"name": "spend", "target": 4299})
    assert _guard_row(result)["gap"]["status"] == "off_target"


def test_the_same_metric_as_a_primary_elsewhere_keeps_its_direction():
    guarded, _ = _parse(guard_metrics=[{"name": "spend", "target": 4000}])
    pursued, _ = svc.parse_objective({
        "id": "q4-spend-up", "owner": f"role:{ROLE}",
        "metrics": [{"name": "spend", "target": 5000}]}, path="canon/objectives/b.yaml")
    result = svc.join_objectives(
        [guarded, pursued],
        [_definition(), _definition(name="spend", direction="up_good")],
        {**_latest(), **_latest(name="spend", value=4300.0)},
        agent_name=AGENT, role_id=ROLE)
    guard = [m for m in result["objectives"][0]["metrics"] if m["name"] == "spend"][0]
    primary = result["objectives"][1]["metrics"][0]
    assert guard["gap"]["status"] == "off_target"
    assert (primary["role"], primary["direction"]) == ("primary", "up_good")
    assert primary["gap"]["status"] == "behind"


def test_a_guard_with_no_target_says_so():
    result = _joined({"name": "spend", "target": None})
    row = _guard_row(result)
    assert row["gap"] == {"status": "not_computable", "delta": None,
                          "reason": "no_target"}
    assert row["finding"]["code"] == "guard_target_unset"
    assert "guard_target_unset" in _codes(result["findings"])


def test_a_guard_declaring_up_is_judged_as_hold_and_named():
    result = _joined({"name": "spend", "direction": "up", "target": 4300})
    row = _guard_row(result)
    assert row["gap"]["status"] == "on_target"
    assert row["objective_direction"] == "up"
    assert row["finding"]["code"] == "guard_direction_invalid"
    assert "direction_mismatch" not in _codes(result["findings"])


def test_an_undeclared_guard_is_named_like_any_metric():
    result = _joined({"name": "spend", "target": 4000},
                     definitions=[_definition()])
    row = _guard_row(result)
    assert row["gap"]["reason"] == "undeclared"
    assert row["finding"]["code"] == "metric_undeclared"


def test_a_retired_guard_is_named_like_any_metric():
    result = _joined({"name": "spend", "target": 4000}, definitions=[
        _definition(), _definition(name="spend", status="retired")])
    assert _guard_row(result)["finding"]["code"] == "metric_retired"


def test_a_stale_guard_keeps_its_gap_and_is_counted_stale():
    result = _joined({"name": "spend", "target": 4300}, latest={
        **_latest(), **_latest(name="spend", value=4300.0, stale=True,
                               freshness="stale")})
    row = _guard_row(result)
    assert (row["stale"], row["gap"]["status"]) == (True, "on_target")
    assert result["summary"]["guards"]["stale"] == 1
    assert result["summary"]["stale"] == 0


def test_a_guard_served_by_a_granted_agent_is_still_a_hold():
    served = {"spend": {"agent": "tom",
                        "definition": _definition(name="spend", direction="up_good"),
                        "latest": _latest(name="spend", value=4300.0)["spend"]}}
    result = _joined({"name": "spend", "target": 4300},
                     definitions=[_definition()], served=served)
    row = _guard_row(result)
    assert row["served_by"] == "tom"
    assert (row["direction"], row["gap"]["status"]) == ("neutral", "on_target")


def test_a_supporting_only_guard_is_declared_elsewhere():
    result = _joined({"name": "spend", "target": 4000},
                     definitions=[_definition()], owner="role:someone-else",
                     role_id=None)
    # The objective is not owned, so it only reaches the agent if it supports.
    row = _guard_row(result)
    assert row["finding"]["code"] == "metric_not_declared_here"


# ===========================================================================
# summary
# ===========================================================================

def test_guards_are_counted_apart_from_the_primaries():
    result = _joined({"name": "spend", "target": 4000, "tolerance": 500})
    summary = result["summary"]
    assert summary["metrics"] == 1
    assert summary["behind"] == 1
    assert summary["on_target"] == 0
    assert summary["guards"] == {"total": 1, "on_target": 1, "off_target": 0,
                                 "not_computable": 0, "stale": 0}


def test_an_objective_without_guards_reports_exactly_as_before():
    obj, _ = _parse()
    result = _join([obj], [_definition()], _latest())
    assert result["summary"]["guards"] == {
        "total": 0, "on_target": 0, "off_target": 0, "not_computable": 0,
        "stale": 0}
    assert [m["role"] for m in result["objectives"][0]["metrics"]] == ["primary"]


def test_the_empty_answer_carries_the_same_summary_shape():
    obj, _ = _parse(guard_metrics=[{"name": "spend"}])
    joined = _join([obj], [_definition()], _latest())
    empty = svc._empty(AGENT, NOW)
    assert set(empty["summary"]) == set(joined["summary"])
    assert set(empty["summary"]["guards"]) == set(joined["summary"]["guards"])


# ===========================================================================
# read_objective_join — the guard's number is actually fetched
# ===========================================================================

GUARDED_YAML = """
id: q4-close-rate
owner: role:revenue-lead
metrics:
  - name: close_rate
    direction: up
    target: 35
guard_metrics:
  - name: spend
    direction: hold
    target: 4300
    rationale: Demand bought with spend the model cannot bear is not demand.
status: active
"""


@pytest.mark.asyncio
async def test_a_guard_is_fetched_through_the_whole_read(store, running):
    store.definitions = [_definition(),
                         _definition(name="spend", direction="up_good")]
    store.points.append({"metric": "spend", "ts": store.points[0]["ts"],
                         "value_numeric": 4300.0, "value_text": None,
                         "dims": None})
    client = _FakeClient(files={
        "template.yaml": TEMPLATE + "  - name: spend\n",
        "canon/objectives/q4-close-rate.yaml": GUARDED_YAML,
    })
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)
    row = _guard_row(result)
    assert (row["actual"], row["gap"]["status"]) == (4300.0, "on_target")
    fetched = [c for c in store.calls if c[0] == "latest_metric_points"]
    assert "spend" in fetched[0][1]


# ===========================================================================
# Workspace role card — the portal projection
# ===========================================================================

def test_the_portal_row_says_which_rows_are_guards():
    from client_portal import role_card as rc
    result = _joined({"name": "spend", "target": None})
    rows = {m["name"]: rc.portal_metric(m)
            for m in result["objectives"][0]["metrics"]}
    assert rows["spend"]["role"] == "guard"
    assert rows["spend"]["finding"] == {"code": "guard_target_unset"}
    assert rows["close_rate"]["role"] == "primary"


@pytest.mark.parametrize("role", [None, "", "GUARD", "<b>x</b>", 1])
def test_the_portal_role_is_a_fixed_value_never_author_text(role):
    from client_portal import role_card as rc
    row = rc.portal_metric({"name": "spend", "role": role,
                            "gap": {"status": "on_target"}})
    assert row["role"] == "primary"

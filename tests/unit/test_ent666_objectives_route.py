"""`GET /api/agents/{name}/objectives` (trinity-enterprise#666, C3).

The route owns transport — gate order, the rate limit, the 503, and the
response MODEL. The service owns the join. Both are exercised here through a
real `TestClient` over the real router with the service's edges stubbed,
because the thing worth proving is that the two halves agree on the shape a
consumer binds to.

The model is the contract, so a key-parity test walks the service's own dict
against the model's fields: an additive service key fails the build here
instead of being silently filtered out of every response (the 2026-07-27
`response_model`-is-an-allowlist trap, done structurally rather than by
remembering).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import models as models_mod  # noqa: E402
import routers.agent_files as route_mod  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import ObjectiveJoinRead, User  # noqa: E402
from services import objective_join_service as svc  # noqa: E402

AGENT = "sales-companion"


def _definition(**overrides):
    d = {
        "name": "close_rate", "type": "percentage", "label": "Close rate",
        "description": None, "unit": "%", "direction": "up_good",
        "aggregation": "last", "cadence": "1h", "cadence_seconds": 3600,
        "warning_threshold": None, "critical_threshold": None,
        "values": None, "dimensions": [], "status": "active",
        "retired_at": None, "type_conflict": None,
    }
    d.update(overrides)
    return d


def _objective(**overrides):
    obj, _ = svc.parse_objective({
        "schema_version": 1,
        "id": "q4-close-rate",
        "statement": "Lift close rate to 35%.",
        "horizon": "quarter",
        "owner": "role:revenue-lead",
        "metrics": [{"name": "close_rate", "direction": "up", "target": 35,
                     "by": "2026-12-31"}],
        "status": "active",
        "review_by": "2026-10-15",
        **overrides,
    }, path="canon/objectives/q4-close-rate.yaml")
    return obj


def _body(**overrides):
    """A full join result, straight out of the service's own composition."""
    joined = svc.join_objectives(
        [_objective()], [_definition()],
        {"close_rate": {
            **_definition(),
            "latest": {"value": 30.0, "ts": "2026-09-22T11:59:00.000000Z",
                       "dims": None},
            "last_point_at": "2026-09-22T11:59:00.000000Z",
            "stale": False, "freshness": "fresh", "stale_after": None,
            "series_count": 1}},
        agent_name=AGENT, role_id="revenue-lead")
    result = {
        "agent_name": AGENT,
        "generated_at": "2026-09-22T12:00:00.000000Z",
        "stale_rule": svc.STALE_RULE,
        "role": {"id": "revenue-lead",
                 "path": "canon/roles/revenue-lead.yaml"},
        "canon_root": "canon",
        "unavailable": None,
        "source": {"template": "read", "objectives_dir": "read",
                   "objectives_listed": 1, "objectives_scanned": 1,
                   "objectives_unscanned": 0, "objectives_skipped": 0,
                   "objectives_truncated": False},
        "objectives": joined["objectives"],
        "findings": joined["findings"],
        "summary": joined["summary"],
        "message": None,
    }
    result.update(overrides)
    return result


@pytest.fixture
def ctx(monkeypatch):
    state = {"result": _body(), "raise": None, "calls": []}

    async def _read(agent_name, **kwargs):
        state["calls"].append(agent_name)
        if state["raise"]:
            raise state["raise"]
        return state["result"]

    monkeypatch.setattr(
        route_mod.objective_join_service, "read_objective_join", _read)
    monkeypatch.setattr(route_mod.rate_limiter, "enforce",
                        lambda *a, **k: None)

    app = FastAPI()
    app.include_router(route_mod.router)
    holder = {"user": User(id=1, username="operator", email="op@example.com",
                           role="user")}

    from dependencies import get_authorized_agent_by_name

    # The access dependency is overridden to "every agent in the path is
    # accessible", so these tests exercise the handler's OWN gates; the uniform
    # 404 is proven by the #186 suite. Keyed off the callable the ROUTE
    # resolved, because the unit island restores backend modules between tests.
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
        users = holder
        service = state
    return Ctx()


def _get(ctx, name=AGENT):
    return ctx.client.get(f"/api/agents/{name}/objectives")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_an_agent_key_reads_its_own_objectives(ctx):
    ctx.users["user"] = User(id=2, username=AGENT, email=f"{AGENT}@agents.local",
                             role="user", agent_name=AGENT)
    assert _get(ctx).status_code == 200


def test_an_agent_key_may_not_read_another_agents_objectives(ctx):
    """403 and not 404: the caller already knows this agent exists (the
    dependency let it through), so the uniform-404 reasoning does not apply."""
    ctx.users["user"] = User(id=3, username="other", email="o@agents.local",
                             role="user", agent_name="other-agent")
    response = _get(ctx)
    assert response.status_code == 403
    assert "own objectives" in response.json()["detail"]


def test_a_human_principal_is_not_self_gated(ctx):
    assert _get(ctx).status_code == 200
    assert ctx.service["calls"] == [AGENT]


def test_the_self_gate_runs_before_the_service_is_called(ctx):
    ctx.users["user"] = User(id=3, username="other", email="o@agents.local",
                             role="user", agent_name="other-agent")
    _get(ctx)
    assert ctx.service["calls"] == []


def test_the_rate_limit_is_its_own_knob_and_is_enforced(monkeypatch, ctx):
    """The read touches the CONTAINER, unlike `/metrics` which is store-only —
    so it gets its own, much lower ceiling rather than the store read's."""
    seen = []
    monkeypatch.setattr(route_mod.rate_limiter, "enforce",
                        lambda key, limit, window, **k: seen.append(
                            (key, limit, window)))
    _get(ctx)

    assert seen == [(f"agent_objectives_read:{AGENT}",
                     route_mod.OBJECTIVES_READ_RATE_LIMIT,
                     route_mod.OBJECTIVES_READ_RATE_WINDOW)]
    assert route_mod.OBJECTIVES_READ_RATE_LIMIT == 60
    assert (route_mod.OBJECTIVES_READ_RATE_LIMIT
            < route_mod.METRICS_READ_RATE_LIMIT)


def test_the_limiter_is_keyed_on_the_validated_name(monkeypatch, ctx):
    """After the access gate, never before: an unvalidated path param in a
    limiter key is a key-amplification surface."""
    order = []
    monkeypatch.setattr(route_mod.rate_limiter, "enforce",
                        lambda *a, **k: order.append("limiter"))
    ctx.users["user"] = User(id=3, username="other", email="o@agents.local",
                             role="user", agent_name="other-agent")
    _get(ctx)
    assert order == []   # the 403 fired first


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------

def test_a_store_outage_is_a_503_with_retry_after(ctx):
    ctx.service["raise"] = OperationalError("select", {}, Exception("gone"))
    response = _get(ctx)

    assert response.status_code == 503
    assert response.json()["detail"] == "metric_store_unavailable"
    assert response.headers["Retry-After"] == "30"


def test_an_unreachable_agent_is_a_200_that_says_so(ctx):
    """Agent-door failures are NAMED FIELDS on a successful read, not statuses:
    "this agent is stopped" is an answer, not an error."""
    ctx.service["result"] = _body(
        unavailable="agent_stopped", objectives=[],
        message="this agent is stopped — start it to read them")
    response = _get(ctx)

    assert response.status_code == 200
    body = response.json()
    assert body["unavailable"] == "agent_stopped"
    assert body["message"]


# ---------------------------------------------------------------------------
# The model IS the contract
# ---------------------------------------------------------------------------

def _keys(model):
    return set(model.model_fields)


def test_the_model_carries_exactly_the_keys_the_service_produces():
    """An additive service key that the model does not carry is silently
    dropped by `response_model` — so it fails the build here instead."""
    result = _body()
    assert _keys(ObjectiveJoinRead) == set(result)
    assert _keys(models_mod.ObjectiveJoinSource) == set(result["source"])
    assert _keys(models_mod.ObjectiveJoinSummary) == set(result["summary"])
    assert _keys(models_mod.ObjectiveRoleRead) == set(result["role"])

    objective = result["objectives"][0]
    assert _keys(models_mod.ObjectiveRead) == set(objective)
    assert _keys(models_mod.ObjectiveMetricRead) == set(objective["metrics"][0])
    assert _keys(models_mod.ObjectiveMetricGap) == set(
        objective["metrics"][0]["gap"])


def test_the_finding_models_match_both_finding_shapes():
    """The flat list and the per-row reference are deliberately different
    shapes — one carries where it came from, the other is what a card renders."""
    joined = svc.join_objectives(
        [_objective()], [], {}, agent_name=AGENT, role_id="revenue-lead")
    assert _keys(models_mod.ObjectiveFinding) == set(joined["findings"][0])
    row_finding = joined["objectives"][0]["metrics"][0]["finding"]
    assert _keys(models_mod.ObjectiveFindingRef) == set(row_finding)


def test_the_response_round_trips_through_the_model_unchanged(ctx):
    body = _get(ctx).json()
    row = body["objectives"][0]["metrics"][0]

    assert body["agent_name"] == AGENT
    assert body["stale_rule"] == "2x cadence"
    assert body["role"]["id"] == "revenue-lead"
    assert (row["target"], row["actual"]) == (35.0, 30.0)
    assert row["gap"] == {"status": "behind", "delta": -5.0, "reason": None}
    assert row["by"] == "2026-12-31" and row["horizon"] == "quarter"
    assert body["summary"]["behind"] == 1


def test_an_undeclared_metric_reaches_the_wire_as_a_finding_not_a_blank(ctx):
    """The AC, pinned at the boundary a consumer actually binds to."""
    joined = svc.join_objectives(
        [_objective()], [], {}, agent_name=AGENT, role_id="revenue-lead")
    ctx.service["result"] = _body(objectives=joined["objectives"],
                                  findings=joined["findings"],
                                  summary=joined["summary"])
    body = _get(ctx).json()
    row = body["objectives"][0]["metrics"][0]

    assert row["actual"] is None
    assert row["finding"]["code"] == "metric_undeclared"
    assert "refresh_metric_definitions" in row["finding"]["message"]
    assert body["findings"][0]["path"] == "canon/objectives/q4-close-rate.yaml"


@pytest.mark.parametrize("target", [
    float("nan"), float("inf"), float("-inf"), [1, 2], True, "s" * 4000,
])
def test_author_shaped_targets_survive_the_real_route_as_strict_json(
        ctx, target):
    """A bare `NaN` in a body is not JSON — `JSON.parse` throws and the card
    renders blank. Pushed through the real route and parsed STRICTLY."""
    joined = svc.join_objectives(
        [_objective(metrics=[{"name": "close_rate", "direction": "up",
                              "target": target}])],
        [_definition()],
        {"close_rate": {**_definition(), "latest": None,
                        "last_point_at": None, "stale": False,
                        "freshness": "no_points", "stale_after": None,
                        "series_count": 0}},
        agent_name=AGENT, role_id="revenue-lead")
    ctx.service["result"] = _body(objectives=joined["objectives"],
                                  findings=joined["findings"],
                                  summary=joined["summary"])
    response = _get(ctx)

    assert response.status_code == 200
    body = json.loads(response.text, parse_constant=_no_constants)
    row = body["objectives"][0]["metrics"][0]
    assert row["target"] is None
    assert row["gap"]["status"] == "not_computable"
    if row["target_text"] is not None:
        assert len(row["target_text"]) <= 64


def _no_constants(name):
    raise AssertionError(f"non-JSON constant {name!r} reached the wire")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_route_declares_its_mcp_surface():
    """Invariant #13: `/validate-architecture` tells "unexposed on purpose"
    from "forgotten" by this header alone."""
    header = Path(route_mod.__file__).read_text().split("\n", 1)[0]
    assert header.startswith("# mcp:")
    assert "get_objectives" in header


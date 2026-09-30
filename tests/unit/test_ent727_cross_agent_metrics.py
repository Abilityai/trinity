"""Narrow cross-agent metrics read (trinity-enterprise#727).

The route gate itself is proven in `test_ent479_metrics_route.py`, beside the
self read it widens. This suite owns the three things ent#727 adds that are not
a route gate:

- the ONE predicate (`metric_access_service.can_read_agent_metrics`) and the
  hourly-deduplicated audit of "who read whose numbers";
- `get_objectives` resolving an `actual` from the agent that SERVES a metric,
  through the reading agent's grant, with ambiguity decided before any viewer
  filter and a fail-closed default when nobody said who is looking;
- the objectives route handing the join a viewer predicate and auditing each
  serving agent it read.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

pytest.importorskip("fastapi", reason="backend venv required")

import database as database_mod  # noqa: E402
import models as models_mod  # noqa: E402
from services import docker_utils  # noqa: E402
from services import metric_access_service as access  # noqa: E402
from services import objective_join_service as svc  # noqa: E402

READER = "sales-companion"
SERVER = "revenue-agent"
OTHER = "finance-agent"
ROLE = "revenue-lead"
HOUR = 3600
NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _definition(**overrides):
    d = {
        "name": "close_rate", "type": "percentage", "label": "Close rate",
        "description": None, "unit": "%", "direction": "up_good",
        "aggregation": "last", "cadence": "1h", "cadence_seconds": HOUR,
        "warning_threshold": None, "critical_threshold": None,
        "values": None, "dimensions": [], "status": "active",
        "retired_at": None, "type_conflict": None,
    }
    d.update(overrides)
    return d


class _Fleet:
    """A store keyed by agent: registries, points and grants."""

    def __init__(self):
        self.definitions = {READER: [], SERVER: [_definition()], OTHER: []}
        self.points = {SERVER: [{
            "metric": "close_rate", "ts": _ago(60), "value_numeric": 30.0,
            "value_text": None, "dims": None}]}
        self.grants = {(READER, SERVER)}
        self.calls = []

    def is_agent_permitted(self, source, target):
        return (source, target) in self.grants

    def get_permitted_agents(self, source):
        return sorted(t for s, t in self.grants if s == source)

    def list_metric_definitions(self, agent, include_retired=False):
        self.calls.append(("list_metric_definitions", agent))
        rows = list(self.definitions.get(agent, []))
        return rows if include_retired else [
            d for d in rows if d.get("status") == "active"]

    def latest_metric_points(self, agent, names, per_metric_limit=200):
        self.calls.append(("latest_metric_points", agent, tuple(names)))
        wanted = set(names)
        return sorted([p for p in self.points.get(agent, [])
                       if p["metric"] in wanted],
                      key=lambda r: r["ts"], reverse=True)


def _live(name):
    """The module the code under test will import at CALL time.

    Other suites in the unit island swap backend modules in `sys.modules`, so
    a reference captured at this file's import can be a stale object the join
    no longer reads — patching it then does nothing (the reason
    `test_ent666_objective_join`'s composition tests fail in a full run).
    """
    import importlib
    return importlib.import_module(name)


@pytest.fixture
def fleet(monkeypatch):
    fake = _Fleet()
    monkeypatch.setattr(database_mod, "db", fake)
    monkeypatch.setattr(_live("database"), "db", fake)
    return fake


def _objective(owner=f"role:{ROLE}", supporting=None):
    obj, findings = svc.parse_objective({
        "schema_version": 1, "id": "q4-close-rate",
        "statement": "Lift close rate to 35% by the end of Q4.",
        "horizon": "quarter", "owner": owner,
        **({"supporting_agents": supporting} if supporting else {}),
        "metrics": [{"name": "close_rate", "direction": "up", "target": 35,
                     "by": "2026-12-31"}],
        "status": "active",
    }, path="canon/objectives/q4-close-rate.yaml")
    assert findings == [], findings
    return obj


# ===========================================================================
# The predicate
# ===========================================================================

def test_self_is_always_readable_and_needs_no_store(fleet):
    fleet.grants.clear()
    assert access.can_read_agent_metrics(READER, READER) is True


def test_a_grant_is_the_edge_and_it_is_directional(fleet):
    assert access.can_read_agent_metrics(READER, SERVER) is True
    assert access.can_read_agent_metrics(SERVER, READER) is False


@pytest.mark.parametrize("reader,target", [("", SERVER), (READER, ""),
                                           (None, SERVER)])
def test_an_empty_name_is_never_readable(fleet, reader, target):
    assert access.can_read_agent_metrics(reader, target) is False


# ===========================================================================
# The audit — once per (reader, target, route) per hour, never lost
# ===========================================================================

class _Redis:
    def __init__(self, fail=False):
        self.keys = {}
        self.fail = fail

    def exists(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return key in self.keys

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.keys[key] = (value, ex)


@pytest.fixture
def audit(monkeypatch):
    state = {"rows": [], "return": "event-1", "raise": None,
             "redis": _Redis()}

    async def _log(**kw):
        if state["raise"]:
            raise state["raise"]
        state["rows"].append(kw)
        return state["return"]

    monkeypatch.setattr(access.platform_audit_service, "log", _log)
    monkeypatch.setattr(access, "get_breaker_redis", lambda: state["redis"])
    return state


@pytest.mark.asyncio
async def test_a_cross_read_is_audited_with_the_reader_and_the_target(audit):
    await access.audit_cross_agent_read(READER, SERVER, "metrics")
    [row] = audit["rows"]
    assert row["event_action"] == "metrics_cross_agent_read"
    assert row["source"] == "api"  # required positional — omitting it drops the row
    assert row["target_id"] == SERVER
    assert row["actor_agent_name"] == READER
    assert row["details"] == {"reader_agent": READER, "target_agent": SERVER,
                              "route": "metrics"}


@pytest.mark.asyncio
async def test_a_polled_read_is_audited_once_per_hour_per_pair_and_route(audit):
    for _ in range(5):
        await access.audit_cross_agent_read(READER, SERVER, "metrics")
    await access.audit_cross_agent_read(READER, SERVER, "definitions")
    await access.audit_cross_agent_read(OTHER, SERVER, "metrics")
    assert [r["details"]["route"] for r in audit["rows"]] == [
        "metrics", "definitions", "metrics"]
    ttl = {v[1] for v in audit["redis"].keys.values()}
    assert ttl == {access.CROSS_READ_AUDIT_WINDOW} == {3600}


@pytest.mark.asyncio
async def test_a_failed_write_does_not_silence_auditing_for_an_hour(audit):
    audit["return"] = None  # `log` returns None when the row was not written
    await access.audit_cross_agent_read(READER, SERVER, "metrics")
    assert audit["redis"].keys == {}
    audit["return"] = "event-2"
    await access.audit_cross_agent_read(READER, SERVER, "metrics")
    assert len(audit["rows"]) == 2


@pytest.mark.asyncio
async def test_an_unreachable_marker_store_audits_anyway(audit):
    """A duplicate row is a smaller failure than a missing one."""
    audit["redis"] = _Redis(fail=True)
    await access.audit_cross_agent_read(READER, SERVER, "metrics")
    await access.audit_cross_agent_read(READER, SERVER, "metrics")
    assert len(audit["rows"]) == 2


@pytest.mark.asyncio
async def test_an_audit_failure_never_raises_into_the_read(audit):
    audit["raise"] = RuntimeError("audit store down")
    await access.audit_cross_agent_read(READER, SERVER, "metrics")  # no raise
    assert audit["redis"].keys == {}


# ===========================================================================
# resolve_served_metrics — whose number, through whose grant
# ===========================================================================

def _resolve(names=("close_rate",), can_view=lambda t: True, agent=READER):
    return svc.resolve_served_metrics(agent, list(names),
                                      can_view=can_view, now=NOW)


def test_one_granted_server_supplies_the_number(fleet):
    served = _resolve()
    assert served["close_rate"]["agent"] == SERVER
    assert served["close_rate"]["latest"]["latest"]["value"] == 30.0
    assert served["close_rate"]["latest"]["freshness"] == "fresh"


def test_no_viewer_predicate_fails_closed(fleet):
    """A caller that did not say who is looking resolves nothing, and pays
    no store read for it."""
    assert _resolve(can_view=None) == {}
    assert fleet.calls == []


def test_the_READERS_grant_counts_not_anyone_elses(fleet):
    """The owner holding a grant on the server does not let a supporting
    agent read through it (user ruling 2026-09-30)."""
    fleet.grants = {("owner-agent", SERVER)}
    assert _resolve() == {}


def test_a_server_the_viewer_cannot_see_is_not_served(fleet):
    assert _resolve(can_view=lambda t: t != SERVER) == {}
    assert not any(c[0] == "latest_metric_points" for c in fleet.calls)


def test_ambiguity_is_decided_BEFORE_the_viewer_filter(fleet):
    """Two granted agents declare it. A viewer who can see only one must get
    the same answer as one who sees both — not a number."""
    fleet.definitions[OTHER] = [_definition()]
    fleet.grants.add((READER, OTHER))
    assert _resolve() == {"close_rate": {"candidates": [OTHER, SERVER],
                                         "candidate_count": 2}}
    only_one = _resolve(can_view=lambda t: t == SERVER)["close_rate"]
    assert "agent" not in only_one                       # still ambiguous
    assert only_one == {"candidates": [SERVER], "candidate_count": 2}


def test_a_retired_declaration_on_the_server_does_not_serve(fleet):
    fleet.definitions[SERVER] = [_definition(status="retired")]
    assert _resolve() == {}


def test_only_the_names_asked_for_are_resolved(fleet):
    fleet.definitions[SERVER].append(_definition(name="pipeline"))
    assert set(_resolve(names=["pipeline"])) == {"pipeline"}
    assert _resolve(names=[]) == {}


# ===========================================================================
# join_objectives — the served row
# ===========================================================================

def _served_row(fleet, obj, *, role_id=ROLE):
    served = _resolve()
    joined = svc.join_objectives([obj], [], {}, agent_name=READER,
                                 role_id=role_id, served=served)
    return joined, joined["objectives"][0]["metrics"][0]


def test_a_supporting_agent_gets_the_actual_instead_of_declared_elsewhere(fleet):
    joined, row = _served_row(fleet, _objective(supporting=[READER]),
                              role_id=None)
    assert row["served_by"] == SERVER
    assert row["declared"] is False          # about THIS agent's registry
    assert row["declared_elsewhere"] is False
    assert (row["target"], row["actual"]) == (35, 30.0)
    assert row["gap"]["status"] == "behind"
    assert row["freshness"] == "fresh" and row["stale"] is False
    assert (row["unit"], row["direction"]) == ("%", "up_good")
    assert row["finding"] is None
    assert joined["summary"]["served_elsewhere"] == 1
    assert joined["summary"]["declared_elsewhere"] == 0
    assert joined["summary"]["undeclared"] == 0


def test_an_OWNED_objective_served_elsewhere_is_not_told_to_declare_it(fleet):
    """R41: the objective's owner is usually not the number's owner."""
    joined, row = _served_row(fleet, _objective())
    assert row["served_by"] == SERVER
    assert row["actual"] == 30.0
    assert not any(f["code"] == "metric_undeclared"
                   for f in joined["findings"])


def test_a_stale_served_number_is_still_stale(fleet):
    fleet.points[SERVER][0]["ts"] = _ago(3 * HOUR)
    _, row = _served_row(fleet, _objective())
    assert row["stale"] is True and row["freshness"] == "stale"


def test_an_ambiguous_metric_is_named_never_guessed(fleet):
    fleet.definitions[OTHER] = [_definition()]
    fleet.grants.add((READER, OTHER))
    joined, row = _served_row(fleet, _objective())
    assert row["served_by"] is None
    assert row["actual"] is None
    assert row["gap"] == {"status": "not_computable", "delta": None,
                          "reason": "served_by_ambiguous"}
    assert row["finding"]["code"] == "metric_served_ambiguously"
    assert OTHER in row["finding"]["message"] and SERVER in row["finding"]["message"]


def test_no_server_keeps_todays_declared_elsewhere_row(fleet):
    fleet.grants.clear()
    joined, row = _served_row(fleet, _objective(supporting=[READER]),
                              role_id=None)
    assert row["served_by"] is None
    assert row["declared_elsewhere"] is True
    assert row["gap"]["reason"] == "declared_elsewhere"
    assert "grant" in row["finding"]["message"]
    assert "ent#80" not in row["finding"]["message"]


def test_a_LOCAL_declaration_wins_over_a_server(fleet):
    """The join only looks elsewhere for names this agent does not declare."""
    obj = _objective()
    served = _resolve()
    joined = svc.join_objectives(
        [obj], [_definition()], {}, agent_name=READER, role_id=ROLE,
        served=served)
    row = joined["objectives"][0]["metrics"][0]
    assert row["served_by"] is None and row["declared"] is True


def test_an_ambiguity_finding_does_not_name_an_agent_the_viewer_cannot_see(fleet):
    fleet.definitions[OTHER] = [_definition()]
    fleet.grants.add((READER, OTHER))
    served = _resolve(can_view=lambda t: t == SERVER)
    joined = svc.join_objectives([_objective()], [], {}, agent_name=READER,
                                 role_id=ROLE, served=served)
    message = joined["objectives"][0]["metrics"][0]["finding"]["message"]
    assert SERVER in message
    assert OTHER not in message
    assert "1 you cannot access" in message


def test_the_model_carries_the_served_keys(fleet):
    joined, row = _served_row(fleet, _objective())
    assert set(models_mod.ObjectiveMetricRead.model_fields) == set(row)
    assert set(models_mod.ObjectiveJoinSummary.model_fields) == set(
        joined["summary"])
    empty = svc._empty(READER, NOW)
    assert set(models_mod.ObjectiveJoinSummary.model_fields) == set(
        empty["summary"])


# ===========================================================================
# read_objective_join — end to end, store + agent door stubbed
# ===========================================================================

TEMPLATE = """
name: sales-companion
x-role:
  role: revenue-lead
x-canon:
  clone_path: canon
"""

OBJECTIVE_YAML = """
schema_version: 1
id: q4-close-rate
statement: Lift close rate to 35% by the end of Q4.
horizon: quarter
owner: role:revenue-lead
metrics:
  - name: close_rate
    direction: up
    target: 35
status: active
"""


@pytest.fixture
def running(monkeypatch):
    async def _state(name):
        return "running"
    monkeypatch.setattr(docker_utils, "agent_container_state_async", _state)
    monkeypatch.setattr(_live("services.docker_utils"),
                        "agent_container_state_async", _state)
    import services
    monkeypatch.setattr(services, "docker_utils",
                        _live("services.docker_utils"), raising=False)


@pytest.mark.asyncio
async def test_the_whole_read_resolves_a_served_actual(fleet, running, monkeypatch):
    door = _door()
    result = await svc.read_objective_join(
        READER, now=NOW, client=door, can_view=lambda t: True)
    row = result["objectives"][0]["metrics"][0]
    assert (row["served_by"], row["actual"]) == (SERVER, 30.0)
    assert result["summary"]["served_elsewhere"] == 1


@pytest.mark.asyncio
async def test_the_whole_read_without_a_viewer_resolves_nothing(fleet, running,
                                                                monkeypatch):
    door = _door()
    result = await svc.read_objective_join(READER, now=NOW, client=door)
    row = result["objectives"][0]["metrics"][0]
    assert row["served_by"] is None and row["actual"] is None
    assert not any(c[1] == SERVER for c in fleet.calls)


class _Response:
    def __init__(self, status, text="", body=None):
        self.status_code = status
        self.text = text
        self._body = body

    def json(self):
        return self._body


class _Door:
    """The agent door, answering the two paths the join asks for: the
    objectives listing and a file download."""

    def __init__(self, files):
        self.files = files

    async def get(self, path, timeout=None, **kwargs):
        from urllib.parse import unquote
        if path.startswith("/api/files?"):
            names = sorted(p.rsplit("/", 1)[-1] for p in self.files
                           if p.startswith("canon/objectives/"))
            return _Response(200, body={"tree": [
                {"name": n, "type": "file"} for n in names]})
        wanted = unquote(path.split("path=", 1)[-1])
        if wanted not in self.files:
            return _Response(404)
        return _Response(200, text=self.files[wanted])


def _door(monkeypatch=None):
    return _Door({
        "template.yaml": TEMPLATE,
        "canon/objectives/q4-close-rate.yaml": OBJECTIVE_YAML,
    })


# ===========================================================================
# The objectives route — viewer predicate + audit
# ===========================================================================

def test_the_objectives_route_passes_a_viewer_predicate_and_audits_servers(
        monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.agent_files as route_mod
    from dependencies import get_authorized_agent_by_name, get_current_user
    from models import User

    fleet = _Fleet()
    access_calls = []
    fleet.can_user_access_agent = (
        lambda user, agent: access_calls.append((user, agent)) or agent != OTHER)
    monkeypatch.setattr(route_mod, "db", fleet)
    monkeypatch.setattr(route_mod.rate_limiter, "enforce", lambda *a, **k: None)

    seen = {}
    joined = svc.join_objectives(
        [_objective()], [], {}, agent_name=READER, role_id=ROLE,
        served={"close_rate": {"agent": SERVER, "definition": _definition(),
                               "latest": {}}})

    async def _read(agent_name, **kwargs):
        seen.update(kwargs)
        body = svc._empty(agent_name, NOW)
        body.update(objectives=joined["objectives"],
                    findings=joined["findings"], summary=joined["summary"])
        return body

    audits = []

    async def _audit(reader, target, route, **kw):
        audits.append((reader, target, route))

    monkeypatch.setattr(route_mod.objective_join_service,
                        "read_objective_join", _read)
    monkeypatch.setattr(route_mod.metric_access_service,
                        "audit_cross_agent_read", _audit)

    app = FastAPI()
    app.include_router(route_mod.router)
    human = User(id=1, username="operator", email="op@example.com", role="user")
    for route in app.routes:
        for dep in getattr(getattr(route, "dependant", None),
                           "dependencies", []) or []:
            name = getattr(dep.call, "__name__", "")
            if name == "get_authorized_agent_by_name":
                app.dependency_overrides[dep.call] = lambda agent_name: agent_name
            if name == "get_current_user":
                app.dependency_overrides[dep.call] = lambda: human
    app.dependency_overrides[get_current_user] = lambda: human
    app.dependency_overrides[get_authorized_agent_by_name] = (
        lambda agent_name: agent_name)

    response = TestClient(app).get(f"/api/agents/{READER}/objectives")
    assert response.status_code == 200
    assert response.json()["objectives"][0]["metrics"][0]["served_by"] == SERVER

    can_view = seen["can_view"]
    assert can_view(SERVER) is True and can_view(OTHER) is False
    assert access_calls[-1] == ("operator", OTHER)
    assert audits == [(READER, SERVER, "objectives")]

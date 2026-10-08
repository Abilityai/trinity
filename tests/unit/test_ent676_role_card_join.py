"""trinity-enterprise#676 — the role card reads the objective ↔ metric join.

The card used to compute its own join and its own 30-day metric staleness. It
now calls `objective_join_service.read_objective_join` and serves a slim client
PROJECTION of it, and its objective read draws on the same per-agent budget as
`GET /api/agents/{name}/objectives`. Four things are worth proving, and each
is proved by running the code rather than reading it:

* **the projection is an allowlist** — the key sets are pinned as literals, so
  a field added to the join tomorrow does not reach a Workspace client until
  someone decides it may (the 2026-09-23 "whole row minus the withheld fields"
  lesson), and the operator's remediation sentences, canon paths and
  `owner: role:<id>` never cross;
* **the wiring is real** — `build_role_card` is driven through the REAL join
  with the agent door and the store faked, so the numbers on the card are the
  store's and the staleness is the 2× cadence rule, not a second one;
* **every way of showing no objectives is named** — a dead door, a slow one, a
  directory whose files will not read, an exhausted budget: none of them may
  look like "this agent has none";
* **one budget, two doors** — exhausting the operator door refuses the card's
  objectives (never the card), a viewer's own cap is spent first, and neither
  door can be starved by a name the roster gate has not validated.

Related flow: docs/memory/feature-flows/workspace-role-card.md
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest

pytestmark = pytest.mark.unit

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

pytest.importorskip("fastapi", reason="backend venv required")

import routers.agent_files as operator_mod  # noqa: E402
from client_portal import models as portal_models  # noqa: E402
from client_portal import role_card as rc  # noqa: E402
from models import User  # noqa: E402
from services import objective_join_service as svc  # noqa: E402

AGENT = "sales-companion"
ROLE = "sales-lead"
VIEWER = "gary@example.com"

#: What a Workspace client may see of one joined metric. A literal on purpose:
#: a key the join grows later must turn this red before it reaches a client.
METRIC_KEYS = {"name", "type", "unit", "target", "actual", "last_point_at",
               "stale", "freshness", "gap", "finding"}
OBJECTIVE_KEYS = {"id", "statement", "horizon", "status", "owned", "metrics"}


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _definition(name, **overrides):
    d = {
        "name": name, "type": "gauge", "label": name, "description": None,
        "unit": None, "direction": "neutral", "aggregation": "last",
        "cadence": "1h", "cadence_seconds": 3600,
        "warning_threshold": None, "critical_threshold": None,
        "values": None, "dimensions": [], "status": "active",
        "retired_at": None, "type_conflict": None,
    }
    d.update(overrides)
    return d


def _point(metric, value, ts):
    numeric = value if isinstance(value, (int, float)) else None
    return {"metric": metric, "ts": ts, "value_numeric": numeric,
            "value_text": None if numeric is not None else value, "dims": None}


# ---------------------------------------------------------------------------
# The agent's files, as the door serves them
# ---------------------------------------------------------------------------

TEMPLATE = """
name: sales-companion
x-role:
  role: sales-lead
  status: calibrating
x-canon:
  clone_path: canon
"""
ROLE_FILE = """
id: sales-lead
title: Sales Lead
mission: Close ICP-fit pipeline.
status: active
review_by: 2099-12-01
"""
OBJ_OWNED = """
id: q4-close-rate
statement: Raise close rate this quarter.
horizon: quarter
owner: role:sales-lead
metrics:
  - {name: close_rate, direction: up, target: 35, by: 2026-12-31}
  - {name: reply_rate, direction: up, target: 20}
  - {name: deal_size, direction: up, target: 10}
  - {name: demo_count, direction: up, target: 8}
  - {name: ghost_metric, direction: up, target: 1}
status: active
"""
OBJ_DONE = """
id: q3-pipeline
statement: Build the Q3 pipeline.
owner: role:sales-lead
metrics: [{name: close_rate, direction: up, target: 20}]
status: achieved
"""
OBJ_SUPPORTED = """
id: q4-icp-demand
statement: Raise ICP-fit inbound demand.
owner: role:marketing-lead
supporting_agents: [sales-companion]
metrics: [{name: icp_fit_rate, direction: up, target: 65}]
"""
OBJ_FOREIGN = """
id: burn
owner: role:cfo
metrics: [{name: burn, target: 1}]
"""


def _files(**extra):
    files = {
        "template.yaml": TEMPLATE,
        "canon/roles/sales-lead.yaml": ROLE_FILE,
        "canon/objectives/q4-close-rate.yaml": OBJ_OWNED,
        "canon/objectives/q3-pipeline.yaml": OBJ_DONE,
        "canon/objectives/q4-icp-demand.yaml": OBJ_SUPPORTED,
        "canon/objectives/burn.yaml": OBJ_FOREIGN,
    }
    files.update(extra)
    return files


class _Response:
    def __init__(self, status_code, text="", body=None):
        self.status_code = status_code
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Door:
    """The agent door. `read_file` is what the card uses for the template and
    the role file; `get` is what the join uses for the listing and each
    objective. Both are recorded, so a test can say which door a read used."""

    def __init__(self, files=None, *, die_after=None, list_status=200,
                 unreadable=()):
        self.files = _files() if files is None else files
        self.reads = []
        self.gets = []
        self.die_after = die_after
        self.list_status = list_status
        self.unreadable = set(unreadable)

    async def read_file(self, path, timeout=30.0):
        self.reads.append(path)
        if path not in self.files:
            return {"success": True, "content": None, "not_found": True}
        return {"success": True, "content": self.files[path]}

    async def get(self, path, timeout=None, **kwargs):
        from services.agent_client.client import AgentNotReachableError
        self.gets.append(path)
        if self.die_after is not None and len(self.gets) > self.die_after:
            raise AgentNotReachableError("agent-server is gone")
        if path.startswith("/api/files?path=/home/developer/"):
            if self.list_status != 200:
                return _Response(self.list_status)
            directory = path.split("/home/developer/", 1)[1]
            names = sorted(p.rsplit("/", 1)[-1] for p in self.files
                           if p.startswith(directory + "/"))
            return _Response(200, body={"tree": [
                {"name": n, "type": "file"} for n in names]})
        if path.startswith("/api/files/download?path="):
            wanted = unquote(path.split("path=", 1)[1])
            if wanted in self.unreadable:
                return _Response(500)
            if wanted in self.files:
                return _Response(200, text=self.files[wanted])
        return _Response(404)


class Store:
    """The registry + the point store — a list, not a database."""

    def __init__(self, definitions=None, points=None, *, broken=False):
        self.definitions = definitions if definitions is not None else [
            _definition("close_rate", type="percentage", unit="%"),
            _definition("reply_rate", type="percentage", unit="%"),
            _definition("deal_size", cadence=None, cadence_seconds=None,
                        unit="k"),
            _definition("demo_count", type="counter"),
        ]
        self.points = points if points is not None else [
            _point("close_rate", 30.0, _ago(minutes=1)),    # fresh, behind 35
            _point("reply_rate", 25.0, _ago(hours=3)),      # 3h on a 1h cadence
            _point("deal_size", 12.0, _ago(days=40)),       # no cadence declared
        ]
        self.broken = broken
        self.calls = []

    def list_metric_definitions(self, agent, include_retired=False):
        self.calls.append("list_metric_definitions")
        if self.broken:
            from sqlalchemy.exc import OperationalError
            raise OperationalError("select", {}, Exception("store is gone"))
        return list(self.definitions)

    def latest_metric_points(self, agent, names, per_metric_limit=200):
        self.calls.append("latest_metric_points")
        wanted = set(names)
        return sorted([p for p in self.points if p["metric"] in wanted],
                      key=lambda r: r["ts"], reverse=True)


@pytest.fixture
def build(monkeypatch):
    """`build(door, store)` → the card, through the REAL join."""
    import importlib

    from services import docker_utils

    # `role_card` resolves `from services.agent_client import get_agent_client`
    # at call time, i.e. through `sys.modules` — reach the same object.
    agent_client_mod = importlib.import_module("services.agent_client")

    async def _running(name):
        return "running"

    monkeypatch.setattr(docker_utils, "agent_container_state_async", _running)
    stamp = {"status": "calibrating", "changed_at": "2026-09-21T10:00:00Z",
             "changed_by": "owner@example.com"}
    monkeypatch.setattr(rc, "_readiness_stamp", lambda agent: stamp)
    monkeypatch.setattr(rc, "_walkthrough", lambda *a: {
        "asks": 0, "target": 10, "rated_down": 0, "unavailable": False})
    monkeypatch.setattr(rc, "_is_owner", lambda *a: True)
    monkeypatch.setattr(rc, "_brief_held", lambda agent, stamp: False)

    def _build(door=None, store=None, *, admitted=True, asked=None):
        door = door or Door()
        store = store or Store()
        asked = asked if asked is not None else []

        def admit():
            asked.append(True)
            return admitted

        monkeypatch.setattr(agent_client_mod, "get_agent_client",
                            lambda name: door)
        # The join (and `latest_by_metric` under it) resolves `database.db` at
        # CALL time, through `sys.modules` — so that entry is the seam that
        # reaches it (patch where the code resolves, learnings 2026-08-10).
        monkeypatch.setattr(sys.modules["database"], "db", store)
        card = asyncio.run(rc.build_role_card(
            AGENT, VIEWER, is_platform=True, admit_objectives=admit))
        return card, door, store

    return _build


def _rows(card):
    return {m["name"]: m for o in card["objectives"] for m in o["metrics"]}


# ===========================================================================
# The projection — pure, driven by the join's own output
# ===========================================================================

def _joined(metrics, definitions, latest, *, owned=True):
    objective, _ = svc.parse_objective({
        "id": "q4-close-rate", "statement": "Raise close rate.",
        "horizon": "quarter", "review_by": "2026-10-15",
        "owner": f"role:{ROLE}" if owned else "role:marketing-lead",
        "supporting_agents": [AGENT], "metrics": metrics, "status": "active",
    }, path="canon/objectives/q4-close-rate.yaml")
    return svc.join_objectives([objective], definitions, latest,
                               agent_name=AGENT, role_id=ROLE)


def _tile(name, value, **overrides):
    entry = {
        **_definition(name),
        "latest": ({"value": value, "ts": "2026-09-22T11:59:00.000000Z",
                    "dims": None} if value is not None else None),
        "last_point_at": ("2026-09-22T11:59:00.000000Z"
                          if value is not None else None),
        "stale": False, "freshness": "fresh", "stale_after": None,
        "series_count": 1,
    }
    entry.update(overrides)
    return {name: entry}


def test_a_metric_crosses_as_exactly_the_client_fields():
    joined = _joined(
        [{"name": "close_rate", "direction": "up", "target": 35,
          "by": "2026-12-31"}],
        [_definition("close_rate", type="percentage", unit="%")],
        _tile("close_rate", 30.0))
    row = rc.portal_metric(joined["objectives"][0]["metrics"][0])

    assert set(row) == METRIC_KEYS
    assert row == {
        "name": "close_rate", "type": "percentage", "unit": "%",
        "target": 35, "actual": 30.0,
        "last_point_at": "2026-09-22T11:59:00.000000Z",
        "stale": False, "freshness": "fresh",
        "gap": {"status": "behind"}, "finding": None,
    }


def test_an_objective_crosses_as_exactly_the_client_fields():
    joined = _joined([{"name": "close_rate", "direction": "up", "target": 35}],
                     [_definition("close_rate")], _tile("close_rate", 30.0))
    source = joined["objectives"][0]
    obj = rc.portal_objective(source)

    assert set(obj) == OBJECTIVE_KEYS
    assert (obj["id"], obj["owned"], obj["status"]) == (
        "q4-close-rate", True, "active")
    # The join really carried what the projection withholds — otherwise the
    # absence below proves nothing.
    assert source["owner"] == f"role:{ROLE}" and source["path"]
    assert source["review_by"] == "2026-10-15"
    blob = json.dumps(obj)
    assert "role:" not in blob and "canon/objectives" not in blob
    assert "2026-10-15" not in blob


def test_a_text_target_is_shown_as_the_author_wrote_it():
    joined = _joined([{"name": "close_rate", "target": "the Q4 plan"}],
                     [_definition("close_rate")], _tile("close_rate", 30.0))
    row = rc.portal_metric(joined["objectives"][0]["metrics"][0])
    assert row["target"] == "the Q4 plan"
    assert row["gap"] == {"status": "not_computable"}


def test_agent_written_text_is_bounded_before_it_reaches_a_client():
    joined = _joined([{"name": "stage", "target": "won"}],
                     [_definition("stage", type="status")],
                     _tile("stage", "x" * 1000))
    row = rc.portal_metric(joined["objectives"][0]["metrics"][0])
    assert row["actual"] == "x" * 64


@pytest.mark.parametrize("metrics,definitions,latest,owned,code", [
    ([{"name": "ghost", "target": 1}], [], {}, True, "metric_undeclared"),
    ([{"name": "ghost", "target": 1}], [], {}, False,
     "metric_not_declared_here"),
    ([{"name": "old", "target": 1}],
     [_definition("old", status="retired", retired_at="2026-09-01T00:00:00Z")],
     {}, True, "metric_retired"),
    ([{"name": "close_rate", "direction": "down", "target": 35}],
     [_definition("close_rate", direction="up_good")],
     _tile("close_rate", 30.0), True, "direction_mismatch"),
    ([{"name": "close_rate", "target": 35}], [_definition("close_rate")],
     _tile("close_rate", 30.0), True, "direction_undeclared"),
], ids=["undeclared", "declared-elsewhere", "retired", "mismatch",
        "no-direction"])
def test_a_finding_crosses_as_its_code_and_never_as_its_sentence(
        metrics, definitions, latest, owned, code):
    joined = _joined(metrics, definitions, latest, owned=owned)
    source = joined["objectives"][0]["metrics"][0]
    row = rc.portal_metric(source)

    assert row["finding"] == {"code": code}
    # Non-vacuous: the operator's row carries a sentence, and it is not here.
    assert source["finding"]["message"]
    assert source["finding"]["message"] not in json.dumps(row)


def _join_result(**overrides):
    base = {"objectives": [], "findings": [], "unavailable": None,
            "source": {"objectives_dir": "read", "objectives_unscanned": 0}}
    base.update(overrides)
    return base


@pytest.mark.parametrize("join,expected", [
    (_join_result(objectives=[{"id": "x"}],
                  findings=[{"code": "objective_unreadable"}]), None),
    (_join_result(), None),
    (_join_result(source={"objectives_dir": "absent"}), None),
    (_join_result(unavailable="agent_unreachable"), "agent_unreachable"),
    (_join_result(unavailable="agent_stopped"), "agent_unreachable"),
    (_join_result(source={"objectives_dir": "timeout"}), "objectives_timeout"),
    (_join_result(source={"objectives_dir": "unreadable"}),
     "objectives_unreadable"),
    (_join_result(findings=[{"code": "objective_unreadable"}]),
     "objectives_unreadable"),
    (_join_result(findings=[{"code": "objective_invalid"}]),
     "objectives_incomplete"),
    (_join_result(findings=[{"code": "objective_file_skipped"}]),
     "objectives_incomplete"),
    (_join_result(source={"objectives_dir": "read",
                          "objectives_unscanned": 7}),
     "objectives_incomplete"),
], ids=["some-joined", "none-and-clean", "no-directory", "door-died",
        "stopped-mid-read", "too-slow", "unlistable", "every-file-unreadable",
        "every-file-invalid", "names-refused", "beyond-the-scan"])
def test_no_objectives_is_named_unless_it_is_a_real_empty(join, expected):
    assert rc.objectives_error(join) == expected


@pytest.mark.parametrize("join,expected", [
    (_join_result(objectives=[{"id": "x"}]), False),
    (_join_result(objectives=[{"id": "x"}],
                  findings=[{"code": "metric_undeclared"}]), False),
    (_join_result(objectives=[{"id": "x"}],
                  findings=[{"code": "objective_unreadable"}]), True),
    (_join_result(objectives=[{"id": "x"}],
                  findings=[{"code": "objective_invalid"}]), True),
    (_join_result(objectives=[{"id": "x"}],
                  findings=[{"code": "objective_file_skipped"}]), True),
    (_join_result(objectives=[{"id": "x"}],
                  source={"objectives_dir": "read", "objectives_unscanned": 3}), True),
    # Zero objectives is objectives_error's case, never "partial".
    (_join_result(findings=[{"code": "objective_unreadable"}]), False),
], ids=["complete", "row-finding-only", "file-unreadable", "file-invalid",
        "name-refused", "beyond-the-scan", "none-joined"])
def test_a_list_missing_some_files_is_marked_partial(join, expected):
    assert rc.objectives_partial(join) is expected


def test_card_level_findings_cross_as_distinct_codes_only():
    join = _join_result(findings=[
        {"code": "objective_invalid", "path": "canon/objectives/cfo.yaml",
         "objective_id": "cfo", "message": "canon/objectives/cfo.yaml is …"},
        {"code": "metric_undeclared", "metric": "ghost", "message": "…"},
        {"code": "objective_invalid", "path": "canon/objectives/b.yaml",
         "message": "…"},
    ])
    assert rc.finding_codes(join) == ["objective_invalid", "metric_undeclared"]


# ===========================================================================
# The wiring — build_role_card through the real join
# ===========================================================================

def test_the_card_shows_the_stores_numbers_under_the_one_stale_rule(build):
    card, _door, _store = build()
    rows = _rows(card)

    # Fresh, and behind its target.
    assert rows["close_rate"]["actual"] == 30.0
    assert rows["close_rate"]["freshness"] == "fresh"
    assert rows["close_rate"]["gap"] == {"status": "behind"}
    assert (rows["close_rate"]["type"], rows["close_rate"]["unit"]) == (
        "percentage", "%")
    # Three hours old on a one-hour cadence: stale by the 2× rule. The card's
    # old 30-day bound would have called this current.
    assert rows["reply_rate"]["stale"] is True
    assert rows["reply_rate"]["freshness"] == "stale"
    assert rows["reply_rate"]["actual"] == 25.0        # last value still shown
    assert rows["reply_rate"]["gap"] == {"status": "ahead"}
    # Forty days old with NO cadence: never stale. The old rule said stale.
    assert rows["deal_size"]["stale"] is False
    assert rows["deal_size"]["freshness"] == "no_cadence"
    assert rows["deal_size"]["last_point_at"]
    # Declared, never recorded: not late, not started.
    assert rows["demo_count"]["freshness"] == "no_points"
    assert rows["demo_count"]["actual"] is None
    assert rows["demo_count"]["stale"] is False


def test_an_undeclared_metric_is_a_code_with_no_number(build):
    card, _door, _store = build()
    rows = _rows(card)
    assert rows["ghost_metric"]["finding"] == {"code": "metric_undeclared"}
    assert rows["ghost_metric"]["actual"] is None
    # A supporting agent is told something different — it cannot declare it.
    assert rows["icp_fit_rate"]["finding"] == {
        "code": "metric_not_declared_here"}


def test_the_card_carries_the_objectives_this_agent_answers_for(build):
    card, _door, _store = build()
    objs = {o["id"]: o for o in card["objectives"]}
    # `burn` is the CFO's; `q3-pipeline` is achieved — the join drops both.
    assert set(objs) == {"q4-close-rate", "q4-icp-demand"}
    assert objs["q4-close-rate"]["owned"] is True
    assert objs["q4-icp-demand"]["owned"] is False
    assert card["objectives_error"] is None
    assert all(set(o) == OBJECTIVE_KEYS for o in card["objectives"])
    assert all(set(m) == METRIC_KEYS for m in _rows(card).values())


def test_operator_text_never_reaches_the_card(build):
    card, door, store = build()
    # The operator's read of the SAME files, for the sentinels.
    operator = asyncio.run(svc.read_objective_join(AGENT, client=door))
    sentences = [f["message"] for f in operator["findings"]]
    assert sentences and "refresh_metric_definitions" in json.dumps(operator)
    assert "canon/objectives/" in json.dumps(operator)

    blob = json.dumps(card)
    for sentence in sentences:
        assert sentence not in blob
    assert "refresh_metric_definitions" not in blob
    assert "canon/objectives/" not in blob
    assert "role:sales-lead" not in blob and "role:marketing-lead" not in blob
    assert card["finding_codes"] == ["metric_undeclared",
                                    "metric_not_declared_here"]


def test_the_template_is_read_once_and_handed_to_the_join(build):
    card, door, _store = build()
    assert card["objectives"]
    assert door.reads.count("template.yaml") == 1
    # The join reads through `get`; had it not been given the template it
    # would have downloaded it here.
    assert not [g for g in door.gets if "template.yaml" in g]
    # …and it read the objectives through the SAME client the card holds.
    assert any("canon%2Fobjectives%2F" in g for g in door.gets)


def test_a_door_that_dies_mid_read_is_named_and_the_card_survives(build):
    card, door, _store = build(Door(die_after=1))   # the listing answers, then nothing
    assert card["objectives"] == []
    assert card["objectives_error"] == "agent_unreachable"
    assert card["role"]["title"] == "Sales Lead"
    assert card["readiness"]["status"] == "calibrating"
    assert card["can_flip_readiness"] is True


def test_a_store_outage_is_named_and_the_card_survives(build):
    card, _door, store = build(store=Store(broken=True))
    assert store.calls == ["list_metric_definitions"]      # it really was reached
    assert card["objectives"] == []
    assert card["objectives_error"] == "objectives_unreadable"
    assert card["role"]["title"] == "Sales Lead"
    assert card["readiness"]["status"] == "calibrating"


def test_a_directory_whose_files_will_not_read_is_not_an_empty_list(build):
    door = Door(unreadable={
        "canon/objectives/q4-close-rate.yaml",
        "canon/objectives/q3-pipeline.yaml",
        "canon/objectives/q4-icp-demand.yaml",
        "canon/objectives/burn.yaml",
    })
    card, _door, _store = build(door)
    assert card["objectives"] == []
    assert card["objectives_error"] == "objectives_unreadable"
    assert card["finding_codes"] == ["objective_unreadable"]


def test_one_unreadable_file_beside_a_joined_objective_marks_the_list_partial(build):
    card, _door, _store = build(Door(unreadable={"canon/objectives/q4-icp-demand.yaml"}))
    assert [o["id"] for o in card["objectives"]] == ["q4-close-rate"]
    assert card["objectives_partial"] is True
    assert card["objectives_error"] is None


def test_a_clean_read_is_not_partial(build):
    card, _door, _store = build()
    assert card["objectives"] and card["objectives_partial"] is False


def test_an_agent_with_no_objective_files_is_a_real_empty(build):
    card, _door, _store = build(Door({
        "template.yaml": TEMPLATE, "canon/roles/sales-lead.yaml": ROLE_FILE}))
    assert card["objectives"] == []
    assert card["objectives_error"] is None


def test_a_refused_read_leaves_out_the_objectives_and_nothing_else(build):
    card, door, store = build(admitted=False)
    assert card["objectives"] == []
    assert card["objectives_error"] == "objectives_rate_limited"
    # No fan-out and no store read happened — that is what the budget is for.
    assert door.gets == [] and store.calls == []
    # Everything that is not an objective still answers.
    assert card["role"]["title"] == "Sales Lead"
    assert card["readiness"]["status"] == "calibrating"
    assert card["can_flip_readiness"] is True


def test_a_role_file_that_failed_reads_no_objectives(build):
    card, door, _store = build(Door({"template.yaml": TEMPLATE}))
    assert card["role"]["error"] == "role_file_not_found"
    assert door.gets == []
    assert card["objectives_error"] is None


@pytest.mark.parametrize("files,state", [
    ({"template.yaml": "name: plain\n"}, "running"),            # no role
    ({"template.yaml": TEMPLATE}, "running"),                     # role file missing
    ({"template.yaml": TEMPLATE.replace("role: sales-lead", "role: ../x")}, "running"),
    (None, "stopped"),
], ids=["no-role", "role-file-missing", "role-id-invalid", "stopped"])
def test_a_card_that_reads_no_objectives_spends_no_budget(build, monkeypatch,
                                                          files, state):
    """The budget bounds the container fan-out, so only a card that is about
    to do one may spend it — not every Info-tab open."""
    from services import docker_utils

    async def _state(name):
        return state
    asked = []
    door = Door(files) if files is not None else Door()
    if state != "running":
        # `build` installs a running agent; override it after, for this case.
        monkeypatch.setattr(docker_utils, "agent_container_state_async", _state)
    build(door, asked=asked)
    assert asked == []


def test_a_card_that_reads_objectives_asks_once(build):
    asked = []
    card, _door, _store = build(asked=asked)
    assert card["objectives"] and asked == [True]


class GrantingStore(Store):
    """The store, plus an `agent_permissions` grant on another agent that DOES
    declare the metric this agent's objective names but does not declare —
    the ent#727 cross-agent case."""

    OTHER = "revenue-agent"

    def __init__(self):
        super().__init__()
        self.grant_reads = []

    def get_permitted_agents(self, agent):
        self.grant_reads.append(agent)
        return [self.OTHER]

    def is_agent_permitted(self, reader, target):
        return target == self.OTHER

    def list_metric_definitions(self, agent, include_retired=False):
        if agent == self.OTHER:
            return [_definition("ghost_metric")]
        return super().list_metric_definitions(agent, include_retired)

    def latest_metric_points(self, agent, names, per_metric_limit=200):
        if agent == self.OTHER:
            return [_point("ghost_metric", 99.0, _ago(minutes=1))]
        return super().latest_metric_points(agent, names, per_metric_limit)


def test_the_card_never_resolves_another_agents_metric(build):
    """ent#727 lets the operator door read a metric from an agent this one
    holds a grant on, but only through a `can_view` that says who is looking.
    The Workspace card passes none, so the join's fail-closed default holds:
    a viewer on a companion's roster never sees a number served by another
    agent they may not be rostered on."""
    store = GrantingStore()
    card, door, _ = build(store=store)
    row = _rows(card)["ghost_metric"]
    assert row["actual"] is None
    assert row["finding"] == {"code": "metric_undeclared"}
    assert "served_by" not in row
    assert store.grant_reads == []          # resolution was never attempted

    # Non-vacuous: the same files and store DO resolve it when a viewer is named.
    operator = asyncio.run(svc.read_objective_join(
        AGENT, client=door, can_view=lambda target: True))
    served = {m["name"]: m for o in operator["objectives"] for m in o["metrics"]}
    assert served["ghost_metric"]["served_by"] == GrantingStore.OTHER
    assert served["ghost_metric"]["actual"] == 99.0


def test_the_response_model_carries_every_key_the_card_produces(build):
    """`response_model` is an allowlist: a key the builder emits and the model
    lacks is dropped from every response without an error."""
    card, _door, _store = build()
    model = portal_models.PortalRoleCard.model_validate(card)
    dumped = model.model_dump()

    assert set(card) <= set(portal_models.PortalRoleCard.model_fields)
    assert {k: dumped[k] for k in card} == card
    assert set(portal_models.PortalRoleMetric.model_fields) == METRIC_KEYS
    assert set(portal_models.PortalRoleObjective.model_fields) == OBJECTIVE_KEYS
    # The early-return shapes validate too.
    refused, _d, _s = build(admitted=False)
    assert portal_models.PortalRoleCard.model_validate(
        refused).objectives_error == "objectives_rate_limited"


# ===========================================================================
# One budget, two doors
# ===========================================================================

def _principal(email=VIEWER):
    from client_portal.portal_auth import PortalPrincipal
    return PortalPrincipal(email=email, is_platform=False)


@pytest.fixture
def doors(monkeypatch):
    """Both doors over the REAL limiter, on its in-process path.

    Forcing Redis off is deliberate (the ent#287 harness): the window becomes
    process-local and deterministic, and `_check_inprocess` is production code
    with the same accept/reject boundary.
    """
    from client_portal import router as portal_router
    from client_portal import service
    from services import objectives_read_budget as budget
    from services import rate_limiter

    # The operator route must hold the same budget module the portal route
    # resolves, or the two doors would be two buckets that merely share a name.
    assert operator_mod.objectives_read_budget is budget
    # The expectations below are arithmetic on the limit; pin it so an
    # environment that tunes OBJECTIVES_READ_RATE_LIMIT cannot move them.
    monkeypatch.setattr(budget, "OBJECTIVES_READ_RATE_LIMIT", 60)
    for limiter in {id(m): m for m in (rate_limiter, budget.rate_limiter)}.values():
        monkeypatch.setattr(limiter, "_get_redis", lambda: None)
        limiter.clear_inprocess()

    monkeypatch.setattr(
        service, "agent_on_roster",
        lambda agent_name, email, include_owned=False:
            not agent_name.startswith("off-"))

    cards = []

    async def _build(agent_name, email, *, is_platform, admit_objectives):
        # The real builder asks once, after the role file; so does this one.
        cards.append((agent_name, email, admit_objectives()))
        return {"agent_name": agent_name, "role": None}

    monkeypatch.setattr(portal_router.role_card, "build_role_card", _build)

    async def _join(agent_name, **kwargs):
        return svc._empty(agent_name, datetime.now(timezone.utc))

    monkeypatch.setattr(operator_mod.objective_join_service,
                        "read_objective_join", _join)

    class Doors:
        limit = budget.OBJECTIVES_READ_RATE_LIMIT
        viewer_limit = portal_router._role_objectives_viewer_limit()
        seen = cards
        limiter = budget.rate_limiter

        @staticmethod
        def card(agent=AGENT, email=VIEWER):
            asyncio.run(portal_router.portal_agent_role(
                agent, principal=_principal(email)))
            return cards[-1][2]          # was the objective read admitted?

        @staticmethod
        def operator(agent=AGENT):
            # `request` feeds only ent#727's cross-read audit, which fires for a
            # served metric; the stubbed join returns none.
            return asyncio.run(operator_mod.get_agent_objectives(
                agent, current_user=User(id=1, username="operator",
                                         email="op@example.com", role="user"),
                request=None))

    try:
        yield Doors
    finally:
        budget.rate_limiter.clear_inprocess()
        rate_limiter.clear_inprocess()


def _raises(status, fn, *args, **kwargs):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    assert exc.value.status_code == status
    return exc.value


def test_a_card_read_is_admitted_inside_the_budget(doors):
    assert doors.card() is True


def test_the_operator_door_can_empty_the_budget_and_the_card_still_answers(doors):
    for _ in range(doors.limit):
        doors.operator()
    refused = _raises(429, doors.operator)
    assert int(refused.headers["Retry-After"]) > 0

    # The card is never refused — only its objectives are.
    assert doors.card() is False
    assert doors.seen[-1] == (AGENT, VIEWER, False)


def test_card_reads_spend_the_same_budget_the_operator_door_draws_on(doors):
    viewers = [f"viewer{i}@example.com"
               for i in range(doors.limit // doors.viewer_limit)]
    for email in viewers:
        for _ in range(doors.viewer_limit):
            assert doors.card(email=email) is True

    _raises(429, doors.operator)


def test_one_viewer_cannot_spend_the_whole_budget(doors):
    for _ in range(doors.viewer_limit):
        assert doors.card() is True
    # Past their own cap the viewer is refused objectives…
    for _ in range(doors.limit):
        assert doors.card() is False
    # …another viewer is not…
    assert doors.card(email="carol@example.com") is True
    # …and none of those refused reads was charged to the shared budget: the
    # operator door still has everything the two viewers did not spend.
    for _ in range(doors.limit - doors.viewer_limit - 1):
        doors.operator()
    _raises(429, doors.operator)


def test_the_viewer_cap_is_a_third_of_the_limit_whatever_the_operator_sets(
        doors, monkeypatch):
    """A fixed cap would let one viewer empty the budget the day an operator
    lowered OBJECTIVES_READ_RATE_LIMIT to it; the cap moves with the limit."""
    from services import objectives_read_budget as budget
    monkeypatch.setattr(budget, "OBJECTIVES_READ_RATE_LIMIT", 12)

    for _ in range(4):                    # 12 // 3
        assert doors.card() is True
    assert doors.card() is False          # the viewer's own cap
    # The operator door still has the other two thirds.
    for _ in range(12 - 4):
        doors.operator()
    _raises(429, doors.operator)


def test_the_budget_is_per_agent(doors):
    for _ in range(doors.limit):
        doors.operator()
    assert doors.card() is False
    assert doors.card(agent="other-agent") is True


def test_an_off_roster_name_is_a_404_before_any_limiter_key_is_minted(doors):
    _raises(404, doors.card, agent="off-roster-agent")
    assert doors.seen == []
    assert not [k for k in doors.limiter._inprocess_buckets
                if "off-roster-agent" in k]


# ===========================================================================
# Guards on the shape of the cut-over
# ===========================================================================

def test_the_card_module_does_not_import_the_metrics_stack_at_module_top():
    """Every portal suite imports `role_card`; the join (and the store behind
    it) must load only when a card is actually built."""
    tree = ast.parse(Path(rc.__file__).read_text())
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level += [f"{node.module}.{alias.name}" for alias in node.names]
    assert top_level, "the guard found no imports — did the parse change?"
    heavy = [name for name in top_level
             if "objective_join" in name or "metric_read" in name
             or "metric_points" in name]
    assert heavy == []


@pytest.fixture(autouse=True)
def _seat_on_record():
    """trinity-enterprise#812: the join reads the seat from Trinity's record,
    not from `x-role`; record the seat these templates declare, as a companion's."""
    from services import assignment_provider as ap

    class _Seats:
        def assignment_for(self, agent_name, triggered_by):
            return None

        def seat_for(self, agent_name):
            return {"case": "serves", "role_id": ROLE, "seats": [ROLE]}

    ap.register_provider(_Seats())
    yield
    ap.clear_provider()

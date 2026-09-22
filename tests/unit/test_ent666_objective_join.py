"""The objective ↔ metric join (trinity-enterprise#666, C2).

Two halves, tested as two things. `join_objectives` and `parse_objective` are
pure and get driven directly with already-fetched inputs — that is where every
"never a blank" rule lives. `read_objective_join` is the composition, and what
is worth proving about it is what it does when the agent door misbehaves: a
stopped container, a fan-out that hits a dead agent-server, a shared canon with
more files than the scan bound.

The store is faked (a list, not a database) and the agent door is a dict keyed
by path, so nothing here needs Docker, a backend or a DB file.
"""

from __future__ import annotations

import json
import re
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
from services import docker_utils  # noqa: E402
from services import objective_join_service as svc  # noqa: E402

AGENT = "sales-companion"
ROLE = "revenue-lead"
HOUR = 3600
NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds: float) -> str:
    return _iso(NOW - timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Fixtures for the pure half
# ---------------------------------------------------------------------------

def _definition(**overrides):
    d = {
        "name": "close_rate", "type": "percentage", "label": "Close rate",
        "description": None, "unit": "%", "direction": "neutral",
        "aggregation": "last", "cadence": "1h", "cadence_seconds": HOUR,
        "warning_threshold": None, "critical_threshold": None,
        "values": None, "dimensions": [], "status": "active",
        "retired_at": None, "type_conflict": None,
    }
    d.update(overrides)
    return d


def _latest(name="close_rate", value=30.0, ts=None, stale=False,
            freshness="fresh", cadence=HOUR):
    return {name: {
        **_definition(name=name, cadence_seconds=cadence),
        "latest": ({"value": value, "ts": ts or _ago(60), "dims": None}
                   if value is not None else None),
        "last_point_at": (ts or _ago(60)) if value is not None else None,
        "stale": stale, "freshness": freshness,
        "stale_after": None, "series_count": 1 if value is not None else 0,
    }}


def _objective(**overrides):
    obj, findings = svc.parse_objective({
        "schema_version": 1,
        "id": "q4-close-rate",
        "statement": "Lift close rate to 35% by the end of Q4.",
        "horizon": "quarter",
        "owner": f"role:{ROLE}",
        "metrics": [{"name": "close_rate", "direction": "up", "target": 35,
                     "by": "2026-12-31"}],
        "status": "active",
        "review_by": "2026-10-15",
        **overrides,
    }, path="canon/objectives/q4-close-rate.yaml")
    assert findings == [], findings
    return obj


def _join(objectives, definitions, latest, *, role_id=ROLE, agent=AGENT):
    return svc.join_objectives(objectives, definitions, latest,
                               agent_name=agent, role_id=role_id, now=NOW)


# ===========================================================================
# parse_objective — the bounds on author-controlled files
# ===========================================================================

def test_the_canon_grammar_round_trips():
    obj = _objective()
    assert obj["id"] == "q4-close-rate"
    assert obj["schema_version"] == "1"
    assert obj["horizon"] == "quarter"
    assert obj["status"] == "active"
    assert obj["review_by"] == "2026-10-15"
    assert obj["metrics"] == [{
        "name": "close_rate", "target": 35, "target_text": None,
        "tolerance": None, "by": "2026-12-31", "direction": "up",
    }]


def test_unknown_keys_are_tolerated_and_never_fatal():
    """The ent#477 reader rule: a field a later schema_version adds must not
    take the objective down with it."""
    obj, findings = svc.parse_objective(
        {"id": "x", "cascade": {"from": "y"}, "weight": 3, "metrics": []},
        path="canon/objectives/x.yaml")
    assert obj is not None and findings == []


def test_a_document_that_is_not_a_mapping_is_a_named_finding():
    """Never a silent `continue` — that is how an objective disappears from a
    card that claims to show them all."""
    obj, findings = svc.parse_objective(
        ["not", "a", "mapping"], path="canon/objectives/broken.yaml")
    assert obj is None
    assert findings[0]["code"] == "objective_invalid"
    assert "canon/objectives/broken.yaml" in findings[0]["message"]


def test_a_missing_id_falls_back_to_the_filename():
    obj, _ = svc.parse_objective({"metrics": []},
                                 path="canon/objectives/q4-close-rate.yaml")
    assert obj["id"] == "q4-close-rate"


@pytest.mark.parametrize("target,number,text", [
    (35, 35, None),
    (35.5, 35.5, None),
    ("thirty-five", None, "thirty-five"),
    (float("nan"), None, "nan"),
    (float("inf"), None, "inf"),
    ([1, 2], None, "[1, 2]"),
    (True, None, "True"),
    (None, None, None),
])
def test_targets_are_normalised_to_a_finite_number_or_bounded_text(
        target, number, text):
    """A bare `NaN` in the body is not JSON: `JSON.parse` throws and the card
    renders blank. The value is kept as text so the author still sees it."""
    obj, _ = svc.parse_objective(
        {"id": "x", "metrics": [{"name": "m", "target": target}]},
        path="canon/objectives/x.yaml")
    row = obj["metrics"][0]
    assert row["target"] == number
    assert row["target_text"] == text
    assert json.dumps(obj)  # strict: no NaN / Infinity can reach the wire


def test_author_text_is_bounded():
    obj, _ = svc.parse_objective({
        "id": "x", "statement": "s" * 5000, "horizon": "h" * 100,
        "owner": "o" * 500, "review_by": "r" * 100,
        "metrics": [{"name": "m", "by": "b" * 200,
                     "direction": "d" * 100, "target": "t" * 500}],
    }, path="canon/objectives/x.yaml")
    assert len(obj["statement"]) == svc.MAX_TEXT
    assert len(obj["horizon"]) == 16
    assert len(obj["owner"]) == 128
    assert len(obj["review_by"]) == 32
    assert len(obj["metrics"][0]["by"]) == 32
    assert len(obj["metrics"][0]["direction"]) == 16
    assert len(obj["metrics"][0]["target_text"]) == 64


def test_a_bad_metric_name_is_dropped_with_a_finding():
    obj, findings = svc.parse_objective({
        "id": "x",
        "metrics": [{"name": "../../etc/passwd"}, {"name": ""},
                    "not a mapping", {"name": "good"}],
    }, path="canon/objectives/x.yaml")
    assert [m["name"] for m in obj["metrics"]] == ["good"]
    assert [f["code"] for f in findings] == ["metric_name_invalid"] * 3


def test_a_duplicate_metric_keeps_the_first_and_says_so():
    obj, findings = svc.parse_objective({
        "id": "x",
        "metrics": [{"name": "m", "target": 1}, {"name": "m", "target": 99}],
    }, path="canon/objectives/x.yaml")
    assert [m["target"] for m in obj["metrics"]] == [1]
    assert findings[0]["code"] == "metric_duplicate"


def test_more_than_twelve_metrics_truncates_and_states_it():
    obj, _ = svc.parse_objective(
        {"id": "x", "metrics": [{"name": f"m{i}"} for i in range(20)]},
        path="canon/objectives/x.yaml")
    assert len(obj["metrics"]) == svc.MAX_METRICS_PER_OBJECTIVE
    assert obj["metrics_truncated"] is True


# ===========================================================================
# select_objectives — scan, THEN filter, THEN cap
# ===========================================================================

def test_the_cap_is_applied_after_the_filter_not_before():
    """A shared fleet canon holds every role's objectives in one directory. If
    the cap ran on the listing, an agent whose objective sorts after twenty
    foreign ones would see nothing at all."""
    foreign = [_objective(id=f"other-{i:03d}", owner="role:someone-else")
               for i in range(30)]
    mine = _objective(id="zzz-mine")
    kept, truncated = svc.select_objectives(
        foreign + [mine], role_id=ROLE, agent_name=AGENT)
    assert [o["id"] for o in kept] == ["zzz-mine"]
    assert truncated is False


def test_more_than_twenty_of_my_own_truncates_and_states_it():
    mine = [_objective(id=f"mine-{i:03d}") for i in range(25)]
    kept, truncated = svc.select_objectives(mine, role_id=ROLE, agent_name=AGENT)
    assert len(kept) == svc.MAX_OBJECTIVES
    assert truncated is True


def test_a_finished_objective_is_dropped_without_a_finding():
    """Only `active` is joined. `achieved` is not a defect, it is finished —
    and a finding for it would be a nag with no action attached."""
    objs = [_objective(id="done", status="achieved"),
            _objective(id="gone", status="dropped"),
            _objective(id="live")]
    kept, _ = svc.select_objectives(objs, role_id=ROLE, agent_name=AGENT)
    assert [o["id"] for o in kept] == ["live"]


# ===========================================================================
# join_objectives — the AC surface
# ===========================================================================

def test_the_happy_join_carries_target_actual_and_freshness():
    result = _join([_objective()], [_definition()], _latest(value=30.0))
    row = result["objectives"][0]["metrics"][0]

    assert (row["target"], row["actual"]) == (35, 30.0)
    assert row["declared"] is True
    assert row["gap"] == {"status": "behind", "delta": -5.0, "reason": None}
    assert row["direction"] == "up_good"
    assert row["direction_source"] == "objective"
    assert row["unit"] == "%"
    assert row["freshness"] == "fresh"
    assert row["finding"] is None
    assert result["summary"]["behind"] == 1
    assert result["objectives"][0]["owned"] is True


def test_by_and_horizon_ride_the_row_so_pace_is_the_consumers_to_compute():
    """TD-3: `behind` is POSITION, never pace. The row carries the deadline and
    the horizon so ent#605 can judge pace itself — nothing here does."""
    row = _join([_objective()], [_definition()],
                _latest())["objectives"][0]["metrics"][0]
    assert row["by"] == "2026-12-31"
    assert row["horizon"] == "quarter"
    assert "pace" not in row


def test_an_undeclared_metric_is_a_named_finding_never_a_blank():
    """AC: the objective names a metric nobody declared. The row still exists,
    it says why it has no number, and it names the fix."""
    result = _join([_objective()], [], {})
    row = result["objectives"][0]["metrics"][0]

    assert row["declared"] is False
    assert row["actual"] is None
    assert row["gap"]["reason"] == "undeclared"
    assert row["finding"]["code"] == "metric_undeclared"
    assert "refresh_metric_definitions" in row["finding"]["message"]
    assert result["findings"][0]["code"] == "metric_undeclared"
    assert result["summary"]["undeclared"] == 1


def test_a_supporting_agent_is_not_told_to_declare_someone_elses_metric():
    """The registry is per-agent and cross-agent reads are ent#80's grant, so
    "declare it and refresh" is advice a supporter cannot take. It gets the
    informational code instead, and is NOT counted as undeclared."""
    supported = _objective(id="shared", owner="role:someone-else",
                           supporting_agents=[AGENT])
    result = _join([supported], [], {})
    row = result["objectives"][0]["metrics"][0]

    assert row["declared"] is False
    assert row["declared_elsewhere"] is True
    assert row["gap"]["reason"] == "declared_elsewhere"
    assert row["finding"]["code"] == "metric_not_declared_here"
    assert result["summary"]["declared_elsewhere"] == 1
    assert result["summary"]["undeclared"] == 0
    assert result["objectives"][0]["owned"] is False
    assert result["objectives"][0]["supporting"] is True


def test_a_supporter_that_does_declare_the_name_joins_normally():
    supported = _objective(id="shared", owner="role:someone-else",
                           supporting_agents=[AGENT])
    row = _join([supported], [_definition()],
                _latest(value=40.0))["objectives"][0]["metrics"][0]
    assert row["declared"] is True
    assert row["declared_elsewhere"] is False
    assert row["gap"]["status"] == "ahead"


def test_a_retired_metric_shows_no_number_and_says_why():
    """§49.2: a retired number rendering as current is the failure this read
    exists to prevent — so the last value is withheld, not badged."""
    retired = _definition(status="retired", retired_at="2026-08-01T00:00:00Z")
    result = _join([_objective()], [retired], _latest(value=99.0))
    row = result["objectives"][0]["metrics"][0]

    assert row["declared"] is False
    assert row["actual"] is None
    assert row["gap"]["reason"] == "retired"
    assert row["finding"]["code"] == "metric_retired"
    assert "2026-08-01T00:00:00Z" in row["finding"]["message"]


def test_stale_and_gap_are_orthogonal():
    """The card renders "30 / 35 · stale"; ent#605 refuses to act on it.
    Collapsing the gap would hide the number the card exists to show."""
    result = _join([_objective()], [_definition()],
                   _latest(value=30.0, stale=True, freshness="stale"))
    row = result["objectives"][0]["metrics"][0]

    assert row["stale"] is True
    assert row["gap"]["status"] == "behind"   # still computed
    assert result["summary"]["stale"] == 1
    assert result["summary"]["behind"] == 1


def test_a_metric_with_no_points_is_not_late_it_has_not_started():
    result = _join([_objective()], [_definition()],
                   _latest(value=None, freshness="no_points"))
    row = result["objectives"][0]["metrics"][0]

    assert row["actual"] is None
    assert row["stale"] is False
    assert row["freshness"] == "no_points"
    assert row["gap"]["reason"] == "no_points"


def test_a_metric_with_no_cadence_cannot_be_stale_but_still_has_a_gap():
    latest = _latest(value=30.0, cadence=None, freshness="no_cadence")
    latest["close_rate"]["stale"] = None
    row = _join([_objective()], [_definition(cadence_seconds=None)],
                latest)["objectives"][0]["metrics"][0]
    assert row["stale"] is False        # `None` is not a stale claim
    assert row["freshness"] == "no_cadence"
    assert row["gap"]["status"] == "behind"


def test_two_declared_directions_that_disagree_are_a_finding():
    """The registry wins — and the disagreement is named rather than silently
    resolved, because one of the two files is wrong."""
    result = _join([_objective()], [_definition(direction="down_good")],
                   _latest(value=30.0))
    row = result["objectives"][0]["metrics"][0]

    assert row["direction"] == "down_good"
    assert row["direction_source"] == "registry"
    assert row["gap"]["status"] == "ahead"   # down_good, below target
    assert row["finding"]["code"] == "direction_mismatch"


def test_when_nobody_declares_a_direction_the_finding_names_the_one_line_fix():
    obj = _objective(metrics=[{"name": "close_rate", "target": 35}])
    result = _join([obj], [_definition()], _latest(value=30.0))
    row = result["objectives"][0]["metrics"][0]

    assert row["direction"] is None
    assert row["direction_source"] == "none"
    assert row["gap"]["reason"] == "no_direction"
    assert row["finding"]["code"] == "direction_undeclared"
    assert "direction: up" in row["finding"]["message"]
    assert result["summary"]["not_computable"] == 1


def test_a_hold_objective_is_on_target_at_the_value_and_off_target_away():
    held = _objective(metrics=[{"name": "close_rate", "direction": "hold",
                                "target": 35, "tolerance": 1}])
    on = _join([held], [_definition()], _latest(value=35.5))
    off = _join([held], [_definition()], _latest(value=40.0))

    assert on["objectives"][0]["metrics"][0]["gap"]["status"] == "on_target"
    assert on["summary"]["on_target"] == 1
    off_row = off["objectives"][0]["metrics"][0]
    assert off_row["gap"] == {"status": "off_target", "delta": 5.0,
                              "reason": None}
    assert off["summary"]["off_target"] == 1
    assert off["summary"]["behind"] == 0 and off["summary"]["ahead"] == 0


def test_a_metric_with_no_objective_is_simply_absent_and_raises_nothing():
    """Metrics without objectives are fine — the join is objective-driven, not
    registry-driven, so an unwatched metric is not a finding."""
    result = _join([_objective()],
                   [_definition(), _definition(name="lonely")],
                   {**_latest(), **_latest(name="lonely", value=1.0)})
    names = [m["name"] for m in result["objectives"][0]["metrics"]]
    assert names == ["close_rate"]
    assert result["findings"] == []


def test_two_files_claiming_one_id_are_both_kept_and_named():
    a = _objective(id="same")
    b = dict(_objective(id="same"), path="canon/objectives/copy.yaml")
    result = _join([a, b], [_definition()], _latest())
    assert len(result["objectives"]) == 2
    codes = [f["code"] for f in result["findings"]]
    assert "objective_id_duplicate" in codes


def test_the_summary_adds_up_to_the_rows():
    objs = [_objective(id="a"),
            _objective(id="b", metrics=[{"name": "nope", "target": 1,
                                         "direction": "up"}])]
    result = _join(objs, [_definition()], _latest(value=40.0))
    s = result["summary"]
    assert s["objectives"] == 2
    assert s["metrics"] == 2
    assert s["ahead"] + s["not_computable"] == 2
    assert s["undeclared"] == 1


# ===========================================================================
# Fake agent door + store for the composition
# ===========================================================================

class _Response:
    def __init__(self, status_code, text="", body=None):
        self.status_code = status_code
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _FakeClient:
    """The agent door, keyed by the path the service asks for."""

    def __init__(self, files=None, listing=None, *, fail_after=None,
                 list_status=200):
        self.files = files or {}
        self.listing = listing
        self.list_status = list_status
        self.calls = []
        self.fail_after = fail_after
        self.max_in_flight = 0
        self._in_flight = 0

    async def get(self, path, timeout=None, **kwargs):
        from services.agent_client.client import AgentNotReachableError
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            self.calls.append(path)
            if (self.fail_after is not None
                    and len(self.calls) > self.fail_after):
                raise AgentNotReachableError("agent-server is gone")
            if path.startswith("/api/files?"):
                if self.list_status != 200:
                    return _Response(self.list_status)
                names = self.listing if self.listing is not None else sorted(
                    p.rsplit("/", 1)[-1] for p in self.files
                    if p.startswith("canon/objectives/"))
                return _Response(200, body={"tree": [
                    {"name": n, "type": "file"} for n in names]})
            wanted = re.sub(r"^/api/files/download\?path=", "", path)
            from urllib.parse import unquote
            wanted = unquote(wanted)
            if wanted not in self.files:
                return _Response(404)
            return _Response(200, text=self.files[wanted])
        finally:
            self._in_flight -= 1


class _FakeDb:
    def __init__(self, definitions=None, points=None):
        self.definitions = definitions if definitions is not None else [
            _definition()]
        self.points = points if points is not None else [{
            "metric": "close_rate", "ts": _ago(60), "value_numeric": 30.0,
            "value_text": None, "dims": None}]
        self.calls = []

    def list_metric_definitions(self, agent, include_retired=False):
        self.calls.append(("list_metric_definitions", agent))
        if include_retired:
            return list(self.definitions)
        return [d for d in self.definitions if d.get("status") == "active"]

    def latest_metric_points(self, agent, names, per_metric_limit=200):
        self.calls.append(("latest_metric_points", tuple(names)))
        wanted = set(names)
        return sorted([p for p in self.points if p["metric"] in wanted],
                      key=lambda r: r["ts"], reverse=True)


TEMPLATE = """
name: sales-companion
x-role:
  role: revenue-lead
x-canon:
  clone_path: canon
metrics:
  - name: close_rate
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
    by: 2026-12-31
status: active
review_by: 2026-10-15
"""


@pytest.fixture
def store(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(database_mod, "db", fake)
    return fake


@pytest.fixture
def running(monkeypatch):
    async def _state(name):
        return "running"
    monkeypatch.setattr(docker_utils, "agent_container_state_async", _state)


# ===========================================================================
# read_objective_join — the composition
# ===========================================================================

@pytest.mark.asyncio
async def test_the_whole_read_end_to_end(store, running):
    client = _FakeClient(files={
        "template.yaml": TEMPLATE,
        "canon/objectives/q4-close-rate.yaml": OBJECTIVE_YAML,
    })
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["unavailable"] is None
    assert result["role"] == {"id": ROLE, "path": "canon/roles/revenue-lead.yaml"}
    assert result["canon_root"] == "canon"
    assert result["stale_rule"] == "2x cadence"
    assert result["source"]["template"] == "read"
    assert result["source"]["objectives_dir"] == "read"
    row = result["objectives"][0]["metrics"][0]
    assert (row["target"], row["actual"]) == (35, 30.0)
    assert row["gap"]["status"] == "behind"
    assert result["message"] is None
    assert json.dumps(result)  # strictly serialisable: no NaN reaches the wire


@pytest.mark.asyncio
@pytest.mark.parametrize("state,code", [
    ("stopped", "agent_stopped"),
    ("missing", "agent_missing"),
    (None, "agent_unreachable"),
])
async def test_a_container_that_is_not_running_is_named_not_guessed(
        monkeypatch, store, state, code):
    """Files are truth and they live in the container — so say which way it is
    gone, because "recreate it" and "start it" are different actions."""
    async def _state(name):
        return state
    monkeypatch.setattr(docker_utils, "agent_container_state_async", _state)

    result = await svc.read_objective_join(AGENT, now=NOW,
                                           client=_FakeClient())
    assert result["unavailable"] == code
    assert result["objectives"] == []
    assert result["message"]
    assert store.calls == []       # no store read for an agent we cannot read


@pytest.mark.asyncio
async def test_no_role_and_no_canon_costs_nothing(store, running):
    """The zero-config path: one template read and NO store query at all."""
    client = _FakeClient(files={"template.yaml": "name: plain\n"})
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["objectives"] == []
    assert result["role"] is None
    assert "nothing to join" in result["message"]
    assert store.calls == []
    assert client.calls == ["/api/files/download?path=template.yaml"]


@pytest.mark.asyncio
async def test_a_template_handed_in_is_not_read_again(store, running):
    """The role card already read template.yaml through the same door."""
    client = _FakeClient(files={
        "canon/objectives/q4-close-rate.yaml": OBJECTIVE_YAML})
    import yaml
    result = await svc.read_objective_join(
        AGENT, now=NOW, client=client,
        template=yaml.safe_load(TEMPLATE))

    assert not any("template.yaml" in c for c in client.calls)
    assert result["source"]["template"] == "read"
    assert len(result["objectives"]) == 1


@pytest.mark.asyncio
async def test_an_absent_objectives_directory_names_the_path(store, running):
    client = _FakeClient(files={"template.yaml": TEMPLATE}, list_status=404)
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["source"]["objectives_dir"] == "absent"
    assert "canon/objectives/" in result["message"]
    assert result["unavailable"] is None


@pytest.mark.asyncio
async def test_an_unlistable_directory_is_unreadable_not_absent(store, running):
    client = _FakeClient(files={"template.yaml": TEMPLATE}, list_status=500)
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)
    assert result["source"]["objectives_dir"] == "unreadable"


@pytest.mark.asyncio
async def test_one_unparseable_file_does_not_take_the_others_down(
        store, running):
    client = _FakeClient(files={
        "template.yaml": TEMPLATE,
        "canon/objectives/q4-close-rate.yaml": OBJECTIVE_YAML,
        "canon/objectives/broken.yaml": "- just\n- a\n- list\n",
    })
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert [o["id"] for o in result["objectives"]] == ["q4-close-rate"]
    codes = [f["code"] for f in result["findings"]]
    assert "objective_invalid" in codes


@pytest.mark.asyncio
async def test_the_scan_is_bounded_and_says_what_it_did_not_look_at(
        store, running):
    names = [f"obj-{i:03d}.yaml" for i in range(150)]
    files = {"template.yaml": TEMPLATE}
    files.update({f"canon/objectives/{n}": OBJECTIVE_YAML.replace(
        "id: q4-close-rate", f"id: {n[:-5]}") for n in names})
    client = _FakeClient(files=files, listing=names)

    result = await svc.read_objective_join(AGENT, now=NOW, client=client)
    source = result["source"]

    assert source["objectives_listed"] == 150
    assert source["objectives_scanned"] == svc.MAX_OBJECTIVE_FILES_SCANNED
    assert source["objectives_unscanned"] == 50
    assert source["objectives_truncated"] is True
    assert len(result["objectives"]) == svc.MAX_OBJECTIVES


@pytest.mark.asyncio
async def test_the_fan_out_stays_inside_the_concurrency_bound(store, running):
    names = [f"obj-{i:03d}.yaml" for i in range(30)]
    files = {"template.yaml": TEMPLATE}
    files.update({f"canon/objectives/{n}": OBJECTIVE_YAML for n in names})
    client = _FakeClient(files=files, listing=names)

    await svc.read_objective_join(AGENT, now=NOW, client=client)
    assert client.max_in_flight <= svc.READ_CONCURRENCY


@pytest.mark.asyncio
async def test_a_dead_agent_server_aborts_the_fan_out(store, running):
    """One dead agent-server must not spend twenty more reads driving open the
    circuit breaker that chat rides on (threshold: three failures)."""
    names = [f"obj-{i:03d}.yaml" for i in range(40)]
    files = {"template.yaml": TEMPLATE}
    files.update({f"canon/objectives/{n}": OBJECTIVE_YAML for n in names})
    # template read + listing + 2 file reads, then the door dies.
    client = _FakeClient(files=files, listing=names, fail_after=4)

    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["unavailable"] == "agent_unreachable"
    assert result["objectives"] == []
    assert result["message"]
    assert len(client.calls) < 10, client.calls
    assert store.calls == []


@pytest.mark.asyncio
async def test_a_dead_door_on_the_template_read_is_unreachable_not_unreadable(
        store, running):
    client = _FakeClient(files={"template.yaml": TEMPLATE}, fail_after=0)
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)
    assert result["unavailable"] == "agent_unreachable"


@pytest.mark.asyncio
async def test_an_unreadable_template_is_named_and_is_not_a_crash(
        store, running):
    client = _FakeClient(files={})    # download → 404
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["source"]["template"] == "not_found"
    assert result["unavailable"] is None
    assert "template.yaml" in result["message"]


@pytest.mark.asyncio
async def test_a_traversing_clone_path_reads_no_file_with_it(store, running):
    client = _FakeClient(files={
        "template.yaml": "x-role:\n  role: revenue-lead\n"
                         "x-canon:\n  clone_path: ../../etc\n"})
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert [f["code"] for f in result["findings"]] == ["canon_path_invalid"]
    assert result["canon_root"] is None
    assert client.calls == ["/api/files/download?path=template.yaml"]


@pytest.mark.asyncio
async def test_an_invalid_role_id_is_a_finding_and_still_reads_supported_work(
        store, running):
    supported = OBJECTIVE_YAML.replace(
        "owner: role:revenue-lead",
        f"owner: role:someone-else\nsupporting_agents: [{AGENT}]")
    client = _FakeClient(files={
        "template.yaml": "x-role:\n  role: 'not a valid id'\n"
                         "x-canon:\n  clone_path: canon\n",
        "canon/objectives/q4-close-rate.yaml": supported,
    })
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert "role_id_invalid" in [f["code"] for f in result["findings"]]
    assert result["objectives"][0]["supporting"] is True
    assert result["objectives"][0]["owned"] is False


@pytest.mark.asyncio
async def test_the_store_is_asked_only_for_metrics_an_objective_references(
        store, running):
    store.definitions = [_definition(), _definition(name="unrelated")]
    client = _FakeClient(files={
        "template.yaml": TEMPLATE,
        "canon/objectives/q4-close-rate.yaml": OBJECTIVE_YAML,
    })
    await svc.read_objective_join(AGENT, now=NOW, client=client)

    points_calls = [c for c in store.calls if c[0] == "latest_metric_points"]
    assert points_calls == [("latest_metric_points", ("close_rate",))]
    # One registry read for the whole join, not one per consumer.
    assert sum(1 for c in store.calls
               if c[0] == "list_metric_definitions") == 1


@pytest.mark.asyncio
async def test_an_objective_naming_nothing_declared_reads_no_points(
        store, running):
    store.definitions = []
    client = _FakeClient(files={
        "template.yaml": TEMPLATE,
        "canon/objectives/q4-close-rate.yaml": OBJECTIVE_YAML,
    })
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert not any(c[0] == "latest_metric_points" for c in store.calls)
    assert result["summary"]["undeclared"] == 1
    assert result["objectives"][0]["metrics"][0]["finding"]["code"] == \
        "metric_undeclared"


@pytest.mark.asyncio
async def test_no_objective_names_this_agent_is_an_empty_state_with_copy(
        store, running):
    foreign = OBJECTIVE_YAML.replace("owner: role:revenue-lead",
                                     "owner: role:someone-else")
    client = _FakeClient(files={
        "template.yaml": TEMPLATE,
        "canon/objectives/q4-close-rate.yaml": foreign,
    })
    result = await svc.read_objective_join(AGENT, now=NOW, client=client)

    assert result["objectives"] == []
    assert "supporting_agents" in result["message"]


# ===========================================================================
# Wiring guards
# ===========================================================================

def test_every_db_call_in_the_service_exists_on_the_real_facade():
    """Derived from the source, so it catches the NEXT `db.<new>(` before
    deploy rather than at the first request (learnings 2026-09-06)."""
    from database import db as real_db

    source = Path(svc.__file__).read_text()
    names = set(re.findall(r"\bdb\.(\w+)\(", source))
    assert names, "the guard found no db.* calls — did the spelling change?"
    missing = [n for n in names if not callable(getattr(real_db, n, None))]
    assert missing == [], f"not on database.db: {missing}"


def test_the_pure_half_imports_without_a_database():
    """`freshness` and the grammar helpers must be importable by a consumer
    that has no store — the import is at module top, the `db` resolution is at
    call time."""
    source = Path(svc.__file__).read_text()
    module_top = source.split("def _text(", 1)[0]
    assert "from database import" not in module_top
    assert "from services import docker_utils" not in module_top


def test_there_is_exactly_one_stale_rule_and_this_module_imports_it():
    """A second implementation anywhere is a defect regardless of whether it
    currently agrees (§49.1)."""
    from services import metric_read_service

    source = Path(svc.__file__).read_text()
    assert "from services.metric_read_service import" in source
    assert "def freshness" not in source
    assert svc.STALE_RULE is metric_read_service.STALE_RULE

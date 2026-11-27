"""A turn reads, by id, itself and the runs it started (#4173).

While an agent runs a turn for someone below the owner (a shared person, a
visitor, a Workspace or room participant), the reads made with the agent's key
are narrowed to that person (ent#875, ent#926). The turn then could not read
its own execution row, or a run it had just started: `get_execution_result`
answered "Execution not found" for a run the caller held a receipt for.

The ruling (2026-11-23): an agent key whose `X-Trinity-Execution-Id` names a
turn that is RUNNING on the key's own agent and rated `share` reads, BY ID,

  (a) that turn's own row, and
  (b) a row whose `source_agent_name` is the key's agent and whose
      `continues_execution_id` is the acting turn's link (the turn itself, or
      its root when the acting turn is a continuation such as a wake).

On the detail, log and stream routes and the fan-out batch read. Lists and
`terminate` keep their rule; an owner turn's id, a finished, unknown or foreign
id open nothing.

Real routes, the real `get_current_user`, a real agent key and real execution
rows on the unit conftest's SQLite, as in `test_ent978_narrowed_read_marker.py`.
Related flow: docs/memory/feature-flows/executions-dashboard.md
"""
from __future__ import annotations

import ast
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

import dependencies as dep
from database import db
from db.engine import get_engine
from db.write_params import ExecutionResult, TaskExecutionFields
from db_models import UserCreate
from dependencies import create_access_token
from routers import chat as chat_router
from routers import fan_out as fan_out_router
from routers import schedules as schedules_router
from services import turn_tier_service
from utils import read_narrowing

pytestmark = pytest.mark.unit

HEADER = "X-Trinity-Read-Narrowed"
TURN = "X-Trinity-Execution-Id"
_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
NOT_FOUND = {"detail": "Execution not found"}


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(read_narrowing.ReadNarrowedMiddleware)
    # The chat router first, as in main.py.
    for module in (chat_router, schedules_router, fan_out_router):
        app.include_router(module.router)
    return TestClient(app)


def _person(role):
    username = f"p4173-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(UserCreate(username=username, email=username, role=role, password="x"))
    user = db.get_user_by_username(username)
    return SimpleNamespace(username=username, id=user["id"], jwt=create_access_token({"sub": username}))


@pytest.fixture
def world():
    """Agent A is shared with `mate`; agent B is not. A may call B. The key is A's."""
    owner, mate = _person("creator"), _person("operator")
    tag = uuid.uuid4().hex[:8]
    a, b = f"a4173-{tag}", f"b4173-{tag}"
    db.register_agent_owner(a, owner.username)
    db.register_agent_owner(b, owner.username)
    db.add_agent_permission(a, b, created_by=owner.username)
    db.share_agent(a, owner.username, mate.username)
    key = db.create_agent_mcp_api_key(a, owner.username).api_key
    return SimpleNamespace(a=a, b=b, owner=owner, mate=mate, key=key)


def _row(agent, trigger="public", *, done=False, **fields):
    """An execution row, RUNNING unless `done`. With no user and a trigger
    nothing at owner tier starts (`public`), it is a visitor's."""
    row = db.create_task_execution(agent, "work", trigger, TaskExecutionFields(**fields))
    if done:
        assert db.update_execution_status(
            row.id, "success", ExecutionResult(response="the answer", execution_log='[{"type": "result"}]'))
    return row.id


def _started(w, turn, *, agent=None, person=None, source="__caller__", **fields):
    """A finished run on B (or `agent`) that `turn` started through A's key."""
    return _row(agent or w.b, "agent", done=True, source_user_id=person,
                source_agent_name=w.a if source == "__caller__" else source,
                continues_execution_id=turn, **fields)


def get(client, w, path, *, turn=None, who="key"):
    headers = {"Authorization": f"Bearer {w.key if who == 'key' else getattr(w, who).jwt}"}
    if turn:
        headers[TURN] = turn
    return client.get(path, headers=headers)


def run_path(agent, run, suffix=""):
    return f"/api/agents/{agent}/executions/{run}{suffix}"


@contextmanager
def query_count():
    seen = []
    engine = get_engine()

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


BY_ID = pytest.mark.parametrize("suffix", ["", "/log"])


# ---- what the turn can read now -----------------------------------------------------------

@BY_ID
def test_a_visitors_turn_reads_its_own_row(client, world, suffix):
    turn = _row(world.a)
    resp = get(client, world, run_path(world.a, turn, suffix), turn=turn)
    assert resp.status_code == 200, resp.text


@BY_ID
def test_a_visitors_turn_reads_a_run_it_started_on_another_agent(client, world, suffix):
    turn = _row(world.a)
    child = _started(world, turn)
    resp = get(client, world, run_path(world.b, child, suffix), turn=turn)
    assert resp.status_code == 200, resp.text
    if suffix:
        assert resp.json()["log"] == [{"type": "result"}]
    else:
        assert resp.json()["response"] == "the answer"


@BY_ID
def test_a_shared_persons_turn_reads_a_run_it_started_on_an_agent_they_cannot_open(client, world, suffix):
    """ent#1148 hides B's rows from a key acting for a person who cannot open
    B. The run this turn started is the exception, transcript included."""
    turn = _row(world.a, "chat", source_user_id=world.mate.id)
    child = _started(world, turn, person=world.mate.id)
    assert dep.db.can_user_access_agent(world.mate.username, world.b) is False   # the premise
    resp = get(client, world, run_path(world.b, child, suffix), turn=turn)
    assert resp.status_code == 200, resp.text


@BY_ID
@pytest.mark.parametrize("channel,trigger", [("portal", "public"), ("room", "room")])
def test_a_workspace_turn_and_a_room_turn_read_their_own_row(client, world, suffix, channel, trigger):
    """Both carry the person's email and no `source_user_id`, so the row was
    outside the very scope it narrowed the key to."""
    turn = _row(world.a, trigger, source_channel=channel, source_user_email=world.mate.username)
    resp = get(client, world, run_path(world.a, turn, suffix), turn=turn)
    assert resp.status_code == 200, resp.text


@BY_ID
@pytest.mark.parametrize("for_a_person", [False, True])
def test_a_wake_reads_the_runs_of_the_turn_it_continues(client, world, suffix, for_a_person):
    """The delegating turn has ended; its wake stands as that turn and reads
    the result it was woken for, and its own row."""
    person = world.mate.id if for_a_person else None
    turn = _row(world.a, "chat" if for_a_person else "public", done=True, source_user_id=person)
    child = _started(world, turn, person=person)
    wake = _row(world.a, "caller_wake", continues_execution_id=turn)
    assert get(client, world, run_path(world.b, child, suffix), turn=wake).status_code == 200
    assert get(client, world, run_path(world.a, wake, suffix), turn=wake).status_code == 200


def test_a_run_started_by_the_wake_carries_the_same_link_and_is_read_by_it(client, world):
    """A wake that delegates again records the ROOT, so one link covers both."""
    turn = _row(world.a, done=True)
    wake = _row(world.a, "caller_wake", continues_execution_id=turn)
    assert turn_tier_service.continuation_origin(db, world.a, wake) == turn     # what dispatch records
    again = _started(world, turn)
    assert get(client, world, run_path(world.b, again), turn=wake).status_code == 200


def test_the_stream_fence_opens_for_a_run_the_turn_started(client, world, monkeypatch):
    """The step after the fence is the container lookup: "Agent not found"
    means the row passed, "Execution not found" means it did not."""
    monkeypatch.setattr(chat_router, "get_agent_container", lambda name: None)
    turn = _row(world.a)
    mine, other = _started(world, turn), _started(world, _row(world.a))
    for agent, run in ((world.b, mine), (world.a, turn)):            # a run it started, its own row
        opened = get(client, world, run_path(agent, run, "/stream"), turn=turn)
        assert (opened.status_code, opened.json()) == (404, {"detail": "Agent not found"})
    closed = get(client, world, run_path(world.b, other, "/stream"), turn=turn)
    missing = get(client, world, run_path(world.b, "no-such-run", "/stream"), turn=turn)
    assert (closed.status_code, closed.json()) == (404, NOT_FOUND)
    assert (closed.status_code, closed.json(), dict(closed.headers)) == (
        missing.status_code, missing.json(), dict(missing.headers))


def _batch(w, turn, *, source="__caller__", n=2, person=None):
    fan_out_id = f"fo_{uuid.uuid4().hex[:12]}"
    for i in range(n):
        _started(w, turn, agent=w.a, source=source, person=person,
                 fan_out_id=fan_out_id, fan_out_task_id=f"t{i}")
    return fan_out_id


def test_a_visitors_turn_reads_its_own_fan_out_batch(client, world):
    turn = _row(world.a)
    resp = get(client, world, f"/api/agents/{world.a}/fan-out/{_batch(world, turn)}", turn=turn)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 2


def test_a_fan_out_read_shows_only_the_legs_the_turn_started(client, world):
    turn, other = _row(world.a), _row(world.a)
    fan_out_id = _batch(world, turn, n=1)
    _started(world, other, agent=world.a, fan_out_id=fan_out_id, fan_out_task_id="theirs")
    resp = get(client, world, f"/api/agents/{world.a}/fan-out/{fan_out_id}", turn=turn)
    assert (resp.status_code, resp.json()["total"]) == (200, 1)
    assert [leg["task_id"] for leg in resp.json()["results"]] == ["t0"]


# ---- the marker does not move (ent#978) --------------------------------------------------

@BY_ID
def test_a_granted_read_carries_the_same_marker_as_a_refused_one(client, world, suffix):
    turn = _row(world.a)
    granted = get(client, world, run_path(world.b, _started(world, turn), suffix), turn=turn)
    refused = get(client, world, run_path(world.b, "no-such-run", suffix), turn=turn)
    assert (granted.status_code, refused.status_code) == (200, 404)
    assert granted.headers[HEADER] == refused.headers[HEADER] == "turn_person"


@BY_ID
@pytest.mark.parametrize("acting", ["turn", "wake"])
def test_a_hidden_run_and_a_missing_id_answer_the_same(client, world, suffix, acting):
    """The reach is decided before the row is read: a row outside it, a row
    that nearly matches (the link, no source agent) and an id that was never a
    run cost the same lookups and answer the same (Invariant #8)."""
    turn = _row(world.a, done=acting == "wake")
    header = _row(world.a, "caller_wake", continues_execution_id=turn) if acting == "wake" else turn
    hidden = _started(world, _row(world.a, done=True))
    near = _started(world, turn, source=None)
    answers = []
    for run in (hidden, near, "no-such-run"):
        with query_count() as queries:
            resp = get(client, world, run_path(world.b, run, suffix), turn=header)
        answers.append((resp.status_code, resp.json(), dict(resp.headers), len(queries)))
    assert answers[0][:2] == (404, NOT_FOUND)
    assert answers[0] == answers[1] == answers[2]


# ---- what stays closed: each row here could only be opened by the new clause -------------

@BY_ID
def test_an_owner_turns_id_opens_nothing_while_a_share_turn_runs(client, world, suffix):
    """Else a share turn that learnt an owner turn's id could read the owner's
    work. The owner's own concurrent run stays narrowed: the stated cost."""
    _row(world.a)                                                    # a visitor's turn is running
    owners = _row(world.a, "manual", source_user_id=world.owner.id)
    child = _started(world, owners, person=world.owner.id)
    for agent, run in ((world.b, child), (world.a, owners)):
        resp = get(client, world, run_path(agent, run, suffix), turn=owners)
        assert (resp.status_code, resp.json(), resp.headers[HEADER]) == (404, NOT_FOUND, "running_turn")


@BY_ID
def test_a_finished_turns_id_opens_nothing(client, world, suffix):
    turn = _row(world.a, done=True)
    child = _started(world, turn)
    for agent, run in ((world.b, child), (world.a, turn)):
        assert get(client, world, run_path(agent, run, suffix), turn=turn).status_code == 404


@BY_ID
def test_another_agents_turn_id_opens_nothing(client, world, suffix):
    theirs = _row(world.b)                               # a visitor's turn running on B
    child = _started(world, theirs)
    assert get(client, world, run_path(world.b, child, suffix), turn=theirs).status_code == 404
    assert get(client, world, run_path(world.b, theirs, suffix), turn=theirs).status_code == 404


@BY_ID
@pytest.mark.parametrize("source", [None, "__callee__", "someone-else"])
def test_the_link_alone_is_not_enough(client, world, suffix, source):
    """The row must also name the key's agent as its dispatcher. A REST call
    that sent no `X-Source-Agent` records none; a wake row copies the link and
    names nobody."""
    turn = _row(world.a)
    child = _started(world, turn, source=world.b if source == "__callee__" else source)
    assert get(client, world, run_path(world.b, child, suffix), turn=turn).status_code == 404


@BY_ID
def test_a_turn_does_not_read_another_running_share_turns_runs(client, world, suffix):
    mine, theirs = _row(world.a), _row(world.a)
    child = _started(world, theirs)
    assert get(client, world, run_path(world.b, child, suffix), turn=mine).status_code == 404
    assert get(client, world, run_path(world.a, theirs, suffix), turn=mine).status_code == 404
    assert get(client, world, run_path(world.b, child, suffix), turn=theirs).status_code == 200   # control


@BY_ID
def test_a_continuation_with_no_single_origin_opens_nothing(client, world, suffix):
    """`NO_SINGLE_ORIGIN` is one string shared by unrelated endings: matching
    on it would join every such turn's runs. Such a turn is rated `share` only
    as the platform's fail-closed answer, so not even its own row opens."""
    ending = _row(world.a, "operator_ending", continues_execution_id=turn_tier_service.NO_SINGLE_ORIGIN)
    assert turn_tier_service.turn_tier(db, world.a, ending) == turn_tier_service.SHARE       # the premise
    for run in (_started(world, turn_tier_service.NO_SINGLE_ORIGIN), _started(world, None)):
        assert get(client, world, run_path(world.b, run, suffix), turn=ending).status_code == 404
    assert get(client, world, run_path(world.a, ending, suffix), turn=ending).status_code == 404


@BY_ID
@pytest.mark.parametrize("root", ["pruned", "another agent's"])
def test_a_wake_whose_turn_cannot_be_followed_opens_nothing(client, world, suffix, root):
    link = f"gone-{uuid.uuid4().hex[:8]}" if root == "pruned" else _row(world.b, done=True)
    wake = _row(world.a, "caller_wake", continues_execution_id=link)
    for run in (_started(world, link), _started(world, None)):
        assert get(client, world, run_path(world.b, run, suffix), turn=wake).status_code == 404
    assert get(client, world, run_path(world.a, wake, suffix), turn=wake).status_code == 404


@BY_ID
def test_a_wake_of_the_owners_turn_opens_nothing_even_if_it_rates_share(client, world, suffix, monkeypatch):
    """`share` is also what a continuation rates when the turn it continues
    could not be read at that moment. The reach reads and rates that turn
    itself, so a rating that failed closed cannot open the owner's runs."""
    _row(world.a)                                                    # a visitor's turn narrows the key
    owners = _row(world.a, "manual", done=True, source_user_id=world.owner.id)
    child = _started(world, owners, person=world.owner.id)
    wake = _row(world.a, "caller_wake", continues_execution_id=owners)
    assert turn_tier_service.turn_tier(db, world.a, wake) == turn_tier_service.OWNER          # the premise
    monkeypatch.setattr(turn_tier_service, "acting_turn",
                        lambda *a, **k: (turn_tier_service.SHARE, turn_tier_service.PERSON, None))
    assert dep.acting_turn_reach(_key(world, wake), dep.NO_ROWS_USER_ID) is None
    for agent, run in ((world.b, child), (world.a, wake)):
        assert get(client, world, run_path(agent, run, suffix), turn=wake).status_code == 404


@BY_ID
@pytest.mark.parametrize("header", [None, "manual"])
def test_without_the_turn_id_nothing_opens(client, world, suffix, header):
    turn = _row(world.a)
    child = _started(world, turn)
    assert get(client, world, run_path(world.b, child, suffix), turn=header).status_code == 404
    assert get(client, world, run_path(world.a, turn, suffix), turn=header).status_code == 404


def test_a_signed_in_person_gains_nothing(client, world):
    turn = _row(world.a)
    resp = get(client, world, run_path(world.a, turn), turn=turn, who="mate")
    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)


def test_the_list_stays_narrowed(client, world):
    turn = _row(world.a)
    _started(world, turn, agent=world.a)                 # a task the turn sent itself
    resp = get(client, world, f"/api/agents/{world.a}/executions", turn=turn)
    assert (resp.status_code, resp.json(), resp.headers[HEADER]) == (200, [], "turn_person")


def test_terminate_keeps_its_rule(client, world):
    turn = _row(world.a)
    child = _row(world.b, "agent", source_agent_name=world.a, continues_execution_id=turn)
    resp = client.post(run_path(world.b, child, "/terminate"),
                       headers={"Authorization": f"Bearer {world.key}", TURN: turn})
    assert (resp.status_code, resp.json()) == (404, {"detail": "Execution not found in agent"})
    assert get(client, world, run_path(world.b, child), turn=turn).status_code == 200            # it can read it


@pytest.mark.parametrize("case", ["another turn's", "another source agent", "no link", "no link, wake with no root"])
def test_a_fan_out_batch_the_turn_did_not_start_stays_not_found(client, world, case):
    """The batch read applies the rule in SQL, a second copy of it."""
    turn = _row(world.a)
    header = turn
    if case == "another turn's":
        fan_out_id = _batch(world, _row(world.a))
    elif case == "another source agent":
        fan_out_id = _batch(world, turn, source=world.b)
    else:
        fan_out_id = _batch(world, None)
        if case.endswith("no root"):
            header = _row(world.a, "caller_wake", continues_execution_id=f"gone-{uuid.uuid4().hex[:8]}")
    resp = get(client, world, f"/api/agents/{world.a}/fan-out/{fan_out_id}", turn=header)
    assert (resp.status_code, resp.json()) == (404, {"detail": "Fan-out not found"})


# ---- the helper, directly: the seams it reads -------------------------------------------

def _key(w, turn):
    return SimpleNamespace(id=w.owner.id, username=w.owner.username, role="creator", mcp_scope="agent",
                           agent_name=w.a, connector_agent=None, portal_delegate=False,
                           is_event_loopback=False, vouched_source_agent=None, _acting_execution_id=turn)


def test_the_reach_is_the_agent_the_turn_and_its_link(world):
    turn = _row(world.a, done=True)
    wake = _row(world.a, "caller_wake", continues_execution_id=turn)
    live = _row(world.a)
    assert tuple(dep.acting_turn_reach(_key(world, live), dep.NO_ROWS_USER_ID)) == (world.a, live, live)
    assert tuple(dep.acting_turn_reach(_key(world, wake), dep.NO_ROWS_USER_ID)) == (world.a, wake, turn)


def test_the_full_view_asks_nothing(world, monkeypatch):
    """`scope is None`: every row is already readable, so no lookup is spent."""
    asked = []
    for seam in ("acting_turn", "continuation_origin"):
        monkeypatch.setattr(turn_tier_service, seam, lambda *a, _seam=seam, **k: asked.append(_seam))
    assert dep.acting_turn_reach(_key(world, _row(world.a)), None) is None
    assert asked == []


@pytest.mark.parametrize("seam", ["acting_turn", "continuation_origin"])
def test_a_lookup_that_fails_opens_nothing(client, world, monkeypatch, seam):
    turn = _row(world.a)
    child = _started(world, turn)
    assert get(client, world, run_path(world.b, child), turn=turn).status_code == 200            # control

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(turn_tier_service, seam, boom)
    assert dep.acting_turn_reach(_key(world, turn), dep.NO_ROWS_USER_ID) is None
    assert get(client, world, run_path(world.b, child), turn=turn).status_code == 404


def test_a_principal_that_is_not_an_agent_key_has_no_reach(world):
    turn = _row(world.a)
    person = SimpleNamespace(id=world.mate.id, username=world.mate.username, role="operator",
                             mcp_scope=None, agent_name=None, _acting_execution_id=turn)
    assert dep.acting_turn_reach(person, world.mate.id) is None
    assert dep.row_is_acting_turns(None, db.get_execution(turn)) is False


@pytest.mark.parametrize("link", [None, ""])
def test_a_reach_with_no_link_never_matches_a_row_with_no_link(world, link):
    """Else every unlinked run the agent ever dispatched would be "started by"
    a turn with no link (`None == None`)."""
    unlinked = db.get_execution(_started(world, None))
    assert (unlinked.continues_execution_id, unlinked.source_agent_name) == (None, world.a)   # the premise
    assert dep.row_is_acting_turns((world.a, "some-turn", link), unlinked) is False


def test_a_granted_read_is_logged(client, world, caplog):
    turn = _row(world.a)
    child = _started(world, turn)
    with caplog.at_level(logging.INFO, logger=dep.logger.name):
        assert get(client, world, run_path(world.b, child, "/log"), turn=turn).status_code == 200
    lines = [r.getMessage() for r in caplog.records if "#4173" in r.getMessage()]
    assert len(lines) == 1 and all(part in lines[0] for part in (world.a, world.b, turn, child))


# ---- the database arm, through the facade callers use -----------------------------------

def test_the_fan_out_arm_needs_a_scope_and_a_link(world):
    turn = _row(world.a)
    linked, unlinked = _batch(world, turn), _batch(world, None)
    nobody = dep.NO_ROWS_USER_ID
    assert len(db.get_fan_out_executions(world.a, linked, source_user_id=nobody)) == 0
    assert len(db.get_fan_out_executions(world.a, linked, source_user_id=nobody, started_by=(world.a, turn))) == 2
    # No scope: the platform's own read sees the whole batch, whatever is passed.
    owners = _batch(world, turn, person=world.owner.id)
    assert len(db.get_fan_out_executions(world.a, owners, started_by=(world.b, "x"))) == 2
    # No link is not "link IS NULL", and no caller is not "source agent IS NULL".
    for link in (None, ""):
        assert db.get_fan_out_executions(world.a, unlinked, source_user_id=nobody, started_by=(world.a, link)) == []
    no_source = _batch(world, turn, source=None)
    for caller in (None, ""):
        assert db.get_fan_out_executions(world.a, no_source, source_user_id=nobody, started_by=(caller, turn)) == []


# ---- only the four by-id readers may use it ---------------------------------------------

def _callers(name):
    found = set()
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("enterprise/"):
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == name:
                    found.add((rel, fn.name))
    return found


def test_only_the_by_id_readers_use_the_reach():
    """A list, the running list, `terminate` or the scope helper itself that
    started calling it would widen what the ruling left narrowed: this fails
    first. `dependencies.py`, where the two are defined, is scanned too."""
    readers = {("routers/schedules.py", "get_execution"), ("routers/schedules.py", "get_execution_log"),
               ("routers/chat.py", "stream_execution_log")}
    assert _callers("row_is_acting_turns") == readers
    assert _callers("acting_turn_reach") == readers | {("routers/fan_out.py", "get_fan_out_status")}

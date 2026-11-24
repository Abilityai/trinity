"""A read narrowed by a running turn says so (trinity-enterprise#978).

While a shared person's (or a visitor's) turn runs on an agent, every request
made with the agent's key reads executions narrowed to that person: the
platform cannot tell which process sent a request, so it fails closed. That
was silent. An owner-started run reading history during such a turn got an
empty list, or "not found" for a finished run, with nothing to say why.

The behaviour stays (operator ruling 2026-11-23). What changes: a response
computed under the narrowed view carries `X-Trinity-Read-Narrowed: <code>`.

* `turn_person`  - the key's own turn is for a person or a visitor;
* `running_turn` - another running turn on the agent narrows this request;
* `unverified`   - the turn or the running turns could not be checked.

The header is decided from the principal, never from a row: a run that exists
and one that does not answer the same status, body and headers (Invariant #8).

Real routes, the real `get_current_user`, real JWTs, a real agent key and real
execution rows on the unit conftest's SQLite. The middleware is mounted the
way `main.py` mounts it, between two `BaseHTTPMiddleware` layers.
"""
from __future__ import annotations

import ast
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import event

import dependencies as dep
from database import db
from db.engine import get_engine
from db.write_params import TaskExecutionFields
from db_models import UserCreate
from dependencies import create_access_token, get_current_user
from routers import activities as activities_router
from routers import executions as executions_router
from routers import fan_out as fan_out_router
from routers import schedules as schedules_router
from services import turn_tier_service
from utils import read_narrowing

pytestmark = pytest.mark.unit

HEADER = "X-Trinity-Read-Narrowed"
TURN = "X-Trinity-Execution-Id"
_REPO = Path(__file__).resolve().parents[2]
SCHEDULE = {"name": "brief", "cron_expression": "0 9 * * 1", "message": "Weekly brief", "timezone": "UTC"}


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def inner(request, call_next):
        return await call_next(request)

    app.add_middleware(read_narrowing.ReadNarrowedMiddleware)

    @app.middleware("http")
    async def outer(request, call_next):
        return await call_next(request)

    return app


@pytest.fixture
def client():
    app = _app()
    for module in (schedules_router, fan_out_router, executions_router, activities_router):
        app.include_router(module.router)

    @app.get("/plain")
    def plain(current_user=Depends(get_current_user)):
        """A route that reads no viewer scope."""
        return {"ok": True}

    @app.get("/api/agents/{name}/uncensused")
    def uncensused(name: str, current_user=Depends(get_current_user)):
        """An agent-naming route the census has never seen: owner tier to the fence."""
        return {"ok": True}

    @app.get("/owner-check")
    def owner_check(agent: str, current_user=Depends(get_current_user)):
        dep.assert_agent_owner(current_user, agent)
        return {"ok": True}

    return TestClient(app)


def _person(role):
    username = f"p978-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(UserCreate(username=username, email=username, role=role, password="x"))
    user = db.get_user_by_username(username)
    return SimpleNamespace(username=username, id=user["id"], jwt=create_access_token({"sub": username}))


def _run(agent, person, *, done=True, **fields):
    row = db.create_task_execution(agent, "work", "manual",
                                   TaskExecutionFields(source_user_id=person.id, **fields))
    if done:
        assert db.update_execution_status(row.id, "success")
    return row.id


@pytest.fixture
def world():
    """An agent, its owner and a person it is shared with. The owner has a
    finished run and a running one; the person has a finished run."""
    owner, mate = _person("creator"), _person("operator")
    agent = f"a978-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(agent, owner.username)
    db.share_agent(agent, owner.username, mate.username)
    key = db.create_agent_mcp_api_key(agent, owner.username).api_key
    return SimpleNamespace(
        agent=agent, owner=owner, mate=mate, key=key,
        owner_done=_run(agent, owner), owner_live=_run(agent, owner, done=False),
        mate_done=_run(agent, mate))


@pytest.fixture
def shared_turn(world):
    """A shared person's turn, RUNNING on the agent. Returns its id."""
    return _run(world.agent, world.mate, done=False)


def get(client, world, path, *, turn=None, who="key"):
    headers = {"Authorization": f"Bearer {world.key if who == 'key' else getattr(world, who).jwt}"}
    if turn:
        headers[TURN] = turn
    return client.get(path, headers=headers)


def runs(world, suffix=""):
    return f"/api/agents/{world.agent}/executions{suffix}"


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


# ---- the header never depends on the row (Invariant #8) -----------------------------------

@pytest.mark.parametrize("suffix", ["", "/log"])
def test_a_run_that_exists_and_one_that_does_not_answer_the_same(client, world, shared_turn, suffix):
    """The owner's finished run is outside the narrowed view; so is an id that
    was never a run. Status, body, headers and query count are identical."""
    with query_count() as real:
        hidden = get(client, world, runs(world, f"/{world.owner_done}{suffix}"), turn=world.owner_live)
    with query_count() as missing_q:
        missing = get(client, world, runs(world, f"/no-such-run{suffix}"), turn=world.owner_live)
    assert (hidden.status_code, missing.status_code) == (404, 404)
    assert hidden.json() == missing.json() == {"detail": "Execution not found"}
    assert hidden.headers[HEADER] == missing.headers[HEADER] == "running_turn"
    assert dict(hidden.headers) == dict(missing.headers)
    assert len(real) == len(missing_q)


@pytest.mark.parametrize("suffix", ["", "/log"])
def test_without_a_narrowing_turn_neither_404_is_marked(client, world, suffix):
    for run in (world.owner_done, "no-such-run"):
        resp = get(client, world, runs(world, f"/{run}{suffix}"), turn=world.owner_live)
        assert HEADER not in resp.headers
    assert get(client, world, runs(world, f"/{world.owner_done}{suffix}"),
               turn=world.owner_live).status_code == 200


def test_the_code_does_not_vary_with_the_turn_id_sent_while_a_shared_turn_runs(
        client, world, shared_turn):
    """Else the header would tell a turn whether an execution id exists, or
    whose it was: an owner's turn, an unknown id, a share turn that has ended,
    another person's ended share turn, no id at all."""
    other = _person("operator")
    db.share_agent(world.agent, world.owner.username, other.username)
    turns = (world.owner_live, world.owner_done, "no-such-run", world.mate_done,
             _run(world.agent, other), None)
    answers = [get(client, world, runs(world, "/no-such-run"), turn=turn) for turn in turns]
    assert {(a.status_code, a.headers.get(HEADER)) for a in answers} == {(404, "running_turn")}


def test_an_ended_share_turns_id_is_not_the_keys_own_turn(client, world):
    """Nothing is running: a finished share turn's id narrows to its person
    as before, and reads `unverified`, like any id that is not a running turn."""
    resp = get(client, world, runs(world), turn=world.mate_done)
    assert (resp.status_code, resp.headers[HEADER]) == (200, "unverified")
    assert {r["id"] for r in resp.json()} == {world.mate_done}


# ---- the owner's concurrent run, on every narrowed read ----------------------------------

def test_the_owners_concurrent_run_reads_a_marked_list(client, world, shared_turn):
    resp = get(client, world, runs(world), turn=world.owner_live)
    assert resp.status_code == 200
    assert resp.headers[HEADER] == "running_turn"
    assert {r["id"] for r in resp.json()} == {world.mate_done, shared_turn}


def test_a_run_with_no_turn_header_is_marked_too(client, world, shared_turn):
    resp = get(client, world, runs(world))
    assert (resp.status_code, resp.headers[HEADER]) == (200, "running_turn")


@pytest.mark.parametrize("suffix", ["", "/log"])
def test_a_run_inside_the_view_is_read_and_marked(client, world, shared_turn, suffix):
    resp = get(client, world, runs(world, f"/{world.mate_done}{suffix}"), turn=world.owner_live)
    assert (resp.status_code, resp.headers[HEADER]) == (200, "running_turn")


def test_schedule_history_is_marked(client, world, shared_turn):
    made = client.post(f"/api/agents/{world.agent}/schedules", json=SCHEDULE,
                       headers={"Authorization": f"Bearer {world.owner.jwt}"})
    assert made.status_code == 201, made.text
    resp = get(client, world, f"/api/agents/{world.agent}/schedules/{made.json()['id']}/executions",
               turn=world.owner_live)
    assert (resp.status_code, resp.headers[HEADER]) == (200, "running_turn")


def test_a_fan_out_outside_the_view_is_a_marked_not_found(client, world, shared_turn):
    _run(world.agent, world.owner, fan_out_id="fo_owner978", fan_out_task_id="t1")
    resp = get(client, world, f"/api/agents/{world.agent}/fan-out/fo_owner978", turn=world.owner_live)
    never = get(client, world, f"/api/agents/{world.agent}/fan-out/fo_never978", turn=world.owner_live)
    assert (resp.status_code, never.status_code) == (404, 404)
    assert resp.json() == never.json()
    assert resp.headers[HEADER] == never.headers[HEADER] == "running_turn"


def test_a_fan_out_inside_the_view_is_read_and_marked(client, world, shared_turn):
    _run(world.agent, world.mate, fan_out_id="fo_mate978", fan_out_task_id="t1")
    resp = get(client, world, f"/api/agents/{world.agent}/fan-out/fo_mate978", turn=world.owner_live)
    assert (resp.status_code, resp.headers[HEADER]) == (200, "running_turn")


@pytest.mark.parametrize("path", ["/api/activities/timeline", "/api/executions"])
def test_the_fleet_reads_are_marked(client, world, shared_turn, path):
    resp = get(client, world, path, turn=world.owner_live)
    assert (resp.status_code, resp.headers[HEADER]) == (200, "running_turn")


def test_the_full_view_returns_unmarked_when_the_turn_ends(client, world, shared_turn):
    assert get(client, world, runs(world), turn=world.owner_live).headers[HEADER] == "running_turn"
    assert db.update_execution_status(shared_turn, "success")
    resp = get(client, world, runs(world), turn=world.owner_live)
    assert HEADER not in resp.headers
    assert {world.owner_done, world.owner_live, world.mate_done, shared_turn} <= {r["id"] for r in resp.json()}


# ---- the three codes ----------------------------------------------------------------------

def test_the_keys_own_shared_turn_reads_turn_person(client, world, shared_turn):
    resp = get(client, world, runs(world), turn=shared_turn)
    assert (resp.status_code, resp.headers[HEADER]) == (200, "turn_person")
    assert {r["id"] for r in resp.json()} == {world.mate_done, shared_turn}


def test_an_unknown_turn_id_with_nothing_running_reads_unverified(client, world):
    resp = get(client, world, runs(world), turn="no-such-run")
    assert (resp.status_code, resp.headers[HEADER], resp.json()) == (200, "unverified", [])


def test_an_unreadable_ledger_reads_unverified(client, world, monkeypatch):
    def boom(agent):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(db, "get_running_turn_attribution", boom)
    resp = get(client, world, runs(world), turn=world.owner_live)
    assert (resp.status_code, resp.headers[HEADER], resp.json()) == (200, "unverified", [])


def test_the_codes_are_the_declared_ones():
    assert turn_tier_service.READ_NARROWED_CODES == ("turn_person", "running_turn", "unverified")
    assert read_narrowing.HEADER == HEADER


# ---- who is never marked ------------------------------------------------------------------

def test_a_route_that_reads_no_scope_is_not_marked(client, world, shared_turn):
    assert get(client, world, runs(world), turn=world.owner_live).headers[HEADER] == "running_turn"  # control
    resp = get(client, world, "/plain", turn=world.owner_live)
    assert resp.status_code == 200 and HEADER not in resp.headers


@pytest.mark.parametrize("who", ["owner", "mate"])
def test_a_signed_in_person_is_never_marked(client, world, shared_turn, who):
    """Only an agent's key is narrowed by a running turn. A share holder's own
    narrowing is by design and unchanged."""
    resp = get(client, world, runs(world), who=who)
    assert resp.status_code == 200 and HEADER not in resp.headers


def test_no_marker_without_a_running_shared_turn(client, world):
    resp = get(client, world, runs(world), turn=world.owner_live)
    assert resp.status_code == 200 and HEADER not in resp.headers
    assert {world.owner_done, world.owner_live, world.mate_done} <= {r["id"] for r in resp.json()}


# ---- the recorder and the middleware ------------------------------------------------------

def _probe_app():
    app = _app()

    @app.get("/sync")
    def sync_route():
        read_narrowing.note("running_turn")
        return {"ok": True}

    @app.get("/async")
    async def async_route():
        read_narrowing.note("turn_person")
        return {"ok": True}

    @app.get("/twice")
    def twice():
        read_narrowing.note("turn_person")
        read_narrowing.note("running_turn")
        return {"ok": True}

    @app.get("/missing")
    def missing():
        read_narrowing.note("unverified")
        raise HTTPException(404, "nope")

    @app.get("/stream")
    def stream():
        read_narrowing.note("running_turn")
        return StreamingResponse(iter([b"a", b"b"]), media_type="text/event-stream")

    @app.get("/clean")
    def clean():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    return app


@pytest.mark.parametrize("path,status,code", [
    ("/sync", 200, "running_turn"), ("/async", 200, "turn_person"),
    ("/missing", 404, "unverified"), ("/stream", 200, "running_turn")])
def test_the_middleware_carries_the_code_on_every_kind_of_response(path, status, code):
    resp = TestClient(_probe_app()).get(path)
    assert (resp.status_code, resp.headers.get_list(HEADER)) == (status, [code])


def test_the_first_code_of_a_request_sticks():
    assert TestClient(_probe_app()).get("/twice").headers.get_list(HEADER) == ["turn_person"]


def test_one_requests_code_does_not_reach_the_next():
    c = TestClient(_probe_app())
    assert c.get("/sync").headers[HEADER] == "running_turn"
    assert HEADER not in c.get("/clean").headers


def test_outside_a_request_noting_is_a_no_op():
    read_narrowing.note("running_turn")
    assert read_narrowing.noted() is None


def test_a_code_of_the_wrong_shape_is_not_sent():
    app = _app()

    @app.get("/bad")
    def bad():
        read_narrowing.note("bad value\r\nX-Injected: 1")
        return {"ok": True}

    resp = TestClient(app).get("/bad")
    assert HEADER not in resp.headers and "X-Injected" not in resp.headers


def test_a_handler_cannot_set_the_header_itself():
    app = _app()

    @app.get("/forged")
    def forged():
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True}, headers={HEADER: "running_turn"})

    @app.get("/forged-and-noted")
    def forged_and_noted():
        from fastapi.responses import JSONResponse
        read_narrowing.note("turn_person")
        return JSONResponse({"ok": True}, headers={HEADER: "running_turn"})

    c = TestClient(app)
    assert HEADER not in c.get("/forged").headers
    assert c.get("/forged-and-noted").headers.get_list(HEADER) == ["turn_person"]


def test_a_websocket_and_the_lifespan_pass_through():
    app = _probe_app()
    started = []
    app.router.on_startup.append(lambda: started.append(True))
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_text() == "hello"
    assert started == [True]


def test_main_mounts_the_middleware_inside_the_host_guard():
    """The mini-apps above mount it themselves; this is the line production
    depends on. `add_middleware` prepends, so the Host guard stays last."""
    tree = ast.parse((_REPO / "src" / "backend" / "main.py").read_text())
    added = [n.args[0].id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_middleware"
             and n.args and isinstance(n.args[0], ast.Name)]
    assert "ReadNarrowedMiddleware" in added, added
    assert added.index("ReadNarrowedMiddleware") < added.index("HostHeaderGuard")


# ---- a refusal caused by another running turn says so -------------------------------------

def _refused(client, world, path, *, turn, method="GET", **kw):
    headers = {"Authorization": f"Bearer {world.key}"}
    if turn:
        headers[TURN] = turn
    resp = client.request(method, path, headers=headers, **kw)
    assert resp.status_code == 403, resp.text
    return resp.json()["detail"]


def test_the_owners_concurrent_run_is_told_another_turn_is_running(client, world, shared_turn):
    agent = world.agent
    for turn in (world.owner_live, None):
        assert _refused(client, world, f"/api/agents/{agent}/uncensused",
                        turn=turn) == dep.OWNER_TIER_TURN_REFUSED_RUNNING_TURN
        assert _refused(client, world, f"/api/agents/{agent}/schedules", turn=turn, method="POST",
                        json=SCHEDULE) == dep.STANDING_WORK_REFUSED_RUNNING_TURN
        assert _refused(client, world, f"/owner-check?agent={agent}",
                        turn=turn) == dep.OWNER_TIER_TURN_REFUSED_RUNNING_TURN


def test_the_shared_persons_own_turn_keeps_its_text(client, world, shared_turn):
    agent = world.agent
    assert _refused(client, world, f"/api/agents/{agent}/uncensused",
                    turn=shared_turn) == dep.OWNER_TIER_TURN_REFUSED
    assert _refused(client, world, f"/api/agents/{agent}/schedules", turn=shared_turn, method="POST",
                    json=SCHEDULE) == dep.STANDING_WORK_REFUSED
    assert _refused(client, world, f"/owner-check?agent={agent}", turn=shared_turn) == dep.OWNER_TIER_TURN_REFUSED


def test_an_unreadable_ledger_keeps_the_old_text(client, world, monkeypatch):
    """Refused (fail closed), but "another turn is running" would be a guess."""
    def boom(agent):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(db, "get_running_turn_attribution", boom)
    assert _refused(client, world, f"/api/agents/{world.agent}/uncensused",
                    turn=world.owner_live) == dep.OWNER_TIER_TURN_REFUSED


def test_the_two_texts_differ_and_name_nobody():
    for old, new in ((dep.OWNER_TIER_TURN_REFUSED, dep.OWNER_TIER_TURN_REFUSED_RUNNING_TURN),
                     (dep.STANDING_WORK_REFUSED, dep.STANDING_WORK_REFUSED_RUNNING_TURN)):
        assert new != old and new.startswith("Another turn is running on this agent")


# ---- the MCP server words every code ------------------------------------------------------

def test_the_mcp_server_has_a_message_for_every_code():
    """A cross-surface pin: the message table lives in the MCP server (its own
    tests execute it); a code added here without wording there would reach an
    agent as the generic fallback."""
    source = (_REPO / "src" / "mcp-server" / "src" / "read-narrowing.ts").read_text()
    table = source[source.index("READ_NARROWED_MESSAGES"):]
    for code in turn_tier_service.READ_NARROWED_CODES:
        assert re.search(rf"^\s*{code}:", table, re.M), f"no MCP message for {code!r}"
    assert f'"{HEADER}"' in source or f'"{HEADER}"' in (
        _REPO / "src" / "mcp-server" / "src" / "turn-context.ts").read_text()

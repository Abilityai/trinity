"""trinity-enterprise#719 — a self-gated route acts only as the agent its key names.

`dependencies.assert_own_agent` is the agent-key self-gate (trinity-enterprise#840):
the handler's first statement, recognised by the route census. This file pins the
rule it now applies, through the real routers (FastAPI routing, the real access
dependencies, a fresh tmp database):

* **emit-event acts as the caller.** `POST /api/agents/{name}/emit-event` records
  `{name}` as the event's source, and each subscriber's delivery is authorised
  against that recorded source (trinity-enterprise#739). So the source must be the
  caller's own agent: an agent key emits only as itself, and a person (the owner,
  on a session or with their own key) may still emit for an agent. The access
  dependency runs first, so a nonexistent agent and one outside the key's reach
  get the same 404, and only an agent the key already reaches gets the 403 (the
  #3929 house shape). After the self-gate the route admits exactly an agent key,
  the system key and a person, as an allowlist that fails closed: every other
  principal the access dependency would admit is refused.
* **The system key is `trinity-system`.** It carries no agent name on REST, and
  the gate holds it to the agent it is — the rule `get_self_acting_agent`
  already applies (trinity-enterprise#611). The
  route set is derived from the census by gate kind, so a route that joins the
  self-gate joins this matrix: naming another agent gets that route's own
  refusal, byte for byte what an agent key acting as another agent gets; naming
  `trinity-system` passes. The three `get_self_acting_agent` routes are pinned
  as they were.

The matrix observes the gate at its call site: the router module's
`assert_own_agent` is wrapped so the REAL helper decides and the wrapper records
the decision, then stops the request when it passes (a fixed 418), so no
handler work runs past the gate. The fresh-install case at the end runs whole
handlers, wrapper-free.

Principals are real `models.User` objects shaped like `get_current_user`'s output
(an MCP key resolves to its owner, carrying the owner's role); the loopback is a
real signed JWT through the real entry fence. Sync throughout
(`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_UNIT = str(Path(__file__).resolve().parent)
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)

import _route_census as rc  # noqa: E402
from db_harness import count, db_backend, run  # noqa: F401  (db_backend is a fixture)

pytestmark = pytest.mark.unit

OWNER = {"id": 1, "username": "admin", "email": "admin@example.com", "role": "admin"}
SYSTEM_AGENT = "trinity-system"
SOURCE = "ent719-source"  # the agent the emit names
SIBLING = "ent719-sibling"  # holds a permission edge to SOURCE: inside its reach
STRANGER = "ent719-stranger"  # exists; SIBLING has no edge to it
MISSING = "ent719-missing"  # does not exist

EMIT_DETAIL = "Agent-scoped key may only emit events as its own agent"
EMITTER_REQUIRED = "Only an agent's own key or a person may emit an event for an agent"
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"


def _user(**fields):
    from models import User

    return User(**{**OWNER, **fields})


def _agent_key(agent: str):
    return _user(mcp_scope="agent", agent_name=agent)


# --------------------------------------------------------------------------- #
# A fresh install: the schema, its admin, trinity-system, and nothing else
# --------------------------------------------------------------------------- #


def _add_agent(name: str, *, is_system: int = 0) -> None:
    run(
        "INSERT INTO agent_ownership (agent_name, owner_id, is_system, created_at) "
        "VALUES (:n, :o, :s, '2026-10-01T00:00:00Z')",
        n=name,
        o=OWNER["id"],
        s=is_system,
    )


def _add_edge(source: str, target: str) -> None:
    run(
        "INSERT INTO agent_permissions (source_agent, target_agent, created_at, created_by) "
        "VALUES (:s, :t, '2026-10-01T00:00:00Z', 'admin')",
        s=source,
        t=target,
    )


@pytest.fixture
def install(db_backend):
    run(
        "INSERT INTO users (id, username, role, email, created_at, updated_at) "
        "VALUES (:i, :u, :r, :e, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
        i=OWNER["id"],
        u=OWNER["username"],
        r=OWNER["role"],
        e=OWNER["email"],
    )
    _add_agent(SYSTEM_AGENT, is_system=1)
    return types.SimpleNamespace(add_agent=_add_agent, add_edge=_add_edge)


def _current_user_leaves(app: FastAPI):
    """Every `get_current_user` the mounted routes resolve through.

    Collected from the routes' own dependency trees rather than imported: the
    unit conftest re-imports `dependencies` between tests while a router keeps
    the function it bound at import (2026-09-23)."""
    leaves, stack = set(), [r.dependant for r in app.routes if hasattr(r, "dependant")]
    while stack:
        dependant = stack.pop()
        for dep in dependant.dependencies:
            if getattr(dep.call, "__name__", "") == "get_current_user":
                leaves.add(dep.call)
            stack.append(dep)
    assert leaves, "no get_current_user in the mounted routes"
    return leaves


def _client(app: FastAPI, holder: dict):
    for leaf in _current_user_leaves(app):
        app.dependency_overrides[leaf] = lambda: holder["principal"]
    return TestClient(app)


# --------------------------------------------------------------------------- #
# emit-event
# --------------------------------------------------------------------------- #


@pytest.fixture
def emit(install, monkeypatch):
    install.add_agent(SOURCE)
    install.add_agent(SIBLING)
    install.add_agent(STRANGER)
    install.add_edge(SIBLING, SOURCE)

    events = importlib.import_module("routers.event_subscriptions")
    # No socket fan-out from a unit test, whatever another test left wired.
    monkeypatch.setattr(events, "_websocket_manager", None)
    monkeypatch.setattr(events, "_filtered_websocket_manager", None)
    app = FastAPI()
    app.include_router(events.router)
    holder: dict = {}
    client = _client(app, holder)

    def post(principal, agent, body=None):
        holder["principal"] = principal
        return client.post(
            f"/api/agents/{agent}/emit-event",
            json=body if body is not None else {"event_type": "work.done"},
        )

    yield types.SimpleNamespace(post=post, app=app)
    app.dependency_overrides.clear()


def _events(source: str) -> int:
    return count("agent_events", "source_agent = :s", s=source)


def test_an_in_reach_sibling_key_is_refused(emit):
    """The sibling reaches SOURCE through its edge, so the access check admits
    it; the self-gate is what refuses it. Nothing is recorded."""
    response = emit.post(_agent_key(SIBLING), SOURCE)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == EMIT_DETAIL
    assert _events(SOURCE) == 0


def test_a_nonexistent_and_an_out_of_reach_agent_get_the_same_404(emit):
    """The access dependency answers first, uniformly: the refusal says nothing
    the key did not already know about the target (#186)."""
    missing = emit.post(_agent_key(SIBLING), MISSING)
    stranger = emit.post(_agent_key(SIBLING), STRANGER)
    assert missing.status_code == 404, missing.text
    assert (stranger.status_code, stranger.content) == (
        missing.status_code,
        missing.content,
    )
    assert _events(MISSING) == _events(STRANGER) == 0


def test_the_agents_own_key_emits_as_itself(emit):
    response = emit.post(
        _agent_key(SOURCE), SOURCE, {"event_type": "work.done", "payload": {"n": 1}}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (
        body["source_agent"],
        body["event_type"],
        body["subscriptions_triggered"],
    ) == (SOURCE, "work.done", 0)
    assert _events(SOURCE) == 1


@pytest.mark.parametrize(
    "principal",
    [lambda: _user(mcp_scope=None), lambda: _user(mcp_scope="user")],
    ids=["owner-session", "owner-user-key"],
)
def test_the_owner_as_a_person_emits_for_the_agent(emit, principal):
    """The docstring's use: a person names the source agent explicitly."""
    response = emit.post(principal(), SOURCE)
    assert response.status_code == 201, response.text
    assert response.json()["source_agent"] == SOURCE
    assert _events(SOURCE) == 1


@pytest.mark.parametrize("vouched", [None, SIBLING], ids=["unvouched", "vouched"])
def test_a_loopback_token_is_refused_by_the_entry_fence(emit, vouched):
    """A real, signed event-loopback JWT, through real authentication: the
    entry fence confines it to the subscriber-task dispatch (ent#614), so it
    never reaches this route's gate."""
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY

    claims = {"sub": "admin", "scope": "event_loopback", "exp": int(time.time()) + 300}
    if vouched:
        claims["source_agent"] = vouched
    token = jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)
    emit.app.dependency_overrides.clear()  # real get_current_user
    response = TestClient(emit.app).post(
        f"/api/agents/{SOURCE}/emit-event",
        json={"event_type": "work.done"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert _events(SOURCE) == 0


class _NoScopeAttribute:
    """The owner as a principal with NO `mcp_scope` attribute at all.

    `models.User` always declares the field, so this is the shape of a stand-in
    that does not match the real principal; the allowlist must fail closed on it
    (a sentinel, never `getattr(..., None)`, which reads absent as a session)."""

    id = OWNER["id"]
    username = OWNER["username"]
    email = OWNER["email"]
    role = OWNER["role"]
    agent_name = None
    connector_agent = None
    portal_delegate = False


# Matrix M's principals outside the emitter allowlist. Authentication is
# overridden, so the `get_current_user` entry fences that stop some of them in
# production are out of the picture: the refusal can only come from the route.
# Each one is admitted by the access dependency (the owner is admin; the
# connector is bound to SOURCE) and passes the self-gate (it acts as no agent),
# so the allowlist is the only thing between it and a recorded event.
OUTSIDE_THE_ALLOWLIST = {
    "unknown-scope": lambda: _user(mcp_scope="a-scope-no-one-has-shipped"),
    "ops": lambda: _user(mcp_scope="ops"),
    "portal-delegate": lambda: _user(mcp_scope="portal_delegate", portal_delegate=True),
    "connector": lambda: _user(mcp_scope="connector", connector_agent=SOURCE),
    "no-mcp-scope": _NoScopeAttribute,
    "vouched-loopback": lambda: _user(
        mcp_scope=None, is_event_loopback=True, vouched_source_agent=SIBLING
    ),
    # The loopback with no source agent, past the entry fence: only
    # `is_person_principal`'s loopback check can refuse it here.
    "unvouched-loopback": lambda: _user(mcp_scope=None, is_event_loopback=True),
    # Not a shape `get_current_user` produces: a vouched source without the
    # loopback flag. It pins the allowlist's own vouched-source refusal, which
    # `is_person_principal`'s loopback check would otherwise mask.
    "vouched-source-no-loopback-flag": lambda: _user(
        mcp_scope=None, vouched_source_agent=SIBLING
    ),
}


@pytest.mark.parametrize("label", sorted(OUTSIDE_THE_ALLOWLIST))
def test_a_principal_outside_the_emitter_allowlist_is_refused(emit, label):
    response = emit.post(OUTSIDE_THE_ALLOWLIST[label](), SOURCE)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == EMITTER_REQUIRED
    assert _events(SOURCE) == 0


def test_the_system_key_emits_as_trinity_system(emit):
    """The allowlist's system branch: the system key names the agent it is."""
    response = emit.post(_user(mcp_scope="system"), SYSTEM_AGENT)
    assert response.status_code == 201, response.text
    assert response.json()["source_agent"] == SYSTEM_AGENT
    assert _events(SYSTEM_AGENT) == 1


# --------------------------------------------------------------------------- #
# The system key is trinity-system on every self-gated route
# --------------------------------------------------------------------------- #

# The census is read once, at collection, to parametrise (a pure AST walk).
_CENSUS = rc.walk()


def _self_gated(kind: str):
    return sorted(
        key
        for key in rc.AGENT_CALLABLE_SELF
        if _CENSUS.routes[key].self_gate is not None
        and _CENSUS.routes[key].self_gate.kind == kind
    )


ASSERT_OWN_ROUTES = _self_gated("assert_own_agent")
IDENTITY_ROUTES = _self_gated("get_self_acting_agent")

TARGET = "ent719-target"  # the agent the path names
CALLER = "ent719-caller"  # an agent key with an edge to TARGET: inside its reach

# A well-formed request per route: enough for FastAPI to resolve every
# dependency and validate the body, so the handler's first statement runs.
# Nothing past the gate is executed in the matrix.
REQUESTS = {
    "routers/a2a.py::call_a2a_agent": {
        "json": {"endpoint": "peer", "message": "hello", "dedup_label": "ent719"}
    },
    "routers/a2a.py::get_a2a_task": {"json": {"endpoint": "peer", "task_id": "task-1"}},
    "routers/agent_files.py::get_agent_canon_roles": {},
    "routers/agent_files.py::get_agent_objectives": {},
    "routers/agent_files.py::share_agent_file": {"json": {"filename": "out.txt"}},
    "routers/agents.py::send_voice_reply_endpoint": {
        "json": {"text": "hello", "execution_id": "exec-1"}
    },
    "routers/canvas.py::bulk_delete_canvases": {"json": {"canvas_ids": ["main"]}},
    "routers/canvas.py::clear_canvas": {},
    "routers/canvas.py::patch_canvas": {
        "json": {"blocks": [{"kind": "markdown", "id": "b1", "payload": {"text": "x"}}]}
    },
    "routers/canvas.py::write_canvas": {
        "json": {"blocks": [{"kind": "markdown", "payload": {"text": "x"}}]}
    },
    "routers/event_subscriptions.py::emit_event_for_agent": {
        "json": {"event_type": "work.done"}
    },
    "routers/metric_points.py::record_metric_points": {
        "json": {"points": [{"metric": "jobs_done", "value": 1}]}
    },
    "routers/nevermined_delegation.py::get_agent_delegation": {},
    "routers/public_memory.py::write_user_memory": {
        "json": {"execution_id": "exec-1", "memory_text": "a note"}
    },
    "routers/reminders.py::cancel_reminder_endpoint": {},
    "routers/reminders.py::create_reminder_endpoint": {
        "json": {"message": "ping", "delay_seconds": 60}
    },
    "routers/reminders.py::list_reminders_endpoint": {},
    "routers/reports.py::create_report": {
        "json": {"report_type": "ops.status", "title": "t", "payload": {}}
    },
    "routers/seat_decisions.py::list_seat_decisions": {
        "params": {"execution_id": "exec-1"}
    },
    "routers/seat_decisions.py::record_seat_decision": {
        "json": {
            "execution_id": "exec-1",
            "outcome": "approved",
            "decided": "d",
            "criterion": "c",
            "reversal": "r",
            "review_by": "2026-12-01",
        }
    },
}
PATH_VALUES = {"canvas_id": "main", "reminder_id": "rem-1"}
PASSED = (418, "passed the self-gate")


def _mount(key: str):
    """The one router of the route's module that serves it, mounted alone."""
    rel, qual = key.split("::")
    module = importlib.import_module(rc._module_of(rel))
    handler = getattr(module, qual)
    owners = [
        value
        for value in vars(module).values()
        if isinstance(value, APIRouter)
        and any(getattr(r, "endpoint", None) is handler for r in value.routes)
    ]
    assert len(owners) == 1, f"{key}: {len(owners)} routers serve the handler"
    app = FastAPI()
    app.include_router(owners[0])
    method, path = rc.AGENT_CALLABLE_SELF[key][0].split(" ", 1)
    assert any(
        isinstance(r, APIRoute)
        and r.endpoint is handler
        and r.path == path
        and method in r.methods
        for r in app.routes
    ), f"{key}: not mounted at the census's {method} {path}"
    return module, app, method, path


def _url(path: str, agent: str) -> str:
    out = path.replace("{name}", agent).replace("{agent_name}", agent)
    for param, value in PATH_VALUES.items():
        out = out.replace("{" + param + "}", value)
    assert "{" not in out, f"unfilled path parameter in {out}"
    return out


@pytest.fixture
def gate(install):
    """Drive one self-gated route through its real router, observing the gate."""
    install.add_agent(TARGET)
    install.add_agent(CALLER)
    install.add_edge(CALLER, TARGET)

    def drive(key: str, principal, agent: str):
        module, app, method, path = _mount(key)
        real = module.assert_own_agent
        # Not `is`: the conftest re-imports `dependencies` between tests while a
        # router keeps the function it bound at import (2026-09-23).
        assert (real.__module__, real.__qualname__) == (
            "dependencies",
            "assert_own_agent",
        )
        seen = []

        def observed(*args, **kwargs):
            try:
                real(*args, **kwargs)
            except HTTPException as refused:
                seen.append(("refused", refused.status_code, refused.detail))
                raise
            seen.append(("passed",))
            raise HTTPException(status_code=PASSED[0], detail=PASSED[1])

        # A private patcher: undoing it must not undo the fresh-DB fixture.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "assert_own_agent", observed)
            if hasattr(module, "a2a_outbound_service"):  # the kill switch, on
                patch.setattr(
                    module.a2a_outbound_service, "is_outbound_enabled", lambda: True
                )
            holder = {"principal": principal}
            response = _client(app, holder).request(
                method, _url(path, agent), **REQUESTS[key]
            )
        assert seen, (
            f"{key}: the request never reached the handler's self-gate: "
            f"{response.status_code} {response.text}"
        )
        return response, seen

    return drive


def test_every_self_gated_route_has_a_request():
    """A route that joins the self-gate joins this matrix; give it a request."""
    assert set(ASSERT_OWN_ROUTES) == set(REQUESTS), (
        sorted(set(ASSERT_OWN_ROUTES) - set(REQUESTS)),
        sorted(set(REQUESTS) - set(ASSERT_OWN_ROUTES)),
    )
    assert len(ASSERT_OWN_ROUTES) >= 20 and len(IDENTITY_ROUTES) == 3


@pytest.mark.parametrize("key", ASSERT_OWN_ROUTES)
def test_the_system_key_naming_another_agent_gets_the_routes_own_refusal(gate, key):
    system, _ = gate(key, _user(mcp_scope="system"), TARGET)
    sibling, sibling_seen = gate(key, _agent_key(CALLER), TARGET)
    assert (
        sibling_seen[0][0] == "refused"
    ), f"{key}: the reference agent key was not refused"
    expected = 403 if not _CENSUS.routes[key].self_gate.refusal else sibling.status_code
    assert system.status_code == expected, f"{key}: {system.status_code} {system.text}"
    assert (system.status_code, system.content) == (
        sibling.status_code,
        sibling.content,
    )


@pytest.mark.parametrize("key", ASSERT_OWN_ROUTES)
def test_the_system_key_passes_as_trinity_system(gate, key):
    response, seen = gate(key, _user(mcp_scope="system"), SYSTEM_AGENT)
    assert seen == [("passed",)], f"{key}: {seen}"
    assert (response.status_code, response.json()["detail"]) == PASSED


@pytest.mark.parametrize("key", ASSERT_OWN_ROUTES)
def test_a_person_and_the_agents_own_key_still_pass(gate, key):
    for principal in (
        _user(mcp_scope=None),
        _user(mcp_scope="user"),
        _agent_key(TARGET),
    ):
        response, seen = gate(key, principal, TARGET)
        assert seen == [("passed",)], f"{key} {principal.mcp_scope}: {seen}"
        assert response.status_code == PASSED[0], response.text


def test_the_canvas_removal_helper_holds_the_system_key_when_reached_alone(install):
    """`_gate_human_removal` runs the self-gate itself, so the helper is never
    weaker than the routes that call it: reached alone, it holds the system key
    to `trinity-system`'s canvases exactly as `assert_own_agent` does."""
    install.add_agent(TARGET)
    install.add_agent(CALLER)
    canvas = importlib.import_module("routers.canvas")

    for principal in (_user(mcp_scope="system"), _agent_key(CALLER)):
        with pytest.raises(HTTPException) as refused:
            canvas._gate_human_removal(principal, TARGET)
        assert (refused.value.status_code, refused.value.detail) == (
            403,
            canvas._OWN_CANVAS,
        ), principal.mcp_scope

    canvas._gate_human_removal(_user(mcp_scope="system"), SYSTEM_AGENT)
    canvas._gate_human_removal(_agent_key(TARGET), TARGET)
    canvas._gate_human_removal(_user(mcp_scope=None), TARGET)  # the owner


# --------------------------------------------------------------------------- #
# The get_self_acting_agent routes, pinned as they were
# --------------------------------------------------------------------------- #

IDENTITY_REQUESTS = {
    "routers/operator_queue.py::get_my_ask": {},
    "routers/operator_queue.py::raise_my_ask": {
        "json": {"request_id": "ask-1", "title": "t"}
    },
    "routers/operator_queue.py::withdraw_my_ask": {"json": {}},
}
IDENTITY_PATH_VALUES = {"request_id": "ask-1"}


@pytest.fixture
def identity(install):
    install.add_agent(TARGET)
    install.add_agent(CALLER)
    install.add_edge(CALLER, TARGET)

    def drive(key: str, principal, agent: str):
        rel, qual = key.split("::")
        module = importlib.import_module(rc._module_of(rel))
        handler = getattr(module, qual)
        app = FastAPI()
        for value in vars(module).values():
            if isinstance(value, APIRouter) and any(
                getattr(r, "endpoint", None) is handler for r in value.routes
            ):
                app.include_router(value)
        method, path = rc.AGENT_CALLABLE_SELF[key][0].split(" ", 1)
        fences = {
            dep.call
            for r in app.routes
            if isinstance(r, APIRoute) and r.endpoint is handler
            for dep in r.dependant.dependencies
            if getattr(dep.call, "__name__", "") == "get_self_acting_agent"
        }
        assert len(fences) == 1, key
        # Past the identity check comes the ordinary access check: reaching it
        # is "identity passed". Stop there, after the real access check ran.
        namespace = next(iter(fences)).__globals__
        real_access = namespace["get_authorized_agent"]

        def access_then_stop(name, current_user):
            real_access(name, current_user)
            raise HTTPException(status_code=PASSED[0], detail=PASSED[1])

        url = path.replace("{name}", agent)
        for param, value in IDENTITY_PATH_VALUES.items():
            url = url.replace("{" + param + "}", value)
        with pytest.MonkeyPatch.context() as patch:
            patch.setitem(namespace, "get_authorized_agent", access_then_stop)
            response = _client(app, {"principal": principal}).request(
                method, url, **IDENTITY_REQUESTS[key]
            )
        return response

    return drive


def _identity_refused(response) -> bool:
    return response.status_code == 403 and response.json()["detail"]["code"] == (
        "agent_identity_required"
    )


@pytest.mark.parametrize("key", IDENTITY_ROUTES)
def test_the_identity_routes_are_unchanged(identity, key):
    assert set(IDENTITY_ROUTES) == set(IDENTITY_REQUESTS)
    passes = {
        "system key as trinity-system": (_user(mcp_scope="system"), SYSTEM_AGENT),
        "the agent's own key": (_agent_key(TARGET), TARGET),
    }
    refused = {
        "system key naming another agent": (_user(mcp_scope="system"), TARGET),
        "an in-reach agent key": (_agent_key(CALLER), TARGET),
        "a person on a session": (_user(mcp_scope=None), TARGET),
        "a person's user key": (_user(mcp_scope="user"), TARGET),
    }
    for label, (principal, agent) in passes.items():
        response = identity(key, principal, agent)
        assert response.status_code == PASSED[0], f"{key} {label}: {response.text}"
    for label, (principal, agent) in refused.items():
        response = identity(key, principal, agent)
        assert _identity_refused(response), f"{key} {label}: {response.text}"


# --------------------------------------------------------------------------- #
# Other shape: a fresh install, whole handlers, no wrapper
# --------------------------------------------------------------------------- #

SOLO = "ent719-solo"  # the only agent besides trinity-system; nobody subscribes


@pytest.fixture
def fresh(install, monkeypatch):
    install.add_agent(SOLO)
    # No socket fan-out from a unit test, whatever another test left wired.
    for owner in ("routers.event_subscriptions", "services.report_service"):
        module = importlib.import_module(owner)
        for attr in ("_websocket_manager", "_filtered_websocket_manager"):
            monkeypatch.setattr(module, attr, None)
    apps = {}
    for name in ("event_subscriptions", "reports", "canvas"):
        app = FastAPI()
        app.include_router(importlib.import_module(f"routers.{name}").router)
        apps[name] = app
    holder: dict = {}

    def call(app_name, principal, method, url, **kwargs):
        holder["principal"] = principal
        return _client(apps[app_name], holder).request(method, url, **kwargs)

    return call


def test_on_a_fresh_install_each_key_acts_as_its_own_agent(fresh):
    assert count("agent_ownership") == 2 and count("agent_event_subscriptions") == 0

    emitted = fresh(
        "event_subscriptions",
        _agent_key(SOLO),
        "POST",
        f"/api/agents/{SOLO}/emit-event",
        json={"event_type": "solo.started"},
    )
    assert emitted.status_code == 201, emitted.text
    assert (
        emitted.json()["source_agent"],
        emitted.json()["subscriptions_triggered"],
    ) == (SOLO, 0)

    system = _user(mcp_scope="system")
    report = fresh(
        "reports",
        system,
        "POST",
        f"/api/agents/{SYSTEM_AGENT}/reports",
        json={
            "report_type": "ops.fleet_status",
            "title": "Fleet",
            "payload": {"agents": 2},
        },
    )
    assert report.status_code == 201, report.text
    assert report.json()["agent_name"] == SYSTEM_AGENT
    canvas = fresh(
        "canvas",
        system,
        "PUT",
        f"/api/agents/{SYSTEM_AGENT}/canvas/main",
        json={
            "title": "Fleet",
            "blocks": [{"kind": "markdown", "payload": {"text": "ok"}}],
        },
    )
    assert canvas.status_code == 200, canvas.text
    assert count("agent_canvases", "agent_name = :a", a=SYSTEM_AGENT) == 1

    # ...and the same key may not write SOLO's.
    refused = fresh(
        "canvas",
        system,
        "PUT",
        f"/api/agents/{SOLO}/canvas/main",
        json={"blocks": [{"kind": "markdown", "payload": {"text": "x"}}]},
    )
    assert refused.status_code == 403, refused.text
    assert count("agent_canvases", "agent_name = :a", a=SOLO) == 0

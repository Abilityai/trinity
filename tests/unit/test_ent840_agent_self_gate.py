"""trinity-enterprise#840 — the agent-key self-gate is one recognisable helper.

An agent-scoped key resolves to its OWNER carrying the owner's role, so the
access check answers "may this key reach that agent" (since ent#629: itself, its
permission-edge targets, the agents it spawned), never "is it acting on its own
agent". `dependencies.assert_own_agent` answers the second question. The route
census (`tests/unit/_route_census.py`) recognises it as a handler's first
statement — or `Depends(get_self_acting_agent)` in the signature — and lists
every such route in `AGENT_CALLABLE_SELF` with why it is a use, not a grant.

Pins:
* the helper's truth table: every principal kind, the route's own message, the
  `refusal=` factory;
* ONE sibling refusal per `AGENT_CALLABLE_SELF` route, driven off the census. The
  handler (or the `get_self_acting_agent` dependency its signature names) is
  called with a sibling agent's key on the path agent and the CALLER's own name
  in every other argument, so a gate that compares the wrong parameter lets the
  call through and the test fails;
* the routes this change converted keep the message they had (ruling 1:
  behaviour-preserving).

Handlers are called directly, not through a `TestClient`: FastAPI validates a
request body before the handler runs, so a client call would need a valid body
per model to reach the gate. The wiring half — that the route is registered and
the gate is its first statement — is the census's (rule 7 pins the paths).
"""
from __future__ import annotations

import importlib
import inspect
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)
if str(_UNIT) not in sys.path:
    sys.path.insert(0, str(_UNIT))

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import HTTPException  # noqa: E402

import _route_census as rc  # noqa: E402
from models import User  # noqa: E402

SIBLING = "census-sibling"  # the agent whose key calls
TARGET = "census-target"  # the agent the path names

# The census is read once, at collection, to parametrise (a pure AST walk).
_CENSUS = rc.walk()
_SELF_KEYS = sorted(rc.AGENT_CALLABLE_SELF)

# Every route this change moved onto the helper, with the message it had before.
CONVERTED = {
    "routers/reminders.py::create_reminder_endpoint": "Agent-scoped key may only manage its own reminders",
    "routers/reminders.py::list_reminders_endpoint": "Agent-scoped key may only manage its own reminders",
    "routers/reminders.py::cancel_reminder_endpoint": "Agent-scoped key may only manage its own reminders",
    "routers/seat_decisions.py::record_seat_decision": "Agent-scoped key may only record and read its own agent's seat decisions",
    "routers/seat_decisions.py::list_seat_decisions": "Agent-scoped key may only record and read its own agent's seat decisions",
    "routers/public_memory.py::write_user_memory": "Agent-scoped key may only write its own agent's user memory",
    "routers/metric_points.py::record_metric_points": "Agent-scoped key may only record metrics as itself",
    "routers/reports.py::create_report": "Agent-scoped key may only report as itself",
    "routers/canvas.py::write_canvas": "Agent-scoped key may only write its own canvas",
    "routers/canvas.py::patch_canvas": "Agent-scoped key may only write its own canvas",
    "routers/canvas.py::clear_canvas": "Agent-scoped key may only write its own canvas",
    "routers/canvas.py::bulk_delete_canvases": "Agent-scoped key may only write its own canvas",
    "routers/agent_files.py::get_agent_canon_roles": "Agent-scoped key may only read its own canon",
    "routers/agent_files.py::get_agent_objectives": "Agent-scoped key may only read its own objectives",
    "routers/agent_files.py::share_agent_file": "Agent-scoped MCP key cannot share files for a different agent.",
    "routers/agents.py::send_voice_reply_endpoint": "Agent-scoped key may only speak as itself",
    "routers/a2a.py::call_a2a_agent": "An agent may only place outbound A2A calls as itself",
    "routers/a2a.py::get_a2a_task": "An agent may only place outbound A2A calls as itself",
}


def _deps():
    """The live `dependencies` module. The unit conftest evicts it between
    tests, so a module-level import would be a stale copy (2026-09-23)."""
    return importlib.import_module("dependencies")


def _principal(**fields) -> User:
    # What `get_current_user` returns for an MCP key: the OWNER, carrying the
    # owner's role (admin here — the breadth that made the gate necessary).
    return User(id=1, username="owner", role="admin", email="owner@example.com", **fields)


def _agent_key(name: str) -> User:
    return _principal(mcp_scope="agent", agent_name=name)


# ===========================================================================
# 1. The helper
# ===========================================================================


class TestAssertOwnAgent:
    def test_the_keys_own_agent_passes(self):
        assert _deps().assert_own_agent(_agent_key("a"), "a") is None

    def test_a_sibling_agent_key_gets_403_with_the_routes_message(self):
        with pytest.raises(HTTPException) as exc:
            _deps().assert_own_agent(_agent_key("b"), "a", detail="the route's own words")
        assert exc.value.status_code == 403
        assert exc.value.detail == "the route's own words"

    def test_without_a_message_it_still_names_the_rule(self):
        with pytest.raises(HTTPException) as exc:
            _deps().assert_own_agent(_agent_key("b"), "a")
        assert exc.value.status_code == 403 and "own agent" in exc.value.detail

    @pytest.mark.parametrize(
        "principal",
        [
            _principal(),  # a person on a JWT
            _principal(mcp_scope="user"),  # the person's own key
            _principal(mcp_scope="system"),  # trinity-system: no agent name on REST
            _principal(mcp_scope="connector", connector_agent="a"),
            _principal(is_event_loopback=True, vouched_source_agent="b"),
        ],
        ids=["jwt", "user-key", "system-key", "connector-key", "event-loopback"],
    )
    def test_a_principal_that_is_not_an_agent_key_passes(self, principal):
        """Exactly the inline copies' rule: they read `agent_name`, which only an
        agent-scoped key carries. Whether these principals may reach another
        agent is the access check's question (trinity-enterprise#719)."""
        assert _deps().assert_own_agent(principal, "a") is None

    def test_refusal_builds_the_exception(self):
        built = HTTPException(status_code=404, detail="Agent not found")
        with pytest.raises(HTTPException) as exc:
            _deps().assert_own_agent(_agent_key("b"), "a", refusal=lambda: built)
        assert exc.value is built

    def test_refusal_is_never_built_for_the_agent_itself(self):
        def explode():
            raise AssertionError("refusal built for the key's own agent")

        _deps().assert_own_agent(_agent_key("a"), "a", refusal=explode)

    def test_an_agent_name_that_is_not_a_string_is_not_an_agent_key(self):
        """The predicate is `agent_principal_name` — the one `agent_may_reach`
        uses — not truthiness. A real `User` cannot hold a non-string name; a
        bare MagicMock double can, and is not an agent."""
        assert _deps().assert_own_agent(types.SimpleNamespace(agent_name=MagicMock()), "a") is None


# ===========================================================================
# 2. One sibling refusal per AGENT_CALLABLE_SELF route, driven off the census
# ===========================================================================


def _handler(key: str):
    rel, qual = key.split("::")
    module = importlib.import_module(rc._module_of(rel))
    fn = module
    for part in qual.split("."):
        fn = getattr(fn, part)
    return module, fn


async def _refusal(key: str) -> HTTPException:
    gate = _CENSUS.routes[key].self_gate
    assert gate is not None, f"{key} is listed in AGENT_CALLABLE_SELF with no recognised self-gate"
    module, fn = _handler(key)
    params = inspect.signature(fn).parameters
    caller = _agent_key(SIBLING)
    if gate.kind == "assert_own_agent":
        bound = getattr(module, "assert_own_agent")
        # Not `is`: the conftest re-imports `dependencies` between tests while a
        # router keeps the function it bound at import (2026-09-23). A stub
        # leaked at that import would fail here, by name, not as a missing 403.
        assert (bound.__module__, bound.__qualname__) == ("dependencies", "assert_own_agent"), (
            f"{key}: assert_own_agent is {bound!r}, not the dependencies helper")
        # The caller's OWN name everywhere but the path agent: a gate compared
        # against any other parameter sees equal names and lets the call through.
        kwargs = {name: SIBLING for name in params}
        kwargs[gate.principal] = caller
        kwargs[gate.agent] = TARGET
        with pytest.raises(HTTPException) as exc:
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                await result
    else:
        dependency = params[gate.agent].default.dependency
        with pytest.raises(HTTPException) as exc:
            dependency(name=TARGET, current_user=caller)
    return exc.value


@pytest.mark.asyncio
@pytest.mark.parametrize("key", _SELF_KEYS)
async def test_a_sibling_agent_key_is_refused(key):
    refused = await _refusal(key)
    if _CENSUS.routes[key].self_gate.refusal:
        # The route builds its own refusal (a router's uniform 404, say).
        assert 400 <= refused.status_code < 500, f"{key}: {refused.status_code} {refused.detail!r}"
    else:
        assert refused.status_code == 403, f"{key}: {refused.status_code} {refused.detail!r}"
    assert refused.detail
    if key in CONVERTED:
        assert refused.detail == CONVERTED[key]


def test_every_converted_route_is_listed_self():
    assert set(CONVERTED) <= set(_SELF_KEYS), set(CONVERTED) - set(_SELF_KEYS)


def test_the_table_is_not_vacuous_and_covers_both_mechanisms():
    """Per family, not a total (2026-08-11): a recognition rule that went blind
    to one spelling would leave the other family green."""
    kinds = {_CENSUS.routes[k].self_gate.kind for k in _SELF_KEYS if _CENSUS.routes.get(k)}
    assert kinds == {"assert_own_agent", "get_self_acting_agent"}, kinds
    assert len(_SELF_KEYS) >= 21


# ===========================================================================
# 3. The canvas helpers are never weaker than their names on their own
# ===========================================================================


def test_the_canvas_helpers_refuse_a_sibling_on_their_own():
    """Each canvas write and delete opens with `assert_own_agent`, and the two
    helpers they call repeat it — so a route that reaches a helper alone (the
    ent#554 failure: a share route calling the gate that admits agents) still
    refuses a sibling."""
    canvas = importlib.import_module("routers.canvas")
    for call in (
        lambda: canvas._gate_write(_agent_key(SIBLING), TARGET, None),
        lambda: canvas._gate_human_removal(_agent_key(SIBLING), TARGET),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert (exc.value.status_code, exc.value.detail) == (403, CONVERTED["routers/canvas.py::write_canvas"])


def test_a_principal_that_is_not_an_agent_key_meets_the_owner_gate(monkeypatch):
    """`_gate_human_removal` picks its branch with the helper's own predicate:
    a principal whose `agent_name` is not a string is not an agent key, so it
    is held to the owner gate — never waved through by neither."""
    canvas = importlib.import_module("routers.canvas")
    seen = []
    monkeypatch.setattr(canvas, "assert_agent_owner", lambda user, name, **kw: seen.append(name))
    canvas._gate_human_removal(types.SimpleNamespace(agent_name=MagicMock()), TARGET)
    assert seen == [TARGET]

"""trinity-enterprise#719 — the Workspace's grant routes take a person.

The Workspace door (`get_portal_principal`) serves people: a Workspace session,
or a platform account signed in or using its own `user`-scoped key. It also
admits the system-scoped key, by design, for the Workspace's uses (#2198: chat,
reads, sessions, uploads). Four of its routes are an owner's grants rather than
uses, and each is recorded as a person's act:

* `POST …/agents/{agent}/role/readiness` — the owner's `calibrating` ⇄ `ready`
  stamp, which releases the scheduled-brief readiness hold;
* `PUT …/agents/{agent}/canvas/{canvas_id}/pin` — which canvas a whole roster
  sees first;
* `POST …/agents/{agent}/decisions` — a decision recorded for a seat, the
  caller's or (for the owner) any seat, with the caller as `decided_by`;
* `POST …/agents/{agent}/decisions/{id}/actions` — closing, reversing,
  reconfirming or superseding one, with the caller recorded.

Each now answers 403 `person_required` to a caller that is not a person
(`PortalPrincipal.is_person`, the rule the Workspace's ask endings already
follow), before the roster or any of the agent's rows is read. A person's
answers are unchanged: the owner on a session or on a `user` key; a shared
person and a Workspace client keep today's owner-only, canvas-manager and
own-seat rules.

Everything goes through the real Workspace router and the real door.
Credentials are real wherever the platform can mint them (JWTs, the owner's
`user` key, agent keys, the system key, connector / ops / portal-delegate keys,
a Workspace session, a signed loopback JWT), seeded in the real schema. The
principals the real path fences earlier, or cannot mint, reach the door through
the resolver it calls (`resolve_platform_user_unfloored`), stated as real
`models.User` values. Every refusal asserts the stored state did not move from
a non-ambient seed; every admission asserts it did.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parents[1] / "src" / "backend"
for _p in (str(_BACKEND), str(_UNIT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _route_census as rc  # noqa: E402
import dependencies  # noqa: E402
from client_portal import portal_auth as _PA  # noqa: E402
from client_portal import router as _PORTAL  # noqa: E402
from database import db  # noqa: E402
from db_harness import db_backend  # noqa: E402,F401 — the fixture, used by name
from db_harness import run as _hrun  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from models import User  # noqa: E402
from services import rate_limiter  # noqa: E402
from services import seat_decision_service as _SVC  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "ws719-owner"  # an admin: the default admin-owned install
OWNER_EMAIL = "ws719-owner@example.com"
SHARED = "ws719-shared"  # a platform person the agent is shared with
SHARED_EMAIL = "ws719-shared@example.com"
CLIENT_EMAIL = "ws719-client@example.com"  # a Workspace client: no platform account
OTHER_SEAT = "ws719-ops@example.com"  # a seat that belongs to none of the callers
AGENT = "ws719-agent"  # the agent the four grants act on
SIBLING = "ws719-sibling"  # holds a call edge to AGENT (inside its use reach)
SYSTEM_AGENT = "ws719-system"  # the agent the system-scoped key belongs to
GHOST = "ws719-no-such-agent"
CANVAS = "board"

TODAY = date(2026, 9, 22)  # pinned: the decision grammar compares review_by to today
REVIEW_BY = (TODAY + timedelta(days=90)).isoformat()

PREFIX = "/api/enterprise/client-portal/agents"
HUMAN_ONLY_DETAIL = dependencies.HUMAN_ONLY_DETAIL
AGENT_DOOR_DETAIL = "This operation is human-only; agent-scoped keys cannot perform it"
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

ROUTE_KEYS = {
    "client_portal/router.py::portal_agent_role_readiness",
    "client_portal/router.py::portal_pin_canvas",
    "client_portal/router.py::portal_seat_decision_record",
    "client_portal/router.py::portal_seat_decision_act",
}

# Each call, as (route id → what it asks for). `act` acts on the decision of the
# seat the system key and the owner share (the owner's own), `act_on_another_seat`
# on a seat that is neither.
ROUTES = [
    "readiness",
    "pin",
    "record",
    "record_for_another_seat",
    "act",
    "act_on_another_seat",
]


def _payload(**over) -> dict:
    base = {
        "outcome": "approved",
        "decided": "Renew the Acme contract",
        "alternatives": ["let it lapse", "renegotiate first"],
        "criterion": "renewal cost below the switching cost",
        "reversal": "Acme raises the price above the switching cost",
        "review_by": REVIEW_BY,
        "ask_class": "vendor-renewal",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# World
# --------------------------------------------------------------------------- #


def _seed_decision(seat: str) -> str:
    row = _SVC.record(
        db,
        agent_name=AGENT,
        seat_email=seat,
        decided_by_person=seat,
        payload=_payload(decided=f"Seeded for {seat}"),
    )
    return row["id"]


def _build(monkeypatch, *, seeded: bool) -> SimpleNamespace:
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    monkeypatch.setattr(_SVC, "_today", lambda: TODAY)
    rate_limiter.clear_inprocess()

    db.create_user(UserCreate(username=OWNER, role="admin", email=OWNER_EMAIL))
    db.create_user(UserCreate(username=SHARED, role="user", email=SHARED_EMAIL))
    for agent in (AGENT, SIBLING, SYSTEM_AGENT):
        db.register_agent_owner(agent, OWNER)
    db.add_agent_permission(SIBLING, AGENT, OWNER)  # SIBLING may call AGENT
    assert db.share_agent(AGENT, OWNER, SHARED_EMAIL)
    assert db.share_agent(AGENT, OWNER, CLIENT_EMAIL)

    decisions = {}
    if seeded:
        # Non-ambient seeds: a stamp, an unpinned canvas and one active decision
        # per seat, so every refusal can show that nothing moved.
        db.set_agent_role_readiness(AGENT, "calibrating", OWNER_EMAIL)
        db.upsert_agent_canvas(
            AGENT, CANVAS, blocks=[{"type": "markdown", "text": "hello"}], title="Board"
        )
        for seat in (OWNER_EMAIL, OTHER_SEAT, SHARED_EMAIL, CLIENT_EMAIL):
            decisions[seat] = _seed_decision(seat)

    app = FastAPI()
    app.include_router(_PORTAL.router)
    return SimpleNamespace(client=TestClient(app), decisions=decisions)


@pytest.fixture
def world(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    w = _build(monkeypatch, seeded=True)
    yield w
    rate_limiter.clear_inprocess()


@pytest.fixture
def fresh(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    """A fresh install: an agent that has never been flipped, with no decisions
    and no canvas."""
    w = _build(monkeypatch, seeded=False)
    yield w
    rate_limiter.clear_inprocess()


def _state() -> dict:
    canvas = db.get_agent_canvas(AGENT, CANVAS)
    return {
        "readiness": db.get_agent_role_readiness(AGENT),
        "pinned": None if canvas is None else bool(canvas.get("pinned")),
        "decisions": sorted(
            (r["id"], r["seat_email"], r["status"], r.get("closed_by"))
            for r in db.list_seat_decisions(AGENT)
        ),
    }


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str = OWNER) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key(agent: str = AGENT) -> dict:
    return _auth(db.create_agent_mcp_api_key(agent, OWNER).api_key)


def _system_key() -> dict:
    """Created the way the platform creates it: an agent key under the admin,
    then its scope column set to `system` (system_agent_service._set_system_scope)."""
    key = db.create_agent_mcp_api_key(SYSTEM_AGENT, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_user_key() -> dict:
    return _auth(db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key)


def _scoped_user_key(scope: str) -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name=f"{scope} key", scope=scope)).api_key
    )


def _connector_key() -> dict:
    owner_id = db.get_user_by_username(OWNER)["id"]
    return _auth(db.mint_connector_key(AGENT, owner_id)["api_key"])


def _workspace_session(email: str = CLIENT_EMAIL) -> dict:
    return _auth(
        dependencies.create_portal_session_token(email, minted_by=dependencies.PORTAL_MINT_OTP)
    )


def _as(monkeypatch, **fields) -> dict:
    """Have the door's credential resolver answer a stated principal."""
    base = {"id": 1, "username": OWNER, "email": OWNER_EMAIL, "role": "admin"}
    base.update(fields)
    principal = User(**base)

    async def resolve(request, token):
        return principal

    monkeypatch.setattr(_PA, "resolve_platform_user_unfloored", resolve)
    return _auth("a-platform-credential")


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


def _call(world, route: str, headers: dict, agent: str = AGENT):
    client = world.client
    if route == "readiness":
        return client.post(
            f"{PREFIX}/{agent}/role/readiness", json={"status": "ready"}, headers=headers
        )
    if route == "pin":
        return client.put(
            f"{PREFIX}/{agent}/canvas/{CANVAS}/pin", json={"pinned": True}, headers=headers
        )
    if route == "record":
        return client.post(f"{PREFIX}/{agent}/decisions", json=_payload(), headers=headers)
    if route == "record_for_another_seat":
        return client.post(
            f"{PREFIX}/{agent}/decisions", json=_payload(seat=OTHER_SEAT), headers=headers
        )
    if route in ("act", "act_on_another_seat"):
        seat = OWNER_EMAIL if route == "act" else OTHER_SEAT
        decision_id = world.decisions.get(seat, "no-such-decision")
        return client.post(
            f"{PREFIX}/{agent}/decisions/{decision_id}/actions",
            json={"action": "close", "reason": "no longer live"},
            headers=headers,
        )
    raise AssertionError(route)


def _assert_person_required(res) -> None:
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == HUMAN_ONLY_DETAIL, res.text


# --------------------------------------------------------------------------- #
# 1. The census: the four routes stay Workspace-door routes, out of the baseline
# --------------------------------------------------------------------------- #


_CENSUS = rc.walk()


def test_the_four_routes_stay_portal_class_and_out_of_the_baseline():
    baseline = rc.load_baseline()
    classes, failures = rc.classify(_CENSUS, baseline)
    assert failures == []
    for key in sorted(ROUTE_KEYS):
        assert classes.get(key) == "portal", (key, classes.get(key))
    assert not ROUTE_KEYS & set(baseline), "a Workspace route entered the baseline"


# --------------------------------------------------------------------------- #
# 2. The system key: refused on all four, recording for another seat included
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ROUTES)
def test_the_system_key_is_refused(world, route):
    headers = _system_key()
    before = _state()
    res = _call(world, route, headers)
    _assert_person_required(res)
    assert _state() == before


def test_the_system_key_cannot_record_a_decision_for_another_seat(world):
    """The owner may record for any seat. A caller that is not a person may
    record for none, another seat included."""
    headers = _system_key()
    before = db.list_seat_decisions(AGENT, OTHER_SEAT)
    res = _call(world, "record_for_another_seat", headers)
    _assert_person_required(res)
    assert db.list_seat_decisions(AGENT, OTHER_SEAT) == before


@pytest.mark.parametrize("route", ROUTES)
def test_the_refusal_comes_before_the_roster(world, route):
    """The answer depends only on the caller: an agent that does not exist gets
    the same 403, so the refusal tells the caller nothing about the agent."""
    headers = _system_key()
    real = _call(world, route, headers)
    ghost = _call(world, route, headers, agent=GHOST)
    _assert_person_required(real)
    _assert_person_required(ghost)


# --------------------------------------------------------------------------- #
# 3. Every other machine principal is refused, at whichever layer answers first
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _agent_key(AGENT), id="agent-own-key"),
        pytest.param(lambda: _agent_key(SIBLING), id="agent-sibling-key"),
    ],
)
def test_an_agent_key_is_refused_at_the_door(world, route, headers):
    """Unchanged: the Workspace door has refused agent keys since #2198."""
    before = _state()
    res = _call(world, route, headers())
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == AGENT_DOOR_DETAIL
    assert _state() == before


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _connector_key(), id="connector-key"),
        pytest.param(lambda: _scoped_user_key("ops"), id="ops-key"),
        pytest.param(lambda: _scoped_user_key("portal_delegate"), id="portal-delegate-key"),
    ],
)
def test_a_real_fenced_key_is_refused_before_the_door(world, route, headers):
    """Through the real resolver: these keys are fenced to their own routes and
    never reach the Workspace door. Whichever layer answers, nothing moves."""
    before = _state()
    res = _call(world, route, headers())
    assert res.status_code == 403, res.text
    assert _state() == before


@pytest.mark.parametrize("route", ROUTES)
def test_an_unvouched_loopback_jwt_is_refused_by_the_entry_fence(world, route, monkeypatch):
    """A real, signed loopback JWT with NO source agent: the resolver's entry
    fence refuses it on every route but the subscriber-task dispatch (ent#614).
    A pass would be a 2xx, not an incidental 401: the subject is a live admin and
    the token is not revoked."""
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY

    monkeypatch.setattr(dependencies, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {"sub": OWNER, "scope": dependencies.EVENT_LOOPBACK_SCOPE, "exp": int(time.time()) + 300},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    before = _state()
    res = _call(world, route, _auth(token))
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert _state() == before


# Principals the door must not treat as a person that the real path fences
# earlier or cannot mint as a key row: stated, never a MagicMock.
STATED_NON_PERSONS = [
    pytest.param({"mcp_scope": "system"}, id="system-key"),
    pytest.param({"mcp_scope": "ops"}, id="ops-key"),
    pytest.param({"mcp_scope": "portal_delegate", "portal_delegate": True}, id="portal-delegate-key"),
    pytest.param({"mcp_scope": "connector", "connector_agent": AGENT}, id="connector-key"),
    pytest.param({"mcp_scope": "a-scope-shipped-tomorrow"}, id="unknown-future-scope"),
    pytest.param(
        {"mcp_scope": None, "is_event_loopback": True, "vouched_source_agent": AGENT},
        id="vouched-loopback-jwt",
    ),
    pytest.param({"mcp_scope": None, "is_event_loopback": True}, id="unvouched-loopback-jwt"),
]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("fields", STATED_NON_PERSONS)
def test_every_non_person_the_door_admits_is_refused(world, route, fields, monkeypatch):
    headers = _as(monkeypatch, **fields)
    before = _state()
    res = _call(world, route, headers)
    _assert_person_required(res)
    assert _state() == before


@pytest.mark.parametrize("route", ROUTES)
def test_a_user_scope_carrying_an_agent_is_refused_at_the_door(world, route, monkeypatch):
    headers = _as(monkeypatch, mcp_scope="user", agent_name=AGENT)
    before = _state()
    res = _call(world, route, headers)
    assert res.status_code == 403, res.text
    assert _state() == before


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute at all: the person rule
    must fail closed (sentinel, never `getattr(..., None)`, the #2323 trap)."""

    id = 1
    username = OWNER
    email = OWNER_EMAIL
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


@pytest.mark.parametrize("route", ROUTES)
def test_a_principal_without_mcp_scope_fails_closed(world, route, monkeypatch):
    async def resolve(request, token):
        return _NoScopeAttribute()

    monkeypatch.setattr(_PA, "resolve_platform_user_unfloored", resolve)
    before = _state()
    res = _call(world, route, _auth("a-platform-credential"))
    _assert_person_required(res)
    assert _state() == before


# --------------------------------------------------------------------------- #
# 4. The owner, as a person, still makes every one of these grants
# --------------------------------------------------------------------------- #


def _assert_owner_moved(world, route: str, res, before: dict) -> None:
    assert res.status_code == 200, res.text
    after = _state()
    if route == "readiness":
        assert res.json()["status"] == "ready"
        assert after["readiness"]["status"] == "ready"
        assert after["readiness"]["changed_by"] == OWNER_EMAIL
    elif route == "pin":
        assert res.json() == {"canvas_id": CANVAS, "pinned": True}
        assert before["pinned"] is False and after["pinned"] is True
    elif route in ("record", "record_for_another_seat"):
        seat = OWNER_EMAIL if route == "record" else OTHER_SEAT
        decision = res.json()["decision"]
        assert decision["seat"] == seat
        assert decision["decided_by"]["person"] == OWNER_EMAIL
        assert len(after["decisions"]) == len(before["decisions"]) + 1
    else:
        seat = OWNER_EMAIL if route == "act" else OTHER_SEAT
        decision = res.json()["decision"]
        assert decision["id"] == world.decisions[seat]
        assert decision["status"] == "closed"
        assert decision["closed_by"] == OWNER_EMAIL
    if route != "readiness":
        assert after["readiness"] == before["readiness"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _jwt(OWNER), id="owner-session"),
        pytest.param(lambda: _owner_user_key(), id="owner-user-key"),
    ],
)
def test_the_owner_as_a_person_passes(world, route, headers):
    before = _state()
    res = _call(world, route, headers())
    _assert_owner_moved(world, route, res, before)


# --------------------------------------------------------------------------- #
# 5. A shared person and a Workspace client keep today's answers
# --------------------------------------------------------------------------- #


def _post_record(world, headers, seat=None):
    body = _payload() if seat is None else _payload(seat=seat)
    return world.client.post(f"{PREFIX}/{AGENT}/decisions", json=body, headers=headers)


def _post_act(world, headers, decision_id):
    return world.client.post(
        f"{PREFIX}/{AGENT}/decisions/{decision_id}/actions",
        json={"action": "close", "reason": "no longer live"},
        headers=headers,
    )


@pytest.mark.parametrize(
    "who",
    [
        pytest.param(("client", CLIENT_EMAIL), id="workspace-client-session"),
        pytest.param(("shared", SHARED_EMAIL), id="shared-person-session"),
    ],
)
def test_a_person_who_is_not_the_owner_keeps_todays_answers(world, who):
    kind, email = who
    headers = _workspace_session(email) if kind == "client" else _jwt(SHARED)

    # Owner-only: readiness is the owner's stamp.
    before = _state()
    res = _call(world, "readiness", headers)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "readiness_owner_only"
    assert _state() == before

    # Canvas-manager: only the owner (or an admin) on the platform may pin.
    res = _call(world, "pin", headers)
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Canvas not found"
    assert _state() == before

    # Own seat: a person records and acts for their own seat...
    res = _post_record(world, headers)
    assert res.status_code == 200, res.text
    assert res.json()["decision"]["seat"] == email
    assert res.json()["decision"]["decided_by"]["person"] == email

    res = _post_act(world, headers, world.decisions[email])
    assert res.status_code == 200, res.text
    assert res.json()["decision"]["status"] == "closed"
    assert res.json()["decision"]["closed_by"] == email

    # ...and not for another seat.
    others = db.list_seat_decisions(AGENT, OTHER_SEAT)
    res = _post_record(world, headers, seat=OTHER_SEAT)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "seat_not_yours"
    res = _post_act(world, headers, world.decisions[OTHER_SEAT])
    assert res.status_code == 404, res.text
    assert res.json()["detail"]["code"] == "decision_not_found"
    assert db.list_seat_decisions(AGENT, OTHER_SEAT) == others


# --------------------------------------------------------------------------- #
# 6. Body validation happens before the handler, alike for every caller
# --------------------------------------------------------------------------- #


def test_a_body_the_route_cannot_validate_is_answered_alike_for_every_caller(world):
    """The person check runs in the handler, after FastAPI validates the body (as
    every other check on these routes does). A body it cannot validate gets the
    same 422 whoever sends it, so the order discloses nothing about the caller
    or the agent, and nothing moves."""
    before = _state()
    answers = []
    for headers in (_system_key(), _jwt(OWNER)):
        res = world.client.post(
            f"{PREFIX}/{AGENT}/role/readiness", json={"status": "done"}, headers=headers
        )
        answers.append((res.status_code, res.json()["detail"][0]["type"]))
    assert answers[0] == answers[1] and answers[0][0] == 422, answers
    assert _state() == before


# --------------------------------------------------------------------------- #
# 7. A fresh install: an agent never flipped, no decisions, no canvas
# --------------------------------------------------------------------------- #


def test_a_fresh_install_refuses_the_system_key_and_serves_the_owner(fresh):
    assert db.get_agent_role_readiness(AGENT) is None
    assert db.list_seat_decisions(AGENT) == []
    assert db.get_agent_canvas(AGENT, CANVAS) is None

    system = _system_key()
    for route in ROUTES:
        _assert_person_required(_call(fresh, route, system))
    assert db.get_agent_role_readiness(AGENT) is None
    assert db.list_seat_decisions(AGENT) == []

    owner = _jwt(OWNER)
    # The lookups now run, for a person only: nothing to pin, nothing to act on.
    res = _call(fresh, "pin", owner)
    assert res.status_code == 404 and res.json()["detail"] == "Canvas not found", res.text
    res = _call(fresh, "act", owner)
    assert res.status_code == 404, res.text
    assert res.json()["detail"]["code"] == "decision_not_found"

    # The owner's first flip and first decision.
    res = _call(fresh, "readiness", owner)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ready" and res.json()["changed_by"] == OWNER_EMAIL
    assert db.get_agent_role_readiness(AGENT)["status"] == "ready"
    res = _call(fresh, "record", owner)
    assert res.status_code == 200, res.text
    assert [r["seat_email"] for r in db.list_seat_decisions(AGENT)] == [OWNER_EMAIL]

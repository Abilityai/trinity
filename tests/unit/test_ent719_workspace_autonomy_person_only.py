"""trinity-enterprise#719 — the autonomy dial's owner writes take a person.

The Workspace door (`get_portal_principal`) admits the system-scoped key, by
design, for the Workspace's uses (#2198). Two of the autonomy dial's writes
(trinity-enterprise#641) are an owner's grants rather than uses:

* `POST …/agents/{agent}/autonomy/classes/{ask_class}` with `release` — lets a
  held class run unprompted again ("a grant, Invariant #8", in the route's own
  words); `hold` shares the route;
* `POST …/agents/{agent}/autonomy/classes/{ask_class}/guard` — the owner's cap
  that decides whether the rule may ever promote the class.

The system key resolves to the admin who owns the install, so the service's
owner check alone let it release a person's hold. Each route now answers 403
`person_required` to a caller that is not a person, first, as the readiness,
canvas-pin and seat-decision writes beside it do. The owner as a person keeps
both writes.

Through the real Workspace router, the real door and real keys in the real
schema. Every refusal asserts the stored row did not move from a non-ambient
seed (a class held by the owner, its guard `capped`); every admission asserts
it did.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parents[1] / "src" / "backend"
for _p in (str(_BACKEND), str(_UNIT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dependencies  # noqa: E402
from client_portal import router as _PORTAL  # noqa: E402
from database import db  # noqa: E402
from db_harness import db_backend  # noqa: E402,F401 — the fixture, used by name
from db_harness import run as _hrun  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services import rate_limiter  # noqa: E402
from services import seat_decision_service as _SVC  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "aut719-owner"  # an admin: the default admin-owned install
OWNER_EMAIL = "aut719-owner@example.com"
AGENT = "aut719-agent"
SYSTEM_AGENT = "aut719-system"
GHOST = "aut719-no-such-agent"
ASK_CLASS = "vendor-renewal"
TODAY = date(2026, 9, 22)  # pinned: the decision grammar compares review_by to today

PREFIX = "/api/enterprise/client-portal/agents"
HUMAN_ONLY_DETAIL = dependencies.HUMAN_ONLY_DETAIL


@pytest.fixture
def world(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    monkeypatch.setattr(_SVC, "_today", lambda: TODAY)
    rate_limiter.clear_inprocess()

    db.create_user(UserCreate(username=OWNER, role="admin", email=OWNER_EMAIL))
    for agent in (AGENT, SYSTEM_AGENT):
        db.register_agent_owner(agent, OWNER)
    # A class exists for a seat while the seat has a decision in it; the owner
    # held it and capped it, so a release or a guard write would move the row.
    _SVC.record(
        db,
        agent_name=AGENT,
        seat_email=OWNER_EMAIL,
        decided_by_person=OWNER_EMAIL,
        payload={
            "outcome": "approved",
            "decided": "Renew the Acme contract",
            "alternatives": ["let it lapse"],
            "criterion": "renewal cost below the switching cost",
            "reversal": "Acme raises the price",
            "review_by": (TODAY + timedelta(days=90)).isoformat(),
            "ask_class": ASK_CLASS,
        },
    )
    db.set_seat_ask_class_hold(
        agent_name=AGENT,
        seat_email=OWNER_EMAIL,
        ask_class=ASK_CLASS,
        held=True,
        by=OWNER_EMAIL,
    )
    db.set_seat_ask_class_guard(
        agent_name=AGENT,
        seat_email=OWNER_EMAIL,
        ask_class=ASK_CLASS,
        guard_metric="capped",
    )
    app = FastAPI()
    app.include_router(_PORTAL.router)
    yield TestClient(app)
    rate_limiter.clear_inprocess()


def _state() -> tuple:
    row = db.get_seat_ask_class_state(AGENT, OWNER_EMAIL, ASK_CLASS) or {}
    return (row.get("held"), row.get("guard_metric"))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _system_key() -> dict:
    """Created the way the platform creates it: an agent key under the admin,
    then its scope column set to `system`."""
    key = db.create_agent_mcp_api_key(SYSTEM_AGENT, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_jwt() -> dict:
    return _auth(dependencies.create_access_token({"sub": OWNER}))


def _owner_user_key() -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key
    )


def _release(client, headers, agent=AGENT):
    return client.post(
        f"{PREFIX}/{agent}/autonomy/classes/{ASK_CLASS}",
        json={"action": "release"},
        headers=headers,
    )


def _guard(client, headers, agent=AGENT):
    return client.post(
        f"{PREFIX}/{agent}/autonomy/classes/{ASK_CLASS}/guard",
        json={"guard_metric": "clear"},
        headers=headers,
    )


CALLS = [pytest.param(_release, id="release"), pytest.param(_guard, id="guard")]


def test_the_seed_is_not_ambient(world):
    assert _state() == (True, "capped")


@pytest.mark.parametrize("call", CALLS)
def test_the_system_key_is_refused_and_nothing_moves(world, call):
    res = call(world, _system_key())
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == HUMAN_ONLY_DETAIL
    assert _state() == (True, "capped")


@pytest.mark.parametrize("call", CALLS)
def test_the_refusal_comes_before_the_roster(world, call):
    """Same 403 for an agent that does not exist: it says nothing about the agent."""
    headers = _system_key()
    for agent in (AGENT, GHOST):
        res = call(world, headers, agent=agent)
        assert res.status_code == 403, res.text
        assert res.json()["detail"] == HUMAN_ONLY_DETAIL


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(_owner_jwt, id="session"),
        pytest.param(_owner_user_key, id="user-key"),
    ],
)
def test_the_owner_as_a_person_releases(world, headers):
    res = _release(world, headers())
    assert res.status_code == 200, res.text
    assert _state() == (False, "capped")


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(_owner_jwt, id="session"),
        pytest.param(_owner_user_key, id="user-key"),
    ],
)
def test_the_owner_as_a_person_sets_the_guard(world, headers):
    res = _guard(world, headers())
    assert res.status_code == 200, res.text
    assert _state() == (True, "clear")

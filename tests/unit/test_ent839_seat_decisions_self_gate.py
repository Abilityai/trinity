"""trinity-enterprise#839 — a sibling agent's key never records or reads
another agent's seat decisions (CWE-863).

An agent-scoped key resolves to its OWNER carrying the owner's role, so
`assert_agent_access` alone passed for any agent the owner reaches — under an
admin owner, every agent on the instance. A forged `active` decision then
became standing instruction text in the victim agent's system prompt, and the
GET returned a person's criteria and notes. Both handlers now self-gate first.

Pins, through the real router:
* a sibling agent key gets the 403 on POST and GET, before ANY lookup (the
  access check and the execution read are tripwires);
* the key's own agent passes the gate;
* a person (no `agent_name`) passes the gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.seat_decisions as route_mod  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402

VICTIM = "agent-a"
RECORD = {
    "execution_id": "exec-of-agent-a",
    "outcome": "approved",
    "decided": "Renew the Acme contract",
    "alternatives": ["let it lapse"],
    "criterion": "renewal cost below the switching cost",
    "reversal": "Acme raises the price",
    "review_by": "2027-01-31",
}


def _agent_key(name: str) -> User:
    # What `get_current_user` returns for an agent-scoped MCP key: the OWNER,
    # carrying the owner's role, with the key's agent attached.
    return User(id=1, username="admin", role="admin", email="admin@example.com",
                mcp_scope="agent", agent_name=name)


def _person() -> User:
    return User(id=1, username="admin", role="admin", email="admin@example.com")


@pytest.fixture
def harness(monkeypatch):
    seen = []

    def access(current_user, agent_name, **_):
        seen.append(("access", agent_name))

    def seat_for(agent_name, execution_id):
        seen.append(("seat_for", agent_name))
        # Past the gate: stop here with a recognisable status, so the test
        # proves the gate let it through without needing the store.
        raise HTTPException(status_code=418, detail="past the gate")

    monkeypatch.setattr(route_mod, "assert_agent_access", access)
    monkeypatch.setattr(route_mod, "_seat_for", seat_for)
    monkeypatch.setattr(route_mod.rate_limiter, "enforce", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(route_mod.router)
    principal = {"user": None}
    app.dependency_overrides[get_current_user] = lambda: principal["user"]
    client = TestClient(app)

    def call(user, method):
        principal["user"] = user
        if method == "POST":
            return client.post(f"/api/agents/{VICTIM}/decisions", json=RECORD)
        return client.get(f"/api/agents/{VICTIM}/decisions",
                          params={"execution_id": "exec-of-agent-a"})

    return call, seen


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_a_sibling_agent_key_is_refused_before_any_lookup(harness, method):
    call, seen = harness
    res = call(_agent_key("agent-b"), method)
    assert res.status_code == 403, res.text
    assert "its own agent's seat decisions" in res.json()["detail"]
    assert seen == [], "the refusal must come before the access check and the execution read"


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_the_keys_own_agent_passes_the_gate(harness, method):
    call, seen = harness
    res = call(_agent_key(VICTIM), method)
    assert res.status_code == 418, res.text
    assert seen == [("access", VICTIM), ("seat_for", VICTIM)]


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_a_person_passes_the_gate(harness, method):
    call, seen = harness
    res = call(_person(), method)
    assert res.status_code == 418, res.text
    assert ("seat_for", VICTIM) in seen

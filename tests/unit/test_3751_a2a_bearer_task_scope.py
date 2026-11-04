"""#3751 — a bearer A2A `tasks/get` / `tasks/cancel` answers only for the task
the calling credential started through A2A.

Before #3751 the bearer branch checked only that the execution belonged to the
agent, so any principal that passed `_authorize_inbound` (owner, shared user,
admin, any agent key of the owner) read or cancelled every execution id on it.
AAuth callers were already bound by `execution_owned_by` and payers by the payer
binding; this is the bearer twin.

The rule (`routers/a2a.py::_bearer_started_by`): the row was started through A2A
(`triggered_by == "a2a"`) by this user id AND this key id (None on both sides
for a signed-in person). Everyone else gets the unknown-id answer, byte for byte.

Driven through the real `_dispatch_jsonrpc` with `db` stubbed to a dict of rows.
Related flow: docs/memory/feature-flows/a2a-inbound-server.md
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import routers.a2a as a2a  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "bot"
OWNER_ID = 1

# The principals. Every agent key of one owner resolves to that owner's user id
# and role (dependencies.get_current_user); only the key id tells them apart.
ALICE_KEY = dict(id=OWNER_ID, username="alice", role="admin", agent_name=None, mcp_key_id="k-alice")
ALICE_JWT = dict(id=OWNER_ID, username="alice", role="admin", agent_name=None, mcp_key_id=None)
AGENT_A = dict(id=OWNER_ID, username="alice", role="admin", agent_name="agent-a", mcp_key_id="k-agent-a")
AGENT_B = dict(id=OWNER_ID, username="alice", role="admin", agent_name="agent-b", mcp_key_id="k-agent-b")
BOB_KEY = dict(id=2, username="bob", role="user", agent_name=None, mcp_key_id="k-bob")
ADMIN_JWT = dict(id=3, username="root", role="admin", agent_name=None, mcp_key_id=None)
SYSTEM_KEY = dict(id=OWNER_ID, username="alice", role="admin", agent_name=None, mcp_key_id="k-system",
                  mcp_scope="system")


def _who(p):
    return types.SimpleNamespace(email=f"{p['username']}@example.com", mcp_key_name=None,
                                 mcp_scope=p.get("mcp_scope", "user" if p["mcp_key_id"] else None), **{
                                     k: v for k, v in p.items() if k != "mcp_scope"})


def _row(started_by, status="running", triggered_by="a2a", agent=AGENT):
    return {"agent_name": agent, "status": status, "response": "secret answer", "error": None,
            "triggered_by": triggered_by,
            "source_user_id": started_by["id"] if started_by else None,
            "source_mcp_key_id": started_by["mcp_key_id"] if started_by else None}


@pytest.fixture
def h(monkeypatch):
    state = {"rows": {}, "terminated": [], "queued_cancelled": [], "audit": []}

    def _cancel_queued(eid, reason=None):
        state["queued_cancelled"].append(eid)
        return True

    monkeypatch.setattr(a2a, "db", types.SimpleNamespace(
        get_execution=lambda eid: state["rows"].get(eid),
        cancel_queued_execution=_cancel_queued,
    ))

    async def _terminate(agent_name, exec_id):
        state["terminated"].append(exec_id)
        return True
    monkeypatch.setattr(a2a, "terminate_execution_on_agent", _terminate)

    class _Audit:
        async def log(self, **kwargs):
            state["audit"].append(kwargs)
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    request = types.SimpleNamespace(scope={"path": f"/a2a/{AGENT}"},
                                    client=types.SimpleNamespace(host="203.0.113.9"))

    def call(method, exec_id, principal, aauth=None):
        resp = asyncio.run(a2a._dispatch_jsonrpc(
            AGENT, request, (method, {"id": exec_id}, "r1"),
            current_user=None if aauth is not None else _who(principal), aauth=aauth))
        return json.loads(resp.body)

    return types.SimpleNamespace(state=state, call=call)


def _not_found(body):
    return body.get("error", {}).get("message") == "Task not found"


# --------------------------------------------------------------------------- #
# The starter keeps today's path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("starter", [ALICE_KEY, ALICE_JWT, AGENT_A, BOB_KEY])
def test_the_starter_polls_its_own_task(h, starter):
    h.state["rows"]["e"] = _row(starter, status="success")
    body = h.call("tasks/get", "e", starter)
    assert body["result"]["status"]["state"] == "completed"
    assert body["result"]["artifacts"][0]["parts"][0]["text"] == "secret answer"


def test_the_starter_cancels_its_own_running_task(h):
    h.state["rows"]["e"] = _row(AGENT_A)
    body = h.call("tasks/cancel", "e", AGENT_A)
    assert body["result"]["status"]["state"] == "canceled"
    assert h.state["terminated"] == ["e"]


def test_the_starter_cancels_its_own_queued_task(h):
    h.state["rows"]["e"] = _row(AGENT_A, status="queued")
    assert h.call("tasks/cancel", "e", AGENT_A)["result"]["status"]["state"] == "canceled"
    assert h.state["queued_cancelled"] == ["e"]


# --------------------------------------------------------------------------- #
# Nobody else does — not even the owner or an admin
# --------------------------------------------------------------------------- #
OTHERS = [
    pytest.param(AGENT_B, id="a-sibling-agent-key-of-the-same-owner"),
    pytest.param(ALICE_KEY, id="the-owners-own-key"),
    pytest.param(ALICE_JWT, id="the-owner-signed-in"),
    pytest.param(BOB_KEY, id="a-shared-user"),
    pytest.param(ADMIN_JWT, id="an-admin"),
    pytest.param(SYSTEM_KEY, id="the-system-key"),
]


@pytest.mark.parametrize("caller", OTHERS)
def test_another_principal_cannot_read_the_task(h, caller):
    h.state["rows"]["e"] = _row(AGENT_A, status="success")
    body = h.call("tasks/get", "e", caller)
    assert _not_found(body)
    assert "secret answer" not in json.dumps(body)


@pytest.mark.parametrize("caller", OTHERS)
def test_another_principal_cannot_cancel_the_task(h, caller):
    for eid, status in (("run", "running"), ("q", "queued")):
        h.state["rows"][eid] = _row(AGENT_A, status=status)
        assert _not_found(h.call("tasks/cancel", eid, caller))
    assert h.state["terminated"] == [] and h.state["queued_cancelled"] == []
    assert h.state["audit"] == []


def test_the_refusal_is_byte_identical_to_an_unknown_id(h):
    h.state["rows"]["e"] = _row(AGENT_A)
    for method in ("tasks/get", "tasks/cancel"):
        assert h.call(method, "e", AGENT_B) == h.call(method, "no-such-id", AGENT_B)


# --------------------------------------------------------------------------- #
# What counts as "started it through A2A"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("trigger", ["manual", "mcp", "schedule", "agent", None])
def test_a_row_the_same_principal_started_outside_a2a_is_not_an_a2a_task(h, trigger):
    """The owner's UI chat, /task or MCP chat_with_agent rows carry the same
    user/key pair — a bearer A2A caller must neither read nor cancel them."""
    h.state["rows"]["e"] = _row(ALICE_JWT, triggered_by=trigger)
    assert _not_found(h.call("tasks/get", "e", ALICE_JWT))
    assert _not_found(h.call("tasks/cancel", "e", ALICE_JWT))
    assert h.state["terminated"] == []


def test_a_row_with_no_starting_user_is_nobodys_bearer_task(h):
    """AAuth and paid rows carry no user id; a bearer caller always has one."""
    h.state["rows"]["e"] = _row(None)
    for caller in (ALICE_JWT, ALICE_KEY, ADMIN_JWT):
        assert _not_found(h.call("tasks/get", "e", caller))


def test_a_re_minted_key_loses_its_in_flight_tasks(h):
    """Agent keys are re-minted on recovery; the new key is a new credential —
    the same rule as the messageId namespace (`_a2a_idem_scope`)."""
    h.state["rows"]["e"] = _row(AGENT_A)
    rotated = {**AGENT_A, "mcp_key_id": "k-agent-a-v2"}
    assert _not_found(h.call("tasks/get", "e", rotated))


def test_the_gate_reads_what_the_mint_writes(h, monkeypatch):
    """End to end over the real `_mint_a2a_execution`: the row it creates is
    pollable by its caller and by no other key of the same owner."""
    created = {}

    def _create(**kw):
        f = kw["fields"]
        created["e"] = {"agent_name": kw["agent_name"], "status": "running", "response": None,
                        "error": None, "triggered_by": kw["triggered_by"],
                        "source_user_id": f.source_user_id, "source_mcp_key_id": f.source_mcp_key_id}
        return types.SimpleNamespace(id="e")
    a2a.db.create_task_execution = _create
    a2a.db.get_agent_subscription_id = lambda name: None
    monkeypatch.setattr(a2a, "settings_service",
                        types.SimpleNamespace(get_platform_default_model=lambda: "m"), raising=False)

    assert a2a._mint_a2a_execution(AGENT, "hi", _who(AGENT_A), model="m") == "e"
    h.state["rows"].update(created)
    assert h.call("tasks/get", "e", AGENT_A)["result"]["status"]["state"] == "working"
    assert _not_found(h.call("tasks/get", "e", AGENT_B))


# --------------------------------------------------------------------------- #
# The other doors keep their own rule
# --------------------------------------------------------------------------- #
def test_an_aauth_caller_is_still_judged_by_execution_owned_by(h, monkeypatch):
    """The bearer pair is skipped when the caller is an AAuth agent: its rows
    carry no Trinity user, and `execution_owned_by` is its binding."""
    owned = {"e": "aauth:peer-1"}
    monkeypatch.setattr(a2a.aauth_inbound, "execution_owned_by",
                        lambda eid, identity: owned.get(eid) == identity)
    h.state["rows"]["e"] = _row(None, status="success")
    peer = types.SimpleNamespace(identity="aauth:peer-1")
    other = types.SimpleNamespace(identity="aauth:peer-2")
    assert h.call("tasks/get", "e", None, aauth=peer)["result"]["status"]["state"] == "completed"
    assert _not_found(h.call("tasks/get", "e", None, aauth=other))

"""#3749 — inbound A2A `message/send` answers a `working` Task for a long turn.

Related flow: docs/memory/feature-flows/a2a-inbound-server.md

The turn is minted (execution row + idempotency attach) before dispatch, then
run as a detached task the request holds for `A2A_SEND_WORKING_AFTER_SECONDS`.
Inside the budget the answer is byte-identical to the pre-#3749 one; past it the
caller receives `working` (or `submitted` for a queued pull row) carrying the
execution id, and polls `tasks/get`. The claim is completed exactly once, at the
terminal, by the detached tail — never with a `working` snapshot.

The slow-path tests drive `_dispatch_jsonrpc` directly under `asyncio.run`
(plan decision 17): `TestClient(app)` without `with` runs each request on a
short-lived loop that would kill the detached turn. The dispatch stub awaits an
`asyncio.Event`, so a "long turn" is deterministic — no sleeps.
"""
from __future__ import annotations

import asyncio
import copy
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
MINTED = "exec-3749"


class _Dec:
    def __init__(self, key=None, replay=False, in_flight=False, snapshot=None, execution_id=None):
        self.key, self.replay, self.in_flight, self.snapshot = key, replay, in_flight, snapshot
        self.execution_id = execution_id
        self.enabled = True
        self.scope = None


class _Idem:
    def __init__(self, state):
        self.state = state
        self.seen = {}
        self.attached = []
        self.completed = []
        self.failed = []

    def begin(self, scope, key):
        forced = self.state.get("force_decision")
        if forced is not None:
            return forced
        if not key:
            return _Dec()
        rec = self.seen.get((scope, key))
        if rec is not None:
            return _Dec(key=(scope, key), replay=True, snapshot=rec)
        return _Dec(key=(scope, key))

    def attach_execution(self, decision, execution_id):
        self.attached.append((decision.key, execution_id))

    def complete(self, decision, execution_id, snapshot):
        self.completed.append((execution_id, copy.deepcopy(snapshot)))
        if decision.key is not None:
            self.seen[decision.key] = snapshot

    def fail(self, decision):
        self.failed.append(decision.key)


@pytest.fixture()
def h(monkeypatch):
    """A `_dispatch_jsonrpc` harness: fake db with a real row store, an
    Event-gated dispatch stub that ECHOES the minted id, recording fakes."""
    state = {
        "executions": {},
        "gate": None,              # asyncio.Event → the turn waits for it
        "exec_status": "success",
        "exec_response": "hello from bot",
        "audit": [],
        "dispatched": [],
        "minted": [],
    }

    def _mint(**kw):
        state["minted"].append(kw)
        state["executions"][MINTED] = {
            "agent_name": kw["agent_name"], "status": "running",
            "response": None, "error": None,
        }
        return types.SimpleNamespace(id=MINTED)

    fake_db = types.SimpleNamespace(
        get_execution=lambda eid: state["executions"].get(eid),
        create_task_execution=_mint,
        get_agent_subscription_id=lambda name: "sub-1",
        cancel_queued_execution=lambda eid, reason=None: False,
    )
    monkeypatch.setattr(a2a, "db", fake_db)
    monkeypatch.setattr(a2a, "settings_service",
                        types.SimpleNamespace(get_platform_default_model=lambda: "claude-test"),
                        raising=False)

    async def _dispatch(**kwargs):
        state["dispatched"].append(kwargs)
        if state["gate"] is not None:
            await state["gate"].wait()
        eid = kwargs["execution_id"]
        row = state["executions"][eid]
        row["status"] = state["exec_status"]
        row["response"] = state["exec_response"] if state["exec_status"] == "success" else None
        row["error"] = "boom" if state["exec_status"] == "failed" else None
        return types.SimpleNamespace(
            execution_id=eid, status=state["exec_status"],
            response=row["response"], error=row["error"])
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _dispatch)

    class _Audit:
        async def log(self, **kwargs):
            state["audit"].append(kwargs)
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    idem = _Idem(state)
    monkeypatch.setattr(a2a, "idempotency_service", idem)

    user = types.SimpleNamespace(id=1, username="alice", email="alice@example.com",
                                 role="user", agent_name=None, mcp_key_id="k1",
                                 mcp_key_name="alice-key")
    request = types.SimpleNamespace(scope={"path": f"/a2a/{AGENT}"},
                                    client=types.SimpleNamespace(host="203.0.113.9"))

    def _params(text="hi", message_id="m-1", configuration=None):
        params = {"message": {"role": "user", "messageId": message_id,
                              "parts": [{"kind": "text", "text": text}]}}
        if configuration is not None:
            params["configuration"] = configuration
        return params

    async def _send(message_id="m-1", configuration=None, aauth=None, rpc_id=1):
        resp = await a2a._dispatch_jsonrpc(
            AGENT, request, ("message/send", _params(message_id=message_id,
                                                      configuration=configuration), rpc_id),
            current_user=None if aauth is not None else user, aauth=aauth)
        return json.loads(resp.body)

    async def _get(exec_id, aauth=None):
        resp = await a2a._dispatch_jsonrpc(
            AGENT, request, ("tasks/get", {"id": exec_id}, "g"),
            current_user=None if aauth is not None else user, aauth=aauth)
        return json.loads(resp.body)

    async def _drain():
        """Release the gate and wait for every detached turn to finish."""
        if state["gate"] is not None:
            state["gate"].set()
        pending = list(a2a._INFLIGHT_SENDS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return types.SimpleNamespace(state=state, idem=idem, user=user, request=request,
                                 send=_send, get=_get, drain=_drain)


def _slow(h, monkeypatch):
    """Make every dispatch outlive the budget until `drain()` releases it."""
    monkeypatch.setattr(a2a, "A2A_SEND_WORKING_AFTER_SECONDS", 0.01)

    async def _arm():
        h.state["gate"] = asyncio.Event()
    return _arm


# --------------------------------------------------------------------------- #
# Fast path: the bytes a caller saw before #3749 are the bytes it sees now
# --------------------------------------------------------------------------- #
def test_fast_path_response_and_snapshot_are_byte_identical(h):
    body = asyncio.run(h.send())
    task = body["result"]
    artifact_id = task["artifacts"][0]["artifactId"]
    # The pre-change literal (routers/a2a.py @ dev 3e5622ab9), with the one
    # random field — the artifactId — substituted.
    expected = {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "id": MINTED, "contextId": MINTED, "kind": "task",
            "status": {"state": "completed"},
            "artifacts": [{"artifactId": artifact_id,
                           "parts": [{"kind": "text", "text": "hello from bot"}]}],
        },
    }
    assert body == expected
    # The snapshot the claim is completed with IS the Task — one complete(),
    # with the terminal, never a `working` one.
    assert h.idem.completed == [(MINTED, task)]
    assert h.idem.failed == []
    assert [a["details"] for a in h.state["audit"]] == [{"execution_id": MINTED, "state": "completed"}]
    assert a2a._INFLIGHT_SENDS == set()


def test_send_pre_mints_the_row_and_dispatches_with_its_id(h):
    asyncio.run(h.send())
    [mint] = h.state["minted"]
    assert mint["agent_name"] == AGENT and mint["message"] == "hi" and mint["triggered_by"] == "a2a"
    fields = mint["fields"]
    assert fields.source_user_email == "alice@example.com"
    assert fields.source_mcp_key_id == "k1"
    assert fields.subscription_id == "sub-1"
    assert fields.model_used == "claude-test"           # E-F1: never NULL on a minted row
    [dispatch] = h.state["dispatched"]
    assert dispatch["execution_id"] == MINTED
    assert dispatch["triggered_by"] == "a2a"
    # T1: the claim names the row at mint time.
    assert h.idem.attached == [((f"a2a:{AGENT}:k1", "m-1"), MINTED)]


# --------------------------------------------------------------------------- #
# Slow path: `working` + id, then the tail lands once
# --------------------------------------------------------------------------- #
def test_slow_turn_returns_working_with_the_execution_id(h, monkeypatch):
    arm = _slow(h, monkeypatch)

    async def run():
        await arm()
        body = await h.send()
        assert body["result"]["status"]["state"] == "working"
        assert body["result"]["id"] == MINTED
        assert "artifacts" not in body["result"]
        # AC e: nothing terminal has happened — no snapshot, no audit row.
        assert h.idem.completed == [] and h.state["audit"] == []
        assert len(a2a._INFLIGHT_SENDS) == 1
        await h.drain()
        # The detached tail finished the turn exactly as the fast path would.
        [(eid, snap)] = h.idem.completed
        assert eid == MINTED and snap["status"]["state"] == "completed"
        assert snap["artifacts"][0]["parts"][0]["text"] == "hello from bot"
        [audit] = h.state["audit"]
        assert audit["event_action"] == "a2a_task"
        assert audit["details"] == {"execution_id": MINTED, "state": "completed"}
        assert audit["endpoint"] == f"/a2a/{AGENT}" and audit["actor_ip"] == "203.0.113.9"
        assert a2a._INFLIGHT_SENDS == set()
    asyncio.run(run())


def test_slow_queued_row_is_submitted(h, monkeypatch):
    """Pull pilot: the row is CASed RUNNING→QUEUED on enqueue; the early answer
    says so rather than claiming the agent is working."""
    arm = _slow(h, monkeypatch)

    async def run():
        await arm()
        orig = h.state["executions"]
        _mint = a2a.db.create_task_execution

        def _mint_queued(**kw):
            row = _mint(**kw)
            orig[MINTED]["status"] = "queued"
            return row
        a2a.db.create_task_execution = _mint_queued
        body = await h.send()
        assert body["result"]["status"]["state"] == "submitted"
        assert body["result"]["id"] == MINTED
        await h.drain()
    asyncio.run(run())


def test_bearer_tasks_get_after_completion(h, monkeypatch):
    arm = _slow(h, monkeypatch)

    async def run():
        await arm()
        body = await h.send()
        assert body["result"]["status"]["state"] == "working"
        mid = await h.get(MINTED)
        assert mid["result"]["status"]["state"] == "working"
        await h.drain()
        done = await h.get(MINTED)
        assert done["result"]["status"]["state"] == "completed"
        assert done["result"]["artifacts"][0]["parts"][0]["text"] == "hello from bot"
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Replay semantics for the messageId
# --------------------------------------------------------------------------- #
def test_replay_inside_window_is_working_and_after_completion_is_terminal(h, monkeypatch):
    arm = _slow(h, monkeypatch)

    async def run():
        await arm()
        first = await h.send(message_id="dup")
        assert first["result"]["status"]["state"] == "working"
        # The real `begin()` answers a re-send of a running claim with
        # in_flight + the attached execution_id — answer FROM THE ROW.
        h.state["force_decision"] = _Dec(key=("s", "dup"), replay=True, in_flight=True,
                                         execution_id=MINTED)
        again = await h.send(message_id="dup")
        assert again["result"]["status"]["state"] == "working"
        assert again["result"]["id"] == MINTED
        assert len(h.state["dispatched"]) == 1, "a replay must never re-dispatch"
        # Row finished but the tail has not completed the claim yet (or the
        # process died mid-turn): the terminal Task comes from the row.
        h.state["executions"][MINTED].update(status="success", response="late answer")
        late = await h.send(message_id="dup")
        assert late["result"]["status"]["state"] == "completed"
        assert late["result"]["artifacts"][0]["parts"][0]["text"] == "late answer"
        # After completion the stored snapshot replays verbatim, as today.
        h.state["force_decision"] = None
        await h.drain()
        replay = await h.send(message_id="dup")
        [(_, snap)] = h.idem.completed
        assert replay["result"] == snap
        assert len(h.state["dispatched"]) == 1
    asyncio.run(run())


def test_in_flight_claim_without_an_id_keeps_the_retryable_error(h):
    h.state["force_decision"] = _Dec(key=("s", "dup"), replay=True, in_flight=True)
    body = asyncio.run(h.send(message_id="dup"))
    assert body["error"]["code"] == -32603 and body["error"]["data"]["retryable"] is True
    assert h.state["dispatched"] == []


def test_in_flight_claim_on_another_agents_row_keeps_the_retryable_error(h):
    h.state["executions"]["exec-other"] = {"agent_name": "someone-else", "status": "running"}
    h.state["force_decision"] = _Dec(key=("s", "dup"), replay=True, in_flight=True,
                                     execution_id="exec-other")
    body = asyncio.run(h.send(message_id="dup"))
    assert "error" in body and body["error"]["code"] == -32603


# --------------------------------------------------------------------------- #
# AAuth caller: ownership is written BEFORE the id is disclosed
# --------------------------------------------------------------------------- #
def test_aauth_caller_can_poll_a_long_turn(h, monkeypatch):
    arm = _slow(h, monkeypatch)
    owners = {}
    remember_calls = []
    inbound = a2a.aauth_inbound

    def _remember(eid, ident):
        remember_calls.append(eid)
        owners[eid] = ident
        return True
    monkeypatch.setattr(inbound, "remember_execution", _remember)
    monkeypatch.setattr(inbound, "execution_owned_by", lambda eid, ident: owners.get(eid) == ident)
    monkeypatch.setattr(inbound, "audit_details", lambda verified, allowlist: {"aauth_identity": verified.identity})

    async def _activity(agent_name, verified, **kw):
        h.state.setdefault("activity", []).append(kw)
    monkeypatch.setattr(inbound, "record_activity", _activity)
    aauth = types.SimpleNamespace(identity="https://caller.example")

    async def run():
        await arm()
        body = await h.send(aauth=aauth)
        assert body["result"]["status"]["state"] == "working"
        # AC 2a: the owner map names the caller before the `working` answer.
        assert owners == {MINTED: "https://caller.example"}
        mid = await h.get(MINTED, aauth=aauth)
        assert mid["result"]["status"]["state"] == "working"
        stranger = await h.get(MINTED, aauth=types.SimpleNamespace(identity="https://other.example"))
        assert stranger["error"]["code"] == a2a._A2A_TASK_NOT_FOUND
        await h.drain()
        done = await h.get(MINTED, aauth=aauth)
        assert done["result"]["status"]["state"] == "completed"
        assert done["result"]["artifacts"][0]["parts"][0]["text"] == "hello from bot"
        # Audit once, on the terminal, with the AAuth details as before.
        [audit] = h.state["audit"]
        assert audit["details"] == {"execution_id": MINTED, "state": "completed",
                                    "aauth_identity": "https://caller.example"}
        assert audit["actor_external_id"] == "https://caller.example"
        assert len(h.state["activity"]) == 1
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# `tasks/get` state map: a gate-refused row is `rejected`, not `working`
# --------------------------------------------------------------------------- #
def test_tasks_get_maps_skipped_to_rejected(h):
    h.state["executions"]["e"] = {"agent_name": AGENT, "status": "skipped",
                                  "response": None, "error": "Not run: skill refused"}
    body = asyncio.run(h.get("e"))
    task = body["result"]
    assert task["status"]["state"] == "rejected"
    assert task["status"]["message"]["parts"][0]["text"] == "Not run: skill refused"


def test_budget_is_below_client_rpc_timeout():
    from services import a2a_client
    budget = a2a.A2A_SEND_WORKING_AFTER_SECONDS
    assert budget < a2a_client.A2A_RPC_TIMEOUT
    # The MCP tool's default outer limit (`MCP_A2A_TIMEOUT_MS`, 40 s) must fit a
    # cold card fetch in front of the send.
    assert a2a_client.A2A_CARD_FETCH_TIMEOUT + budget < 40

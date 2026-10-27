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
        fields = kw.get("fields")
        state["executions"][MINTED] = {
            "agent_name": kw["agent_name"], "status": "running",
            "response": None, "error": None,
            # #3751: the attribution the real mint stamps, which the bearer
            # `tasks/get` / `tasks/cancel` gate reads.
            "triggered_by": kw.get("triggered_by"),
            "source_user_id": getattr(fields, "source_user_id", None),
            "source_mcp_key_id": getattr(fields, "source_mcp_key_id", None),
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


# #3751: the fixture user's starter fields, for rows these tests poll directly.
_STARTED_BY_ALICE = {"triggered_by": "a2a", "source_user_id": 1, "source_mcp_key_id": "k1"}


# --------------------------------------------------------------------------- #
# `tasks/get` state map: a gate-refused row is `rejected`, not `working`
# --------------------------------------------------------------------------- #
def test_tasks_get_maps_skipped_to_rejected(h):
    h.state["executions"]["e"] = {"agent_name": AGENT, "status": "skipped",
                                  "response": None, "error": "Not run: skill refused",
                                  **_STARTED_BY_ALICE}
    body = asyncio.run(h.get("e"))
    task = body["result"]
    assert task["status"]["state"] == "rejected"
    assert task["status"]["message"]["parts"][0]["text"] == "Not run: skill refused"


def test_tasks_cancel_treats_skipped_as_terminal(h, monkeypatch):
    """Review I3: a row that polls `rejected` is terminal for `tasks/cancel` too —
    the same not-cancelable answer as any other terminal state, no signal sent."""
    terminated = []

    async def _terminate(agent_name, exec_id):
        terminated.append(exec_id)
        return True
    monkeypatch.setattr(a2a, "terminate_execution_on_agent", _terminate)
    h.state["executions"]["e"] = {"agent_name": AGENT, "status": "skipped",
                                  "response": None, "error": "Not run: skill refused",
                                  **_STARTED_BY_ALICE}

    resp = asyncio.run(a2a._dispatch_jsonrpc(
        AGENT, h.request, ("tasks/cancel", {"id": "e"}, "c"), current_user=h.user))
    body = json.loads(resp.body)

    assert body["error"]["code"] == a2a._A2A_TASK_NOT_CANCELABLE
    assert body["error"]["message"] == "Task is already in a terminal state"
    assert terminated == [] and h.state["audit"] == []


def _mcp_a2a_timeout_seconds() -> float:
    """The MCP tool's default outer limit, `MCP_A2A_TIMEOUT_MS`, read from the
    TypeScript source it lives in (Python cannot import it) — so a change there
    re-runs this bound instead of leaving a stale literal behind."""
    import re
    client_ts = _BACKEND.parent / "mcp-server" / "src" / "client.ts"
    m = re.search(r"MCP_A2A_TIMEOUT_MS\s*\|\|\s*(\d+)", client_ts.read_text())
    assert m, "MCP_A2A_TIMEOUT_MS default not found in client.ts"
    return int(m.group(1)) / 1000


MCP_A2A_TIMEOUT_SECONDS = _mcp_a2a_timeout_seconds()


def test_budget_is_below_client_rpc_timeout():
    from services import a2a_client
    budget = a2a.A2A_SEND_WORKING_AFTER_SECONDS
    assert budget < a2a_client.A2A_RPC_TIMEOUT
    # The MCP tool's default outer limit must fit a cold card fetch in front of
    # the send.
    assert a2a_client.A2A_CARD_FETCH_TIMEOUT + budget < MCP_A2A_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# `configuration.blocking` (A2A v0.3 MessageSendConfiguration, ruling T4)
# --------------------------------------------------------------------------- #
def test_blocking_true_holds_the_whole_turn_past_the_budget(h, monkeypatch):
    arm = _slow(h, monkeypatch)

    async def run():
        await arm()
        send = asyncio.create_task(h.send(configuration={"blocking": True}))
        # Five budgets later the request is still holding — no `working` answer.
        await asyncio.sleep(0.05)
        assert not send.done()
        await h.drain()
        body = await send
        assert body["result"]["status"]["state"] == "completed"
        assert body["result"]["artifacts"][0]["parts"][0]["text"] == "hello from bot"
        [(eid, snap)] = h.idem.completed
        assert eid == MINTED and snap == body["result"]
    asyncio.run(run())


def test_blocking_false_answers_at_the_mint(h):
    """The real 25 s budget is left in place: `blocking: false` must not wait
    for it — the answer is built from the row the moment it exists."""
    assert a2a.A2A_SEND_WORKING_AFTER_SECONDS == 25.0

    async def run():
        h.state["gate"] = asyncio.Event()
        body = await asyncio.wait_for(h.send(configuration={"blocking": False}), timeout=5)
        assert body["result"]["status"]["state"] == "working"
        assert body["result"]["id"] == MINTED
        assert h.idem.completed == []
        await h.drain()
        assert [eid for eid, _ in h.idem.completed] == [MINTED]
    asyncio.run(run())


@pytest.mark.parametrize("configuration", [None, {}, {"blocking": "yes"}])
def test_blocking_absent_or_non_bool_uses_the_budget(h, monkeypatch, configuration):
    arm = _slow(h, monkeypatch)

    async def run():
        await arm()
        send = asyncio.create_task(h.send(configuration=configuration))
        body = await asyncio.wait_for(send, timeout=5)
        assert body["result"]["status"]["state"] == "working"
        await h.drain()
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Mint failure: fail the claim, answer -32603, never dispatch un-minted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mint", ["raises", "returns_none", "returns_no_id", "settings_raises"])
def test_failed_mint_fails_the_claim_and_never_dispatches(h, monkeypatch, mint):
    if mint == "settings_raises":
        # Review I1: a settings read failure must not be swallowed into a NULL
        # model — `execute_task` re-resolves it and fails, stranding the row.
        def _settings_down():
            raise RuntimeError("settings unavailable")
        monkeypatch.setattr(a2a.settings_service, "get_platform_default_model", _settings_down)
    else:
        def _bad(**kw):
            if mint == "raises":
                raise RuntimeError("db is locked")
            return None if mint == "returns_none" else types.SimpleNamespace(id=None)
        a2a.db.create_task_execution = _bad
    body = asyncio.run(h.send(message_id="m-mint"))
    assert body["error"]["code"] == -32603
    assert body["error"]["message"] == "Task execution failed"
    # The claim is released so the caller's retry runs; nothing was attached.
    assert h.idem.failed == [(f"a2a:{AGENT}:k1", "m-mint")]
    assert h.idem.attached == [] and h.idem.completed == []
    if mint == "settings_raises":
        assert h.state["minted"] == [] and h.state["executions"] == {}  # no stranded row
    # Never the pre-#3749 path: no un-minted dispatch, no detached turn.
    assert h.state["dispatched"] == []
    assert a2a._INFLIGHT_SENDS == set()


# --------------------------------------------------------------------------- #
# The detached turn outlives the request
# --------------------------------------------------------------------------- #
def test_client_disconnect_does_not_cancel_the_turn(h):
    """Even if the handler is cancelled (the app's middleware does not cancel
    on disconnect, but loop shutdown can), the turn it detached must run on to
    its terminal, complete the claim and audit once."""
    async def run():
        h.state["gate"] = asyncio.Event()
        handler = asyncio.create_task(h.send())
        while not h.state["dispatched"]:
            await asyncio.sleep(0)
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        [turn] = list(a2a._INFLIGHT_SENDS)
        assert not turn.done() and not turn.cancelled()
        await h.drain()
        assert not turn.cancelled()
        [(eid, snap)] = h.idem.completed
        assert eid == MINTED and snap["status"]["state"] == "completed"
        assert h.idem.failed == []
        assert [a["details"]["state"] for a in h.state["audit"]] == ["completed"]
        assert h.state["executions"][MINTED]["status"] == "success"
    asyncio.run(run())


def test_terminal_at_the_deadline_answers_the_terminal_task(h, monkeypatch):
    """The row reached its terminal but the tail has not returned yet when the
    budget fires — the early answer reads the row, so it is the terminal Task,
    never a stale `working`."""
    arm = _slow(h, monkeypatch)
    inner = a2a.dispatch_and_await_terminal

    async def _finished_row_then_wait(**kwargs):
        row = h.state["executions"][kwargs["execution_id"]]
        row.update(status="success", response="done already")
        return await inner(**kwargs)
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _finished_row_then_wait)

    async def run():
        await arm()
        h.state["exec_response"] = "done already"
        body = await h.send()
        assert body["result"]["status"]["state"] == "completed"
        assert body["result"]["artifacts"][0]["parts"][0]["text"] == "done already"
        # The tail still owns the claim + audit, exactly once.
        assert h.idem.completed == []
        await h.drain()
        assert len(h.idem.completed) == 1 and len(h.state["audit"]) == 1
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# AAuth: an id the caller could not poll is never disclosed
# --------------------------------------------------------------------------- #
def test_aauth_remember_false_keeps_holding_and_discloses_no_id(h, monkeypatch):
    arm = _slow(h, monkeypatch)
    inbound = a2a.aauth_inbound
    monkeypatch.setattr(inbound, "remember_execution", lambda eid, ident: False)
    monkeypatch.setattr(inbound, "audit_details", lambda verified, allowlist: {})

    async def _activity(agent_name, verified, **kw):
        return None
    monkeypatch.setattr(inbound, "record_activity", _activity)
    aauth = types.SimpleNamespace(identity="https://caller.example")

    async def run():
        await arm()
        send = asyncio.create_task(h.send(aauth=aauth))
        await asyncio.sleep(0.05)
        # Past the budget, but the owner map could not be written: no `working`.
        assert not send.done()
        await h.drain()
        body = await send
        assert body["result"]["status"]["state"] == "completed"
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Paid door (ent#679): the payer is bound before `working`, polls, may cancel
# --------------------------------------------------------------------------- #
PAYER = "0xPayer3749"


@pytest.fixture()
def p(monkeypatch):
    """`_send_paid_task` over the REAL `run_a2a_paid_turn` / `run_paid_turn`,
    with a fake facilitator and a fake db whose payer binding is computed from
    the payment-log rows, exactly as `nevermined_payer_owns_execution` is."""
    from services import a2a_gate, a2a_payment_gate
    state = {"executions": {}, "payment_log": [], "settles": [], "gate": None,
             "dispatched": [], "minted": [], "terminated": [], "audit": [],
             "exec_status": "success"}

    def _mint(**kw):
        state["minted"].append(kw)
        state["executions"][MINTED] = {"agent_name": kw["agent_name"], "status": "running",
                                       "response": None, "error": None}
        return types.SimpleNamespace(id=MINTED)

    def _owns(agent, eid, payer):
        return any(r.get("execution_id") == eid and r.get("subscriber_address") == payer
                   and r["agent_name"] == agent for r in state["payment_log"])

    monkeypatch.setattr(a2a, "db", types.SimpleNamespace(
        get_execution=lambda eid: state["executions"].get(eid),
        create_task_execution=_mint,
        get_agent_subscription_id=lambda name: None,
        get_public_channel_model=lambda name: "claude-public",
        log_nevermined_payment=lambda **kw: state["payment_log"].append(kw),
        nevermined_payer_owns_execution=_owns,
        cancel_queued_execution=lambda eid, reason=None: False,
    ))
    monkeypatch.setattr(a2a, "build_public_channel_caller_prompt", lambda name: "PUBLIC")

    async def _dispatch(**kwargs):
        state["dispatched"].append(kwargs)
        if state["gate"] is not None:
            await state["gate"].wait()
        row = state["executions"][kwargs["execution_id"]]
        if row["status"] == "running":
            row["status"] = state["exec_status"]
            row["response"] = "paid answer" if row["status"] == "success" else None
        return types.SimpleNamespace(execution_id=kwargs["execution_id"], status=row["status"],
                                     response=row["response"], error=None)
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _dispatch)

    async def _terminate(agent, eid):
        state["terminated"].append((agent, eid))
        state["executions"][eid]["status"] = "cancelled"
        state["gate"].set()
        return True
    monkeypatch.setattr(a2a, "terminate_execution_on_agent", _terminate)

    class _Audit:
        async def log(self, **kwargs):
            state["audit"].append(kwargs)
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    class _Payments:
        async def verify_payment(self, **kw):
            return types.SimpleNamespace(success=True, payer=PAYER, error=None,
                                         agent_request_id="req-1", retryable=False)

        async def settle_payment_once(self, **kw):
            state["settles"].append(kw.get("execution_id"))
            return types.SimpleNamespace(success=True, error=None, tx_hash="0xtx",
                                         remaining_balance="1", credits_redeemed="2")
    monkeypatch.setattr(a2a, "get_nevermined_payment_service", lambda: _Payments())

    class _PaidDec:
        def __init__(self, scope, key, in_flight=False):
            self.scope, self.key = scope, key
            self.replay = self.in_flight = in_flight
            self.snapshot, self.execution_id, self.enabled = None, None, True

    class _PaidIdem:
        """Pending claims are tracked, so a re-send while running is in-flight."""
        def __init__(self):
            self.pending, self.attached, self.failed = set(), [], []

        @staticmethod
        def derive_payment_key(token, body):
            return f"{token}:{body!r}"

        def begin(self, scope, key):
            if (scope, key) in self.pending:
                return _PaidDec(scope, key, in_flight=True)
            self.pending.add((scope, key))
            return _PaidDec(scope, key)

        def attach_execution(self, decision, execution_id):
            self.attached.append(execution_id)

        def complete(self, decision, execution_id, snapshot):
            self.pending.discard((decision.scope, decision.key))

        def upgrade_snapshot(self, scope, key, snapshot):
            pass

        def fail(self, decision):
            self.failed.append(decision.key)
            self.pending.discard((decision.scope, decision.key))
    idem = _PaidIdem()
    monkeypatch.setattr(a2a, "idempotency_service", idem)
    a2a_gate.clear_provider()

    priced = a2a_payment_gate.PricedAgent(
        config=types.SimpleNamespace(agent_name=AGENT, enabled=True, credits_per_request=2,
                                     nvm_environment="sandbox", nvm_plan_id="plan-1",
                                     nvm_agent_id="agent-1"),
        nvm_api_key="sandbox:jwt")
    request = types.SimpleNamespace(scope={"path": f"/a2a/{AGENT}"})

    async def _send(blocking=None):
        resp = await a2a._send_paid_task(AGENT, "hi", priced, "tok", "http://t", 1,
                                         "203.0.113.9", request, blocking=blocking)
        return json.loads(resp.body)

    async def _task_rpc(method, exec_id):
        if not await a2a._payer_for_task(AGENT, exec_id, priced, "tok", "http://t"):
            return {"error": {"code": a2a._A2A_TASK_NOT_FOUND}}
        resp = await a2a._bound_task_rpc(AGENT, method, exec_id, "g", "203.0.113.9")
        return json.loads(resp.body)

    async def _drain():
        if state["gate"] is not None:
            state["gate"].set()
        pending = list(a2a._INFLIGHT_SENDS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return types.SimpleNamespace(state=state, idem=idem, send=_send, rpc=_task_rpc,
                                 drain=_drain)


def _verify_rows(state):
    return [r for r in state["payment_log"] if r["action"] == "verify"]


def test_paid_slow_turn_binds_the_payer_before_working(p, monkeypatch):
    monkeypatch.setattr(a2a, "A2A_SEND_WORKING_AFTER_SECONDS", 0.01)

    async def run():
        p.state["gate"] = asyncio.Event()
        body = await p.send()
        assert body["result"]["status"]["state"] == "working"
        assert body["result"]["id"] == MINTED
        # The bound verify row (agent, execution id, payer) exists BEFORE the
        # answer — and nothing has settled.
        assert {"agent_name": AGENT, "action": "verify", "success": True,
                "subscriber_address": PAYER, "execution_id": MINTED} in p.state["payment_log"]
        assert p.state["settles"] == []
        # payer_owns_execution is true mid-turn: the token polls `working`.
        mid = await p.rpc("tasks/get", MINTED)
        assert mid["result"]["status"]["state"] == "working"
        await p.drain()
        done = await p.rpc("tasks/get", MINTED)
        assert done["result"]["status"]["state"] == "completed"
        assert done["result"]["artifacts"][0]["parts"][0]["text"] == "paid answer"
        assert p.state["settles"] == [MINTED]
        # T6: the plain verify row, our bound one, and run_paid_turn's own
        # post-execute bound row — three, tolerated (nothing aggregates them).
        rows = _verify_rows(p.state)
        assert len(rows) == 3
        assert [r.get("execution_id") for r in rows] == [None, MINTED, MINTED]
        # `model_used` on the paid row is the public-channel model the turn runs on.
        [mint] = p.state["minted"]
        assert mint["fields"].model_used == "claude-public"
        assert mint["fields"].source_user_id is None
        assert p.state["dispatched"][0]["model"] == "claude-public"
    asyncio.run(run())


def test_paid_resend_while_running_keeps_in_flight(p, monkeypatch):
    """T2: the payer already holds the id — a re-send is refused IN_FLIGHT, it
    neither re-dispatches nor re-charges."""
    monkeypatch.setattr(a2a, "A2A_SEND_WORKING_AFTER_SECONDS", 0.01)

    async def run():
        p.state["gate"] = asyncio.Event()
        first = await p.send()
        assert first["result"]["status"]["state"] == "working"
        again = await p.send()
        assert again["error"]["code"] == -32603
        assert again["error"]["data"] == {"code": "in_flight", "retryable": True}
        assert len(p.state["dispatched"]) == 1 and len(p.state["minted"]) == 1
        await p.drain()
        assert p.state["settles"] == [MINTED]
    asyncio.run(run())


def test_paid_cancel_mid_turn_is_no_charge_and_no_answer(p, monkeypatch):
    """T5: the early binding lets the payer cancel mid-turn; a cancelled paid
    turn is never settled and delivers no answer."""
    monkeypatch.setattr(a2a, "A2A_SEND_WORKING_AFTER_SECONDS", 0.01)

    async def run():
        p.state["gate"] = asyncio.Event()
        body = await p.send()
        assert body["result"]["status"]["state"] == "working"
        cancel = await p.rpc("tasks/cancel", MINTED)
        assert cancel["result"]["status"]["state"] == "canceled"
        assert p.state["terminated"] == [(AGENT, MINTED)]
        await p.drain()
        assert p.state["settles"] == [], "a cancelled paid turn must never be charged"
        after = await p.rpc("tasks/get", MINTED)
        assert after["result"]["status"]["state"] == "canceled"
        assert "artifacts" not in after["result"]
        assert not any(r["action"].startswith("settle") for r in p.state["payment_log"])
    asyncio.run(run())


def test_paid_another_wallet_cannot_poll_the_bound_turn(p, monkeypatch):
    monkeypatch.setattr(a2a, "A2A_SEND_WORKING_AFTER_SECONDS", 0.01)

    async def run():
        p.state["gate"] = asyncio.Event()
        await p.send()
        for row in p.state["payment_log"]:
            row["subscriber_address"] = "0xSomeoneElse"
        stranger = await p.rpc("tasks/get", MINTED)
        assert stranger["error"]["code"] == a2a._A2A_TASK_NOT_FOUND
        await p.drain()
    asyncio.run(run())


def test_paid_fast_turn_writes_no_early_binding(p):
    """Inside the budget the paid answer is the terminal Task, and only the two
    pre-#3749 verify rows are written."""
    async def run():
        body = await p.send()
        assert body["result"]["status"]["state"] == "completed"
        assert [r.get("execution_id") for r in _verify_rows(p.state)] == [None, MINTED]
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# `remember_execution` — the real body (review I7): the router keeps holding on
# False, so both return values must mean what they say.
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.sets = []

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.sets.append((key, value, ex))


@pytest.mark.parametrize("client", ["ok", "none", "raises"])
def test_remember_execution_returns_whether_the_owner_was_recorded(monkeypatch, client):
    from services import rate_limiter
    from services.aauth import inbound
    fake = None if client == "none" else _FakeRedis(fail=client == "raises")
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: fake)

    recorded = inbound.remember_execution("exec-9", "agent:peer")

    assert recorded is (client == "ok")
    if client == "ok":
        assert fake.sets == [(inbound._EXEC_KEY.format("exec-9"), "agent:peer",
                              inbound.EXECUTION_OWNER_TTL)]

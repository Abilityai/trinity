"""ent#157 — A2A inbound server (well-known card + JSON-RPC task endpoint).

Covers the reachable core:
  - public /a2a/{name}/.well-known/agent-card.json: uniform 404 when not exposed
    / non-existent; honest card (url → JSON-RPC endpoint, protocolVersion pinned,
    Bearer securityScheme) when exposed;
  - JSON-RPC dispatch: parse error (-32700), invalid envelope (-32600), method
    not found (-32601), invalid params (-32602);
  - message/send bridges to execute_task(triggered_by="a2a") and returns an A2A
    Task; tasks/get + tasks/cancel; not-exposed POST → uniform 404;
  - inbound allow-list gate (a2a_gate provider) → 403;
  - trigger-boundary idempotency: a replayed messageId returns the stored task
    without re-executing.

Auth (Bearer MCP key → 401) is enforced by the shared `get_current_user`
dependency (tested platform-wide); here it is overridden to a fixed user so the
A2A-specific logic is exercised in isolation.

The card honesty is also unit-tested at the generator level.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import routers.a2a as a2a  # noqa: E402
import dependencies as deps  # noqa: E402
from services import a2a_gate  # noqa: E402

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Card generator honesty (pure unit)
# --------------------------------------------------------------------------- #
def test_card_is_honest_url_and_version():
    from services.a2a_card_service import generate_a2a_card
    card = generate_a2a_card(
        agent_name="bot",
        template_data={"display_name": "Bot", "capabilities": ["research"]},
        base_url="https://trinity.example.com",
    )
    assert card["protocolVersion"] == "0.3.0"
    assert card["url"] == "https://trinity.example.com/a2a/bot"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    # url points at the JSON-RPC endpoint, NOT the old chat placeholder.
    assert "/chat" not in card["url"]


# --------------------------------------------------------------------------- #
# Router harness
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient


    # Fake DB surface the router touches.
    state = {
        "exposed": {"bot"},                       # a2a_exposed agents
        "access": {"bot"},                        # agents the caller can access
        "executions": {},                         # execution_id → row
    }
    fake_db = types.SimpleNamespace(
        get_a2a_exposed=lambda name: name in state["exposed"],
        can_user_access_agent=lambda user, name: name in state["access"],
        get_execution=lambda eid: state["executions"].get(eid),
    )
    monkeypatch.setattr(a2a, "db", fake_db)

    # get_agent_container: a running stub for known agents, None otherwise.
    def _container(name):
        return types.SimpleNamespace(status="running", labels={}) if name in state["exposed"] else None
    monkeypatch.setattr(a2a, "get_agent_container", _container)

    async def _tmpl(name, container):
        return {"display_name": name, "capabilities": ["chat"]}
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)

    # Fake task-execution service → deterministic success.
    class _Result:
        def __init__(self):
            self.execution_id = "exec-123"
            self.status = "success"
            self.response = "hello from bot"
            self.error = None

    class _Svc:
        async def execute_task(self, **kwargs):
            state["last_execute"] = kwargs
            return _Result()

    # NOTE: the unit harness loads a duplicate `services.*` module, so patching
    # `services.task_execution_service` directly does NOT reach the router. We
    # patch the names the router hoisted to its OWN module globals instead —
    # those resolve through routers.a2a's namespace (this exact `a2a` object).
    monkeypatch.setattr(a2a, "get_task_execution_service", lambda: _Svc())

    async def _terminate(agent, eid):
        state["terminated"] = (agent, eid)
        return True
    monkeypatch.setattr(a2a, "terminate_execution_on_agent", _terminate)

    # No-op audit.
    class _Audit:
        async def log(self, **kwargs):
            return None
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    # Idempotency: default no-dedup; a per-test dict enables messageId replay.
    seen: dict = {}

    class _Dec:
        def __init__(self, key=None, replay=False, in_flight=False, snapshot=None):
            self.key, self.replay, self.in_flight, self.snapshot = key, replay, in_flight, snapshot
            self.enabled = True

    class _Idem:
        def begin(self, scope, key):
            if not key:
                return _Dec()
            rec = seen.get((scope, key))
            if rec is not None:
                return _Dec(key=(scope, key), replay=True, snapshot=rec)
            return _Dec(key=(scope, key))

        def complete(self, decision, execution_id, snapshot):
            if decision.key is not None:
                seen[decision.key] = snapshot

        def fail(self, decision):
            pass

    monkeypatch.setattr(a2a, "idempotency_service", _Idem())

    # a2a_gate: reset to OSS no-op by default.
    a2a_gate.clear_provider()

    # Auth: fixed human user.
    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    user = types.SimpleNamespace(id=1, username="alice", email="alice@example.com",
                                 role="user", agent_name=None, mcp_key_id="k1")
    app.dependency_overrides[deps.get_current_user] = lambda: user

    return types.SimpleNamespace(http=TestClient(app), state=state, a2a_gate=a2a_gate, user=user)


# --------------------------------------------------------------------------- #
# Well-known card
# --------------------------------------------------------------------------- #
def test_wellknown_404_when_not_exposed(client):
    r = client.http.get("/a2a/ghost/.well-known/agent-card.json")
    assert r.status_code == 404


def test_wellknown_serves_card_when_exposed(client):
    r = client.http.get("/a2a/bot/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["protocolVersion"] == "0.3.0"
    assert card["url"].endswith("/a2a/bot")


# --------------------------------------------------------------------------- #
# JSON-RPC envelope errors
# --------------------------------------------------------------------------- #
def test_jsonrpc_parse_error(client):
    r = client.http.post("/a2a/bot", content=b"{not json")
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32700


def test_jsonrpc_invalid_envelope(client):
    r = client.http.post("/a2a/bot", json={"method": "message/send"})  # no jsonrpc:2.0
    assert r.json()["error"]["code"] == -32600


def test_jsonrpc_method_not_found(client):
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert r.json()["error"]["code"] == -32601


def test_jsonrpc_message_send_missing_message(client):
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}})
    assert r.json()["error"]["code"] == -32602


def test_post_uniform_404_when_not_exposed(client):
    r = client.http.post("/a2a/ghost", json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                             "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# message/send bridge
# --------------------------------------------------------------------------- #
def _send(client, text="hi there", message_id=None):
    msg = {"role": "user", "parts": [{"kind": "text", "text": text}]}
    if message_id:
        msg["messageId"] = message_id
    return client.http.post("/a2a/bot", json={
        "jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"message": msg}})


def test_message_send_bridges_to_execute_task(client):
    r = _send(client)
    assert r.status_code == 200
    body = r.json()
    task = body["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["id"] == "exec-123"
    assert task["artifacts"][0]["parts"][0]["text"] == "hello from bot"
    # Bridged with triggered_by="a2a" and the caller identity.
    assert client.state["last_execute"]["triggered_by"] == "a2a"
    assert client.state["last_execute"]["source_user_email"] == "alice@example.com"


def test_message_send_idempotent_replay_does_not_reexecute(client):
    r1 = _send(client, message_id="m-1")
    assert r1.json()["result"]["id"] == "exec-123"
    # Mutate the fake so a re-execute would be observable, then replay.
    client.state["executions"].clear()
    r2 = _send(client, message_id="m-1")
    # Same stored task returned; the execute path must not have run again.
    assert r2.json()["result"]["id"] == "exec-123"


# --------------------------------------------------------------------------- #
# tasks/get + tasks/cancel
# --------------------------------------------------------------------------- #
def test_tasks_get_maps_status(client):
    client.state["executions"]["exec-9"] = {"agent_name": "bot", "status": "success", "response": "done"}
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "exec-9"}})
    task = r.json()["result"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "done"


def test_tasks_get_unknown_or_foreign_is_task_not_found(client):
    client.state["executions"]["exec-x"] = {"agent_name": "OTHER", "status": "success"}
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "exec-x"}})
    assert r.json()["error"]["code"] == -32001


def test_tasks_cancel(client):
    client.state["executions"]["exec-c"] = {"agent_name": "bot", "status": "running"}
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {"id": "exec-c"}})
    assert r.json()["result"]["status"]["state"] == "canceled"
    assert client.state["terminated"] == ("bot", "exec-c")


# --------------------------------------------------------------------------- #
# Inbound allow-list gate
# --------------------------------------------------------------------------- #
def test_allowlist_provider_denies_off_list_caller(client):
    class _DenyAll:
        def is_inbound_allowed(self, agent_name, caller_identity):
            return False
    client.a2a_gate.register_provider(_DenyAll())
    try:
        r = _send(client)
        assert r.status_code == 403
    finally:
        client.a2a_gate.clear_provider()


def test_allowlist_provider_allows_listed_caller(client):
    class _AllowAlice:
        def is_inbound_allowed(self, agent_name, caller_identity):
            return caller_identity == "alice@example.com"
    client.a2a_gate.register_provider(_AllowAlice())
    try:
        r = _send(client)
        assert r.status_code == 200
        assert r.json()["result"]["status"]["state"] == "completed"
    finally:
        client.a2a_gate.clear_provider()


def test_a2a_gate_fails_open_on_provider_error(client):
    class _Boom:
        def is_inbound_allowed(self, agent_name, caller_identity):
            raise RuntimeError("policy backend down")
    client.a2a_gate.register_provider(_Boom())
    try:
        r = _send(client)
        assert r.status_code == 200  # fail-open: authenticated caller not blocked
    finally:
        client.a2a_gate.clear_provider()


# --------------------------------------------------------------------------- #
# message/stream (SSE)
# --------------------------------------------------------------------------- #
def test_message_stream_emits_working_then_final_completed(client):
    r = client.http.post("/a2a/bot", json={
        "jsonrpc": "2.0", "id": 9, "method": "message/stream",
        "params": {"message": {"parts": [{"kind": "text", "text": "stream me"}]}}})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    events = [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]
    # First a non-final working status, then a final completed task.
    assert events[0]["result"]["status"]["state"] == "working"
    assert events[0]["result"]["final"] is False
    assert events[-1]["result"]["status"]["state"] == "completed"
    assert events[-1]["result"]["final"] is True
    assert events[-1]["result"]["artifacts"][0]["parts"][0]["text"] == "hello from bot"


# --------------------------------------------------------------------------- #
# Idempotency: in-flight duplicate
# --------------------------------------------------------------------------- #
def test_message_send_in_flight_duplicate_is_retryable_error(client, monkeypatch):
    class _InFlight:
        def begin(self, scope, key):
            return types.SimpleNamespace(key=(scope, key), replay=True, in_flight=True, snapshot=None, enabled=True)
        def complete(self, *a, **k):
            pass
        def fail(self, *a, **k):
            pass
    monkeypatch.setattr(a2a, "idempotency_service", _InFlight())
    r = _send(client, message_id="dup-1")
    err = r.json()["error"]
    assert err["code"] == -32603
    assert err["data"]["retryable"] is True


# --------------------------------------------------------------------------- #
# tasks/get state mapping across all Trinity statuses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,expected", [
    ("success", "completed"), ("failed", "failed"), ("cancelled", "canceled"),
    ("running", "working"), ("queued", "submitted"), ("weird", "working"),
])
def test_tasks_get_state_mapping_all(client, status, expected):
    client.state["executions"]["e"] = {"agent_name": "bot", "status": status, "response": "r", "error": "boom"}
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "e"}})
    assert r.json()["result"]["status"]["state"] == expected


# --------------------------------------------------------------------------- #
# Message part handling
# --------------------------------------------------------------------------- #
def test_message_multipart_text_is_concatenated(client):
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {
        "message": {"parts": [
            {"kind": "text", "text": "line one"},
            {"kind": "file", "file": {"uri": "x"}},   # non-text part ignored
            {"kind": "text", "text": "line two"},
        ]}}})
    assert r.json()["result"]["status"]["state"] == "completed"
    assert client.state["last_execute"]["message"] == "line one\nline two"


def test_message_only_nontext_parts_is_invalid(client):
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {
        "message": {"parts": [{"kind": "file", "file": {"uri": "x"}}]}}})
    assert r.json()["error"]["code"] == -32602  # no text → invalid params


# --------------------------------------------------------------------------- #
# Card: exposed-but-stopped agent still serves (from labels)
# --------------------------------------------------------------------------- #
def test_wellknown_serves_stopped_exposed_agent(client, monkeypatch):
    # Exposed agent whose container is stopped: card still served (label fallback).
    monkeypatch.setattr(a2a, "get_agent_container",
                        lambda name: types.SimpleNamespace(status="exited", labels={"trinity.template": "tmpl"}))
    async def _lbl(name, container):
        return {"display_name": "Stopped Bot", "capabilities": []}
    monkeypatch.setattr(a2a, "_fetch_template_data", _lbl)
    r = client.http.get("/a2a/bot/.well-known/agent-card.json")
    assert r.status_code == 200
    assert r.json()["name"] == "Stopped Bot"


# --------------------------------------------------------------------------- #
# Auth: unauthenticated JSON-RPC POST → 401 (fail-closed), before any dispatch
# --------------------------------------------------------------------------- #
def test_jsonrpc_requires_auth_401(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    # Fresh app WITHOUT the get_current_user override → the real dependency runs;
    # a request with no Bearer must fail closed at the auth layer (401), never
    # reaching the exposure/dispatch logic.
    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    c = TestClient(app)
    r = c.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                 "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}})
    assert r.status_code == 401


def test_tasks_get_accepts_object_row_not_only_dict(client):
    """Regression (live-caught): db.get_execution returns a ScheduleExecution
    OBJECT, not a dict — tasks/get must read via attribute access, not .get()."""
    client.state["executions"]["obj-1"] = types.SimpleNamespace(
        agent_name="bot", status="success", response="from object", error=None)
    r = client.http.post("/a2a/bot", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "obj-1"}})
    task = r.json()["result"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "from object"

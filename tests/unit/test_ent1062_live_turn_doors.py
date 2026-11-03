"""The seat-decision and voice-reply doors serve the turn the agent is in
(trinity-enterprise#1062). Related: #4121 (the same rule on write_user_memory).

`POST/GET /api/agents/{name}/decisions` and `POST /api/agents/{name}/voice-reply`
pick a person (or a conversation) from the `execution_id` the caller names.
ent#840 stopped a sibling agent's key from naming a run; the agent's OWN key
could still name another person's finished run.

Each door now holds the ent#926 ledger rule: the named run must be live, and no
other live turn on the agent may resolve to a different scope (for decisions,
the seat's person; for voice, the destination conversation). The decision doors
are also the agent's own: a human is sent away with `agent_only`. People read
and decide through the client-portal routes.

Everything goes through the real routes with the real `get_current_user`, a real
`trinity_mcp_…` key and real rows on the unit conftest's SQLite. Only the
channel send of a voice note is stubbed. The same rule over the network, on a
sibling stack: tests/integration/test_ent1062_live_turn_doors.py.
"""
from __future__ import annotations

import types
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import db
from db.write_params import ExecutionResult, TaskExecutionFields
from db_models import McpApiKeyCreate, UserCreate
from dependencies import create_access_token
from routers import agents as agents_router
from routers import seat_decisions

pytestmark = pytest.mark.unit

ANN, BOB = "ann@example.com", "bob@example.com"
RECORD = {
    "outcome": "approved",
    "decided": "Renew the Acme contract",
    "alternatives": ["let it lapse"],
    "criterion": "renewal cost below the switching cost",
    "reversal": "Acme raises the price",
    "review_by": "2027-06-30",
}


@pytest.fixture
def client(monkeypatch):
    from services import voice_reply_service, tts_service
    sends = []

    async def _send(**kwargs):
        sends.append(kwargs)
        return types.SimpleNamespace(delivered=True, channel=kwargs["channel"], reason=None)

    monkeypatch.setattr(voice_reply_service, "send_voice_reply", _send)
    monkeypatch.setattr(tts_service, "resolve_voice_id", lambda name: "voice-1", raising=False)
    monkeypatch.setattr(seat_decisions.rate_limiter, "enforce", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(seat_decisions.router)
    app.include_router(agents_router.router)
    c = TestClient(app)
    c.sends = sends
    return c


@pytest.fixture
def agent():
    suffix = uuid.uuid4().hex[:8]
    owner = f"owner-{suffix}@example.com"
    db.create_user(UserCreate(username=owner, email=owner, role="creator", password="x"))
    name = f"door-{suffix}"
    db.register_agent_owner(name, owner)
    return SimpleNamespace(name=name, owner=owner, key=db.create_agent_mcp_api_key(name, owner).api_key)


def run(agent_name, triggered_by="public", *, email=ANN, channel=None, chat=None, thread=None,
        client_stamp=None, finished=None):
    row = db.create_task_execution(agent_name, "hello", triggered_by, fields=TaskExecutionFields(
        source_user_email=email, source_channel=channel, source_channel_chat_id=chat,
        source_channel_thread=thread, source_channel_client=client_stamp))
    if finished:
        db.update_execution_status(row.id, finished, ExecutionResult(response="done"))
    return row.id


def end(execution_id):
    db.update_execution_status(execution_id, "success", ExecutionResult(response="done"))


def tg(agent_name, chat, *, email=ANN, finished=None):
    """A one-to-one Telegram turn with a verified speaker."""
    return run(agent_name, "telegram", email=email, channel="telegram", chat=chat, client_stamp=email,
               finished=finished)


def record(client, agent, execution_id, key=None):
    # The platform stamps the turn's id on the call (#2392); #2984 requires it to match.
    return client.post(f"/api/agents/{agent.name}/decisions", json={**RECORD, "execution_id": execution_id},
                       headers={"Authorization": f"Bearer {key or agent.key}",
                                "X-Trinity-Execution-Id": execution_id})


def read(client, agent, execution_id, key=None):
    return client.get(f"/api/agents/{agent.name}/decisions", params={"execution_id": execution_id},
                      headers={"Authorization": f"Bearer {key or agent.key}"})


def speak(client, agent, execution_id):
    return client.post(f"/api/agents/{agent.name}/voice-reply", json={"execution_id": execution_id, "text": "hi"},
                       headers={"Authorization": f"Bearer {agent.key}"})


def decisions(agent, email=ANN):
    return db.list_seat_decisions(agent.name, email, limit=50)


def refused(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text


# ------------------------------------------------------------------ decisions

class TestDecisionsServeTheLiveTurn:
    @pytest.mark.parametrize("ended", ["success", "failed", "cancelled"])
    def test_a_finished_run_of_another_person_cannot_record(self, client, agent, ended):
        theirs = run(agent.name)
        assert record(client, agent, theirs).status_code == 200, "the live turn records"
        db.update_execution_status(theirs, ended, ExecutionResult(response="done"))
        refused(record(client, agent, theirs), 422, "decision_turn_not_live")
        assert len(decisions(agent)) == 1, "a refused record stores nothing"

    def test_a_finished_run_cannot_read_that_persons_decisions(self, client, agent):
        theirs = run(agent.name)
        assert record(client, agent, theirs).status_code == 200
        end(theirs)
        resp = read(client, agent, theirs)
        refused(resp, 422, "decision_turn_not_live")
        assert "renewal cost" not in resp.text, "nothing of the seat is returned"

    def test_the_refusal_tells_the_agent_to_say_so(self, client, agent):
        msg = record(client, agent, run(agent.name, finished="success")).json()["detail"]["message"]
        assert "not recorded" in msg

    def test_the_live_turn_records_and_reads_its_own_seat(self, client, agent):
        ann = run(agent.name)
        assert record(client, agent, ann).status_code == 200
        body = read(client, agent, ann).json()
        assert [d["decided"] for d in body["decisions"]] == ["Renew the Acme contract"]

    def test_two_people_live_on_the_agent_refuse_each_other(self, client, agent):
        ann, bob = run(agent.name), run(agent.name, email=BOB)
        refused(record(client, agent, ann), 422, "decision_turn_ambiguous")
        refused(read(client, agent, bob), 422, "decision_turn_ambiguous")
        assert decisions(agent) == []
        end(bob)
        assert record(client, agent, ann).status_code == 200, "once alone, the turn records"

    def test_the_same_person_on_two_live_turns_is_one_scope(self, client, agent):
        first = run(agent.name)
        run(agent.name, email="Ann@Example.com")
        assert record(client, agent, first).status_code == 200

    def test_an_unreadable_ledger_refuses(self, client, agent, monkeypatch):
        ann = run(agent.name)
        monkeypatch.setattr(db, "get_running_turn_attribution", lambda name: (_ for _ in ()).throw(RuntimeError("down")))
        refused(record(client, agent, ann), 422, "decision_turn_ambiguous")

    def test_a_run_of_another_agent_is_refused_before_liveness(self, client, agent):
        other = run(f"{agent.name}-other", finished="success")
        resp = record(client, agent, other)
        assert resp.status_code == 403 and "does not belong" in resp.text


class TestDecisionsAreTheAgentsDoor:
    @pytest.mark.parametrize("door", [record, read])
    def test_the_owners_session_is_refused(self, client, agent, door):
        ann = run(agent.name)
        refused(door(client, agent, ann, key=create_access_token({"sub": agent.owner})), 403, "agent_only")

    @pytest.mark.parametrize("door", [record, read])
    def test_a_user_scoped_key_is_refused(self, client, agent, door):
        ann = run(agent.name)
        user_key = db.create_mcp_api_key(agent.owner, McpApiKeyCreate(name="mine")).api_key
        refused(door(client, agent, ann, key=user_key), 403, "agent_only")
        assert decisions(agent) == []

    def test_a_sibling_agents_key_is_still_stopped_by_the_self_gate(self, client, agent):
        sibling = f"{agent.name}-sibling"
        db.register_agent_owner(sibling, agent.owner)
        key = db.create_agent_mcp_api_key(sibling, agent.owner).api_key
        resp = record(client, agent, run(agent.name), key=key)
        assert resp.status_code == 403 and "own agent" in resp.text


# ----------------------------------------------------------------------- voice

class TestVoiceServesTheLiveTurn:
    def test_a_live_turn_speaks_into_its_own_conversation(self, client, agent):
        ann = tg(agent.name, "chat-ann")
        resp = speak(client, agent, ann)
        assert resp.status_code == 200 and resp.json()["delivered"] is True, resp.text
        assert [s["chat_id"] for s in client.sends] == ["chat-ann"]

    @pytest.mark.parametrize("ended", ["success", "failed", "cancelled"])
    def test_a_finished_run_of_another_person_sends_nothing(self, client, agent, ended):
        theirs = tg(agent.name, "chat-bob", email=BOB, finished=ended)
        body = speak(client, agent, theirs).json()
        assert body["delivered"] is False and body["reason"] == "turn_not_live"
        assert "text" in body["guidance"]
        assert client.sends == []

    def test_another_live_conversation_makes_it_ambiguous(self, client, agent):
        ann = tg(agent.name, "chat-ann")
        tg(agent.name, "chat-bob", email=BOB)
        body = speak(client, agent, ann).json()
        assert body["delivered"] is False and body["reason"] == "turn_ambiguous"
        assert client.sends == []

    def test_two_live_turns_in_the_same_conversation_are_not_ambiguous(self, client, agent):
        first = tg(agent.name, "chat-ann")
        tg(agent.name, "chat-ann")
        assert speak(client, agent, first).json()["delivered"] is True

    def test_an_unreadable_ledger_sends_nothing(self, client, agent, monkeypatch):
        ann = tg(agent.name, "chat-ann")
        monkeypatch.setattr(db, "get_running_turn_attribution", lambda name: (_ for _ in ()).throw(RuntimeError("down")))
        assert speak(client, agent, ann).json()["reason"] == "turn_ambiguous"
        assert client.sends == []

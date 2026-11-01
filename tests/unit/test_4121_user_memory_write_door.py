"""The write_user_memory door serves the turn the agent is in (#4121).
Related flow: docs/memory/feature-flows/write-user-memory.md

`POST /api/agents/{name}/user-memory` resolves whose notes it replaces from the
execution the caller names. ent#840 stopped a sibling agent's key from naming a
run; this agent's OWN key, driven by whoever it is shared with, could still name
another person's FINISHED run and replace that person's notes.

The door now holds the ent#926 ledger rule, as `search_memory` does (ent#1028):
the named run must be live, and no other live turn on the agent may resolve to a
different scope. It is also the agent's own door: a human is sent away with
`agent_only`, because a human the agent is shared with reached the same write.

Everything here goes through the real route with the real `get_current_user`,
a real `trinity_mcp_…` key (minted by the function agent creation uses) and real
rows on the unit conftest's SQLite: calling the handler would hand it a principal
the auth stack never built, which is the gap the issue names. The same rule over
the network, on a sibling stack: tests/integration/test_4121_user_memory_write_door.py.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from database import db
from db.engine import get_engine
from db.tables import mcp_api_keys
from db.write_params import ExecutionResult, TaskExecutionFields
from db_models import McpApiKeyCreate, UserCreate
from dependencies import create_access_token
from routers import public_memory

pytestmark = pytest.mark.unit

ANN, BOB = "ann@example.com", "bob@example.com"
NOTE = "Prefers invoices as PDF."


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(public_memory.router)
    return TestClient(app)


@pytest.fixture
def agent():
    suffix = uuid.uuid4().hex[:8]
    owner = f"owner-{suffix}@example.com"
    db.create_user(UserCreate(username=owner, email=owner, role="creator", password="x"))
    name = f"memory-door-{suffix}"
    db.register_agent_owner(name, owner)
    return SimpleNamespace(name=name, owner=owner, key=db.create_agent_mcp_api_key(name, owner).api_key)


def run(agent_name, triggered_by="public", *, email=ANN, client_stamp=None, channel=None, finished=None):
    """A run of ``agent_name``. Live (`running`) unless ``finished`` names the
    terminal status it ended in."""
    row = db.create_task_execution(agent_name, "hello", triggered_by, fields=TaskExecutionFields(
        source_user_email=email, source_channel=channel, source_channel_client=client_stamp))
    if finished:
        db.update_execution_status(row.id, finished, ExecutionResult(response="done"))
    return row.id


def end(execution_id):
    db.update_execution_status(execution_id, "success", ExecutionResult(response="done"))


def write(client, agent, execution_id, *, text=NOTE, key=None):
    return client.post(f"/api/agents/{agent.name}/user-memory",
                       json={"execution_id": execution_id, "memory_text": text},
                       headers={"Authorization": f"Bearer {key or agent.key}"})


def notes(agent, email=ANN):
    return db.get_public_user_memory_agent_notes(agent.name, email)


def history(agent, email=ANN):
    return db.list_public_user_memory_writes(agent.name, email)


def refused(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text


class TestTheTurnIsLive:
    @pytest.mark.parametrize("ended", ["success", "failed", "cancelled"])
    def test_a_finished_run_of_another_person_cannot_be_named(self, client, agent, ended):
        """The reported write: during any turn, the agent's own key names a run
        that ended and replaces that person's notes."""
        theirs = run(agent.name)
        assert write(client, agent, theirs, text="Ann's real notes.").status_code == 200
        db.update_execution_status(theirs, ended, ExecutionResult(response="done"))

        refused(write(client, agent, theirs, text="Planted by someone else's turn."),
                422, "memory_turn_not_live")

        assert notes(agent) == "Ann's real notes."
        assert len(history(agent)) == 1, "a refused write records nothing"

    def test_a_finished_seat_run_cannot_be_named(self, client, agent):
        """The seat is what the issue title names: a scheduled run addressed to
        one person, finished, and its seat's notes rewritten by a later turn."""
        seat_run = db.create_schedule_execution("schedule-1", agent.name, "the brief").id
        assert db.stamp_execution_channel_context(seat_run, source_channel="portal", source_channel_chat_id="session-1",
                                                  source_channel_client=BOB)
        assert write(client, agent, seat_run, text="Bob's real notes.").status_code == 200, "the seat run is live"
        end(seat_run)

        refused(write(client, agent, seat_run, text="Planted."), 422, "memory_turn_not_live")
        assert notes(agent, BOB) == "Bob's real notes."

    def test_liveness_is_judged_before_whose_memory(self, client, agent):
        """A finished group turn says the run ended, not that it was a group:
        the door serves the turn the agent is in, whichever turn was named."""
        group = run(agent.name, "telegram", client_stamp=None, channel="telegram", finished="success")
        refused(write(client, agent, group), 422, "memory_turn_not_live")

    def test_a_refusal_leaves_a_log_line_because_the_note_is_gone(self, client, agent, caplog):
        """The agent is told, the person may not be: the log is how an operator
        finds a lost note. It names the rule and the run, never the text."""
        finished = run(agent.name, finished="success")
        with caplog.at_level("INFO", logger="routers.public_memory"):
            write(client, agent, finished, text="Secret detail.")
        assert "memory_turn_not_live" in caplog.text and finished in caplog.text
        assert "Secret detail" not in caplog.text

    def test_the_live_turn_writes_its_own_person(self, client, agent):
        resp = write(client, agent, run(agent.name))
        assert resp.status_code == 200, resp.text
        assert resp.json()["user_email"] == ANN and notes(agent) == NOTE


class TestNoOtherLiveTurnResolvesElsewhere:
    def test_two_people_live_on_the_agent_is_refused_from_either_side(self, client, agent):
        """Nothing proves which live turn a request comes from, so while two
        resolve to different people neither may write; when one ends, the other
        can."""
        ann, bob = run(agent.name), run(agent.name, email=BOB)
        for named in (ann, bob):
            refused(write(client, agent, named), 422, "memory_turn_ambiguous")
        assert notes(agent) == "" and notes(agent, BOB) == ""
        assert history(agent) == [] and history(agent, BOB) == []

        end(bob)
        assert write(client, agent, ann).status_code == 200
        assert notes(agent) == NOTE and notes(agent, BOB) == ""

    @pytest.mark.parametrize("beside", [
        pytest.param(lambda a: run(a, "schedule", email=None), id="headless"),
        pytest.param(lambda a: run(a, "public", email="anonymous (10.0.0.1)"), id="anonymous-public"),
        pytest.param(lambda a: run(a, "telegram", client_stamp=None, channel="telegram"), id="group"),
        pytest.param(lambda a: run(a, "voice", email=None), id="trigger-with-no-subject"),
    ])
    def test_any_other_live_turn_that_is_not_this_person_is_refused(self, client, agent, beside):
        """A headless run resolves to the agent scope, and a turn that serves no
        one resolves to nothing; neither is this person, so the note is refused
        and the agent is told, which is the known cost of the rule on a shared
        agent."""
        ann = run(agent.name)
        beside(agent.name)
        refused(write(client, agent, ann), 422, "memory_turn_ambiguous")
        assert notes(agent) == ""

    def test_two_seats_briefed_at_the_same_time_refuse_each_other(self, client, agent):
        """The sharpest cost of the rule. ent#637's companion runs one schedule per
        seat, and two seats on one cron are two live scopes (default parallelism
        is 3), so neither brief can write until the other ends. Pinned as the
        known cost: the fix is staggered schedules, stated in the user docs."""
        alice, bob = (db.create_schedule_execution(f"schedule-{i}", agent.name, "the brief").id for i in (1, 2))
        for seat_run, seat in ((alice, ANN), (bob, BOB)):
            assert db.stamp_execution_channel_context(seat_run, source_channel="portal", source_channel_chat_id="s",
                                                      source_channel_client=seat)
        for seat_run in (alice, bob):
            refused(write(client, agent, seat_run), 422, "memory_turn_ambiguous")

        end(bob)
        assert write(client, agent, alice).status_code == 200

    @pytest.mark.parametrize("child", ["self_task", "fan_out"])
    def test_a_persons_own_child_run_refuses_its_parent(self, client, agent, child):
        """A turn that tasks its own agent starts a headless run, which is the agent
        scope, so the parent cannot write until the child ends. The refusal must
        not blame someone else: there is no one else here."""
        ann = run(agent.name)
        kid = run(agent.name, child, email=None)
        resp = write(client, agent, ann)
        refused(resp, 422, "memory_turn_ambiguous")
        assert "someone else" not in resp.text

        end(kid)
        assert write(client, agent, ann).status_code == 200

    def test_the_same_person_on_two_live_turns_is_one_scope(self, client, agent):
        """Case is not a second person: `scope_for` folds it, as `memory_subject` does."""
        first = run(agent.name)
        run(agent.name, email="Ann@Example.com")
        assert write(client, agent, first).status_code == 200

    def test_a_turn_on_another_agent_is_no_neighbour(self, client, agent):
        ann = run(agent.name)
        run(f"{agent.name}-other", email=BOB)
        assert write(client, agent, ann).status_code == 200

    def test_scope_is_not_person_the_owner_shares_the_agent_scope(self, client, agent):
        """The filed rule compares SCOPES, and `scope_for` folds the owner into the
        agent scope a headless run also resolves to. So a headless run beside the
        owner's live turn is NOT ambiguous, though notes are stored per address: a
        headless run can name that turn and write the owner's notes. Pinned so a
        change here is a decision, not a drift (write-user-memory.md, Security
        Considerations)."""
        owners_turn = run(agent.name, email=agent.owner)
        run(agent.name, "schedule", email=None)
        assert write(client, agent, owners_turn).status_code == 200
        assert notes(agent, agent.owner) == NOTE

    def test_an_unreadable_ledger_refuses_the_write(self, client, agent, monkeypatch):
        """An unknown neighbour is a neighbour: the ledger read fails closed."""
        ann = run(agent.name)

        def boom(_name):
            raise RuntimeError("ledger down")
        monkeypatch.setattr(db, "get_running_turn_attribution", boom)
        refused(write(client, agent, ann), 422, "memory_turn_ambiguous")
        assert notes(agent) == ""

    def test_the_ambiguous_refusal_is_logged_too(self, client, agent, caplog):
        ann = run(agent.name)
        run(agent.name, email=BOB)
        with caplog.at_level("INFO", logger="routers.public_memory"):
            write(client, agent, ann)
        assert "memory_turn_ambiguous" in caplog.text and ann in caplog.text

    def test_whose_memory_refusals_still_come_first(self, client, agent):
        """A live group turn beside a headless run gets the reason that holds
        whoever else is running."""
        group = run(agent.name, "telegram", client_stamp=None, channel="telegram")
        run(agent.name, "schedule", email=None)
        refused(write(client, agent, group), 422, "memory_not_one_to_one")


class TestItIsTheAgentsDoor:
    def test_the_owners_own_session_is_refused(self, client, agent):
        """The owner has the Workspace to see and undo a write; nobody writes
        through the run the agent is serving but the agent."""
        ann = run(agent.name)
        jwt = create_access_token({"sub": agent.owner})
        refused(write(client, agent, ann, key=jwt), 403, "agent_only")
        assert notes(agent) == ""

    def test_a_user_scoped_key_is_refused(self, client, agent):
        ann = run(agent.name)
        user_key = db.create_mcp_api_key(agent.owner, McpApiKeyCreate(name="mine")).api_key
        refused(write(client, agent, ann, key=user_key), 403, "agent_only")
        assert notes(agent) == ""

    def test_the_system_key_is_refused(self, client, agent):
        """`trinity-system` carries no `agent_name`: the same answer as search.
        Its key is an agent key re-scoped, as `_set_system_scope` does it."""
        ann = run(agent.name)
        system = db.create_agent_mcp_api_key(f"{agent.name}-system", agent.owner)
        with get_engine().begin() as conn:
            conn.execute(update(mcp_api_keys).where(mcp_api_keys.c.id == system.id).values(scope="system"))
        refused(write(client, agent, ann, key=system.api_key), 403, "agent_only")
        assert notes(agent) == ""

    def test_a_sibling_agents_key_is_still_stopped_by_the_self_gate(self, client, agent):
        ann = run(agent.name)
        sibling = f"{agent.name}-sibling"
        db.register_agent_owner(sibling, agent.owner)
        key = db.create_agent_mcp_api_key(sibling, agent.owner).api_key
        resp = write(client, agent, ann, key=key)
        assert resp.status_code == 403 and "own agent" in resp.text
        assert notes(agent) == ""

    def test_a_run_of_another_agent_is_refused_before_liveness_is_read(self, client, agent):
        """A row that is not this agent's says nothing about whether it ended."""
        other = run(f"{agent.name}-other", finished="success")
        resp = write(client, agent, other)
        assert resp.status_code == 403 and "does not belong" in resp.text

"""
A room post the skill gate held is never put to that agent again (trinity#3402).

A held room wake used to return before the read cursor moved, so every later
wake of the agent re-read the same post: a second approval ask for one request
on each wake (up to the per-requester cap, then refusals), and the agent never
answered anything else in that room. A cold wake re-reads the last 30 messages
whatever the cursor says, so the post also came back as prose.

The fix records, per agent, the post a hold was raised for and its notice line
(`room_gate_holds`), and every wake leaves those rows out of what the agent is
shown and of what the gate scans. The cursor keeps its rule — it moves only on
a reply — so an ordinary post that landed beside the held one is still answered.

Targets, each through its own layer, over the real schema (`db_harness`) and the
real gate (`skill_gate_service.enforce` inside the `execute_task` backstop):
  * ``shared_sessions.service.post_message`` → ``_wake_agent_locked``;
  * ``shared_sessions.service.get_room`` — what an agent reads with MCP ``read_room``;
  * ``shared_sessions.db.record_gate_holds`` / ``gate_held_seqs``.

Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import count as _count, db_backend  # noqa: E402,F401

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent751_gate_entries import FIN, OWNER, OWNER_EMAIL, REQ, world  # noqa: E402,F401

import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from models import User  # noqa: E402
from services.execution_envelope import TaskExecutionResult  # noqa: E402

pytestmark = pytest.mark.unit

HELD = "please /pay-invoice 100 EUR to ACME"
NOTICE_HEAD = "Not run:"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def room_env(world, monkeypatch):
    """The gate world (FIN gates `pay-invoice`; REQ gates nothing), a room with
    both agents, and an agent that answers whatever reaches it. Records what
    each dispatch was shown and what the gate was asked to scan."""
    # Imported HERE, not at module level: the unit conftest pops both modules
    # before every test, and the room imports them lazily — a patch on the
    # collection-time copy would never be seen.
    import dependencies
    import services.task_execution_service as tes
    from shared_sessions import service

    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(tes, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)

    shown: list = []

    async def _answer(self, **kw):
        shown.append(kw)
        return False, TaskExecutionResult(
            execution_id=kw["execution_id"], status="success",
            response=f"{kw['agent_name']} answers", session_id=f"sess-{kw['agent_name']}")

    monkeypatch.setattr(tes.TaskExecutionService, "_admission_gate", _answer)

    scanned: list = []
    real_enforce = _GATE.enforce

    async def _spy(agent_name, **kw):
        scanned.append((agent_name, kw.get("request_text")))
        return await real_enforce(agent_name, **kw)

    monkeypatch.setattr(_GATE, "enforce", _spy)

    person = User(id=world.owner_id, username=OWNER, email=OWNER_EMAIL, role="user")
    room = service.create_room(person, "Ops", [FIN, REQ])
    return {"service": service, "room": room["id"], "person": person,
            "shown": shown, "scanned": scanned, "world": world}


def _post(env, text):
    return _run(env["service"].post_message(env["person"], env["room"], text))


def _shown_to(env, agent):
    return [kw["message"] for kw in env["shown"] if kw["agent_name"] == agent]


def _last_shown(env, agent):
    """The newest transcript `agent` was dispatched with — failing by name when
    the wake never reached it (held or refused again)."""
    seen = _shown_to(env, agent)
    assert seen, f"{agent} was never dispatched: the wake was stopped by the gate again"
    return seen[-1]


def _scanned_for(env, agent):
    return [text for name, text in env["scanned"] if name == agent]


def _messages(env):
    from shared_sessions import db as room_db
    return room_db.get_messages(env["room"])


def _participant(env, agent):
    from shared_sessions import db as room_db
    return room_db.get_participant(env["room"], "agent", agent)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def test_a_later_wake_does_not_raise_a_second_approval_for_the_same_post(room_env):
    _post(room_env, f"@{FIN} {HELD}")
    assert db.count_pending_gate_requests(FIN) == 1
    assert _shown_to(room_env, FIN) == []          # held: nothing reached the agent

    _post(room_env, f"@{FIN} what is overdue this week?")

    assert db.count_pending_gate_requests(FIN) == 1, "the held post was asked about twice"
    transcript = _last_shown(room_env, FIN)
    assert "what is overdue this week?" in transcript
    assert HELD not in transcript
    assert NOTICE_HEAD not in transcript
    assert HELD not in _scanned_for(room_env, FIN)[-1]


def test_a_warm_wake_after_a_hold_gets_the_ordinary_post_that_shared_its_delta(room_env):
    """A post with no mention lands beside the held one in the same delta. The
    hold must not swallow it: the next wake delivers it."""
    _post(room_env, f"@{FIN} hello")                        # a reply → warm session
    assert _participant(room_env, FIN)["cached_session_id"] == f"sess-{FIN}"
    cursor = _participant(room_env, FIN)["last_read_seq"]

    _post(room_env, "the Q3 numbers are in the shared folder")   # no mention, no wake
    _post(room_env, f"@{FIN} {HELD}")                        # held
    assert db.count_pending_gate_requests(FIN) == 1
    assert _participant(room_env, FIN)["last_read_seq"] == cursor   # a hold is not a reply

    _post(room_env, f"@{FIN} anything else?")
    transcript = _last_shown(room_env, FIN)
    assert "the Q3 numbers are in the shared folder" in transcript
    assert "anything else?" in transcript
    assert HELD not in transcript and NOTICE_HEAD not in transcript
    assert db.count_pending_gate_requests(FIN) == 1


def test_a_new_post_naming_the_skill_is_still_held(room_env):
    """The record hides the post it was written for — never every later request."""
    _post(room_env, f"@{FIN} {HELD}")
    _post(room_env, f"@{FIN} /pay-invoice 7 EUR to Initech")
    assert db.count_pending_gate_requests(FIN) == 2
    assert _shown_to(room_env, FIN) == []


def test_the_post_stays_in_the_transcript_of_an_agent_that_does_not_gate_the_skill(room_env):
    _post(room_env, f"@{FIN} @{REQ} {HELD}")
    assert db.count_pending_gate_requests(FIN) == 1
    assert HELD in _last_shown(room_env, REQ)


def test_rejected_and_ungated_later_the_post_still_never_comes_back(room_env):
    """The record is the fact, not the live gate map: un-gating the skill must
    not resurface a request a person already rejected."""
    _post(room_env, f"@{FIN} {HELD}")
    (pending,) = db.list_pending_gate_requests(FIN)
    assert db.transition_gate_request(pending["request_id"], "denied")
    room_env["world"].state["gates"] = {}                  # the skill is no longer gated

    _post(room_env, f"@{FIN} status?")
    transcript = _last_shown(room_env, FIN)
    assert HELD not in transcript and NOTICE_HEAD not in transcript


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_a_refusal_the_same_request_would_get_again_is_recorded(room_env):
    room_env["world"].state["gates"] = {
        "pay-invoice": _GATE.SkillGate(approver="primary"),
        "send-wire": _GATE.SkillGate(approver="approver"),
    }
    _post(room_env, f"@{FIN} /pay-invoice 1 and /send-wire 2")
    assert any("approved by different people" in m["content"] for m in _messages(room_env))
    scans = len(_scanned_for(room_env, FIN))

    _post(room_env, f"@{FIN} ok, just tell me the balance")
    transcript = _last_shown(room_env, FIN)
    assert "/send-wire 2" not in transcript
    assert "approved by different people" not in transcript
    assert "/send-wire" not in _scanned_for(room_env, FIN)[scans]


def test_a_transient_refusal_records_nothing_and_the_post_is_delivered_again(room_env, monkeypatch):
    calls = {"n": 0}
    real = _GATE.list_skill_gates

    def _flaky(agent):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(agent)

    monkeypatch.setattr(_GATE, "list_skill_gates", _flaky)
    _post(room_env, f"@{FIN} {HELD}")                       # refused: the map could not be read
    assert db.count_pending_gate_requests(FIN) == 0
    assert _count("room_gate_holds") == 0

    _post(room_env, f"@{FIN} are you there?")               # the map reads again
    assert db.count_pending_gate_requests(FIN) == 1, "the post was dropped, not re-delivered"


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def test_a_wake_left_with_nothing_to_answer_dispatches_nothing(room_env):
    """Only rows this agent's gate stopped are left: no empty transcript is sent,
    no second ask is raised, and the cursor keeps its rule (it moves on a reply)."""
    service = room_env["service"]
    _post(room_env, f"@{FIN} {HELD}")
    cursor = _participant(room_env, FIN)["last_read_seq"]

    _run(service._wake_agent(room_env["person"], room_env["room"], FIN, 0))

    assert _shown_to(room_env, FIN) == []
    assert db.count_pending_gate_requests(FIN) == 1
    assert _participant(room_env, FIN)["last_read_seq"] == cursor


def test_a_delta_with_none_of_the_agents_holds_is_handled_as_before(room_env):
    """Holds elsewhere in the room change nothing for a delta that holds none of
    them — here a delta of one system line, which a wake dispatched before too."""
    from shared_sessions import db as room_db

    service = room_env["service"]
    _post(room_env, f"@{FIN} {HELD}")                      # a hold, recorded
    _post(room_env, f"@{FIN} hello")                       # answered, so the session is warm
    # Read up to and including its own reply, so the next delta is ONE system line.
    room_db.advance_read_cursor(room_env["room"], FIN, max(m["seq"] for m in _messages(room_env)),
                                None)
    seen = len(_shown_to(room_env, FIN))
    service._post_system(room_env["room"], "scout joined the room")

    _run(service._wake_agent(room_env["person"], room_env["room"], FIN, 0))

    assert len(_shown_to(room_env, FIN)) == seen + 1
    assert "scout joined the room" in _last_shown(room_env, FIN)


def test_read_room_by_the_agent_leaves_out_what_was_held_for_it(room_env):
    service = room_env["service"]
    _post(room_env, f"@{FIN} {HELD}")
    agent = User(id=room_env["world"].owner_id, username=OWNER, email=OWNER_EMAIL, role="user",
                 agent_name=FIN, mcp_scope="agent", mcp_key_id="k-fin")
    other = User(id=room_env["world"].owner_id, username=OWNER, email=OWNER_EMAIL, role="user",
                 agent_name=REQ, mcp_scope="agent", mcp_key_id="k-mkt")

    mine = [m["content"] for m in service.get_room(agent, room_env["room"])["messages"]]
    theirs = [m["content"] for m in service.get_room(other, room_env["room"])["messages"]]
    people = [m["content"] for m in service.get_room(room_env["person"], room_env["room"])["messages"]]

    assert not any(HELD in c or c.startswith(NOTICE_HEAD) for c in mine)
    assert any(HELD in c for c in theirs)
    assert any(HELD in c for c in people) and any(c.startswith(NOTICE_HEAD) for c in people)


def test_a_holds_read_that_fails_never_shows_the_post_ungated(room_env, monkeypatch):
    """Fail open in a wake is safe: everything shown is scanned, so the post is
    held again (one more ask), never put to the agent unchecked."""
    from shared_sessions import db as room_db

    _post(room_env, f"@{FIN} {HELD}")

    def _boom(*a, **k):
        raise RuntimeError("no such table")

    monkeypatch.setattr(room_db, "gate_held_seqs", _boom)
    _post(room_env, f"@{FIN} and the balance?")
    assert _shown_to(room_env, FIN) == []
    assert db.count_pending_gate_requests(FIN) == 2


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_holds_are_per_agent_and_written_once(db_backend):
    from shared_sessions import db as room_db

    now = "2026-10-20T12:00:00.000000Z"
    room_db.record_gate_holds("room-x", "agent-a", [3, 4], "gate-1", now)
    room_db.record_gate_holds("room-x", "agent-a", [4, 5, None], "gate-2", now)   # 4 already held
    room_db.record_gate_holds("room-x", "agent-b", [9], "refused:mixed_approvers", now)
    room_db.record_gate_holds("room-x", "agent-b", [None], "gate-3", now)          # nothing to write

    assert room_db.gate_held_seqs("room-x", "agent-a") == {3, 4, 5}
    assert room_db.gate_held_seqs("room-x", "agent-b") == {9}
    assert room_db.gate_held_seqs("room-y", "agent-a") == set()
    assert _count("room_gate_holds") == 4


def test_deleting_an_agent_deletes_its_holds():
    from db.agent_cleanup import AGENT_REFS, Policy

    refs = {(r.table, r.column): r.policy for r in AGENT_REFS}
    assert refs.get(("room_gate_holds", "agent_name")) == Policy.CASCADE

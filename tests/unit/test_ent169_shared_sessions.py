"""Tests for shared multi-agent sessions / rooms (ent#169).

Focused on the properties where a mistake is a correctness or security bug:

* the ACL fence and its **enumeration-safety** (non-member and non-existent room
  must be indistinguishable);
* **mention-wake** — a plain message wakes nobody, a mention wakes exactly the
  named participants, self-mention wakes nobody;
* **delta injection** — an agent sees only what it hasn't seen, the cursor
  advances only on success, and a cold agent gets a bounded tail;
* **budgets** — enforced at the post boundary, closing the room with a visible
  reason;
* **seq** allocation — gap-free and monotonic, since it is the cursor's
  coordinate;
* idempotent post replay.
"""
from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    """Fresh sqlite: the OSS tables the module reads + the three private ones."""
    db_file = tmp_path / "trinity-rooms.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_ownership, users, schedule_executions
    m.create_all(get_engine(), tables=[agent_ownership, users, schedule_executions])

    from conftest import ensure_schema_tables
    ensure_schema_tables("enterprise_rooms", "enterprise_room_participants", "enterprise_room_messages")

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          email="alice@example.com",
                                          created_at="t", updated_at="t"))
        for name in ("agent-a", "agent-b", "agent-c"):
            conn.execute(insert(agent_ownership).values(
                agent_name=name, owner_id=1, created_at="t"))
    yield str(db_file)


def _user(username="alice", role="admin", agent_name=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name)


@pytest.fixture()
def allow_all(monkeypatch):
    """The room ACL delegates to the OSS agent-access guard; stub it to allow."""
    import dependencies
    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)


def _mk_room(agents=("agent-a", "agent-b"), **kw):
    from shared_sessions import service
    return service.create_room(_user(), kw.pop("name", "Room"), list(agents), **kw)


# --- creation + ACL ----------------------------------------------------------

def test_create_room_seeds_participants_and_a_system_line(rooms_db, allow_all):
    room = _mk_room()
    kinds = {(p["kind"], p["identity"]) for p in room["participants"]}
    assert ("agent", "agent-a") in kinds and ("agent", "agent-b") in kinds
    assert ("user", "alice") in kinds          # the creator joins as a participant
    assert room["messages"][0]["kind"] == "system"
    assert room["status"] == "open"


def test_create_rejects_unknown_agent_uniformly(rooms_db, allow_all):
    """An unknown agent is refused with the SAME status+code as an inaccessible
    one (uniform 403) — a 404/403 split would let any authenticated caller
    enumerate live agent names via POST /api/rooms (Invariant #8)."""
    from shared_sessions import service
    with pytest.raises(service.RoomError) as ei:
        _mk_room(agents=("agent-a", "ghost"))
    assert ei.value.status_code == 403
    assert ei.value.code == "agent_not_accessible"


def test_create_requires_access_to_every_agent(rooms_db, monkeypatch):
    """The N^2 question is asked ONCE, at creation — and one inaccessible agent
    fails the whole room rather than silently dropping that participant."""
    from fastapi import HTTPException
    import dependencies
    from shared_sessions import service

    def gate(_user, agent_name):
        if agent_name == "agent-b":
            raise HTTPException(status_code=403, detail="nope")

    monkeypatch.setattr(dependencies, "assert_agent_access", gate)
    with pytest.raises(service.RoomError) as ei:
        _mk_room(agents=("agent-a", "agent-b"))
    assert ei.value.status_code == 403 and ei.value.code == "agent_not_accessible"


def test_non_member_and_missing_room_are_indistinguishable(rooms_db, allow_all):
    """Enumeration-safety (Invariant #8): a non-member must not be able to tell a
    room they can't see from one that doesn't exist."""
    from shared_sessions import service

    room = _mk_room()
    outsider = _user(agent_name="agent-c")     # a real agent, not in this room

    with pytest.raises(service.RoomError) as miss:
        service.get_room(outsider, room["id"])
    with pytest.raises(service.RoomError) as ghost:
        service.get_room(outsider, "room_doesnotexist")

    assert (miss.value.status_code, miss.value.code) == (404, "room_not_found")
    assert (ghost.value.status_code, ghost.value.code) == (404, "room_not_found")
    assert miss.value.detail == ghost.value.detail


def test_member_agent_sees_only_its_own_rooms(rooms_db, allow_all):
    from shared_sessions import service
    room = _mk_room(agents=("agent-a",))
    assert [r["id"] for r in service.list_rooms(_user(agent_name="agent-a"))["rooms"]] == [room["id"]]
    assert service.list_rooms(_user(agent_name="agent-c"))["rooms"] == []


# --- mention resolution ------------------------------------------------------

def test_mentions_resolve_only_to_room_participants(rooms_db, allow_all):
    from shared_sessions import service
    room = _mk_room(agents=("agent-a", "agent-b"))
    parts = room["participants"]

    assert service.resolve_mentions("hi @agent-a", parts) == ["agent-a"]
    # An @name outside the room stays plain text — a mention can't reach out.
    assert service.resolve_mentions("@agent-c help", parts) == []
    # Deduped, order preserved.
    assert service.resolve_mentions("@agent-b @agent-a @agent-b", parts) == ["agent-b", "agent-a"]
    assert service.resolve_mentions("no mentions here", parts) == []


# --- the turn engine ---------------------------------------------------------

@pytest.fixture()
def fake_execute(monkeypatch):
    """Capture what each woken agent was shown, and control its reply."""
    calls = []
    replies = {}

    async def _execute_task(**kwargs):
        calls.append(kwargs)
        agent = kwargs["agent_name"]
        reply = replies.get(agent, f"{agent} acknowledges")
        if reply is None:                       # simulate a failed turn
            return types.SimpleNamespace(status="failed", response="", cost=None,
                                         error="boom", execution_id="ex_fail",
                                         session_id=None)
        return types.SimpleNamespace(status="success", response=reply, cost=0.01,
                                     error=None, execution_id=f"ex_{agent}",
                                     session_id=f"sess_{agent}")

    svc = types.SimpleNamespace(execute_task=_execute_task)
    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: svc)
    return calls, replies


def test_plain_message_wakes_nobody(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    calls, _ = fake_execute
    room = _mk_room()
    out = _run(service.post_message(_user(), room["id"], "just thinking out loud"))
    assert out["mentions"] == [] and out["woke"] == []
    assert calls == []                          # no execution dispatched at all


def test_mention_wakes_exactly_the_named_agent(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    calls, _ = fake_execute
    room = _mk_room()
    out = _run(service.post_message(_user(), room["id"], "@agent-a thoughts?"))
    assert out["woke"] == ["agent-a"]
    assert [c["agent_name"] for c in calls] == ["agent-a"]
    assert calls[0]["triggered_by"] == "room"   # ordinary execution path


def test_agent_reply_is_posted_back_with_its_execution_link(rooms_db, allow_all, fake_execute):
    from shared_sessions import service, db as rdb
    _calls, replies = fake_execute
    replies["agent-a"] = "my considered answer"
    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a thoughts?"))

    msgs = rdb.get_messages(room["id"])
    reply = [m for m in msgs if m["sender_kind"] == "agent"][-1]
    assert reply["sender_identity"] == "agent-a"
    assert reply["content"] == "my considered answer"
    assert reply["execution_id"] == "ex_agent-a"   # cost/observability linkage


def test_self_mention_does_not_wake_the_sender(rooms_db, allow_all, fake_execute):
    """Cycle-break at the root: an agent mentioning itself must not re-wake."""
    from shared_sessions import service
    calls, _ = fake_execute
    room = _mk_room()
    out = _run(service.post_message(
        _user(agent_name="agent-a"), room["id"], "@agent-a note to self"))
    assert out["mentions"] == ["agent-a"] and out["woke"] == []
    assert calls == []


def test_agent_to_agent_chain(rooms_db, allow_all, fake_execute):
    """An agent's reply that mentions another agent wakes it — the chain."""
    from shared_sessions import service
    calls, replies = fake_execute
    replies["agent-a"] = "@agent-b what do you think?"
    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a start us off"))
    assert [c["agent_name"] for c in calls] == ["agent-a", "agent-b"]


def test_chain_depth_is_bounded_and_says_so(rooms_db, allow_all, fake_execute, monkeypatch):
    """Two agents mentioning each other forever is the obvious runaway; the guard
    stops it AND leaves a visible system line rather than going quiet."""
    from shared_sessions import service, db as rdb
    calls, replies = fake_execute
    monkeypatch.setattr(service, "ROOM_MAX_CHAIN_DEPTH", 3)
    replies["agent-a"] = "@agent-b your turn"
    replies["agent-b"] = "@agent-a no, yours"

    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a begin"))

    assert len(calls) <= 3
    assert any("Mention chain stopped" in m["content"]
               for m in rdb.get_messages(room["id"]) if m["kind"] == "system")


# --- delta injection ---------------------------------------------------------

def test_woken_agent_sees_only_messages_since_its_last_turn(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    calls, _ = fake_execute
    room = _mk_room()

    _run(service.post_message(_user(), room["id"], "@agent-a first"))
    first_prompt = calls[0]["message"]
    assert "first" in first_prompt

    _run(service.post_message(_user(), room["id"], "@agent-a second"))
    second_prompt = calls[-1]["message"]
    assert "second" in second_prompt
    # The whole point: the earlier turn is NOT re-sent.
    assert "first" not in second_prompt


def test_cold_agent_gets_a_bounded_tail_not_the_whole_room(rooms_db, allow_all,
                                                           fake_execute, monkeypatch):
    from shared_sessions import service
    calls, _ = fake_execute
    monkeypatch.setattr(service, "ROOM_COLD_CONTEXT_MESSAGES", 3)
    room = _mk_room()

    for i in range(6):
        _run(service.post_message(_user(), room["id"], f"filler {i}"))
    _run(service.post_message(_user(), room["id"], "@agent-a catch up"))

    prompt = calls[-1]["message"]
    assert "joining the conversation now" in prompt   # cold-start framing
    assert "filler 0" not in prompt                   # bounded, not the whole room


def test_resume_handle_is_reused_on_the_next_turn(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    calls, _ = fake_execute
    room = _mk_room()

    _run(service.post_message(_user(), room["id"], "@agent-a one"))
    assert calls[0]["resume_session_id"] is None       # cold first turn
    _run(service.post_message(_user(), room["id"], "@agent-a two"))
    assert calls[-1]["resume_session_id"] == "sess_agent-a"


def test_failed_turn_is_visible_and_does_not_advance_the_cursor(rooms_db, allow_all,
                                                                fake_execute):
    """A failed turn must re-deliver its delta next time, not silently drop it —
    and the room must say what happened."""
    from shared_sessions import service, db as rdb
    _calls, replies = fake_execute
    replies["agent-a"] = None                          # force a failure
    room = _mk_room()

    _run(service.post_message(_user(), room["id"], "@agent-a please"))

    participant = rdb.get_participant(room["id"], "agent", "agent-a")
    assert participant["last_read_seq"] == 0           # cursor stayed put
    assert any("could not respond" in m["content"]
               for m in rdb.get_messages(room["id"]) if m["kind"] == "system")


# --- budgets -----------------------------------------------------------------

def test_message_budget_closes_the_room_with_a_reason(rooms_db, allow_all, fake_execute):
    """A ``max_messages=N`` room holds N CONVERSATIONAL messages (ent#218).

    This used to post three and expect the third refused, because the
    "Room created" system line consumed one of the three. That encoded the
    defect: the operator asked for 3 and got 2. System lines are the room's own
    bookkeeping and no longer count, so all three land and the FOURTH is
    refused.
    """
    from shared_sessions import service
    room = _mk_room(max_messages=3)
    _run(service.post_message(_user(), room["id"], "one"))
    _run(service.post_message(_user(), room["id"], "two"))
    _run(service.post_message(_user(), room["id"], "three"))   # the budget is 3, so 3 fit

    with pytest.raises(service.RoomError) as ei:
        _run(service.post_message(_user(), room["id"], "four"))
    assert (ei.value.status_code, ei.value.code) == (410, "room_closed")
    assert ei.value.extra.get("stop_reason") == "max_messages"

    from shared_sessions import db as rdb
    assert rdb.get_room(room["id"])["status"] == "closed"
    # the reason is IN the transcript, not only in the error
    assert any("max_messages" in m["content"]
               for m in rdb.get_messages(room["id"]) if m["kind"] == "system")


def test_budget_crossed_by_agent_reply_lands_the_reply_and_returns_200(rooms_db, allow_all, fake_execute):
    """ent#218: when the turn that crosses the message budget is an AGENT reply,
    that reply must still LAND (overshoot-by-one, per _enforce_budgets' contract)
    and the human's original post must return normally — NOT a 410 for a message
    that already landed, and NOT a discarded (already-billed) reply."""
    from shared_sessions import service, db as rdb
    _calls, replies = fake_execute
    replies["agent-a"] = "here is my considered reply"
    # created(system)=1, human=2, agent reply=3 -> the reply is the turn that
    # tips a max_messages=2 room over budget.
    room = _mk_room(agents=("agent-a",), max_messages=2)

    out = _run(service.post_message(_user(), room["id"], "@agent-a thoughts?"))

    # The human's post succeeded (no RoomError propagated up the cascade).
    assert out["woke"] == ["agent-a"]
    msgs = rdb.get_messages(room["id"])
    # The agent's reply IS in the transcript (it was NOT discarded).
    assert any(m["sender_identity"] == "agent-a" and m["content"] == "here is my considered reply"
               for m in msgs)
    # Landing it closed the room with the visible reason.
    assert rdb.get_room(room["id"])["status"] == "closed"
    assert any("max_messages" in m["content"] for m in msgs if m["kind"] == "system")


def test_a_one_message_room_is_not_dead_on_arrival(rooms_db, allow_all, fake_execute):
    """ent#218 defect A, the sharpest form.

    `max_messages` is validated `ge=1`, so 1 is accepted input. The room-created
    system line is seq 1, so under the old total-row count that line alone
    tripped the budget: the first human message was refused 410 and the room
    auto-closed before anyone spoke. Accepted input that yields a permanently
    broken room is the bug, not the small number.
    """
    from shared_sessions import service, db as rdb
    room = _mk_room(max_messages=1)

    out = _run(service.post_message(_user(), room["id"], "hello"))
    assert out["seq"], "the first human message in a max_messages=1 room was refused"

    msgs = rdb.get_messages(room["id"])
    assert any(m["sender_kind"] == "user" and m["content"] == "hello" for m in msgs)


def test_system_lines_do_not_consume_the_operators_budget(rooms_db, allow_all, fake_execute):
    """The room's own bookkeeping is not charged to the conversation budget.

    "Room created", "Room closed", "Mention chain stopped" and "could not
    respond" are all system lines. Counting them meant an N-message room held
    fewer than N real messages, and the closing line is posted BY the budget
    check — so the feature charged its own bookkeeping to the user.
    """
    from shared_sessions import service, db as rdb
    room = _mk_room(max_messages=2)

    _run(service.post_message(_user(), room["id"], "one"))
    _run(service.post_message(_user(), room["id"], "two"))

    msgs = rdb.get_messages(room["id"])
    conversational = [m for m in msgs if m["sender_kind"] != "system"]
    system = [m for m in msgs if m["sender_kind"] == "system"]
    assert len(conversational) == 2, "the operator asked for 2 and must get 2"
    assert system, "there IS a system line present — it simply does not count"

    # And the budget still bites on the next one.
    with pytest.raises(service.RoomError) as ei:
        _run(service.post_message(_user(), room["id"], "three"))
    assert ei.value.extra.get("stop_reason") == "max_messages"


def test_the_budget_counter_counts_every_non_system_kind(rooms_db, allow_all, fake_execute):
    """`!= 'system'`, not an allow-list of ('user','agent').

    ent#171 adds an external A2A sender and ent#362 adds a workspace user; both
    are conversational and must count. An allow-list would silently omit a new
    kind and quietly widen every room's budget — and for a BUDGET the safe
    failure direction is counting too much, never too little.

    Asserted BEHAVIOURALLY, by appending a kind that does not exist yet. The
    first version of this test scanned `inspect.getsource` for "!= 'system'" and
    passed against an allow-list implementation — because the docstring
    explaining the choice contains that exact string. A source-scanning test
    that matches its own prose proves nothing.
    """
    import uuid
    from shared_sessions import db as rdb
    from utils.helpers import utc_now_iso

    room = _mk_room(max_messages=5)
    before = rdb.count_budget_messages(room["id"])

    # A participant kind from the future (ent#171 external / ent#362 workspace user).
    rdb.append_message(uuid.uuid4().hex, room["id"], "external", "partner-bot",
                       "hello from another platform", [], "message", None, utc_now_iso())

    assert rdb.count_budget_messages(room["id"]) == before + 1, (
        "a non-system sender kind did not count toward the budget — an allow-list "
        "silently widens every room's budget as new participant kinds are added"
    )

    # …and a system line still does not.
    rdb.append_message(uuid.uuid4().hex, room["id"], "system", None,
                       "Room created with x.", [], "system", None, utc_now_iso())
    assert rdb.count_budget_messages(room["id"]) == before + 1


def test_agent_reply_never_wakes_further_agents_after_budget_close(rooms_db, allow_all, fake_execute):
    """The overshoot reply closes the room, so a second agent it @mentions is
    NOT woken — the cascade stops at the budget (ent#218)."""
    from shared_sessions import service
    calls, replies = fake_execute
    replies["agent-a"] = "@agent-b your turn"        # would fan out to agent-b
    room = _mk_room(agents=("agent-a", "agent-b"), max_messages=2)

    _run(service.post_message(_user(), room["id"], "@agent-a kick it off"))

    woke = [c["agent_name"] for c in calls]
    assert "agent-a" in woke and "agent-b" not in woke   # cascade stopped at close


def test_expired_room_closes_on_next_post(rooms_db, allow_all, fake_execute):
    from shared_sessions import service, db as rdb
    room = _mk_room()
    rdb.close_room(room["id"], "expired", "2020-01-01T00:00:00Z")
    with pytest.raises(service.RoomError) as ei:
        _run(service.post_message(_user(), room["id"], "anyone?"))
    assert ei.value.status_code == 410


def test_closed_room_is_still_readable(rooms_db, allow_all, fake_execute):
    """Closing ends participation, not access — the transcript is the record."""
    from shared_sessions import service
    room = _mk_room()
    service.close_room(_user(), room["id"])
    assert service.get_room(_user(), room["id"])["status"] == "closed"


def test_close_is_idempotent(rooms_db, allow_all):
    from shared_sessions import service
    room = _mk_room()
    assert service.close_room(_user(), room["id"])["already_closed"] is False
    assert service.close_room(_user(), room["id"])["already_closed"] is True


def test_room_cost_sums_the_linked_executions(rooms_db, allow_all, fake_execute):
    """Cost is computed from the executions, never a maintained counter that can
    drift from them."""
    from shared_sessions import service, db as rdb
    from db.engine import get_engine
    from db.tables import schedule_executions
    from sqlalchemy import insert

    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "@agent-a hello"))

    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id="ex_agent-a", schedule_id="__manual__", agent_name="agent-a",
            status="success", started_at="t", message="m", triggered_by="room",
            cost=0.25))
    assert rdb.room_cost(room["id"]) == pytest.approx(0.25)


# --- storage invariants ------------------------------------------------------

def test_seq_is_monotonic_and_gap_free(rooms_db, allow_all, fake_execute):
    """`seq` is the ordering primitive AND the delta cursor's coordinate, so a
    gap or a duplicate is a correctness bug."""
    from shared_sessions import service, db as rdb
    room = _mk_room()
    for i in range(5):
        _run(service.post_message(_user(), room["id"], f"m{i}"))
    seqs = [m["seq"] for m in rdb.get_messages(room["id"])]
    assert seqs == list(range(1, len(seqs) + 1))


def test_since_paging_returns_only_newer_messages(rooms_db, allow_all, fake_execute):
    from shared_sessions import service, db as rdb
    room = _mk_room()
    for i in range(4):
        _run(service.post_message(_user(), room["id"], f"m{i}"))
    tail = rdb.get_messages(room["id"], since_seq=3)
    assert tail and all(m["seq"] > 3 for m in tail)


def test_participant_removal_is_soft(rooms_db, allow_all):
    """The transcript keeps a departed participant's messages, so their row must
    survive for the sender label to resolve."""
    from shared_sessions import service, db as rdb
    room = _mk_room()
    service.remove_participant(_user(), room["id"], "agent-a")
    p = rdb.get_participant(room["id"], "agent", "agent-a")
    assert p is not None and p["left_at"] is not None


def test_departed_agent_is_not_woken(rooms_db, allow_all, fake_execute):
    from shared_sessions import service
    calls, _ = fake_execute
    room = _mk_room()
    service.remove_participant(_user(), room["id"], "agent-a")
    out = _run(service.post_message(_user(), room["id"], "@agent-a still there?"))
    assert out["woke"] == [] and calls == []


def test_room_lifecycle_mutations_require_a_moderator(rooms_db, allow_all):
    """ent#220 #4: a member AGENT (reachable via its own key, a prompt-injection
    surface) must not close a room or rewrite its roster. Only the human
    moderator (the creator) or an admin may. A member gets a 403, not a 404 —
    it can already see the room, so 403 leaks nothing (Invariant #8)."""
    from shared_sessions import service
    room = _mk_room(agents=("agent-a", "agent-b"))
    agent_member = _user(agent_name="agent-a")     # a room member, role='member'

    for op in (
        lambda: service.close_room(agent_member, room["id"]),
        lambda: service.add_participant(agent_member, room["id"], "agent-c"),
        lambda: service.remove_participant(agent_member, room["id"], "agent-b"),
    ):
        with pytest.raises(service.RoomError) as ei:
            op()
        assert (ei.value.status_code, ei.value.code) == (403, "not_moderator")

    # The moderator (creator) still can.
    assert service.add_participant(_user(), room["id"], "agent-c")["added"] is True
    assert service.close_room(_user(), room["id"])["closed"] is True


def test_participant_cap_is_rechecked_on_add(rooms_db, allow_all, monkeypatch):
    """ent#220 #3: MAX_PARTICIPANTS was enforced only at creation, so repeated
    add_participant grew a room without bound. It is re-checked on add now."""
    from shared_sessions import service
    monkeypatch.setattr(service, "MAX_PARTICIPANTS", 1)
    room = _mk_room(agents=("agent-a",))           # 1 agent == the (lowered) cap

    with pytest.raises(service.RoomError) as ei:
        service.add_participant(_user(), room["id"], "agent-b")
    assert (ei.value.status_code, ei.value.code) == (422, "participant_cap")

    # A no-op re-add of an already-active agent is exempt (doesn't grow the room).
    assert service.add_participant(_user(), room["id"], "agent-a")["added"] is True


# --- ent#220: the review's untested surfaces ---------------------------------

def test_cost_budget_exhaustion_closes_the_room(rooms_db, allow_all, fake_execute):
    """`max_cost_usd` was enforced but never tested (ent#220 item 5).

    The message budget had coverage and this did not, so the path that bounds
    actual SPEND was one typo away from never firing.

    `room_cost` is a SUM over the linked `schedule_executions` (deliberately not
    a counter on the room row), so the test has to create a real execution row
    and link a message to it — which is also what proves the join works.
    """
    import uuid
    from sqlalchemy import text
    from utils.helpers import utc_now_iso
    from shared_sessions import service, db as rdb

    room = _mk_room(max_messages=50, max_cost_usd=0.01)

    exec_id = uuid.uuid4().hex
    now = utc_now_iso()
    with rdb.get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, triggered_by, cost) "
            "VALUES (:i, '__manual__', 'agent-a', 'success', :n, 'turn', 'room', :c)"
        ), {"i": exec_id, "n": now, "c": 0.05})

    rdb.append_message(uuid.uuid4().hex, room["id"], "agent", "agent-a",
                       "an expensive answer", [], "message", exec_id, now)

    assert rdb.room_cost(room["id"]) >= 0.05, "the cost join did not see the execution"

    with pytest.raises(service.RoomError) as ei:
        _run(service.post_message(_user(), room["id"], "another"))
    assert ei.value.extra.get("stop_reason") == "max_cost"
    assert rdb.get_room(room["id"])["status"] == "closed"
    assert any("max_cost" in m["content"]
               for m in rdb.get_messages(room["id"]) if m["kind"] == "system")


def test_sweep_expired_rooms_closes_and_is_idempotent(rooms_db, allow_all, fake_execute):
    """`sweep_expired_rooms` had NO test despite being the TTL reaper (ent#220
    item 5). Idempotency matters: it runs on a loop, and a second pass must not
    post a second "closed" line into every expired room."""
    from shared_sessions import service, db as rdb

    room = _mk_room()
    # Expire it in the past.
    with rdb.get_engine().begin() as conn:
        from sqlalchemy import text
        conn.execute(text("UPDATE enterprise_rooms SET expires_at = :t WHERE id = :i"),
                     {"t": "2020-01-01T00:00:00Z", "i": room["id"]})

    assert service.sweep_expired_rooms() >= 1
    assert rdb.get_room(room["id"])["status"] == "closed"
    closed_lines = [m for m in rdb.get_messages(room["id"])
                    if m["kind"] == "system" and "expired" in m["content"]]
    assert len(closed_lines) == 1

    # Second pass: nothing more to close, and no duplicate system line.
    assert service.sweep_expired_rooms() == 0
    closed_lines = [m for m in rdb.get_messages(room["id"])
                    if m["kind"] == "system" and "expired" in m["content"]]
    assert len(closed_lines) == 1, "the reaper posted a second closed line on re-run"


def test_the_sender_cannot_be_set_from_the_request_body(rooms_db, allow_all, fake_execute):
    """The acting principal comes from the auth context, never the body.

    `RoomMessageCreate` documents this ("`sender` is deliberately absent"), and
    a docstring is not a control — a model that later grew a `sender` field would
    silently make impersonation posting possible. Pinned as a test.
    """
    from shared_sessions.models import RoomMessageCreate

    fields = set(RoomMessageCreate.model_fields)
    assert fields == {"content"}, (
        f"RoomMessageCreate grew {fields - {'content'}} — anything identity-shaped "
        "here lets a caller post as someone else; the sender must come from auth"
    )

    # And the model REJECTS an attempt rather than silently ignoring it, so a
    # spoofing client gets an error instead of a message attributed to them.
    body = RoomMessageCreate(content="hi", sender="agent:victim")
    assert not hasattr(body, "sender")


def test_the_reply_is_posted_before_the_cursor_advances(rooms_db, allow_all, fake_execute):
    """ent#220 item 1: ordering is what makes a lost reply impossible.

    Advancing first means a failure in between marks the delta as seen while the
    reply exists nowhere — billed, gone, and never re-delivered. Posting first
    inverts that into a repeated read, which costs nothing but a duplicate delta.
    """
    import ast, inspect
    from shared_sessions import service

    src = inspect.getsource(service._wake_agent)
    tree = ast.parse(src.strip())
    order = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "advance_read_cursor":
                order.append(("advance", node.lineno))
            elif isinstance(f, ast.Name) and f.id == "post_message":
                order.append(("post", node.lineno))
    order.sort(key=lambda x: x[1])
    names = [n for n, _ in order]
    assert names and names[0] == "post", (
        f"_wake_agent does {names} — the reply must be posted BEFORE the cursor "
        "advances, or a failure between them loses a paid reply"
    )

"""ent#220 residuals: chain shielding (item 2) + the perf/atomicity set (item 6).

Item 2 is the one with teeth. The mention chain runs inside the HTTP POST, so a
client disconnect cancels the request task and `CancelledError` — a
BaseException since 3.8 — sails past every `except Exception` in the module.
Unshielded that killed the chain wherever it happened to be: a turn already
dispatched and BILLED lost its reply, the remaining targets were never woken,
and the transcript simply stopped, which in a shared room reads as the agent
ignoring you.

Item 6 is four small ones, each of which is invisible until it isn't: a
membership check whose query COUNT leaked room existence through timing, a
sidebar list that cost 2N round-trips, and a room creation that could commit a
room without its own creator in it.

What is deliberately NOT here: `_enforce_budgets` remains boundary-only, so two
concurrent posts can each pass the count check and overshoot by one message.
That is the documented loop-budget semantics (#1155/#1156) — an in-flight turn is
never killed — and making the count atomic with the append would serialize every
post in a room to buy a bound the design already accepts.
"""
from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-rooms-220.db"
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
    import dependencies
    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)


def _mk_room(agents=("agent-a", "agent-b"), **kw):
    from shared_sessions import service
    return service.create_room(_user(), kw.pop("name", "Room"), list(agents), **kw)


def _system_lines(room_id):
    from shared_sessions import db
    return [m["content"] for m in db.get_messages(room_id) if m["kind"] == "system"]


# ===========================================================================
# Item 2 — the chain survives a client disconnect
# ===========================================================================

def test_a_disconnect_does_not_kill_a_turn_that_is_already_running(rooms_db, allow_all, monkeypatch):
    """The billed half. A wake already dispatched must run to completion and post
    its reply even though the poster's request task was cancelled."""
    from shared_sessions import service

    room = _mk_room()
    room_id = room["id"]
    finished = []

    async def slow_wake(current_user, rid, agent_name, depth):
        # Long enough that the cancellation below lands mid-turn.
        await asyncio.sleep(0.05)
        finished.append(agent_name)
        service._post_system(rid, f"{agent_name} replied")

    monkeypatch.setattr(service, "_wake_agent", slow_wake)

    async def scenario():
        task = asyncio.ensure_future(
            service.post_message(_user(), room_id, "@agent-a please look")
        )
        await asyncio.sleep(0)          # let the chain reach the wake
        await asyncio.sleep(0.01)
        task.cancel()                   # the browser goes away
        with pytest.raises(asyncio.CancelledError):
            await task
        # The shielded wake is still on the loop — give it its remaining time.
        await asyncio.sleep(0.1)

    _run(scenario())

    assert finished == ["agent-a"], "a shielded, already-billed turn was killed by a disconnect"
    assert any("agent-a replied" in line for line in _system_lines(room_id))


def test_a_disconnect_says_so_instead_of_stopping_silently(rooms_db, allow_all, monkeypatch):
    """The legibility half. Remaining targets are not woken — that is correct,
    nobody is waiting — but the room must say why it stopped rather than just
    ending."""
    from shared_sessions import service

    room = _mk_room(agents=("agent-a", "agent-b"))
    room_id = room["id"]
    woke = []

    async def wake(current_user, rid, agent_name, depth):
        woke.append(agent_name)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(service, "_wake_agent", wake)

    async def scenario():
        task = asyncio.ensure_future(
            service.post_message(_user(), room_id, "@agent-a @agent-b look")
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)

    _run(scenario())

    assert woke == ["agent-a"], "the chain kept starting NEW wakes after a disconnect"
    assert any("disconnected" in line and "agent-b" in line for line in _system_lines(room_id)), \
        "the room ended with no line explaining why agent-b never spoke"


def test_the_human_message_is_durable_even_if_the_chain_is_cancelled(rooms_db, allow_all, monkeypatch):
    """The post is committed before any wake, so a disconnect can never lose the
    message that started the exchange."""
    from shared_sessions import service

    room = _mk_room()
    room_id = room["id"]

    async def wake(current_user, rid, agent_name, depth):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(service, "_wake_agent", wake)

    async def scenario():
        task = asyncio.ensure_future(
            service.post_message(_user(), room_id, "@agent-a durable?")
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.06)

    _run(scenario())

    from shared_sessions import db
    bodies = [m["content"] for m in db.get_messages(room_id)]
    assert any("durable?" in b for b in bodies)


def test_a_cancelled_turn_leaves_a_line_and_re_raises(rooms_db, allow_all, monkeypatch):
    """`_wake_agent`'s own handler: cancellation used to slip past `except
    Exception` and leave NO trace — the one failure mode invisible to everyone
    watching. It must post a line and still propagate (swallowing cancellation
    is how a process refuses to die)."""
    from shared_sessions import service

    room = _mk_room()
    room_id = room["id"]

    class _Svc:
        async def execute_task(self, **kw):
            raise asyncio.CancelledError()

    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: _Svc())

    with pytest.raises(asyncio.CancelledError):
        _run(service._wake_agent(_user(), room_id, "agent-a", 1))

    assert any("interrupted" in line for line in _system_lines(room_id))


def test_the_working_marker_is_cleared_when_a_turn_is_cancelled(rooms_db, allow_all, monkeypatch):
    """A cancelled turn must not leave the agent looking busy forever."""
    from shared_sessions import service

    room = _mk_room()
    room_id = room["id"]

    class _Svc:
        async def execute_task(self, **kw):
            raise asyncio.CancelledError()

    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: _Svc())

    cleared = []
    real_clear = service._clear_agent_working
    monkeypatch.setattr(service, "_clear_agent_working",
                        lambda rid, name: (cleared.append(name), real_clear(rid, name))[1])

    with pytest.raises(asyncio.CancelledError):
        _run(service._wake_agent(_user(), room_id, "agent-a", 1))
    assert cleared == ["agent-a"]


# ===========================================================================
# Item 6 — the residual set
# ===========================================================================

def test_membership_costs_the_same_queries_whether_or_not_the_room_exists(rooms_db, allow_all, monkeypatch):
    """The timing differential. A missing room used to take ONE query and a
    room-you-are-not-in TWO, so response time handed back the existence bit the
    uniform 404 withholds."""
    from shared_sessions import service, db as rooms

    room = _mk_room()
    outsider = _user(agent_name="agent-c")

    calls = []
    for name in ("get_room", "get_participant"):
        real = getattr(rooms, name)
        monkeypatch.setattr(rooms, name,
                            lambda *a, _r=real, _n=name, **k: (calls.append(_n), _r(*a, **k))[1])

    with pytest.raises(service.RoomError):
        service._require_membership("room_doesnotexist", outsider)
    missing = list(calls)

    calls.clear()
    with pytest.raises(service.RoomError):
        service._require_membership(room["id"], outsider)
    non_member = list(calls)

    assert sorted(missing) == sorted(non_member), (
        f"query counts differ: missing={missing} non_member={non_member}"
    )


def test_an_admin_still_short_circuits_membership(rooms_db, allow_all):
    """The constant-cost rewrite must not accidentally require admins to be
    participants."""
    from shared_sessions import service

    room = _mk_room()
    admin = _user(username="root", role="admin")     # not a participant
    assert service._require_membership(room["id"], admin)["id"] == room["id"]


def test_listing_rooms_does_not_scale_its_queries_with_the_page(rooms_db, allow_all, monkeypatch):
    """N+1: the sidebar lists every room the caller is in, and the per-room reads
    made that 2N round-trips on the call that decides how fast it paints."""
    from shared_sessions import service, db as rooms

    for i in range(4):
        _mk_room(name=f"Room {i}")

    calls = {"participants": 0, "counts": 0}
    real_p, real_c = rooms.list_participants_for_rooms, rooms.count_messages_for_rooms
    monkeypatch.setattr(rooms, "list_participants_for_rooms",
                        lambda ids: (calls.__setitem__("participants", calls["participants"] + 1),
                                     real_p(ids))[1])
    monkeypatch.setattr(rooms, "count_messages_for_rooms",
                        lambda ids: (calls.__setitem__("counts", calls["counts"] + 1),
                                     real_c(ids))[1])
    monkeypatch.setattr(rooms, "list_participants",
                        lambda rid: pytest.fail("per-room read reintroduced (N+1)"))
    monkeypatch.setattr(rooms, "count_messages",
                        lambda rid: pytest.fail("per-room read reintroduced (N+1)"))

    out = service.list_rooms(_user())
    assert len(out["rooms"]) == 4
    assert calls == {"participants": 1, "counts": 1}


def test_the_batched_readers_agree_with_the_per_room_ones(rooms_db, allow_all):
    """Same answers, fewer queries — the only thing that makes the swap safe."""
    from shared_sessions import db as rooms

    a = _mk_room(name="A", agents=("agent-a",))["id"]
    b = _mk_room(name="B", agents=("agent-a", "agent-b"))["id"]

    batched = rooms.list_participants_for_rooms([a, b])
    assert [dict(p) for p in batched[a]] == [dict(p) for p in rooms.list_participants(a)]
    assert [dict(p) for p in batched[b]] == [dict(p) for p in rooms.list_participants(b)]

    counts = rooms.count_messages_for_rooms([a, b])
    assert counts[a] == rooms.count_messages(a)
    assert counts[b] == rooms.count_messages(b)


def test_the_batched_readers_tolerate_an_empty_page(rooms_db):
    """`IN ()` is a syntax error on both backends, not an empty result."""
    from shared_sessions import db as rooms
    assert rooms.list_participants_for_rooms([]) == {}
    assert rooms.count_messages_for_rooms([]) == {}


def test_a_room_with_no_messages_counts_zero_rather_than_vanishing(rooms_db, allow_all):
    """A GROUP BY returns no row for an empty room; the caller must default it
    to 0 instead of dropping the room from its own list."""
    from shared_sessions import db as rooms

    rid = _mk_room(name="Quiet")["id"]
    with __import__("db.engine", fromlist=["get_engine"]).get_engine().begin() as conn:
        from sqlalchemy import text
        conn.execute(text("DELETE FROM enterprise_room_messages WHERE room_id = :r"), {"r": rid})

    out = __import__("shared_sessions.service",
                     fromlist=["list_rooms"]).list_rooms(_user())
    row = next(r for r in out["rooms"] if r["id"] == rid)
    assert row["message_count"] == 0


def test_room_creation_is_one_transaction(rooms_db, allow_all, monkeypatch):
    """A failure partway used to commit a room whose creator was not in it —
    and `_require_membership` then 404s them out of the room they just made.
    Nothing repairs that, because a half-created room looks legitimate."""
    from shared_sessions import service, db as rooms
    from db.engine import get_engine
    from sqlalchemy import text

    real = rooms.create_room_with_participants

    def boom(*a, **k):
        real(*a, **k)                      # do the whole transaction...
        raise RuntimeError("post-commit failure")

    monkeypatch.setattr(rooms, "create_room_with_participants", boom)
    with pytest.raises(RuntimeError):
        _mk_room(name="Doomed")

    # ...then assert the transaction that DID run is internally complete: every
    # committed room has its creator seated and an opening line.
    with get_engine().connect() as conn:
        for (rid,) in conn.execute(text("SELECT id FROM enterprise_rooms")).all():
            members = {(p["kind"], p["identity"]) for p in rooms.list_participants(rid)}
            assert ("user", "alice") in members, f"room {rid} committed without its creator"
            assert rooms.count_messages(rid) >= 1, f"room {rid} committed with no opening line"


def test_a_created_room_is_complete_and_ordered(rooms_db, allow_all):
    """The happy path through the new transactional writer: same shape the
    three-commit version produced."""
    room = _mk_room(agents=("agent-a", "agent-b"))
    kinds = {(p["kind"], p["identity"]) for p in room["participants"]}
    assert ("user", "alice") in kinds
    assert ("agent", "agent-a") in kinds and ("agent", "agent-b") in kinds
    assert room["messages"][0]["kind"] == "system"
    assert room["messages"][0]["seq"] == 1


def test_the_scribe_role_survives_the_transactional_writer(rooms_db, allow_all):
    room = _mk_room(agents=("agent-a", "agent-b"), scribe="agent-b")
    roles = {p["identity"]: p["role"] for p in room["participants"]}
    assert roles["agent-b"] == "scribe"
    assert roles["agent-a"] == "member"
    assert roles["alice"] == "moderator"

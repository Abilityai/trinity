"""abilityai/trinity-enterprise#863 — an event-stream key's agent list follows
access changes.

What is pinned here:

  * every non-admin ``/ws/events`` socket gets a roster read that re-reads the
    key's OWNER each time (``event_stream_roster_reader``), and the endpoint
    wires it for user keys as well as agent keys — executed through the real
    ``main.py`` handler, not a shape check;
  * the ``refresh`` command reads the owner's current row too;
  * the read applies the connect-time owner checks: an owner that is gone,
    deactivated or Workspace-only reads as an empty list, and the owner's
    current email is the one read;
  * a change that lands while a roster read is in flight is followed by one
    more read, for a ``/ws`` slot and a ``/ws/events`` slot;
  * every non-admin roster is re-read about once a minute by a per-worker loop
    the dispatcher owns: it runs its reads one at a time, reads nothing when no
    roster is held, survives a failing read, and stops with the dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
while str(_BACKEND) in sys.path:
    sys.path.remove(str(_BACKEND))
sys.path.insert(0, str(_BACKEND))
_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

from services.event_bus import SCOPE_ALL, SCOPE_SCOPED, StreamDispatcher, _ClientSlot  # noqa: E402

pytestmark = pytest.mark.unit

OWNER, OTHER = "roster-owner", "roster-other"
OWNER_EMAIL, OTHER_EMAIL = "roster-owner@example.test", "roster-other@example.test"
OWN, SHARED, FOREIGN = "r-own-agent", "r-shared-agent", "r-foreign-agent"


def _run(sql: str, **binds) -> None:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text(sql), binds)


@pytest.fixture
def people(db_backend):  # noqa: F811 — the db_harness fixture, by name
    """OWNER owns OWN; OTHER owns SHARED (shared with OWNER) and FOREIGN."""
    from database import db

    seed_user(1, OWNER, role="creator")
    seed_user(2, OTHER, role="creator")
    _run("UPDATE users SET email = :e WHERE id = 1", e=OWNER_EMAIL)
    _run("UPDATE users SET email = :e WHERE id = 2", e=OTHER_EMAIL)
    seed_agent(OWN, owner_id=1)
    seed_agent(SHARED, owner_id=2)
    seed_agent(FOREIGN, owner_id=2)
    assert db.share_agent(SHARED, OTHER, OWNER_EMAIL) is not None
    return db


def _user_key(**over):
    key = {
        "scope": "user",
        "agent_name": None,
        "user_email": OWNER_EMAIL,
        "user_id": OWNER,
        "id": "key-roster",
    }
    key.update(over)
    return key


def _fields(payload: dict, scope: str, agent_name: str = "") -> dict:
    return {"payload": json.dumps(payload), "scope": scope, "agent_name": agent_name}


async def _drain(dispatcher: StreamDispatcher) -> None:
    """Await every roster read, including the ones a read schedules after it."""
    for _ in range(50):
        if not dispatcher._refresh_tasks:
            return
        await asyncio.gather(*list(dispatcher._refresh_tasks))
    raise AssertionError("roster reads never settled")


# ---------------------------------------------------------------------------
# The endpoint: every non-admin key gets the owner re-read
# ---------------------------------------------------------------------------


def _open_event_stream(monkeypatch, key, after_connect):
    """Drive main.py's real /ws/events handler with ``key``. Returns what the
    handler handed the connection manager, and the ``refresh`` reply after
    ``after_connect()`` has changed the database."""
    import main
    from fastapi.testclient import TestClient
    from database import db

    monkeypatch.setattr(db, "validate_mcp_api_key", lambda token: dict(key))
    seen = {}

    async def connect(
        ws, email, is_admin, roster, last_event_id=None, roster_resolver=None
    ):
        seen.update(is_admin=is_admin, roster=list(roster), resolver=roster_resolver)

    monkeypatch.setattr(main.filtered_manager, "connect", connect)
    monkeypatch.setattr(main.filtered_manager, "disconnect", lambda ws: None)
    monkeypatch.setattr(
        main.filtered_manager,
        "update_accessible_agents",
        lambda ws, roster: seen.update(updated=list(roster)),
    )
    with TestClient(main.app).websocket_connect(
        "/ws/events?token=trinity_mcp_roster"
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected"
        seen["connected"] = set(hello["accessible_agents"])
        after_connect()
        ws.send_text("refresh")
        reply = ws.receive_json()
        assert reply["type"] == "refreshed"
        seen["refreshed"] = set(reply["accessible_agents"])
    return seen


def test_an_unshared_agent_leaves_a_user_keys_live_event_stream(people, monkeypatch):
    """The endpoint hands a user key's slot the owner re-read, and after an
    unshare fan-out that slot receives none of the unshared agent's events."""
    seen = _open_event_stream(
        monkeypatch,
        _user_key(),
        lambda: people.unshare_agent(SHARED, OTHER, OWNER_EMAIL),
    )

    assert seen["is_admin"] is False and set(seen["roster"]) == {OWN, SHARED}
    resolver = seen["resolver"]
    assert resolver is not None, "a non-admin user key's slot must get a roster read"
    assert seen["refreshed"] == {OWN} == set(seen["updated"])

    async def go():
        d = StreamDispatcher()
        slot = _ClientSlot(
            ws=None,
            scope=SCOPE_SCOPED,
            send_func=None,
            accessible_agents=set(seen["roster"]),
            roster_resolver=resolver,
        )
        d._clients["k"] = slot
        await d._fanout(
            "1-1",
            _fields({"event": "agent_unshared", "data": {"name": SHARED}}, SCOPE_ALL),
        )
        await _drain(d)
        await d._fanout(
            "1-2",
            _fields(
                {"type": "agent_activity", "agent_name": SHARED}, SCOPE_SCOPED, SHARED
            ),
        )
        await d._fanout(
            "1-3",
            _fields({"type": "agent_activity", "agent_name": OWN}, SCOPE_SCOPED, OWN),
        )
        delivered = [
            slot.queue.get_nowait()[1]["agent_name"] for _ in range(slot.queue.qsize())
        ]
        assert delivered == [OWN]

    asyncio.run(go())


def test_an_admin_keys_slot_is_never_re_read(people, monkeypatch):
    """Admins are never filtered, so their slot gets no roster read at all."""
    people.update_user_role(OWNER, "admin")
    seen = _open_event_stream(monkeypatch, _user_key(), lambda: None)
    assert seen["is_admin"] is True
    assert seen["resolver"] is None


def test_refresh_reads_the_owners_current_role(people, monkeypatch):
    """An owner who opened the stream as an admin and is then lowered gets the
    lowered list from ``refresh``, not the list of the row read at connect."""
    people.update_user_role(OWNER, "admin")
    seen = _open_event_stream(
        monkeypatch, _user_key(), lambda: people.update_user_role(OWNER, "creator")
    )
    assert seen["connected"] == {OWN, SHARED, FOREIGN}
    assert seen["refreshed"] == {OWN, SHARED}


def test_refresh_reads_an_owner_deactivated_since_connect_as_no_agents(
    people, monkeypatch
):
    seen = _open_event_stream(
        monkeypatch,
        _user_key(),
        lambda: _run(
            "UPDATE users SET suspended_at = :t WHERE id = 1", t="2026-11-08T10:00:00Z"
        ),
    )
    assert seen["connected"] == {OWN, SHARED}
    assert seen["refreshed"] == set()


# ---------------------------------------------------------------------------
# The read itself
# ---------------------------------------------------------------------------


def test_the_read_re_reads_the_owner_on_every_call(people):
    from services.ws_identity_service import event_stream_roster_reader

    read = event_stream_roster_reader(_user_key())
    assert set(read()) == {OWN, SHARED}
    people.unshare_agent(SHARED, OTHER, OWNER_EMAIL)
    assert set(read()) == {OWN}
    people.share_agent(FOREIGN, OTHER, OWNER_EMAIL)
    assert set(read()) == {OWN, FOREIGN}


@pytest.mark.parametrize(
    "change", ["suspended", "suspended_admin", "gone", "workspace_only"]
)
def test_an_owner_who_may_not_open_the_stream_reads_as_no_agents(people, change):
    from services.ws_identity_service import event_stream_roster_reader

    read = event_stream_roster_reader(_user_key())
    assert set(read()) == {OWN, SHARED}  # the control: the same read, before the change
    if change == "suspended":
        _run(
            "UPDATE users SET suspended_at = :t WHERE id = 1", t="2026-11-08T10:00:00Z"
        )
    elif change == "suspended_admin":
        people.update_user_role(OWNER, "admin")
        _run(
            "UPDATE users SET suspended_at = :t WHERE id = 1", t="2026-11-08T10:00:00Z"
        )
    elif change == "gone":
        _run("UPDATE users SET username = 'roster-renamed-away' WHERE id = 1")
    else:
        people.update_user_role(OWNER, "user")
    assert read() == []


@pytest.mark.parametrize("cleared", [None, ""])
def test_a_reactivated_owner_reads_their_agents_again(people, cleared):
    from services.ws_identity_service import event_stream_roster_reader

    read = event_stream_roster_reader(_user_key())
    _run("UPDATE users SET suspended_at = :t WHERE id = 1", t="2026-11-08T10:00:00Z")
    assert read() == []
    _run("UPDATE users SET suspended_at = :t WHERE id = 1", t=cleared)
    assert set(read()) == {OWN, SHARED}


def test_the_read_follows_the_owners_current_email(people):
    """The read uses the account's address now, not the one the key resolved
    at connect."""
    from services.ws_identity_service import event_stream_roster_reader

    read = event_stream_roster_reader(_user_key())
    _run("UPDATE users SET email = :e WHERE id = 1", e="roster-owner-new@example.test")
    seed_user(3, "roster-taker", role="creator")
    _run("UPDATE users SET email = :e WHERE id = 3", e=OWNER_EMAIL)
    seed_agent("r-taker-agent", owner_id=3)
    assert set(read()) == {OWN}


def test_an_owner_with_no_email_reads_as_no_agents(people):
    from services.ws_identity_service import (
        event_stream_identity,
        event_stream_roster_reader,
    )

    _run("UPDATE users SET email = NULL WHERE id = 1")
    assert event_stream_roster_reader(_user_key())() == []
    assert event_stream_identity(_user_key(user_email=""), {"role": "creator"}) == (
        False,
        [],
    )


def test_an_agent_keys_read_is_its_reach_until_its_owner_is_deactivated(
    people, monkeypatch
):
    import dependencies
    from services.ws_identity_service import event_stream_roster_reader

    monkeypatch.setattr(dependencies.db, "get_agent_mcp_api_key", lambda agent: None)
    read = event_stream_roster_reader(_user_key(scope="agent", agent_name=OWN))
    assert read() == [OWN]
    _run("UPDATE users SET suspended_at = :t WHERE id = 1", t="2026-11-08T10:00:00Z")
    assert read() == []


# ---------------------------------------------------------------------------
# A change that lands while a roster is being read
# ---------------------------------------------------------------------------


class _SlowFirstRead:
    """The first call blocks until released and returns ``first``; later calls
    return ``then`` at once. Models a read that started before a change."""

    def __init__(self, first, then):
        self.first, self.then = list(first), list(then)
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *_args):
        with self._lock:
            self.calls += 1
            n = self.calls
        if n == 1:
            self.started.set()
            assert self.release.wait(5), "the first read was never released"
            return self.first
        return self.then


async def _wait_started(read: _SlowFirstRead) -> None:
    for _ in range(500):
        if read.started.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the first read never started")


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["/ws", "/ws/events"])
async def test_a_change_during_an_in_flight_read_gets_one_more_read(door):
    read = _SlowFirstRead(first=["kept", "unshared"], then=["kept"])
    d = StreamDispatcher()
    if door == "/ws":
        slot = _ClientSlot(
            ws=None,
            scope=SCOPE_ALL,
            send_func=None,
            accessible_agents={"kept", "unshared"},
            email="u@example.test",
        )
        d.set_accessible_resolver(read)
    else:
        slot = _ClientSlot(
            ws=None,
            scope=SCOPE_SCOPED,
            send_func=None,
            accessible_agents={"kept", "unshared"},
            roster_resolver=read,
        )
    d._clients["c"] = slot

    d._maybe_invalidate_rosters({"event": "agent_shared", "data": {"name": "kept"}})
    await _wait_started(read)
    d._maybe_invalidate_rosters(
        {"event": "agent_unshared", "data": {"name": "unshared"}}
    )
    read.release.set()
    await _drain(d)

    assert slot.accessible_agents == {"kept"}
    assert read.calls == 2 and not slot.refreshing


@pytest.mark.asyncio
async def test_a_refresh_command_during_an_in_flight_read_has_the_last_word():
    read = _SlowFirstRead(first=["kept", "unshared"], then=["kept"])
    d = StreamDispatcher()
    slot = _ClientSlot(
        ws=None,
        scope=SCOPE_SCOPED,
        send_func=None,
        accessible_agents={"kept", "unshared"},
        roster_resolver=read,
    )
    d._clients["c"] = slot

    d._maybe_invalidate_rosters({"event": "agent_created", "data": {"name": "x"}})
    await _wait_started(read)
    d.update_accessible_agents("c", ["kept"])
    read.release.set()
    await _drain(d)

    assert slot.accessible_agents == {"kept"}


class _BlockedReads:
    """Every call blocks until its own release; returns the next value."""

    def __init__(self, *values):
        self.values = [list(v) for v in values]
        self.releases = [threading.Event() for _ in values]
        self.started = [threading.Event() for _ in values]
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *_args):
        with self._lock:
            n = self.calls
            self.calls += 1
        self.started[n].set()
        assert self.releases[n].wait(5), f"read {n} was never released"
        return self.values[n]


async def _until_set(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the read never started")


@pytest.mark.asyncio
async def test_a_read_that_a_refresh_command_superseded_is_not_applied():
    """The in-flight read started before the access change. Its result is
    older than the list the refresh command set, so it never replaces it,
    not even until the follow-up read lands."""
    read = _BlockedReads(["kept", "revoked"], ["kept"])
    d = StreamDispatcher()
    slot = _ClientSlot(
        ws=None, scope=SCOPE_SCOPED, send_func=None,
        accessible_agents={"kept", "revoked"}, roster_resolver=read,
    )
    d._clients["c"] = slot

    d._maybe_invalidate_rosters({"event": "agent_created", "data": {"name": "x"}})
    await _until_set(read.started[0])
    d.update_accessible_agents("c", ["kept"])  # the refresh command's fresh read
    read.releases[0].set()
    await _until_set(read.started[1])  # the follow-up read is in flight
    try:
        assert slot.accessible_agents == {"kept"}
    finally:
        read.releases[1].set()
        await _drain(d)
    assert slot.accessible_agents == {"kept"}


@pytest.mark.asyncio
async def test_a_read_that_an_access_change_superseded_is_not_applied():
    """An access change while a read is in flight: that read may predate the
    change, so its result is dropped and only the follow-up read is applied."""
    read = _BlockedReads(["kept", "new-but-stale"], ["kept"])
    d = StreamDispatcher()
    slot = _ClientSlot(
        ws=None, scope=SCOPE_SCOPED, send_func=None,
        accessible_agents={"kept"}, roster_resolver=read,
    )
    d._clients["c"] = slot

    d._maybe_invalidate_rosters({"event": "agent_shared", "data": {"name": "x"}})
    await _until_set(read.started[0])
    d._maybe_invalidate_rosters({"event": "agent_unshared", "data": {"name": "x"}})
    read.releases[0].set()
    await _until_set(read.started[1])
    try:
        assert slot.accessible_agents == {"kept"}
    finally:
        read.releases[1].set()
        await _drain(d)
    assert slot.accessible_agents == {"kept"}


@pytest.mark.asyncio
async def test_a_slot_that_disconnects_mid_read_is_not_read_again():
    read = _SlowFirstRead(first=["a"], then=["b"])
    d = StreamDispatcher()
    slot = _ClientSlot(
        ws=None,
        scope=SCOPE_SCOPED,
        send_func=None,
        accessible_agents={"a"},
        roster_resolver=read,
    )
    d._clients["c"] = slot

    d._maybe_invalidate_rosters({"event": "agent_created", "data": {"name": "x"}})
    await _wait_started(read)
    d._maybe_invalidate_rosters({"event": "agent_deleted", "data": {"name": "x"}})
    d.unregister("c")
    read.release.set()
    await _drain(d)

    assert read.calls == 1


@pytest.mark.asyncio
async def test_two_slots_of_one_key_are_each_re_read():
    calls = []
    d = StreamDispatcher()
    slots = [
        _ClientSlot(
            ws=None,
            scope=SCOPE_SCOPED,
            send_func=None,
            accessible_agents={"old"},
            roster_resolver=lambda: calls.append(1) or ["new"],
        )
        for _ in range(2)
    ]
    d._clients = {str(i): s for i, s in enumerate(slots)}
    d._maybe_invalidate_rosters({"event": "agent_unshared", "data": {"name": "old"}})
    await _drain(d)
    assert len(calls) == 2
    assert all(s.accessible_agents == {"new"} for s in slots)


# ---------------------------------------------------------------------------
# The periodic re-read
# ---------------------------------------------------------------------------


# The module the loop actually reads its interval from. Reached through the
# function's own globals, not ``services.event_bus`` by name: a file collected
# earlier can leave another copy of the module registered.
def _loop_globals() -> dict:
    return StreamDispatcher._reread_rosters_periodically.__globals__


def _fast_interval(monkeypatch, seconds):
    monkeypatch.setitem(_loop_globals(), "ROSTER_REREAD_INTERVAL_S", seconds)
    assert StreamDispatcher._reread_rosters_periodically.__globals__["ROSTER_REREAD_INTERVAL_S"] == seconds


async def _no_reader(self):
    await asyncio.Event().wait()


async def _until(predicate, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


def test_the_re_read_interval_is_about_a_minute():
    assert _loop_globals()["ROSTER_REREAD_INTERVAL_S"] == 60


@pytest.mark.asyncio
async def test_rosters_are_re_read_periodically_with_no_access_event(monkeypatch):
    _fast_interval(monkeypatch, 0.02)
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    admin_reads = []
    d = StreamDispatcher()
    ws_slot = _ClientSlot(
        ws=None,
        scope=SCOPE_ALL,
        send_func=None,
        accessible_agents={"kept", "unshared"},
        email="u@example.test",
    )
    key_slot = _ClientSlot(
        ws=None,
        scope=SCOPE_SCOPED,
        send_func=None,
        accessible_agents={"kept", "unshared"},
        roster_resolver=lambda: ["kept"],
    )
    admin_slot = _ClientSlot(
        ws=None,
        scope=SCOPE_SCOPED,
        send_func=None,
        is_admin=True,
        roster_resolver=lambda: admin_reads.append(1) or ["x"],
    )
    d._clients = {"w": ws_slot, "k": key_slot, "a": admin_slot}
    d.set_accessible_resolver(lambda email: ["kept"])

    await d.start()
    try:
        await _until(
            lambda: ws_slot.accessible_agents == {"kept"}
            and key_slot.accessible_agents == {"kept"}
        )
    finally:
        await d.stop()
    assert admin_reads == []


@pytest.mark.asyncio
async def test_the_periodic_re_read_runs_one_read_at_a_time(monkeypatch):
    _fast_interval(monkeypatch, 0.02)
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    lock = threading.Lock()
    state = {"now": 0, "max": 0, "reads": 0}

    def read(*_args):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["reads"] += 1
        threading.Event().wait(0.01)
        with lock:
            state["now"] -= 1
        return ["kept"]

    d = StreamDispatcher()
    d._clients = {
        str(i): _ClientSlot(
            ws=None, scope=SCOPE_SCOPED, send_func=None, roster_resolver=read
        )
        for i in range(4)
    }
    d._clients.update(
        {
            f"w{i}": _ClientSlot(
                ws=None, scope=SCOPE_ALL, send_func=None, email=f"u{i}@example.test"
            )
            for i in range(3)
        }
    )
    d.set_accessible_resolver(read)
    await d.start()
    try:
        await _until(lambda: state["reads"] >= 14)
    finally:
        await d.stop()
    assert state["max"] == 1


@pytest.mark.asyncio
async def test_an_access_change_during_a_periodic_cycle_is_read_at_once(monkeypatch):
    """The periodic cycle reads its rosters one after another. A slot it has
    not reached yet is still free, so an access change is read for that slot
    at once rather than after the earlier reads of the cycle."""
    _fast_interval(monkeypatch, 0.01)
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    slow = _SlowFirstRead(first=["a"], then=["a"])
    d = StreamDispatcher()
    first = _ClientSlot(
        ws=None, scope=SCOPE_SCOPED, send_func=None,
        accessible_agents={"a"}, roster_resolver=slow,
    )
    second = _ClientSlot(
        ws=None, scope=SCOPE_SCOPED, send_func=None,
        accessible_agents={"kept", "unshared"}, roster_resolver=lambda: ["kept"],
    )
    d._clients = {"first": first, "second": second}
    await d.start()
    try:
        await _wait_started(slow)  # the cycle is blocked on its first read
        d._maybe_invalidate_rosters(
            {"event": "agent_unshared", "data": {"name": "unshared"}}
        )
        await _until(lambda: second.accessible_agents == {"kept"}, timeout=1.0)
    finally:
        slow.release.set()
        await d.stop()


@pytest.mark.asyncio
async def test_a_slot_that_disconnects_before_its_periodic_read_is_not_read(monkeypatch):
    _fast_interval(monkeypatch, 0.01)
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    slow = _SlowFirstRead(first=["a"], then=["a"])
    gone_reads = []
    d = StreamDispatcher()
    d._clients = {
        "first": _ClientSlot(
            ws=None, scope=SCOPE_SCOPED, send_func=None, roster_resolver=slow
        ),
        "gone": _ClientSlot(
            ws=None, scope=SCOPE_SCOPED, send_func=None,
            roster_resolver=lambda: gone_reads.append(1) or ["x"],
        ),
    }
    await d.start()
    try:
        await _wait_started(slow)  # the cycle is blocked on its first read
        d.unregister("gone")
        slow.release.set()
        await _until(lambda: slow.calls >= 2)  # the next cycle has begun
    finally:
        slow.release.set()
        await d.stop()
    assert gone_reads == []


@pytest.mark.asyncio
async def test_the_periodic_re_read_reads_nothing_when_no_roster_is_held(monkeypatch):
    _fast_interval(monkeypatch, 0.01)
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    calls = []
    d = StreamDispatcher()
    d.set_accessible_resolver(lambda email: calls.append(email) or [])
    d._clients = {
        "a": _ClientSlot(
            ws=None,
            scope=SCOPE_ALL,
            send_func=None,
            is_admin=True,
            email="admin@example.test",
        )
    }
    await d.start()
    try:
        await asyncio.sleep(0.1)
    finally:
        await d.stop()
    assert calls == []


@pytest.mark.asyncio
async def test_a_failing_periodic_read_keeps_the_roster_and_the_loop(
    monkeypatch, caplog
):
    _fast_interval(monkeypatch, 0.02)
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    calls = []

    def read():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db unavailable")
        return ["kept"]

    d = StreamDispatcher()
    slot = _ClientSlot(
        ws=None,
        scope=SCOPE_SCOPED,
        send_func=None,
        accessible_agents={"kept", "unshared"},
        roster_resolver=read,
    )
    d._clients = {"k": slot}
    await d.start()
    try:
        await _until(lambda: len(calls) >= 1)
        assert slot.accessible_agents == {"kept", "unshared"}
        await _until(lambda: slot.accessible_agents == {"kept"})
    finally:
        await d.stop()
    assert "roster refresh failed" in caplog.text


@pytest.mark.asyncio
async def test_the_periodic_re_read_stops_with_the_dispatcher(monkeypatch):
    monkeypatch.setattr(StreamDispatcher, "_supervised_reader", _no_reader)
    d = StreamDispatcher()
    await d.start()
    task = d._roster_task
    assert task is not None and not task.done()
    await d.stop()
    assert task.done() and d._roster_task is None

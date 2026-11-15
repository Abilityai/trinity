"""abilityai/trinity-enterprise#863 — live connections follow the account.

What is pinned here:

  * one comparison, used by the per-worker tick and the immediate re-check: the
    account snapshot a door recorded when the connection opened, against the
    account's current row. A changed role (raised or lowered), email or
    password closes with the reconnect reason; a deactivated or removed account,
    or an inactive key, closes with the ended reason;
  * the role route re-checks the account's connections on its own worker at
    once, after the write, and only when the role changed;
  * every worker's tick closes what another worker's write left stale; a tick
    with nothing open reads nothing; a tick whose read fails keeps everything
    open, and the next tick retries;
  * a re-check after a reported change whose read fails closes that account's
    connections; one failing closer does not stop the others or the loop;
  * ``notify_account_changed`` is safe from a plain thread and runs the close on
    the registry's loop;
  * a door registers before it reads, so a change that lands while it opens
    refuses it, and a door that opens after the change stays open;
  * the real ``/ws`` and ``/ws/events`` handlers close with their codes,
    unregister their delivery slot themselves, and return without raising;
  * none of these paths uses the event bus.
"""

from __future__ import annotations

import ast
import asyncio
import logging
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

from db_harness import db_backend, seed_user  # noqa: E402,F401

from services import live_connections as lc  # noqa: E402
from services.live_connections import (  # noqa: E402
    DOOR_EVENT_STREAM,
    DOOR_WS,
    CloseReason,
    LiveConnectionRegistry,
)

pytestmark = pytest.mark.unit

ALICE, BOB, ADMIN = "lc-alice", "lc-bob", "lc-admin"
ALICE_EMAIL, BOB_EMAIL = "lc-alice@example.test", "lc-bob@example.test"


def _run(sql: str, **binds) -> None:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text(sql), binds)


@pytest.fixture
def people(db_backend):  # noqa: F811 — the db_harness fixture, by name
    """ALICE (creator) and BOB (operator) with emails and password hashes; an
    admin with no email, like the built-in one."""
    from database import db

    seed_user(1, ALICE, role="creator")
    seed_user(2, BOB, role="operator")
    seed_user(3, ADMIN, role="admin")
    _run("UPDATE users SET email = :e, password_hash = 'hash-a1' WHERE id = 1", e=ALICE_EMAIL)
    _run("UPDATE users SET email = :e, password_hash = 'hash-b1' WHERE id = 2", e=BOB_EMAIL)
    return db


@pytest.fixture
def registry(monkeypatch):
    """A fresh registry installed as the worker's, so doors and routes use it."""
    reg = LiveConnectionRegistry()
    monkeypatch.setattr(lc, "registry", reg)
    return reg


class Closes:
    """Records each closer call: (name, reason, thread)."""

    def __init__(self) -> None:
        self.calls = []

    def closer(self, name):
        async def close(reason):
            self.calls.append((name, reason, threading.get_ident()))
        return close

    def by_name(self):
        return {name: reason for name, reason, _ in self.calls}


def _open(reg, door, username, closer, key_id=None):
    """Admit, read and go live exactly as a door does."""
    entry = reg.admit(door, username, closer, key_id=key_id)
    assert entry.activate(lc.read_access(username))
    assert entry.go_live()
    return entry


async def _settle(reg) -> None:
    for _ in range(50):
        if not reg._inflight:
            return
        await asyncio.gather(*list(reg._inflight))
    raise AssertionError("re-checks never settled")


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def test_the_recheck_period_is_fifteen_seconds_and_a_code_constant():
    assert lc.RECHECK_INTERVAL_S == 15.0
    assert LiveConnectionRegistry()._interval == 15.0


# ---------------------------------------------------------------------------
# The role route: immediate, on its own worker
# ---------------------------------------------------------------------------


def _admin():
    from models import User

    return User(id=3, username=ADMIN, role="admin")


def _request():
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "PUT", "path": "/api/users/x/role", "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "client": ("127.0.0.1", 5000), "headers": [(b"host", b"backend:8000")],
    })


@pytest.fixture
def role_route(monkeypatch, people):
    import routers.users as users_mod
    from models import UserRoleUpdate

    async def no_audit(**_kw):
        return None

    monkeypatch.setattr(users_mod.platform_audit_service, "log", no_audit)
    # The route's own module object, whatever another test re-imported.
    monkeypatch.setattr(users_mod.live_connections, "registry", lc.registry)

    async def change(username, role, actor=None):
        return await users_mod.update_user_role(username, UserRoleUpdate(role=role), _request(), actor or _admin())

    return change


@pytest.mark.asyncio
async def test_a_role_change_through_the_route_closes_that_accounts_connections_at_once(registry, role_route):
    from database import db

    closes = Closes()
    _open(registry, DOOR_WS, ALICE, closes.closer("alice-ws"))
    _open(registry, DOOR_EVENT_STREAM, ALICE, closes.closer("alice-events"), key_id=_key(db, ALICE).id)
    _open(registry, DOOR_WS, BOB, closes.closer("bob-ws"))

    await role_route(ALICE, "operator")
    await _settle(registry)

    assert closes.by_name() == {"alice-ws": CloseReason.CHANGED, "alice-events": CloseReason.CHANGED}


@pytest.mark.asyncio
async def test_a_promotion_closes_too_and_a_same_role_write_closes_nothing(registry, role_route, monkeypatch):
    """D1: any role change closes, a raise included. A write that leaves the
    role as it was re-checks nothing at all."""
    calls = []
    real = registry.recheck_account
    monkeypatch.setattr(registry, "recheck_account", lambda u: (calls.append(u), real(u)))
    closes = Closes()
    _open(registry, DOOR_WS, BOB, closes.closer("bob-ws"))

    await role_route(BOB, "operator")  # unchanged
    await _settle(registry)
    assert calls == [] and closes.calls == []

    await role_route(BOB, "admin")  # a promotion
    await _settle(registry)
    assert calls == [BOB]
    assert closes.by_name() == {"bob-ws": CloseReason.CHANGED}


@pytest.mark.asyncio
async def test_an_admin_cannot_change_their_own_role_and_nothing_closes(registry, role_route):
    from fastapi import HTTPException

    closes = Closes()
    _open(registry, DOOR_WS, ADMIN, closes.closer("admin-ws"))
    with pytest.raises(HTTPException) as refused:
        await role_route(ADMIN, "operator")
    assert refused.value.status_code == 400
    await _settle(registry)
    assert closes.calls == []


# ---------------------------------------------------------------------------
# The comparison, and the tick on every worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_worker_closes_a_stale_connection_on_its_next_tick(people):
    """Two workers' registries; only A served the change. B's next tick closes
    B's stale connection from its own comparison."""
    a, b = LiveConnectionRegistry(), LiveConnectionRegistry()
    closes = Closes()
    _open(a, DOOR_WS, ALICE, closes.closer("on-a"))
    _open(b, DOOR_WS, ALICE, closes.closer("on-b"))

    people.update_user_role(ALICE, "operator")
    a.recheck_account(ALICE)
    await _settle(a)
    assert closes.by_name() == {"on-a": CloseReason.CHANGED}

    await b.revalidate()
    assert closes.by_name() == {"on-a": CloseReason.CHANGED, "on-b": CloseReason.CHANGED}


@pytest.mark.asyncio
@pytest.mark.parametrize("change, reason", [
    ("UPDATE users SET suspended_at = '2026-11-08T00:00:00Z' WHERE username = :u", CloseReason.ENDED),
    ("DELETE FROM users WHERE username = :u", CloseReason.ENDED),
    ("UPDATE users SET email = 'moved@example.test' WHERE username = :u", CloseReason.CHANGED),
    ("UPDATE users SET password_hash = 'hash-a2' WHERE username = :u", CloseReason.CHANGED),
    ("UPDATE users SET role = 'admin' WHERE username = :u", CloseReason.CHANGED),
])
async def test_the_tick_closes_a_connection_whose_account_changed(people, change, reason):
    reg = LiveConnectionRegistry()
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    _open(reg, DOOR_WS, BOB, closes.closer("bob"))

    await reg.revalidate()
    assert closes.calls == [], "an unchanged account must stay open"

    _run(change, u=ALICE)
    await reg.revalidate()
    assert closes.by_name() == {"alice": reason}


def test_a_snapshot_tells_password_hashes_apart_without_holding_one():
    row = {"role": "creator", "email": ALICE_EMAIL, "password": "hash-a1"}
    snap = lc.access_snapshot(row)
    assert snap == lc.access_snapshot(dict(row))
    assert snap != lc.access_snapshot({**row, "password": "hash-a2"})
    assert snap != lc.access_snapshot({**row, "password": None})
    assert "hash-a1" not in repr(snap)
    assert all("hash-a1" not in str(value) for value in vars(snap).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", [None, ""])
async def test_an_active_account_stays_open_whatever_form_its_active_state_takes(people, cleared):
    reg = LiveConnectionRegistry()
    closes = Closes()
    _run("UPDATE users SET suspended_at = :s WHERE username = :u", s=cleared, u=ALICE)
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    await reg.revalidate()
    assert closes.calls == []


def _key(db, username):
    from db_models import McpApiKeyCreate

    created = db.create_mcp_api_key(username, McpApiKeyCreate(name="lc-key"))
    assert created is not None
    return created


@pytest.mark.asyncio
async def test_an_inactive_or_deleted_key_closes_only_the_streams_opened_with_it(people):
    k1, k2 = _key(people, ALICE), _key(people, ALICE)
    reg = LiveConnectionRegistry()
    closes = Closes()
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("k1"), key_id=k1.id)
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("k2"), key_id=k2.id)
    _open(reg, DOOR_WS, ALICE, closes.closer("alice-ws"))

    await reg.revalidate()
    assert closes.calls == []

    assert people.revoke_mcp_api_key(k1.id, ALICE)
    await reg.revalidate()
    assert closes.by_name() == {"k1": CloseReason.ENDED}

    assert people.delete_mcp_api_key(k2.id, ALICE)
    await reg.revalidate()
    assert closes.by_name() == {"k1": CloseReason.ENDED, "k2": CloseReason.ENDED}


@pytest.mark.asyncio
async def test_a_key_recheck_reaches_only_the_connections_opened_with_that_key(people):
    """``notify_key_changed`` is key-scoped: it never closes the owner's other
    connections, even ones that are stale for another reason."""
    k1 = _key(people, ALICE)
    reg = LiveConnectionRegistry()
    closes = Closes()
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("k1"), key_id=k1.id)
    _open(reg, DOOR_WS, ALICE, closes.closer("alice-ws"))

    people.update_user_role(ALICE, "operator")
    assert people.revoke_mcp_api_key(k1.id, ALICE)
    reg.recheck_key(k1.id)
    await _settle(reg)
    assert closes.by_name() == {"k1": CloseReason.ENDED}


@pytest.mark.asyncio
async def test_with_nothing_open_a_recheck_reads_nothing_and_raises_nothing():
    reads = []
    reg = LiveConnectionRegistry(read_state=lambda u, k: (reads.append((u, k)), ({}, set()))[1])

    lc.LiveConnectionRegistry().recheck_account(ALICE)  # never bound to a loop
    worker = threading.Thread(target=lc.LiveConnectionRegistry().recheck_account, args=(ALICE,))
    worker.start()
    worker.join()

    await reg.revalidate()
    reg.recheck_account(ALICE)
    reg.recheck_key("k")
    await _settle(reg)
    entry = reg.admit(DOOR_WS, BOB, Closes().closer("bob"))
    entry.release()
    await reg.revalidate()
    reg.recheck_account(BOB)
    await _settle(reg)
    assert reads == []


# ---------------------------------------------------------------------------
# Failures: the read, the closer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_tick_read_keeps_every_connection_open_and_the_next_tick_retries(people, caplog):
    state = {"fail": True}

    def flaky(usernames, key_ids):
        if state["fail"]:
            raise RuntimeError("database is locked")
        return lc._read_state(usernames, key_ids)

    reg = LiveConnectionRegistry(read_state=flaky)
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    people.update_user_role(ALICE, "operator")

    with caplog.at_level(logging.ERROR, logger="services.live_connections"):
        await reg.revalidate()
    assert closes.calls == []
    assert any(r.levelno == logging.ERROR and "keeping 1 connections open" in r.getMessage()
               for r in caplog.records)

    state["fail"] = False
    await reg.revalidate()
    assert closes.by_name() == {"alice": CloseReason.CHANGED}


@pytest.mark.asyncio
async def test_a_failed_read_after_a_reported_change_closes_that_accounts_connections(people):
    def broken(usernames, key_ids):
        raise RuntimeError("database is locked")

    reg = LiveConnectionRegistry(read_state=broken)
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("alice-events"), key_id="k")
    _open(reg, DOOR_WS, BOB, closes.closer("bob"))

    reg.recheck_account(ALICE)
    await _settle(reg)
    assert closes.by_name() == {"alice": CloseReason.CHANGED, "alice-events": CloseReason.CHANGED}


@pytest.mark.asyncio
async def test_a_closer_that_raises_does_not_stop_the_others_or_the_loop(people):
    reg = LiveConnectionRegistry(interval=0.01)
    closes = Closes()
    attempts = []

    async def broken(reason):
        attempts.append(reason)
        raise RuntimeError("socket already gone")

    _open(reg, DOOR_WS, ALICE, broken)
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("alice-events"), key_id=None)
    people.update_user_role(ALICE, "operator")

    await reg.start()
    try:
        await _until(lambda: closes.calls and len(attempts) >= 2)
        assert not reg._task.done(), "the re-check loop died"
    finally:
        await reg.stop()
    assert closes.by_name() == {"alice-events": CloseReason.CHANGED}


# ---------------------------------------------------------------------------
# The thread contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_close_that_hangs_delays_neither_the_other_closes_nor_the_tick(people, monkeypatch):
    """Closes run side by side, each bounded: one connection that does not
    finish closing holds up no other connection's close, and the tick ends."""
    monkeypatch.setattr(lc, "CLOSE_TIMEOUT_S", 0.5)
    loop = asyncio.get_running_loop()
    reg = LiveConnectionRegistry()
    closed_after = {}
    started = [loop.time()]

    async def never_finishes(reason):
        await asyncio.sleep(60)

    async def closes(reason):
        closed_after["bob"] = loop.time() - started[0]

    _open(reg, DOOR_WS, ALICE, never_finishes)
    _open(reg, DOOR_WS, BOB, closes)
    people.update_user_role(ALICE, "operator")
    people.update_user_role(BOB, "creator")

    started[0] = loop.time()
    await asyncio.wait_for(reg.revalidate(), timeout=5)
    assert closed_after["bob"] < 0.25, f"a hanging close held up another for {closed_after['bob']:.2f}s"
    assert loop.time() - started[0] < 2


@pytest.mark.asyncio
async def test_notify_from_a_plain_thread_closes_on_the_registrys_loop(registry, people):
    loop_thread = threading.get_ident()
    closes = Closes()
    _open(registry, DOOR_WS, ALICE, closes.closer("alice"))
    people.update_user_role(ALICE, "operator")

    worker = threading.Thread(target=lc.notify_account_changed, args=(ALICE,))
    worker.start()
    worker.join()
    assert closes.calls == [], "the thread ran the close itself instead of handing it over"

    await _until(lambda: closes.calls)
    await _settle(registry)
    [(name, reason, thread)] = closes.calls
    assert (name, reason) == ("alice", CloseReason.CHANGED)
    assert thread == loop_thread


# ---------------------------------------------------------------------------
# Ordering: register before the read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_change_while_a_door_is_connecting_refuses_it_and_a_later_door_stays_open(people):
    reg = LiveConnectionRegistry()
    closes = Closes()
    early = reg.admit(DOOR_WS, ALICE, closes.closer("early"))
    assert early.activate(lc.read_access(ALICE))  # read the OLD role
    people.update_user_role(ALICE, "operator")  # the change commits before the door connects
    reg.recheck_account(ALICE)
    await _settle(reg)

    assert early.revoked is CloseReason.CHANGED
    assert early.go_live() is False
    assert closes.calls == [], "a connection that is not open yet must not be closed under its door"

    late = reg.admit(DOOR_WS, ALICE, closes.closer("late"))
    assert late.activate(lc.read_access(ALICE))  # read the NEW role
    assert late.go_live()
    reg.recheck_account(ALICE)  # e.g. the same change reported again
    await _settle(reg)
    assert late.revoked is None and closes.calls == []


@pytest.mark.asyncio
async def test_a_recheck_that_reaches_a_door_before_its_read_marks_it_dirty(people):
    reg = LiveConnectionRegistry()
    entry = reg.admit(DOOR_WS, ALICE, Closes().closer("alice"))
    reg.recheck_account(ALICE)  # a change reported while the door is still reading
    assert entry.activate(lc.read_access(ALICE)) is False
    other = reg.admit(DOOR_WS, ALICE, Closes().closer("alice-2"))
    assert other.activate(lc.read_access(ALICE)) is True


@pytest.mark.asyncio
async def test_a_released_entry_is_neither_compared_nor_closed(people):
    reg = LiveConnectionRegistry()
    closes = Closes()
    entry = _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    entry.release()
    entry.release()  # idempotent
    people.update_user_role(ALICE, "operator")
    await reg.revalidate()
    assert closes.calls == [] and reg.count() == 0


# ---------------------------------------------------------------------------
# The real doors
# ---------------------------------------------------------------------------


def _on_loop(reg, coro_fn):
    """Run ``coro_fn()`` on the registry's loop (the TestClient's) and wait."""
    return asyncio.run_coroutine_threadsafe(coro_fn(), reg._loop).result(timeout=10)


def _browser_socket(monkeypatch, username):
    import main
    import services.ws_ticket_service as tickets
    from fastapi.testclient import TestClient

    monkeypatch.setattr(tickets, "consume_ticket", lambda ticket, scope: {"sub": username, "scope": scope})
    monkeypatch.setattr(main.live_connections, "registry", lc.registry)
    return main, TestClient(main.app).websocket_connect("/ws?ticket=t-live")


def _ping(ws) -> None:
    ws.send_text('{"type": "ping"}')
    assert ws.receive_json() == {"type": "pong"}


@pytest.mark.timeout(10)
@pytest.mark.parametrize("change, code", [
    (lambda db: db.update_user_role(ALICE, "operator"), 4000),
    (lambda db: _run("UPDATE users SET suspended_at = '2026-11-08T00:00:00Z' WHERE username = :u", u=ALICE), 4001),
])
def test_the_browser_socket_closes_with_its_code_and_unregisters_itself(registry, people, monkeypatch, caplog,
                                                                         change, code):
    from fastapi import WebSocketDisconnect

    main, connect = _browser_socket(monkeypatch, ALICE)
    with caplog.at_level(logging.WARNING), connect as ws:
        caplog.clear()  # only what the connection logs, not the app import
        _ping(ws)
        # main's own dispatcher: another test may have re-imported the bus module.
        held = main.stream_dispatcher.client_count()
        assert held >= 1, "the socket never registered a delivery slot"
        assert registry.count() == 1

        change(people)
        lc.notify_account_changed(ALICE)  # from this (non-loop) thread
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == code
        # The handler is still parked in receive(); the closer unregistered.
        assert main.stream_dispatcher.client_count() == held - 1
    assert registry.count() == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.timeout(10)
def test_the_browser_socket_is_closed_by_the_tick_too(registry, people, monkeypatch):
    from fastapi import WebSocketDisconnect

    main, connect = _browser_socket(monkeypatch, ALICE)
    with connect as ws:
        _ping(ws)
        _run("UPDATE users SET email = 'moved@example.test' WHERE username = :u", u=ALICE)
        _on_loop(registry, registry.revalidate)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 4000


@pytest.mark.timeout(10)
def test_a_change_while_the_browser_socket_connects_refuses_it(registry, people, monkeypatch):
    from fastapi import WebSocketDisconnect

    main, connect = _browser_socket(monkeypatch, ALICE)
    real_connect = main.manager.connect

    async def connect_after_a_change(websocket, **kw):
        people.update_user_role(ALICE, "operator")
        lc.notify_account_changed(ALICE)  # on the loop, mid-connect
        await _settle(registry)
        await real_connect(websocket, **kw)

    monkeypatch.setattr(main.manager, "connect", connect_after_a_change)
    with connect as ws:
        ws.send_text('{"type": "ping"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 4000
    assert registry.count() == 0


@pytest.mark.timeout(10)
def test_a_change_while_the_browser_socket_reads_the_account_refuses_it(registry, people, monkeypatch):
    from fastapi import WebSocketDisconnect

    main, connect = _browser_socket(monkeypatch, ALICE)
    real_resolve = main.resolve_ws_identity

    def resolve_during_a_change(username):
        lc.notify_account_changed(username)  # reaches the entry before it has a snapshot
        return real_resolve(username)

    monkeypatch.setattr(main, "resolve_ws_identity", resolve_during_a_change)
    with pytest.raises(WebSocketDisconnect) as refused:
        with connect:
            pass
    assert refused.value.code == 4000
    assert registry.count() == 0


@pytest.mark.timeout(10)
@pytest.mark.parametrize("middle, undo", [
    (lambda db: db.update_user_role(ALICE, "admin"), lambda db: db.update_user_role(ALICE, "operator")),
    (lambda db: _run("UPDATE users SET email = 'moved@example.test' WHERE username = :u", u=ALICE),
     lambda db: _run("UPDATE users SET email = :e WHERE username = :u", e=ALICE_EMAIL, u=ALICE)),
])
def test_a_browser_socket_is_refused_when_its_scope_read_disagrees_with_its_snapshot(registry, people, monkeypatch,
                                                                                    middle, undo):
    """The door reads the account twice: the snapshot, then the scope. When
    the scope read sees another role or email than the snapshot, the socket is
    refused, even when the account reads as the snapshot again before any
    re-check (so no re-check could ever tell the two apart)."""
    from fastapi import WebSocketDisconnect

    people.update_user_role(ALICE, "operator")
    main, connect = _browser_socket(monkeypatch, ALICE)
    real_resolve = main.resolve_ws_identity

    def resolve_between_two_writes(username):
        middle(people)
        identity = real_resolve(username)
        undo(people)
        return identity

    monkeypatch.setattr(main, "resolve_ws_identity", resolve_between_two_writes)
    with pytest.raises(WebSocketDisconnect) as refused:
        with connect:
            pass
    assert refused.value.code == 4000
    assert registry.count() == 0


def _event_stream(monkeypatch, token):
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.live_connections, "registry", lc.registry)
    return main, TestClient(main.app).websocket_connect(f"/ws/events?token={token}")


@pytest.mark.timeout(10)
@pytest.mark.parametrize("change, code", [
    (lambda db, key: db.update_user_role(ALICE, "operator"), 4000),
    (lambda db, key: db.revoke_mcp_api_key(key.id, ALICE), 4001),
    (lambda db, key: db.delete_mcp_api_key(key.id, ALICE), 4001),
])
def test_the_event_stream_closes_with_its_code_and_unregisters_itself(registry, people, monkeypatch, caplog,
                                                                       change, code):
    from fastapi import WebSocketDisconnect

    key = _key(people, ALICE)
    main, connect = _event_stream(monkeypatch, key.api_key)
    with caplog.at_level(logging.WARNING), connect as ws:
        caplog.clear()  # only what the connection logs, not the app import
        assert ws.receive_json()["type"] == "connected"
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
        # main's own dispatcher: another test may have re-imported the bus module.
        held = main.stream_dispatcher.client_count()
        assert held >= 1, "the socket never registered a delivery slot"
        [entry] = registry._entries.values()
        assert entry.key_id == key.id and entry.username == ALICE

        change(people, key)
        _on_loop(registry, registry.revalidate)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == code
        assert main.stream_dispatcher.client_count() == held - 1
    assert registry.count() == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.timeout(10)
def test_an_event_stream_is_refused_when_its_owner_email_differs_from_its_key_read(registry, people, monkeypatch):
    """The stream's scope is resolved with the email its key read returned,
    and its snapshot with the owner read after it. When the two disagree the
    stream is refused, so its scope and its snapshot describe one account."""
    from database import db
    from fastapi import WebSocketDisconnect

    key = _key(people, ALICE)
    main, connect = _event_stream(monkeypatch, key.api_key)
    real_get = db.get_user_by_username
    moved = []

    def owner_read_after_an_email_change(username):
        if username == ALICE and not moved:
            moved.append(username)
            _run("UPDATE users SET email = 'moved@example.test' WHERE username = :u", u=username)
        return real_get(username)

    monkeypatch.setattr(db, "get_user_by_username", owner_read_after_an_email_change)
    with pytest.raises(WebSocketDisconnect) as refused:
        with connect:
            pass
    assert refused.value.code == 4000
    assert moved == [ALICE] and registry.count() == 0


# ---------------------------------------------------------------------------
# Shape: where the doors register, and where the loop starts
# ---------------------------------------------------------------------------


def _main_tree():
    return ast.parse((_BACKEND / "main.py").read_text(encoding="utf-8"))


def _fn(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _call_lines(fn, attr):
    return [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", None)) == attr]


@pytest.mark.parametrize("handler, read", [
    ("websocket_endpoint", "read_access"),
    ("websocket_events_endpoint", "get_user_by_username"),
])
def test_each_event_door_registers_before_it_reads_and_goes_live_after_it_connects(handler, read):
    fn = _fn(_main_tree(), handler)
    [admit] = _call_lines(fn, "admit")
    [activate] = _call_lines(fn, "activate")
    [go_live] = _call_lines(fn, "go_live")
    reads = _call_lines(fn, read)
    connect = min(_call_lines(fn, "connect"))
    assert admit < min(reads) < activate < connect < go_live
    awaits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await)]
    between = [ln for ln in awaits if admit < ln < activate]
    # The only awaits allowed in that window are refusals that return.
    for ln in between:
        assert any(isinstance(n, ast.If) and n.lineno <= ln <= n.end_lineno
                   and any(isinstance(s, ast.Return) for s in n.body)
                   for n in ast.walk(fn)), f"{handler}: an await at line {ln} between admit and activate"
    finals = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert any("release" in ast.unparse(t.finalbody[-1]) for t in finals), \
        f"{handler}: the entry is not released in a finally"


def test_the_recheck_loop_starts_with_the_bus_even_when_the_bus_fails_and_stops_with_it():
    tree = _main_tree()
    start = _fn(tree, "_start_event_bus")
    top = [ast.unparse(s) for s in start.body]
    assert "await live_connections.start()" in top, \
        "the re-check must start outside the bus's try, so a failed bus does not stop it"
    stop = _fn(tree, "_shutdown_audit_and_event_bus")
    assert "live_connections.stop" in ast.unparse(stop)


def test_the_live_connection_module_does_not_use_the_bus():
    source = (_BACKEND / "services" / "live_connections.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if "redis" in m or "event_bus" in m}, imported


@pytest.mark.asyncio
async def test_rechecks_close_without_touching_the_bus(people, monkeypatch):
    from services import event_bus

    async def unreachable(*_a, **_k):
        raise AssertionError("a live-connection path reached the bus")

    monkeypatch.setattr(event_bus.event_bus, "_get_redis", unreachable)
    monkeypatch.setattr(event_bus.stream_dispatcher, "_get_redis", unreachable)
    reg = LiveConnectionRegistry()
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("immediate"))
    _open(reg, DOOR_WS, BOB, closes.closer("tick"))
    people.update_user_role(ALICE, "operator")
    people.update_user_role(BOB, "creator")
    reg.recheck_account(ALICE)
    await _settle(reg)
    await reg.revalidate()
    assert closes.by_name() == {"immediate": CloseReason.CHANGED, "tick": CloseReason.CHANGED}


# ---------------------------------------------------------------------------
# The batched reads
# ---------------------------------------------------------------------------


def test_the_batched_user_read_returns_only_rows_that_exist(people):
    rows = people.get_users_access_state({ALICE, BOB, "lc-nobody"})
    assert set(rows) == {ALICE, BOB}
    assert rows[ALICE]["role"] == "creator" and rows[ALICE]["email"] == ALICE_EMAIL
    assert rows[ALICE]["password"] == "hash-a1"


def test_the_batched_key_read_returns_only_active_keys(people):
    k1, k2, k3 = _key(people, ALICE), _key(people, ALICE), _key(people, BOB)
    assert people.revoke_mcp_api_key(k2.id, ALICE)
    assert people.delete_mcp_api_key(k3.id, BOB)
    assert people.get_keys_active({k1.id, k2.id, k3.id, "k-nobody"}) == {k1.id}


def test_the_batched_reads_skip_the_database_for_an_empty_set(people, monkeypatch):
    import db.mcp_keys as keys_mod
    import db.users as users_mod

    def no_engine():
        raise AssertionError("an empty read reached the database")

    monkeypatch.setattr(users_mod, "get_engine", no_engine)
    monkeypatch.setattr(keys_mod, "get_engine", no_engine)
    assert people.get_users_access_state(set()) == {}
    assert people.get_keys_active([""]) == set()

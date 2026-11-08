"""abilityai/trinity-enterprise#863 — an account or key change reaches every worker.

What is pinned here:

  * a change re-checks the account's (or key's) connections on the worker that
    served it AND publishes one control event on the event bus; every worker's
    dispatcher hands that event to its own registry, which re-checks its own
    connections with the same comparison and never publishes again;
  * a control event never reaches a client: not live, not on a replay, not an
    admin's socket, not an admin's event stream;
  * the publishing worker's own echo closes nothing that already matches (a
    client that reconnected at the new scope stays open);
  * revoking or deleting an MCP key closes only the event streams opened with
    that key, on every worker, whoever owns it; the owner's other connections
    stay open;
  * a password change closes the account's connections at once;
  * a publish that fails still leaves the local re-check done and raises
    nothing into the writer; a handler that fails does not stop delivery;
  * ``main.py`` wires the handler into the dispatcher and publishes control
    events through the bus.
"""

from __future__ import annotations

import asyncio
import json
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

ALICE, BOB, ADMIN = "ce-alice", "ce-bob", "ce-admin"
# A door label the registry has never seen: the comparison is door-agnostic, so
# a terminal entry behaves like any other connection that holds no key.
DOOR_TERMINAL = "ce-terminal"
OLD_PASSWORD, NEW_PASSWORD = "Old-Passw0rd-ce1!", "New-Passw0rd-ce2!"


def _bus():
    """The bus module as it is now (another test file may have re-loaded it)."""
    from services import event_bus

    return event_bus


def _run(sql: str, **binds) -> None:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text(sql), binds)


@pytest.fixture
def people(db_backend):  # noqa: F811 — the db_harness fixture, by name
    from database import db

    seed_user(1, ALICE, role="creator")
    seed_user(2, BOB, role="operator")
    seed_user(3, ADMIN, role="admin")
    _run("UPDATE users SET email = 'ce-alice@example.test', password_hash = 'hash-a1' WHERE id = 1")
    _run("UPDATE users SET email = 'ce-bob@example.test', password_hash = 'hash-b1' WHERE id = 2")
    return db


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


class Published:
    """A control publisher that records what it was asked to publish."""

    def __init__(self) -> None:
        self.events = []
        self.threads = []

    async def __call__(self, control):
        self.events.append(dict(control))
        self.threads.append(threading.get_ident())


def _open(reg, door, username, closer, key_id=None):
    entry = reg.admit(door, username, closer, key_id=key_id)
    assert entry.activate(lc.read_access(username))
    assert entry.go_live()
    return entry


async def _settle(*regs) -> None:
    for _ in range(50):
        pending = [t for reg in regs for t in reg._inflight]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError("re-checks never settled")


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _key(db, username):
    from db_models import McpApiKeyCreate

    created = db.create_mcp_api_key(username, McpApiKeyCreate(name="ce-key"))
    assert created is not None
    return created


@pytest.fixture
def worker(monkeypatch):
    """This worker's registry, installed as the module's, with a recording
    publisher in place of the bus."""
    reg = LiveConnectionRegistry()
    published = Published()
    monkeypatch.setattr(lc, "_control_publisher", published)
    monkeypatch.setattr(lc, "registry", reg)
    return reg, published


def _control_fields(control) -> dict:
    """A control event as the stream carries it (what XREAD hands ``_fanout``)."""
    return {"payload": json.dumps(control), "scope": _bus().SCOPE_CONTROL, "agent_name": ""}


# ---------------------------------------------------------------------------
# The writer's side: re-check here, tell the others once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_change_rechecks_here_and_publishes_one_control_event(worker, people):
    reg, published = worker
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    _open(reg, DOOR_WS, BOB, closes.closer("bob"))
    people.update_user_role(ALICE, "operator")

    lc.notify_account_changed(ALICE)
    await _settle(reg)

    assert closes.by_name() == {"alice": CloseReason.CHANGED}
    assert published.events == [{"recheck": "account", "username": ALICE}]


@pytest.mark.asyncio
async def test_a_key_change_rechecks_here_and_publishes_one_key_scoped_control_event(worker, people):
    reg, published = worker
    key = _key(people, ALICE)
    closes = Closes()
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("stream"), key_id=key.id)
    assert people.revoke_mcp_api_key(key.id, ALICE)

    lc.notify_key_changed(key.id)
    await _settle(reg)

    assert closes.by_name() == {"stream": CloseReason.ENDED}
    assert published.events == [{"recheck": "key", "key_id": key.id}]


@pytest.mark.asyncio
async def test_a_control_event_rechecks_locally_and_is_never_published_again(people, monkeypatch):
    """The receiving side runs the local re-check only. Were it to call
    ``notify_*``, every worker would republish every event it read, forever."""
    reg = LiveConnectionRegistry()
    published = Published()
    monkeypatch.setattr(lc, "_control_publisher", published)
    key = _key(people, ALICE)
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    _open(reg, DOOR_EVENT_STREAM, BOB, closes.closer("bob-stream"), key_id=_key(people, BOB).id)
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("alice-stream"), key_id=key.id)
    people.update_user_role(ALICE, "operator")

    reg.handle_control({"recheck": "account", "username": ALICE})
    await _settle(reg)
    assert closes.by_name() == {"alice": CloseReason.CHANGED, "alice-stream": CloseReason.CHANGED}

    # A key nobody here opened a stream with: nothing to compare, nothing closes.
    reg.handle_control({"recheck": "key", "key_id": "ce-no-such-key"})
    await _settle(reg)
    assert set(closes.by_name()) == {"alice", "alice-stream"}
    assert published.events == [], "a received control event was published again"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [
        {"recheck": "account"},
        {"recheck": "account", "username": 7},
        {"recheck": "key", "key_id": ""},
        {"recheck": "role", "username": ALICE},
        {},
    ],
)
async def test_an_unrecognised_control_event_is_logged_and_closes_nothing(people, caplog, control):
    reg = LiveConnectionRegistry()
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    people.update_user_role(ALICE, "operator")
    with caplog.at_level(logging.WARNING):
        reg.handle_control(control)
        await _settle(reg)
    assert closes.calls == []
    assert any("control event" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_publishing_worker_closes_nothing_on_its_own_echo(worker, people):
    """Every worker reads the whole stream, the publisher included. By the time
    its own event comes back, the client it closed has reconnected at the new
    scope: that connection matches the account and stays open."""
    reg, published = worker
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("before"))
    people.update_user_role(ALICE, "operator")
    lc.notify_account_changed(ALICE)
    await _settle(reg)
    assert closes.by_name() == {"before": CloseReason.CHANGED}

    _open(reg, DOOR_WS, ALICE, closes.closer("reconnected"))
    [echo] = published.events
    reg.handle_control(echo)
    await _settle(reg)

    assert closes.by_name() == {"before": CloseReason.CHANGED}
    assert len(published.events) == 1


@pytest.mark.asyncio
async def test_a_publish_that_fails_still_rechecks_here_and_raises_nothing(monkeypatch, people, caplog):
    reg = LiveConnectionRegistry()

    async def failing(_control):
        raise ConnectionError("the bus refused the event")

    monkeypatch.setattr(lc, "_control_publisher", failing)
    monkeypatch.setattr(lc, "registry", reg)
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    people.update_user_role(ALICE, "operator")

    with caplog.at_level(logging.WARNING):
        lc.notify_account_changed(ALICE)  # must not raise into the writer
        await _settle(reg)

    assert closes.by_name() == {"alice": CloseReason.CHANGED}
    assert any("other workers" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_publish_into_a_bus_that_cannot_reach_its_stream_raises_nothing(monkeypatch, people):
    """The real ``EventBus.publish`` as the publisher, with no stream to write
    to: the writer is not held up, and this worker's re-check still ran."""
    bus = _bus().EventBus()

    async def no_stream():
        return None

    monkeypatch.setattr(bus, "_get_redis", no_stream)
    await bus.start()
    try:
        reg = LiveConnectionRegistry()
        monkeypatch.setattr(lc, "_control_publisher", lambda control: bus.publish(control, scope=_bus().SCOPE_CONTROL))
        monkeypatch.setattr(lc, "registry", reg)
        closes = Closes()
        _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
        people.update_user_role(ALICE, "operator")

        lc.notify_account_changed(ALICE)
        await _settle(reg)
        await asyncio.wait_for(bus._outbound.join(), timeout=2.0)
    finally:
        await bus.stop(drain_timeout=1.0)
    assert closes.by_name() == {"alice": CloseReason.CHANGED}


@pytest.mark.asyncio
async def test_notify_from_a_plain_thread_publishes_on_the_registrys_loop(worker, people):
    reg, published = worker
    loop_thread = threading.get_ident()
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice"))
    people.update_user_role(ALICE, "operator")

    caller = threading.Thread(target=lc.notify_account_changed, args=(ALICE,))
    caller.start()
    caller.join()
    assert published.events == [], "the thread published itself instead of handing it over"

    await _until(lambda: published.events and closes.calls)
    await _settle(reg)
    assert published.threads == [loop_thread]
    assert closes.by_name() == {"alice": CloseReason.CHANGED}


@pytest.mark.asyncio
async def test_a_worker_with_no_connections_still_tells_the_others(worker):
    """The connections to close may all be on another worker."""
    reg, published = worker
    await reg.start()
    try:
        lc.notify_account_changed(ALICE)
        await _settle(reg)
    finally:
        await reg.stop()
    assert published.events == [{"recheck": "account", "username": ALICE}]


# ---------------------------------------------------------------------------
# The dispatcher: control events go to the handler, never to a client
# ---------------------------------------------------------------------------


class _WS:
    async def close(self, *args, **kwargs):
        pass


async def _slots(dispatcher):
    """One slot of every kind; returns the payloads each one was sent."""
    eb = _bus()
    sent = {}

    async def register(name, scope, **kw):
        sent[name] = []

        async def send(payload):
            sent[name].append(payload)

        await dispatcher.register(_WS(), scope=scope, send_func=send, **kw)

    await register("admin-ws", eb.SCOPE_ALL, is_admin=True)
    await register("member-ws", eb.SCOPE_ALL, accessible_agents=["a"], email="m@example.test")
    await register("admin-events", eb.SCOPE_SCOPED, is_admin=True)
    await register("member-events", eb.SCOPE_SCOPED, accessible_agents=["a"])
    return sent


def _queued(dispatcher):
    return sum(slot.queue.qsize() for slot in dispatcher._clients.values())


@pytest.mark.asyncio
async def test_a_control_entry_goes_to_the_handler_and_reaches_no_client():
    eb = _bus()
    dispatcher = eb.StreamDispatcher()
    handled = []
    dispatcher.set_control_handler(handled.append)
    sent = await _slots(dispatcher)
    try:
        control = {"recheck": "account", "username": ALICE}
        await dispatcher._fanout("5-0", _control_fields(control))
        assert handled == [control]
        assert _queued(dispatcher) == 0

        # Delivery itself is untouched: an ordinary event still reaches the admins.
        await dispatcher._fanout(
            "6-0",
            {
                "payload": json.dumps({"type": "agent_started", "agent_name": "a"}),
                "scope": eb.SCOPE_ALL,
                "agent_name": "",
            },
        )
        await _until(lambda: sent["admin-ws"])
    finally:
        await dispatcher.stop()
    assert all(p.get("recheck") is None for payloads in sent.values() for p in payloads)


@pytest.mark.asyncio
async def test_a_control_entry_reaches_no_client_when_no_handler_is_wired():
    eb = _bus()
    dispatcher = eb.StreamDispatcher()
    await _slots(dispatcher)
    try:
        await dispatcher._fanout("5-0", _control_fields({"recheck": "account", "username": ALICE}))
        assert _queued(dispatcher) == 0
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_a_control_entry_does_not_trigger_a_roster_read():
    eb = _bus()
    dispatcher = eb.StreamDispatcher()
    reads = []
    dispatcher.set_accessible_resolver(lambda email: reads.append(email) or [])
    dispatcher.set_control_handler(lambda _payload: None)
    await _slots(dispatcher)
    try:
        await dispatcher._fanout(
            "5-0", _control_fields({"recheck": "account", "username": ALICE, "event": "agent_unshared"})
        )
        await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop()
    assert reads == []


@pytest.mark.asyncio
async def test_a_failing_control_handler_does_not_stop_delivery(caplog):
    eb = _bus()
    dispatcher = eb.StreamDispatcher()

    def broken(_payload):
        raise RuntimeError("handler broke")

    dispatcher.set_control_handler(broken)
    sent = await _slots(dispatcher)
    try:
        with caplog.at_level(logging.ERROR):
            await dispatcher._fanout("5-0", _control_fields({"recheck": "account", "username": ALICE}))
        await dispatcher._fanout(
            "6-0", {"payload": json.dumps({"type": "agent_started"}), "scope": eb.SCOPE_ALL, "agent_name": ""}
        )
        await _until(lambda: sent["admin-ws"])
    finally:
        await dispatcher.stop()
    assert any("control" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_no_slot_of_any_kind_may_see_a_control_entry():
    eb = _bus()
    for scope in (eb.SCOPE_ALL, eb.SCOPE_SCOPED):
        for is_admin in (True, False):
            slot = eb._ClientSlot(ws=None, scope=scope, send_func=None, is_admin=is_admin, accessible_agents={"a"})
            assert not eb._event_is_visible(slot, eb.SCOPE_CONTROL, "a", frozenset({"a"}), "agent_started")


class _ReplayRedis:
    """Just enough of a stream for ``_catchup``: one control entry between two
    ordinary ones."""

    def __init__(self, entries):
        self.entries = entries

    async def xrange(self, key, min="-", max="+", count=None):
        if min == "-":  # the stream's oldest entry: the cursor itself, so nothing was trimmed
            return [("9-0", {"payload": "{}", "scope": "all", "agent_name": ""})]
        return list(self.entries)


@pytest.mark.asyncio
async def test_a_replay_never_delivers_a_control_entry_nor_reruns_it():
    eb = _bus()
    dispatcher = eb.StreamDispatcher()
    handled = []
    dispatcher.set_control_handler(handled.append)
    entries = [
        ("10-0", {"payload": json.dumps({"type": "agent_started"}), "scope": eb.SCOPE_ALL, "agent_name": ""}),
        ("11-0", _control_fields({"recheck": "account", "username": ALICE})),
        ("12-0", {"payload": json.dumps({"type": "agent_stopped"}), "scope": eb.SCOPE_ALL, "agent_name": ""}),
    ]
    redis = _ReplayRedis(entries)

    async def get_redis():
        return redis

    dispatcher._get_redis = get_redis
    for scope in (eb.SCOPE_ALL, eb.SCOPE_SCOPED):
        slot = eb._ClientSlot(ws=None, scope=scope, send_func=None, is_admin=True)
        await dispatcher._catchup("c", slot, "9-0", None)
        replayed = []
        while not slot.queue.empty():
            replayed.append(slot.queue.get_nowait())
        expected = ["10-0", "12-0"] if scope == eb.SCOPE_ALL else []
        assert [eid for eid, _ in replayed] == expected
    assert handled == []


@pytest.mark.asyncio
async def test_two_workers_each_recheck_their_own_connections_from_one_control_entry(people):
    eb = _bus()
    regs = [LiveConnectionRegistry(), LiveConnectionRegistry()]
    dispatchers = [eb.StreamDispatcher(), eb.StreamDispatcher()]
    closes = Closes()
    for i, (reg, dispatcher) in enumerate(zip(regs, dispatchers, strict=True)):
        dispatcher.set_control_handler(reg.handle_control)
        _open(reg, DOOR_WS, ALICE, closes.closer(f"alice-{i}"))
        _open(reg, DOOR_WS, BOB, closes.closer(f"bob-{i}"))
    people.update_user_role(ALICE, "operator")

    fields = _control_fields({"recheck": "account", "username": ALICE})
    for dispatcher in dispatchers:
        await dispatcher._fanout("20-0", fields)
    await _settle(*regs)

    assert closes.by_name() == {"alice-0": CloseReason.CHANGED, "alice-1": CloseReason.CHANGED}


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_one_change_reaches_every_workers_connections_through_the_stream(people, monkeypatch):
    """Two workers over one stream, through the bus's real write and read:
    worker A serves the change; worker B closes its stale connection from the
    event alone, with no tick, and the event is published exactly once."""
    import fakeredis

    eb = _bus()
    server = fakeredis.FakeServer()

    def connect(owner):
        client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

        async def get_redis():
            owner._redis = client
            return client

        monkeypatch.setattr(owner, "_get_redis", get_redis)

    bus = eb.EventBus()
    connect(bus)
    workers = []
    publishes = []
    for _ in range(2):
        reg = LiveConnectionRegistry()
        dispatcher = eb.StreamDispatcher()
        connect(dispatcher)
        dispatcher._last_stream_id = "0-0"  # read from the start: no race with the first XADD
        dispatcher.set_control_handler(reg.handle_control)
        workers.append((reg, dispatcher))

    async def publish_on_a(control):
        publishes.append(control)
        await bus.publish(control, scope=eb.SCOPE_CONTROL)

    monkeypatch.setattr(lc, "_control_publisher", publish_on_a)
    monkeypatch.setattr(lc, "registry", workers[0][0])
    closes = Closes()
    _open(workers[0][0], DOOR_WS, ALICE, closes.closer("on-a"))
    _open(workers[1][0], DOOR_WS, ALICE, closes.closer("on-b"))
    _open(workers[1][0], DOOR_WS, BOB, closes.closer("bob-on-b"))

    await bus.start()
    for _reg, dispatcher in workers:
        await dispatcher.start()
    try:
        people.update_user_role(ALICE, "operator")
        lc.notify_account_changed(ALICE)
        await _until(lambda: len(closes.calls) == 2)
        await asyncio.sleep(0.2)  # room for a republish or a second close, were there one
        await _settle(*(reg for reg, _d in workers))
    finally:
        for _reg, dispatcher in workers:
            await dispatcher.stop()
        await bus.stop(drain_timeout=1.0)

    assert closes.by_name() == {"on-a": CloseReason.CHANGED, "on-b": CloseReason.CHANGED}
    assert publishes == [{"recheck": "account", "username": ALICE}]
    assert all(d.client_count() == 0 for _r, d in workers)


# ---------------------------------------------------------------------------
# The writers: key revoke / delete, password change
# ---------------------------------------------------------------------------


def _request(method="POST", path="/api/mcp/keys/x/revoke"):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "root_path": "",
            "query_string": b"",
            "scheme": "http",
            "server": ("backend", 8000),
            "client": ("127.0.0.1", 5000),
            "headers": [(b"host", b"backend:8000")],
        }
    )


@pytest.fixture
def key_routes(monkeypatch, people):
    import routers.mcp_keys as keys_mod
    from models import User

    async def no_audit(**_kw):
        return None

    monkeypatch.setattr(keys_mod.platform_audit_service, "log", no_audit)
    # The route's own module object, whatever another test re-imported.
    monkeypatch.setattr(keys_mod.live_connections, "registry", lc.registry)
    users = {ALICE: User(id=1, username=ALICE, role="creator"), ADMIN: User(id=3, username=ADMIN, role="admin")}

    async def revoke(key_id, actor=ALICE):
        return await keys_mod.revoke_mcp_api_key_endpoint(key_id, _request(), users[actor])

    async def delete(key_id, actor=ALICE):
        return await keys_mod.delete_mcp_api_key_endpoint(key_id, _request("DELETE"), users[actor])

    return revoke, delete


@pytest.mark.asyncio
async def test_revoking_or_deleting_a_key_closes_only_its_streams_on_every_worker(
    worker, people, key_routes, monkeypatch
):
    """The route re-checks by key, never by account: the owner's browser socket,
    open terminal and other key's stream stay open, here and on the other
    worker, even though the account itself is unchanged."""
    reg, published = worker
    other = LiveConnectionRegistry()
    revoke, delete = key_routes
    k1, k2 = _key(people, ALICE), _key(people, ALICE)
    closes = Closes()
    for name, r in (("here", reg), ("there", other)):
        _open(r, DOOR_WS, ALICE, closes.closer(f"{name}-ws"))
        _open(r, DOOR_TERMINAL, ALICE, closes.closer(f"{name}-terminal"))
        _open(r, DOOR_EVENT_STREAM, ALICE, closes.closer(f"{name}-k1"), key_id=k1.id)
        _open(r, DOOR_EVENT_STREAM, ALICE, closes.closer(f"{name}-k2"), key_id=k2.id)
    by_account = []
    monkeypatch.setattr(reg, "recheck_account", lambda u: by_account.append(u))

    await revoke(k1.id)
    await _settle(reg)
    for event in published.events:
        other.handle_control(event)
    await _settle(other)
    assert closes.by_name() == {"here-k1": CloseReason.ENDED, "there-k1": CloseReason.ENDED}

    await delete(k2.id)
    await _settle(reg)
    for event in published.events[1:]:
        other.handle_control(event)
    await _settle(other)
    assert set(closes.by_name()) == {"here-k1", "there-k1", "here-k2", "there-k2"}
    assert by_account == []
    assert published.events == [{"recheck": "key", "key_id": k1.id}, {"recheck": "key", "key_id": k2.id}]


@pytest.mark.asyncio
async def test_an_admin_revoking_another_persons_key_closes_that_persons_stream(worker, people, key_routes):
    """The admin, not the owner, serves the change, so only a key-scoped
    re-check reaches the owner's stream at once."""
    reg, _published = worker
    revoke, _delete = key_routes
    key = _key(people, BOB)
    closes = Closes()
    _open(reg, DOOR_EVENT_STREAM, BOB, closes.closer("bob-stream"), key_id=key.id)
    _open(reg, DOOR_WS, ADMIN, closes.closer("admin-ws"))

    await revoke(key.id, actor=ADMIN)
    await _settle(reg)
    assert closes.by_name() == {"bob-stream": CloseReason.ENDED}


@pytest.mark.asyncio
async def test_a_refused_key_revoke_closes_nothing_and_publishes_nothing(worker, people, key_routes):
    from fastapi import HTTPException

    reg, published = worker
    revoke, _delete = key_routes
    key = _key(people, BOB)
    closes = Closes()
    _open(reg, DOOR_EVENT_STREAM, BOB, closes.closer("bob-stream"), key_id=key.id)

    with pytest.raises(HTTPException) as refused:
        await revoke(key.id)  # ALICE does not own BOB's key
    assert refused.value.status_code == 404
    await _settle(reg)
    assert closes.calls == [] and published.events == []


@pytest.fixture
def password_change(monkeypatch, people):
    import fakeredis
    from passlib.context import CryptContext
    from services import password_service

    redis = fakeredis.FakeRedis()
    for scope in {
        id(g): g
        for g in (password_service.change_own_password.__globals__, password_service.revoke_user_sessions.__globals__)
    }.values():
        monkeypatch.setitem(scope, "get_breaker_redis", lambda: redis)
    monkeypatch.setattr(password_service.live_connections, "registry", lc.registry)
    people.update_user_password(ALICE, CryptContext(schemes=["bcrypt"]).hash(OLD_PASSWORD))

    def change(**kw):
        return password_service.change_own_password(ALICE, OLD_PASSWORD, NEW_PASSWORD, None, **kw)

    return change


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_keys, stream_reason", [(False, CloseReason.CHANGED), (True, CloseReason.ENDED)])
async def test_a_password_change_closes_the_accounts_connections_at_once_on_every_worker(
    worker, people, password_change, revoke_keys, stream_reason
):
    reg, published = worker
    key = _key(people, ALICE)
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice-ws"))
    _open(reg, DOOR_EVENT_STREAM, ALICE, closes.closer("alice-stream"), key_id=key.id)
    _open(reg, DOOR_WS, BOB, closes.closer("bob-ws"))

    password_change(revoke_keys=revoke_keys)
    await _settle(reg)

    assert closes.by_name() == {"alice-ws": CloseReason.CHANGED, "alice-stream": stream_reason}
    assert published.events == [{"recheck": "account", "username": ALICE}]


@pytest.mark.asyncio
async def test_a_refused_password_change_closes_nothing(worker, people, password_change):
    from services.password_service import PasswordChangeError

    reg, published = worker
    closes = Closes()
    _open(reg, DOOR_WS, ALICE, closes.closer("alice-ws"))
    with pytest.raises(PasswordChangeError):
        from services import password_service

        password_service.change_own_password(ALICE, "Wrong-Passw0rd-x!", NEW_PASSWORD, None)
    await _settle(reg)
    assert closes.calls == [] and published.events == []


# ---------------------------------------------------------------------------
# The wiring in main.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_wires_the_handler_into_the_dispatcher_and_publishes_through_the_bus(monkeypatch):
    import main

    assert main.stream_dispatcher._control_handler is main.live_connections.handle_control
    publisher = main.live_connections._control_publisher
    assert publisher is main._publish_live_connection_control, "main.py set no control publisher"
    assert "broadcast" not in getattr(publisher, "__name__", ""), (
        "a publisher named *broadcast* is collected by the /ws payload guard"
    )

    sent = []

    async def capture(event, scope="all", agent_name=None):
        sent.append((event, scope))

    monkeypatch.setattr(main.event_bus, "publish", capture)
    await publisher({"recheck": "account", "username": ALICE})
    assert sent == [({"recheck": "account", "username": ALICE}, main.SCOPE_CONTROL)]


def test_main_hands_control_events_to_the_registry_it_rechecks(monkeypatch):
    """``handle_control`` reaches the worker's registry as it is at call time."""
    reg = LiveConnectionRegistry()
    seen = []
    monkeypatch.setattr(reg, "recheck_account", seen.append)
    monkeypatch.setattr(lc, "registry", reg)
    lc.handle_control({"recheck": "account", "username": ALICE})
    assert seen == [ALICE]

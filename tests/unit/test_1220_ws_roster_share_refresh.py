"""A Workspace-only `/ws` slot that gains a share mid-connection receives that
agent's next allowed event (abilityai/trinity-enterprise#1220).

A SCOPE_ALL slot's roster is a connect-time snapshot, refreshed only when an
access-changing event (`agent_created` / `agent_deleted` / `agent_shared` /
`agent_unshared` / `agent_renamed`) comes off the stream (ent#467). Two
properties of that contract are pinned here, each through `_fanout`:

  * the share event is what heals the roster: an allowed event about an agent
    shared after the socket connected is withheld until `agent_shared` is read,
    and delivered (cut to the Workspace fields) right after it;
  * an invalidation that lands while the slot's refresh is already in flight is
    not swallowed. The in-flight read may have run before the share committed,
    so the dispatcher reads once more instead of skipping the slot and keeping
    the stale, smaller roster until something unrelated happens to refresh it.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from services import event_bus as event_bus_mod
from services.event_bus import SCOPE_ALL, StreamDispatcher
from services.ws_identity_service import (
    WORKSPACE_WS_EVENT_FIELDS,
    WORKSPACE_WS_EVENT_TYPES,
)

pytestmark = pytest.mark.unit

EMAIL = "person@example.com"


def _workspace_slot(agents=()):
    async def _send(_payload):  # pragma: no cover — never awaited here
        return None

    return event_bus_mod._ClientSlot(
        ws=object(), scope=SCOPE_ALL, send_func=_send, is_admin=False,
        accessible_agents=set(agents), email=EMAIL,
        allowed_types=WORKSPACE_WS_EVENT_TYPES,
        allowed_fields=WORKSPACE_WS_EVENT_FIELDS,
    )


def _fields(payload: dict) -> dict:
    return {"payload": json.dumps(payload), "scope": SCOPE_ALL, "agent_name": ""}


def _drain(slot) -> list:
    return [slot.queue.get_nowait()[1] for _ in range(slot.queue.qsize())]


async def _settle(dispatcher) -> None:
    """Await every roster refresh, including any a refresh spawns."""
    for _ in range(20):
        tasks = list(dispatcher._refresh_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks)
    raise AssertionError("roster refreshes never settled")


ACTIVITY = {"type": "agent_activity", "agent_name": "assistant", "activity_state": "completed",
            "details": {"execution_id": "e1", "prompt_preview": "secret"}}


@pytest.mark.asyncio
async def test_an_allowed_event_is_withheld_until_the_share_event_is_read():
    db_rows = {EMAIL: []}
    dispatcher = StreamDispatcher()
    slot = _workspace_slot()
    dispatcher._clients = {"tab": slot}
    dispatcher.set_accessible_resolver(lambda email: list(db_rows[email]))

    # The share row lands in the database with no event on the stream.
    db_rows[EMAIL] = ["assistant"]
    await dispatcher._fanout("1-1", _fields(ACTIVITY))
    assert _drain(slot) == [], "a silent share must not be visible yet — the roster is a snapshot"

    # The share announced: the next allowed event arrives, cut to the Workspace fields.
    await dispatcher._fanout("1-2", _fields({"event": "agent_shared", "data": {"name": "assistant"}}))
    await _settle(dispatcher)
    assert _drain(slot) == [], "the share event itself is operator UI, never delivered to this slot"

    await dispatcher._fanout("1-3", _fields(ACTIVITY))
    assert _drain(slot) == [{"type": "agent_activity", "agent_name": "assistant",
                             "activity_state": "completed", "_eid": "1-3"}]


@pytest.mark.asyncio
async def test_an_invalidation_during_an_in_flight_refresh_is_not_swallowed():
    """Two access changes close together (a bulk invite): the first refresh
    reads before the second share commits. Skipping the busy slot on the second
    event left the roster without the agent until an unrelated access change."""
    db_rows = {EMAIL: []}
    entered, release = threading.Event(), threading.Event()
    calls = []

    def _resolver(email):
        snapshot = list(db_rows[email])
        calls.append(snapshot)
        if len(calls) == 1:
            entered.set()
            release.wait(5)
        return snapshot

    dispatcher = StreamDispatcher()
    slot = _workspace_slot()
    dispatcher._clients = {"tab": slot}
    dispatcher.set_accessible_resolver(_resolver)

    await dispatcher._fanout("1-1", _fields({"event": "agent_created", "data": {"name": "other"}}))
    assert await asyncio.to_thread(entered.wait, 5), "the first refresh never started"

    # The first read has taken its snapshot; now the share commits and is announced.
    db_rows[EMAIL] = ["assistant"]
    await dispatcher._fanout("1-2", _fields({"event": "agent_shared", "data": {"name": "assistant"}}))
    release.set()
    await _settle(dispatcher)

    assert slot.accessible_agents == {"assistant"}
    assert slot.refreshing is False
    assert len(calls) == 2, "one more read after the in-flight one, not one per event"

    await dispatcher._fanout("1-3", _fields(ACTIVITY))
    assert [p["agent_name"] for p in _drain(slot)] == ["assistant"]


@pytest.mark.asyncio
async def test_a_quiet_refresh_reads_once():
    """The refresh-again flag only fires on an event that arrived mid-read."""
    calls = []
    dispatcher = StreamDispatcher()
    dispatcher._clients = {"tab": _workspace_slot()}
    dispatcher.set_accessible_resolver(lambda email: calls.append(email) or ["assistant"])

    await dispatcher._fanout("1-1", _fields({"event": "agent_shared", "data": {"name": "assistant"}}))
    await _settle(dispatcher)

    assert calls == [EMAIL]

"""A room turn is identifiable on its execution row, and nothing downstream
mistakes it for a 1:1 Workspace thread (#2792).

The bug: a Workspace room inlined EVERY live execution of a working participant
as a room work card — a schedule run, a loop turn, a 1:1 thread, another room —
because the room turn stamped no source chat, so the room could only filter the
Work feed by agent name. The 1:1 thread stamps `source_channel_chat_id =
session_id`; the room now stamps `source_channel_chat_id = room_id` under its
OWN channel value, `room`.

Why its own value and not `portal` (the plan review's finding): every reader of
`portal` branches on "this is a 1:1 Workspace thread". Reusing it would have
sent every room terminal into `_resolve_portal` (an unscoped session lookup
that can only fail, logging "session no longer exists"), and the voice-reply
route would have told a room's DELEGATED child — `triggered_by="mcp"` carrying
the inherited stamp — that "the client hears your reply when they switch on
the speaker control", which a room does not have (the #2157 false-claim class).
With `room` in neither map, parent and child are suppressed by construction.

Suites:
  1. the stamp — `_wake_agent` kwargs, on a first wake and a chained one
  2. no report-back leg — `report_completion` never resolves a room row
  3. no voice destination — the route answers `not_a_channel_turn` for both
  4. the projection — `chat_id` is the room, for the turn and its child
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

ROOM = "room-1"
EMAIL = "client@example.com"
PARTICIPANT = "researcher"
DELEGATE = "writer"


# ---------------------------------------------------------------------------
# 1. The stamp
# ---------------------------------------------------------------------------

def _wake_capture(monkeypatch, principal, *, depth=0):
    """Drive `_wake_agent` far enough to capture the execute_task kwargs
    (the `test_ent363_user_facing_room.py` shape)."""
    from shared_sessions import service

    captured = {}

    class _Result:
        status, response, error = "success", "ok", None
        execution_id = session_id = None

    class _Svc:
        async def execute_task(self, **kwargs):
            captured.update(kwargs)
            return _Result()

    monkeypatch.setattr(service.db, "get_participant",
                        lambda *a, **k: {"last_read_seq": 0, "cached_session_id": "s1"})
    monkeypatch.setattr(service.db, "get_room",
                        lambda *a, **k: {"id": ROOM, "name": "Room", "status": "open",
                                         "topic": None})
    monkeypatch.setattr(service.db, "get_messages", lambda *a, **k: [
        {"seq": 1, "sender_kind": "user", "sender_identity": EMAIL,
         "content": f"@{PARTICIPANT} hi", "kind": "message"}])
    monkeypatch.setattr(service.db, "list_participants", lambda _room_id: [
        {"kind": "user", "identity": EMAIL, "left_at": None},
        {"kind": "agent", "identity": PARTICIPANT, "left_at": None},
    ])
    monkeypatch.setattr(service, "_mark_agent_working", lambda *a, **k: None)
    monkeypatch.setattr(service, "_clear_agent_working", lambda *a, **k: None)
    monkeypatch.setattr(service, "_broadcast", lambda *a, **k: None)
    monkeypatch.setattr(service, "_post_system", lambda *a, **k: None)
    monkeypatch.setattr(service.db, "advance_read_cursor", lambda *a, **k: None)

    async def _noop_post(*a, **k):
        return {}
    monkeypatch.setattr(service, "post_message", _noop_post)

    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: _Svc())

    asyncio.run(service._wake_agent(principal, ROOM, PARTICIPANT, depth))
    return captured


def test_room_turn_is_stamped_with_the_room_as_its_source_chat(monkeypatch):
    """AC #1 — the same shape as the 1:1 thread's stamp, with the room as the chat."""
    captured = _wake_capture(monkeypatch, types.SimpleNamespace(email=EMAIL))
    assert captured["triggered_by"] == "room"
    assert captured.get("source_channel") == "room"
    assert captured.get("source_channel_chat_id") == ROOM
    # The client the context belongs to, the same expression as `source_user_email`.
    assert captured.get("source_channel_client") == EMAIL
    assert captured.get("source_user_email") == EMAIL


def test_a_chained_wake_carries_the_same_stamp(monkeypatch):
    """An @mention chain re-enters `_wake_agent` at depth+1 with the ORIGINAL
    principal; every room turn is a room turn."""
    captured = _wake_capture(monkeypatch, types.SimpleNamespace(email=EMAIL), depth=1)
    assert captured.get("source_channel") == "room"
    assert captured.get("source_channel_chat_id") == ROOM
    assert captured.get("source_channel_client") == EMAIL


def test_a_principal_without_an_email_stamps_no_client(monkeypatch):
    """Fail closed the way `source_user_email` already does — never a fabricated
    recipient."""
    captured = _wake_capture(monkeypatch, object())
    assert captured.get("source_channel") == "room"
    assert captured.get("source_channel_client") is None


def test_the_room_channel_is_its_own_value():
    """The stamp is read from `config` (one home), and it is NOT the portal value —
    the whole point, see the module docstring."""
    import config
    assert config.ROOM_SOURCE_CHANNEL == "room"
    assert config.ROOM_SOURCE_CHANNEL != config.PORTAL_SOURCE_CHANNEL


# ---------------------------------------------------------------------------
# 2. No report-back leg — parent AND child
# ---------------------------------------------------------------------------

def _execution(**over):
    row = types.SimpleNamespace(
        id="exec-1", agent_name=PARTICIPANT, status="success",
        triggered_by="room", source_channel="room", source_channel_chat_id=ROOM,
        source_channel_thread=None, source_channel_agent=None,
        source_channel_client=EMAIL,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


@pytest.mark.parametrize("triggered_by, agent", [
    ("room", PARTICIPANT),   # the room's own turn: it posts its reply itself
    ("mcp", DELEGATE),       # a delegated child, inheriting the stamp (ent#265 D0)
])
def test_report_completion_never_resolves_a_room_row(monkeypatch, triggered_by, agent):
    """A property that holds by construction — `room` has no resolver — pinned
    so that adding one later is a deliberate act with a test to update, and so
    that the inline-trigger set is never asked to cover rooms (it would cover
    the parent only). Every EXISTING resolver is replaced by a spy: if a room
    row ever reached one, the spy records it."""
    from services import channel_completion_report as ccr
    import database

    monkeypatch.setattr(database.db, "get_execution",
                        lambda eid: _execution(triggered_by=triggered_by, agent_name=agent))
    reached = []

    def _spy(**kw):
        reached.append(kw)
        return None

    monkeypatch.setattr(ccr, "_CHANNEL_RESOLVERS", {k: _spy for k in ccr._CHANNEL_RESOLVERS})

    delivered = asyncio.run(ccr.report_completion(
        execution_id="exec-1", agent_name=agent, status="success", summary_or_error="done",
    ))
    assert delivered is False
    assert reached == []
    assert "room" not in ccr.SUPPORTED_CHANNELS


# ---------------------------------------------------------------------------
# 3. No voice destination — parent AND child
# ---------------------------------------------------------------------------

async def _call_voice_reply(execution, *, agent):
    import routers.agents as ar
    from models import VoiceReplyRequest

    db_mock = MagicMock()
    db_mock.get_execution.return_value = execution
    user = types.SimpleNamespace(agent_name=agent, id=1, username="u", role="admin")
    with patch("routers.agents.db", db_mock):
        return await ar.send_voice_reply_endpoint(
            agent, VoiceReplyRequest(text="hello", execution_id="exec-1"), user,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("triggered_by, agent", [("room", PARTICIPANT), ("mcp", DELEGATE)])
async def test_voice_reply_on_a_room_row_is_not_a_channel_turn(triggered_by, agent):
    """Rooms have no speaker control, so the portal's `portal_client_narrated`
    guidance would be a false claim here — for the turn, and for the delegated
    child the portal branch would otherwise catch by its inherited stamp."""
    res = await _call_voice_reply(_execution(triggered_by=triggered_by, agent_name=agent), agent=agent)
    assert res["delivered"] is False
    assert res["reason"] == "not_a_channel_turn"


# ---------------------------------------------------------------------------
# 4. The projection: `chat_id` is the room
# ---------------------------------------------------------------------------

def _work_row(**over):
    from datetime import datetime, timedelta, timezone
    started = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = dict(
        id="exec-1", agent_name=PARTICIPANT, status="running", started_at=started,
        completed_at=None, duration_ms=None, message="Compare the two drafts",
        triggered_by="room", source_user_email=EMAIL, source_agent_name=None,
        source_channel="room", source_channel_chat_id=ROOM, loop_id=None,
        error_summary=None,
    )
    base.update(over)
    return base


def test_a_room_turn_projects_the_room_as_its_chat(monkeypatch):
    """The join key the room renders on (AC #2): `WorkItem.chat_id` is the room
    id for a room-stamped row, exactly as it is the session id for a portal one."""
    from datetime import datetime, timezone
    from client_portal.work import service as svc

    now = datetime.now(timezone.utc)
    roster = {PARTICIPANT, DELEGATE}
    turn = svc._project(_work_row(), email=EMAIL, roster=roster, turn_timeout=3600, now=now)
    assert turn.kind == "room"
    assert turn.chat_id == ROOM


def test_a_room_turns_delegated_child_projects_the_room_and_is_delegated():
    """The child inherits the stamp and carries `triggered_by="mcp"`; it is
    `delegated` (not `other`) and joins the room by the same key, so the Work
    tab groups it under its parent via `childrenForChat`."""
    from datetime import datetime, timezone
    from client_portal.work import service as svc

    now = datetime.now(timezone.utc)
    roster = {PARTICIPANT, DELEGATE}
    child = svc._project(
        _work_row(id="exec-2", agent_name=DELEGATE, triggered_by="mcp",
                  source_agent_name=PARTICIPANT),
        email=EMAIL, roster=roster, turn_timeout=3600, now=now,
    )
    assert child.kind == "delegated"
    assert child.chat_id == ROOM
    assert child.delegated_by == PARTICIPANT


def test_a_channel_stamp_is_still_not_a_chat_the_client_can_open():
    """The gate widened from `portal` to `portal | room`; a Telegram destination
    is still nobody's business (the ent#525 rule, unchanged)."""
    from datetime import datetime, timezone
    from client_portal.work import service as svc

    now = datetime.now(timezone.utc)
    tg = svc._project(
        _work_row(source_channel="telegram", source_channel_chat_id="12345", triggered_by="mcp"),
        email=EMAIL, roster={PARTICIPANT}, turn_timeout=3600, now=now,
    )
    assert tg.chat_id is None
    assert tg.kind == "other"

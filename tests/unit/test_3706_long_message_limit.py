"""#3706 — a person can paste a long reference document into a room or a
Workspace chat.

The inbound cap is 100,000 characters on both surfaces, the agent reply bound
(`MAX_AGENT_REPLY_CHARS`). 13,661 is the reported document. `ROOM_TRANSCRIPT_BUDGET_CHARS` bounds the cold-wake
transcript whatever one message may hold.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_3210_room_oversized_reply import (  # noqa: E402,F401 — the shared world
    _mk_room, _run, _user, allow_all, fake_execute, rooms_db,
)

LIMIT = 100_000
REPORTED = 13_661


def test_the_room_model_and_service_cap_agree():
    from shared_sessions import service
    from shared_sessions.models import RoomMessageCreate

    assert service.MAX_CONTENT_CHARS == LIMIT
    RoomMessageCreate(content="x" * LIMIT)
    with pytest.raises(ValidationError):
        RoomMessageCreate(content="x" * (LIMIT + 1))


def test_the_workspace_chat_model_takes_the_same_limit():
    from client_portal.models import PortalChatRequest

    PortalChatRequest(message="x" * LIMIT)
    with pytest.raises(ValidationError):
        PortalChatRequest(message="x" * (LIMIT + 1))


@pytest.mark.parametrize("length", [REPORTED, LIMIT])
def test_a_long_paste_lands_in_the_room(rooms_db, allow_all, fake_execute, length):
    from shared_sessions import db, service

    room = _mk_room()
    _run(service.post_message(_user(), room["id"], "y" * length))
    assert [len(m["content"]) for m in db.get_messages(room["id"])
            if m["sender_kind"] == "user"] == [length]


def test_one_character_over_is_refused(rooms_db, allow_all, fake_execute):
    from shared_sessions import service

    room = _mk_room()
    with pytest.raises(service.RoomError) as e:
        _run(service.post_message(_user(), room["id"], "y" * (LIMIT + 1)))
    assert e.value.status_code == 413 and e.value.code == "message_too_large"


def test_a_cold_wake_of_full_size_messages_stays_within_the_budget():
    """Thirty messages at the cap is 3,000,000 characters; the agent is shown
    at most the transcript budget, its own ask and the newest message kept."""
    from shared_sessions import service

    biggest = max(service.MAX_CONTENT_CHARS, service.MAX_AGENT_REPLY_CHARS)
    assert 2 * biggest < service.ROOM_TRANSCRIPT_BUDGET_CHARS
    msgs = [{"seq": i, "sender_kind": "user", "sender_identity": "alice",
             "content": "z" * LIMIT, "mentions": ["agent-a"] if i == 3 else []}
            for i in range(service.ROOM_COLD_CONTEXT_MESSAGES)]
    shown = service._fit_transcript(msgs, "agent-a")
    assert len(service._format_delta(shown)) <= service.ROOM_TRANSCRIPT_BUDGET_CHARS
    kept = [m["seq"] for m in shown if m.get("seq") is not None]
    assert 3 in kept and msgs[-1]["seq"] in kept

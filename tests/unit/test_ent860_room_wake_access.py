"""trinity-enterprise#860 — a room re-checks the sender's access to an agent
before every wake, for every kind of principal.

Membership in a room was the grant: room creation and mention-joins check that
the sender can reach each agent, but the wake-time re-check ran only for
Workspace principals. A platform member who lost access to an agent could keep
waking it in a room they already had, for as long as the room lived.

Now `_after_landing` asks `_can_still_reach` — the rule room creation applies
(`_assert_can_reach_agent`: the portal roster for a Workspace principal, the
platform ACL for everyone else) — and an agent that fails it is not woken; the
room gets the same "could not be reached" line it always did.

Driven through the real `_after_landing` with the wake, the wake cap and the
system-line writer stubbed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent860.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent860-logs"))

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

ROOM = "room-860"


@pytest.fixture
def room(monkeypatch):
    """`_after_landing` with its side effects captured, and a switchable ACL."""
    from fastapi import HTTPException

    import dependencies
    from client_portal import service as portal
    from shared_sessions import service as rooms

    state = {"woken": [], "lines": [], "platform_access": {"atlas", "scout"},
             "roster": {"atlas", "scout"}}

    async def _wake(current_user, room_id, agent_name, depth):
        state["woken"].append(agent_name)

    def _assert_access(user, agent_name, *a, **k):
        if agent_name not in state["platform_access"]:
            raise HTTPException(status_code=403, detail="no access")

    monkeypatch.setattr(rooms, "_wake_agent", _wake)
    monkeypatch.setattr(rooms, "_apply_wake_cap", lambda room_id, kind, ident, targets: targets)
    monkeypatch.setattr(rooms, "_post_system", lambda room_id, text: state["lines"].append(text))
    monkeypatch.setattr(rooms, "_budget_exceeded_reason", lambda room: None)
    monkeypatch.setattr(dependencies, "assert_agent_access", _assert_access)
    monkeypatch.setattr(rooms.db, "agent_exists", lambda name: True)
    monkeypatch.setattr(portal, "agent_on_roster",
                        lambda agent, email, include_owned: agent in state["roster"])

    def land(user, mentions, *, is_agent_reply=False, sender=("user", "bob")):
        woke = asyncio.run(rooms._after_landing(
            user, ROOM, {"id": ROOM}, sender[0], sender[1], mentions,
            is_agent_reply, 0))
        return woke

    return SimpleNamespace(state=state, land=land, rooms=rooms)


PLATFORM = SimpleNamespace(id=7, username="bob", email="bob@example.com", role="operator",
                           agent_name=None, is_portal=False)
PORTAL = SimpleNamespace(email="client@example.com", is_portal=True, is_platform=False, agent_name=None)


def test_a_platform_member_still_shared_wakes_the_agent(room):
    assert room.land(PLATFORM, ["atlas"]) == ["atlas"]
    assert room.state["woken"] == ["atlas"] and room.state["lines"] == []


def test_a_platform_member_whose_share_was_revoked_does_not_wake_it(room):
    room.state["platform_access"].discard("atlas")
    assert room.land(PLATFORM, ["atlas", "scout"]) == ["scout"]
    assert room.state["woken"] == ["scout"]
    assert room.state["lines"] == ["atlas could not be reached (no longer shared with the sender)."]


def test_a_workspace_principal_is_still_held_to_its_roster(room):
    room.state["roster"].discard("scout")
    assert room.land(PORTAL, ["atlas", "scout"], sender=("workspace", "client@example.com")) == ["atlas"]
    assert room.state["woken"] == ["atlas"]
    assert "scout could not be reached" in room.state["lines"][0]


def test_the_workspace_check_reads_the_roster_not_the_platform_acl(room):
    """A Workspace principal has no `users` row; the platform ACL must not decide."""
    room.state["platform_access"].clear()
    assert room.land(PORTAL, ["atlas"], sender=("workspace", "client@example.com")) == ["atlas"]


def test_an_agent_reply_is_held_to_the_access_of_the_person_whose_turn_started_it(room):
    """The chain spends agents' time on that person's behalf."""
    room.state["platform_access"].discard("scout")
    woke = room.land(PLATFORM, ["scout"], is_agent_reply=True, sender=("agent", "atlas"))
    assert woke == [] and room.state["woken"] == []


def test_an_agent_that_no_longer_exists_is_not_woken(room, monkeypatch):
    monkeypatch.setattr(room.rooms.db, "agent_exists", lambda name: name != "atlas")
    assert room.land(PLATFORM, ["atlas"]) == []

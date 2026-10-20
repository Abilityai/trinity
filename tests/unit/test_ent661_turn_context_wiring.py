"""The turn-context seam reaches both Workspace composers (ent#661).

- A room turn: the composed message that reaches `execute_task` STARTS with the
  provider's line, and the provider is told whether the audience is internal —
  the room's own `room_is_user_facing` verdict, never a participant's claim.
- A 1:1 chat turn: the line rides both arms (resumed and cold), like the open
  canvas does, because the session's memory of it would otherwise go stale.
"""
import inspect

import pytest

from unit.test_2794_room_file_awareness import _WakeHarness, ROOM, AGENT, EMAIL

pytestmark = pytest.mark.unit


@pytest.fixture
def rooms():
    from shared_sessions import service as mod
    return mod


@pytest.fixture(autouse=True)
def _clean():
    from services import turn_context
    turn_context.clear_providers()
    yield
    turn_context.clear_providers()


def _record(seen, line="[Project] in project X"):
    def provider(ctx):
        seen.append(ctx)
        return line
    return provider


def test_room_turn_leads_with_the_line(monkeypatch, rooms):
    from services import turn_context
    seen = []
    turn_context.register_provider(_record(seen))
    h = _WakeHarness(monkeypatch, rooms, context=("[Client Portal] file\n\n", []))
    kw = h.run(rooms)
    assert kw["message"].startswith("[Project] in project X\n\n[Client Portal] file")
    ctx = seen[0]
    assert (ctx.surface, ctx.chat_id, ctx.agent_name, ctx.person_email) == ("room", ROOM, AGENT, EMAIL)
    assert ctx.internal_audience is True


def test_room_with_a_client_is_not_an_internal_audience(monkeypatch, rooms):
    from services import turn_context
    seen = []
    turn_context.register_provider(_record(seen, ""))
    h = _WakeHarness(monkeypatch, rooms)
    monkeypatch.setattr(rooms, "room_is_user_facing", lambda *a, **k: True)
    kw = h.run(rooms)
    assert seen[0].internal_audience is False
    assert kw["message"].startswith("You are participating in the Trinity room")


def test_room_without_providers_is_unchanged(monkeypatch, rooms):
    h = _WakeHarness(monkeypatch, rooms, context=("", []))
    kw = h.run(rooms)
    assert kw["message"].startswith("You are participating in the Trinity room")


def test_chat_turn_carries_the_line_on_both_arms():
    from client_portal import service
    src = inspect.getsource(service.portal_chat)
    assert "turn_context.collect(" in src
    assert "(delta_prefix + turn_prefix + canvas_prefix + manifest_prefix + reply_prefix + message) if resuming" in src
    assert "cold_message = history_prefix + turn_prefix + canvas_prefix + manifest_prefix + reply_prefix + message" in src


def test_chat_turn_derives_the_audience_from_the_principal_not_the_request():
    from client_portal import service
    src = inspect.getsource(service.portal_chat)
    assert 'surface="thread"' in src
    assert "internal_audience=bool(include_owned)" in src

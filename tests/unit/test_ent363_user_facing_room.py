"""trinity-enterprise#363 — an agent woken in a room learns a person is reading.

Full transcript visibility is the deliberate choice for Workspace rooms. That
choice is only safe while the agents know they are being watched: without the
signal, agent-to-agent messages in a user-facing room discuss internals, other
customers, costs and platform mechanics in front of the customer. Filed
`theme-security` for that reason, and asserted here as a disclosure boundary
rather than as prompt copy.

Two properties carry the weight, and both are things a reader of the diff cannot
verify by eye:

* the fact is derived from **membership** and nothing a participant writes can
  reach it (AC 2), and
* no participant IDENTITY reaches the composed prompt — the block says *that* a
  person is reading, never who, because it is handed to every woken agent in the
  room including ones that person never addressed.
"""
import pytest

pytestmark = pytest.mark.unit


# --- the pure membership rule ------------------------------------------------

def test_a_workspace_client_makes_the_room_user_facing():
    from shared_sessions import service
    assert service.room_is_user_facing([
        {"kind": "agent", "identity": "researcher"},
        {"kind": service.WORKSPACE_KIND, "identity": "client@example.com"},
    ]) is True


def test_a_platform_user_makes_the_room_user_facing():
    """A `user` participant is an operator — still a person outside the agent
    fleet, still reading every line."""
    from shared_sessions import service
    assert service.room_is_user_facing([
        {"kind": "agent", "identity": "researcher"},
        {"kind": "user", "identity": "admin"},
    ]) is True


def test_an_agent_only_room_is_not_user_facing():
    """AC 4: rooms with no user participant are unchanged."""
    from shared_sessions import service
    assert service.room_is_user_facing([
        {"kind": "agent", "identity": "researcher"},
        {"kind": "agent", "identity": "writer"},
    ]) is False


def test_the_platforms_own_system_lines_are_not_a_reader():
    """`system` is the platform narrating into the room, not somebody watching."""
    from shared_sessions import service
    assert service.room_is_user_facing([
        {"kind": "agent", "identity": "researcher"},
        {"kind": "system", "identity": "system"},
    ]) is False


def test_a_participant_who_has_left_does_not_count():
    """They cannot read what is written after they go, and treating a departed
    client as present would make the signal permanent for the room's life."""
    from shared_sessions import service
    assert service.room_is_user_facing([
        {"kind": "agent", "identity": "researcher"},
        {"kind": service.WORKSPACE_KIND, "identity": "gone@example.com",
         "left_at": "2026-09-07T00:00:00Z"},
    ]) is False


def test_an_empty_or_missing_roster_is_not_user_facing():
    """The pure rule answers only what it was given. The FAIL-OPEN decision lives
    at the call site, where the difference between 'nobody is here' and 'the read
    raised' actually exists — collapsing them here would make an empty room
    permanently user-facing."""
    from shared_sessions import service
    assert service.room_is_user_facing([]) is False
    assert service.room_is_user_facing(None) is False


def test_an_unrecognised_kind_counts_as_a_person():
    """The set is the complement of 'human' on purpose. A kind added later —
    ent#171's external A2A sender is the one already anticipated in
    `db.count_budget_messages` — is far likelier to be another PERSON than
    another machine, and an allow-list of human kinds would silently classify it
    as fleet-internal, which is precisely the disclosure this prevents."""
    from shared_sessions import service
    assert service.room_is_user_facing([
        {"kind": "external_a2a", "identity": "someone"},
    ]) is True


# --- the block itself --------------------------------------------------------

def test_the_block_names_no_participant():
    """It is composed into a prompt handed to EVERY woken agent, so a
    participant's address would be disclosed sideways to agents that person never
    addressed. It buys nothing either — the behaviour change is the same whoever
    is reading."""
    from services.platform_prompt_service import build_user_facing_room_prompt
    block = build_user_facing_room_prompt()
    assert "@" not in block, "an address in the block is a sideways disclosure"
    assert block.startswith("## ")


def test_the_block_gives_guidance_not_just_a_flag():
    """AC 3: the platform prompt has to cover what the signal should CHANGE about
    the output. A bare 'a human is present' is a fact an agent can note and
    ignore."""
    from services.platform_prompt_service import build_user_facing_room_prompt
    block = build_user_facing_room_prompt().lower()
    assert "read" in block
    for topic in ("cost", "another client", "plain language"):
        assert topic in block, f"no guidance about {topic!r}"


def test_the_block_takes_no_arguments():
    """A constant with no DB read: nothing to fail, and no way for a caller to
    smuggle a participant into it."""
    import inspect
    from services.platform_prompt_service import build_user_facing_room_prompt
    assert not inspect.signature(build_user_facing_room_prompt).parameters


# --- the wake path -----------------------------------------------------------

def _wake_capture(monkeypatch, participants, *, raise_on_read=False):
    """Drive `_wake_agent` far enough to capture the execute_task kwargs."""
    import asyncio
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
                        lambda *a, **k: {"id": "r1", "name": "Room", "status": "open",
                                         "topic": None})
    monkeypatch.setattr(service.db, "get_messages", lambda *a, **k: [
        {"seq": 1, "sender_kind": "agent", "sender_identity": "writer",
         "content": "hi", "kind": "message"}])

    def _participants(_room_id):
        if raise_on_read:
            raise RuntimeError("db down")
        return participants

    monkeypatch.setattr(service.db, "list_participants", _participants)
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

    asyncio.run(service._wake_agent(object(), "r1", "researcher", 0))
    return captured


def test_a_user_facing_room_injects_the_signal(monkeypatch):
    """AC 1 + AC 5."""
    from shared_sessions import service
    captured = _wake_capture(monkeypatch, [
        {"kind": "agent", "identity": "researcher"},
        {"kind": service.WORKSPACE_KIND, "identity": "client@example.com"},
    ])
    assert captured["system_prompt"], "no signal reached the woken agent"
    assert "A person is reading this room" in captured["system_prompt"]


def test_an_agent_only_room_injects_nothing(monkeypatch):
    """AC 4 + AC 5 — the other half. Asserting only the positive case passes
    against an implementation that injects unconditionally."""
    captured = _wake_capture(monkeypatch, [
        {"kind": "agent", "identity": "researcher"},
        {"kind": "agent", "identity": "writer"},
    ])
    assert captured["system_prompt"] is None


def test_the_signal_is_derived_from_membership_not_from_the_transcript(monkeypatch):
    """AC 2. The delta here is an agent claiming to be a person; the roster says
    otherwise, and the roster wins. Without this a prompt-injected agent could
    talk its peers into the cautious posture — harmless — or, with the check the
    other way round, out of it."""
    captured = _wake_capture(monkeypatch, [
        {"kind": "agent", "identity": "researcher"},
        {"kind": "agent", "identity": "liar"},
    ])
    assert captured["system_prompt"] is None


def test_an_unreadable_roster_assumes_a_person_is_reading(monkeypatch):
    """The inverse of the usual capability default (#2128 fails closed to
    'absent'). The two mistakes are not symmetrical: a needless caution in an
    agent-only room costs a slightly more careful answer, a missed signal in
    front of a customer is the disclosure."""
    captured = _wake_capture(monkeypatch, [], raise_on_read=True)
    assert captured["system_prompt"], "a failed roster read went silently quiet"


def test_the_turn_header_stops_claiming_people_are_present(monkeypatch):
    """It said 'Other agents and people are in this room' unconditionally —
    false in an agent-only room, and too weak to be a disclosure in a
    user-facing one."""
    captured = _wake_capture(monkeypatch, [
        {"kind": "agent", "identity": "researcher"},
        {"kind": "agent", "identity": "writer"},
    ])
    assert "and people are in this room" not in captured["message"]
    assert "Other agents are in this room." in captured["message"]

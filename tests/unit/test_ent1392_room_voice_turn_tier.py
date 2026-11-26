"""A room turn and a voice tool turn are rated by the person they are for
(abilityai/trinity-enterprise#1392).

The ent#926 rule: a turn for a person the agent is only shared with never gets
the shell. Two doors went around it.

The room. A room turn's row carried the poster's email and no user id, and the
turn-tier rule read a row with no user id as a visitor. Visitors get the shell
while `visitor_shell` is on (the default), so a shared person who opened a room
with the agent got the shell their own chat turn is refused, and the owner got
a visitor's limits in their own room.

The voice tool. The Agent Detail voice session and a phone call sent their tool
turn to the agent with no execution id. The agent made one up, the platform had
no row for it, and while the managed hooks are skipped (ent#1176) the refusal
for a turn that may not hold the shell returns early for a turn with no id.

Suites:
  1. the rule: a room row is rated by its person, never as a visitor
  2. the stamp: which person each kind of room principal puts on the row
  3. the wake: the row a room turn creates is rated as the poster
  4. the voice tool turn runs under a row that names the caller
  5. no backend code sends an agent a task without an execution id
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

from _turn_tier_world import ADMIN_ID, AGENT, MATE_ID, OWNER_ID, USERS, World  # noqa: E402
from services import turn_tier_service as tts  # noqa: E402

pytestmark = pytest.mark.unit

OWNER_EMAIL, ADMIN_EMAIL, MATE_EMAIL = USERS[OWNER_ID][3], USERS[ADMIN_ID][3], USERS[MATE_ID][3]
ROOM = "room-1"


def _room_row(*, user_id=None, email=None, trigger="room", channel="room", agent=AGENT):
    return {"id": "e1", "agent_name": agent, "source_user_id": user_id, "schedule_id": None,
            "triggered_by": trigger, "source_channel": channel, "source_user_email": email,
            "source_agent_name": None, "continues_execution_id": None}


def _actor(row, **world):
    return tts.turn_actor(World(**world), AGENT, execution=row)


# ---------------------------------------------------------------------------
# 1. The rule
# ---------------------------------------------------------------------------

class TestARoomRowIsRatedByItsPerson:
    def test_a_shared_person_is_a_person_not_a_visitor(self):
        """The door: this row was `share / visitor`, and a visitor gets the shell."""
        assert _actor(_room_row(user_id=MATE_ID, email=MATE_EMAIL)) == ("share", "person", MATE_ID)

    def test_a_shared_person_gets_no_shell_with_visitor_shell_on(self):
        world = World(visitor_shell=True)
        tier, kind, _ = tts.turn_actor(world, AGENT, execution=_room_row(user_id=MATE_ID))
        assert tts.shell_allowed(world, AGENT, tier, kind) is False
        assert tts.restricts_standing_work(world, AGENT, tier, kind) is True

    @pytest.mark.parametrize("user_id", [OWNER_ID, ADMIN_ID])
    def test_the_owner_and_an_admin_are_owner_tier(self, user_id):
        assert _actor(_room_row(user_id=user_id)) == ("owner", None, None)

    def test_the_owner_keeps_the_shell_when_visitor_shell_is_off(self):
        """The other half of the bug: in a room the owner was a visitor too."""
        world = World(visitor_shell=False)
        tier, kind, _ = tts.turn_actor(world, AGENT, execution=_room_row(user_id=OWNER_ID))
        assert tts.shell_allowed(world, AGENT, tier, kind) is True

    def test_an_email_only_row_is_rated_by_that_email_s_account(self):
        assert _actor(_room_row(email=MATE_EMAIL)) == ("share", "person", MATE_ID)
        assert _actor(_room_row(email=OWNER_EMAIL)) == ("owner", None, None)

    def test_an_email_with_no_account_is_a_person_with_nobody_behind_it(self):
        assert _actor(_room_row(email="client@example.org")) == ("share", "person", None)

    def test_a_row_that_names_nobody_is_a_person_never_a_visitor(self):
        assert _actor(_room_row()) == ("share", "person", None)

    def test_a_child_that_inherits_the_room_stamp_and_names_nobody_is_not_a_visitor(self):
        """A turn the room turn dispatches inherits `source_channel="room"`."""
        assert _actor(_room_row(trigger="mcp")) == ("share", "person", None)

    def test_an_unreadable_account_lookup_is_a_person_never_higher(self):
        assert _actor(_room_row(email=OWNER_EMAIL),
                      fail_after={"get_user_by_email": 0}) == ("share", "person", None)

    def test_the_person_of_an_email_only_row_is_that_account(self):
        assert tts.turn_person(World(), AGENT, _room_row(email=MATE_EMAIL)) == MATE_ID

    def test_a_channel_row_is_still_a_visitor(self):
        """Only the room and the Workspace: a channel's email is not an account."""
        assert _actor(_room_row(email=OWNER_EMAIL, trigger="slack", channel="slack")) \
            == ("share", "visitor", None)


# ---------------------------------------------------------------------------
# 2. The stamp
# ---------------------------------------------------------------------------

def _user(uid, **extra):
    username, role, _name, email = USERS[uid]
    return SimpleNamespace(id=uid, username=username, role=role, email=email, **extra)


class TestWhoARoomTurnNames:
    def test_a_signed_in_person_is_named_by_user_id(self):
        from shared_sessions import service

        assert service._turn_person(_user(MATE_ID), MATE_EMAIL) == (MATE_ID, MATE_EMAIL)

    def test_an_account_with_no_email_is_still_named(self):
        """The `ADMIN_PASSWORD` admin has no email; the id is the record."""
        from shared_sessions import service

        admin = SimpleNamespace(id=ADMIN_ID, username="admin", role="admin", email=None)
        assert service._turn_person(admin, None) == (ADMIN_ID, None)

    def test_a_workspace_participant_is_named_by_verified_email_only(self):
        from shared_sessions import service

        principal = SimpleNamespace(email="client@example.org", is_platform=False, is_portal=True)
        assert service._turn_person(principal, "client@example.org") == (None, "client@example.org")

    def test_the_engine_s_waker_names_only_a_seated_answerer(self):
        from shared_sessions import service

        seated = service.Waker(kind="ask", identity=AGENT, email=OWNER_EMAIL)
        nobody = service.Waker(kind="ask", identity=AGENT)
        assert service._turn_person(seated, OWNER_EMAIL) == (None, OWNER_EMAIL)
        assert service._turn_person(nobody, None) == (None, None)

    def test_an_agent_s_own_key_names_the_person_of_its_turn_not_its_owner(self, monkeypatch):
        """An agent key resolves to the agent's OWNER. A post it makes while
        acting for a shared person must not wake the next agent as the owner."""
        import database
        from shared_sessions import service

        world = World(executions={"run-1": _room_row(user_id=MATE_ID)})
        monkeypatch.setattr(database, "db", world)
        key = _user(OWNER_ID, agent_name=AGENT, mcp_scope="agent", _acting_execution_id="run-1")
        assert service._turn_person(key, OWNER_EMAIL) == (MATE_ID, MATE_EMAIL)

    def test_an_agent_s_key_in_a_turn_that_names_nobody_names_nobody(self, monkeypatch):
        import database
        from shared_sessions import service

        world = World(executions={"run-1": _room_row()})
        monkeypatch.setattr(database, "db", world)
        key = _user(OWNER_ID, agent_name=AGENT, mcp_scope="agent", _acting_execution_id="run-1")
        assert service._turn_person(key, OWNER_EMAIL) == (None, None)

    def test_a_failed_lookup_names_nobody(self, monkeypatch):
        import database
        from shared_sessions import service

        world = World(executions={"run-1": _room_row(user_id=MATE_ID)},
                      fail_after={"get_execution": 0})
        monkeypatch.setattr(database, "db", world)
        key = _user(OWNER_ID, agent_name=AGENT, mcp_scope="agent", _acting_execution_id="run-1")
        assert service._turn_person(key, OWNER_EMAIL) == (None, None)


# ---------------------------------------------------------------------------
# 3. The wake
# ---------------------------------------------------------------------------

def _wake_row(monkeypatch, principal):
    """Run `_wake_agent` and return the row its dispatch would create (the
    `test_2792_room_work_chat_id.py` shape)."""
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
                        lambda *a, **k: {"id": ROOM, "name": "Room", "status": "open", "topic": None})
    monkeypatch.setattr(service.db, "get_messages", lambda *a, **k: [
        {"seq": 1, "sender_kind": "user", "sender_identity": "mate",
         "content": f"@{AGENT} run whoami", "kind": "message"}])
    monkeypatch.setattr(service.db, "list_participants", lambda _room_id: [
        {"kind": "user", "identity": "mate", "left_at": None},
        {"kind": "agent", "identity": AGENT, "left_at": None},
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

    asyncio.run(service._wake_agent(principal, ROOM, AGENT, 0))
    assert captured, "the wake dispatched nothing"
    return {"id": "e1", "agent_name": AGENT, "schedule_id": None,
            "source_agent_name": None, "continues_execution_id": None,
            **{k: captured.get(k) for k in ("source_user_id", "source_user_email",
                                            "triggered_by", "source_channel")}}


class TestTheRowARoomTurnCreates:
    def test_a_shared_person_s_room_turn_is_rated_a_shared_person(self, monkeypatch):
        """Red on `dev`: the row had no user id and was rated `share / visitor`."""
        row = _wake_row(monkeypatch, _user(MATE_ID))
        assert row["source_user_id"] == MATE_ID
        world = World()
        tier, kind, person = tts.turn_actor(world, AGENT, execution=row)
        assert (tier, kind, person) == ("share", "person", MATE_ID)
        assert tts.shell_allowed(world, AGENT, tier, kind) is False

    @pytest.mark.parametrize("user_id", [OWNER_ID, ADMIN_ID])
    def test_the_owner_s_and_an_admin_s_room_turns_are_owner_tier(self, monkeypatch, user_id):
        row = _wake_row(monkeypatch, _user(user_id))
        assert tts.turn_actor(World(visitor_shell=False), AGENT, execution=row) == ("owner", None, None)

    def test_a_workspace_participant_s_room_turn_is_rated_by_email(self, monkeypatch):
        principal = SimpleNamespace(email=MATE_EMAIL, is_platform=True, is_portal=True)
        row = _wake_row(monkeypatch, principal)
        assert row["source_user_id"] is None
        assert tts.turn_actor(World(), AGENT, execution=row) == ("share", "person", MATE_ID)


# ---------------------------------------------------------------------------
# 4. The voice tool turn
# ---------------------------------------------------------------------------

def _voice_tool(monkeypatch, *, result=None, user_id=MATE_ID, email=MATE_EMAIL):
    import services.skill_gate_service as sgs
    import services.task_execution_service as tes
    from services import gemini_voice as gv

    monkeypatch.setattr(sgs, "enforce", AsyncMock(return_value=None))
    dispatch = AsyncMock(return_value=result or SimpleNamespace(
        status="success", response="developer", error=None))
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", dispatch)
    svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
    session = SimpleNamespace(agent_name=AGENT, user_id=user_id, user_email=email)
    out = asyncio.run(svc._execute_tool(session, "run_task", {"prompt": "run whoami"}))
    return out, dispatch


class TestAVoiceToolTurnRunsUnderARow:
    def test_it_is_dispatched_through_the_task_service_as_the_caller(self, monkeypatch):
        out, dispatch = _voice_tool(monkeypatch)
        assert out == "developer"
        kwargs = dispatch.await_args.kwargs
        assert kwargs["agent_name"] == AGENT and kwargs["message"] == "run whoami"
        assert kwargs["triggered_by"] == "voice"
        assert (kwargs["source_user_id"], kwargs["source_user_email"]) == (MATE_ID, MATE_EMAIL)

    def test_the_row_it_creates_is_rated_as_the_caller(self, monkeypatch):
        """A shared person's voice tool turn is refused the shell; the owner's is not."""
        for user_id, email, expected, shell in ((MATE_ID, MATE_EMAIL, ("share", "person", MATE_ID), False),
                                                (OWNER_ID, OWNER_EMAIL, ("owner", None, None), True)):
            _, dispatch = _voice_tool(monkeypatch, user_id=user_id, email=email)
            kwargs = dispatch.await_args.kwargs
            row = _room_row(user_id=kwargs["source_user_id"], email=kwargs["source_user_email"],
                            trigger=kwargs["triggered_by"], channel=kwargs.get("source_channel"))
            world = World()
            tier, kind, person = tts.turn_actor(world, AGENT, execution=row)
            assert (tier, kind, person) == expected
            assert tts.shell_allowed(world, AGENT, tier, kind) is shell

    def test_a_voice_row_keeps_shared_auto_memory_off(self, monkeypatch):
        """#4156: the id-less call turned it off by flag; the row's trigger does now."""
        from services import memory_subject

        _, dispatch = _voice_tool(monkeypatch, user_id=OWNER_ID, email=OWNER_EMAIL)
        row = SimpleNamespace(**_room_row(user_id=OWNER_ID, email=OWNER_EMAIL,
                                          trigger=dispatch.await_args.kwargs["triggered_by"], channel=None),
                              source_channel_client=None)
        assert memory_subject.keeps_shared_auto_memory(row, OWNER_EMAIL, OWNER_ID) is False

    def test_a_failed_turn_is_voiced_as_an_error_not_as_an_answer(self, monkeypatch):
        failed = SimpleNamespace(status="failed", response="", error="Agent is not running")
        out, _ = _voice_tool(monkeypatch, result=failed)
        assert out == "Task error: Agent is not running"

    def test_a_blank_prompt_dispatches_nothing(self, monkeypatch):
        import services.task_execution_service as tes
        from services import gemini_voice as gv

        dispatch = AsyncMock()
        monkeypatch.setattr(tes, "dispatch_and_await_terminal", dispatch)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        session = SimpleNamespace(agent_name=AGENT, user_id=MATE_ID, user_email=MATE_EMAIL)
        assert asyncio.run(svc._execute_tool(session, "run_task", {})) == "No prompt provided."
        dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. No task reaches an agent without an execution id
# ---------------------------------------------------------------------------

def _id_less_task_calls():
    """Every `<...client>.task(...)` call in the backend that passes no
    `execution_id`. The agent's ent#1176 refusal reads a turn with no id as
    "not a platform turn" and lets it start, so the platform must never send one."""
    found, seen = [], 0
    for path in sorted(_BACKEND.rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if node.func.attr != "task" or not str(name).endswith("client"):
                continue
            seen += 1
            if not any(k.arg == "execution_id" or k.arg is None for k in node.keywords):
                found.append(f"{path.relative_to(_BACKEND)}:{node.lineno}")
    return found, seen


def test_no_backend_code_sends_an_agent_a_task_without_an_execution_id():
    found, _seen = _id_less_task_calls()
    assert not found, (
        f"a task is sent to an agent with no execution id: {found}. The agent rates such a "
        "turn as not the platform's, so nothing limits it while its hooks are skipped "
        "(trinity-enterprise#1392). Dispatch through `execute_task`.")


def test_the_id_less_scan_sees_a_call_it_should_flag(tmp_path, monkeypatch):
    """The scan is not blind: it flags the shape this fix removed."""
    (tmp_path / "svc.py").write_text(
        "async def f(client):\n"
        "    await client.task('p', timeout=28.0)\n"
        "    await client.task('p', execution_id='e1')\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "_BACKEND", tmp_path)
    found, seen = _id_less_task_calls()
    assert seen == 2 and found == ["svc.py:2"]

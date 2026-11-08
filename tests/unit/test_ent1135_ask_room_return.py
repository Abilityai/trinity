"""The answer to an ask raised in a room comes back to that room
(abilityai/trinity-enterprise#1135).

Two halves, one file:

1. **The ending line.** One ending observer in `shared_sessions` posts how the
   ask ended — every disposition, whether or not a wake runs — into the room it
   was raised in. Coarse who; never the answer, the notes or a cancel reason.
2. **The return turn.** The answer (or a cancel / dismiss) wakes the agent
   THROUGH THE ROOM ENGINE as a room turn, under an engine `Waker` that acts for
   nobody; over budget, closed, departed or busy, the room says so and the
   agent is woken outside the room exactly as before.

Harness: the attachment suite's `world` (the ent#611 `ask` fixture over the
real per-process SQLite, a real room), with `dispatch_and_await_terminal`
stubbed at the room engine's and the resume service's import site.

Requirement: docs/memory/requirements/security.md §26.9, §26.12
Related flow: docs/memory/feature-flows/operating-room.md
"""
from __future__ import annotations

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body, _drain, OWNER  # noqa: F401
from unit.test_ent1135_ask_room_attachment import (  # noqa: F401 — fixtures + helpers
    world, _execution, _raise, _lines, OWNER_USERNAME,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def observed(world, monkeypatch):
    """The world with the rooms observer on the sink, for this test only (the
    production registration is `main.py`'s, pinned below)."""
    monkeypatch.setattr(world.svc, "_observers", [world.svc._wake_filer])
    world.rooms.register_ask_observers()
    return world


def _room_ask(world, real_db, rid, **body):
    turn = _execution(real_db, world.agent, chat=world.room)
    receipt = _raise(world, rid, platform_execution_id=turn, body=body or None)
    return world.db.get_operator_queue_item(receipt["id"])


def _new_lines(world, before):
    return _lines(world)[before:]


# ---------------------------------------------------------------------------
# 1. The ending line
# ---------------------------------------------------------------------------

class TestEndingLine:
    def test_a_cancel_by_the_seated_addressee_names_them(self, observed, real_db):
        item = _room_ask(observed, real_db, "e-1", title="Pay the vendor")
        before = len(_lines(observed))
        observed.svc.cancel(item["id"], actor=observed.svc.Actor(email=OWNER),
                            reason="SECRET-REASON")
        new = _new_lines(observed, before)
        assert new == [f"{observed.agent}'s ask “Pay the vendor” was cancelled by "
                       f"{OWNER_USERNAME}."]
        assert "SECRET-REASON" not in new[0]

    def test_an_answer_by_a_non_member_names_a_role_never_the_email(self, observed, real_db):
        item = _room_ask(observed, real_db, "e-2")
        row = observed.db.get_operator_queue_item(item["id"])
        row["status"], row["response"], row["response_text"] = "responded", "approve", "NOTES"
        before = len(_lines(observed))
        observed.rooms.on_ask_ending(observed.svc.EndingEvent(
            observed.svc.ANSWERED, (row,), "stranger-1135@example.com"))
        new = _new_lines(observed, before)
        assert new == [f"{observed.agent}'s ask “Pay invoice” was answered by an operator."]
        assert "stranger-1135" not in new[0] and "approve" not in new[0] and "NOTES" not in new[0]

    def test_an_expiry_says_denied_by_timeout(self, observed, real_db):
        item = _room_ask(observed, real_db, "e-3")
        before = len(_lines(observed))
        observed.rooms.on_ask_ending(observed.svc.EndingEvent(
            observed.svc.EXPIRED, (item,), None))
        assert _new_lines(observed, before) == [
            f"{observed.agent}'s ask “Pay invoice” expired — denied by timeout."]

    def test_a_replaced_and_a_withdrawn_ask_each_get_a_line(self, observed, real_db):
        """The agent's own endings (#3247, ent#901) still tell the room."""
        first = _room_ask(observed, real_db, "e-4", title="Old ask")
        before = len(_lines(observed))
        turn = _execution(real_db, observed.agent, chat=observed.room)
        _raise(observed, "e-4b", platform_execution_id=turn,
               body={"title": "New ask", "replaces": "e-4"})
        new = _new_lines(observed, before)
        assert f"{observed.agent}'s ask “Old ask” was replaced by a newer ask." in new

        _room_ask(observed, real_db, "e-5", title="Gone ask")
        before = len(_lines(observed))
        observed.svc.withdraw(observed.agent, "e-5", "no longer needed")
        assert _new_lines(observed, before) == [
            f"{observed.agent}'s ask “Gone ask” was withdrawn by {observed.agent}."]
        assert first["id"]

    def test_several_asks_ending_in_one_event_get_one_line(self, observed, real_db):
        rows = [_room_ask(observed, real_db, f"e-6-{n}", title=f"T{n}") for n in range(3)]
        before = len(_lines(observed))
        observed.rooms.on_ask_ending(observed.svc.EndingEvent(
            observed.svc.CANCELLED, tuple(rows), OWNER, batch_id="b-1"))
        assert _new_lines(observed, before) == [
            f"3 asks by {observed.agent} were cancelled by {OWNER_USERNAME}: “T0”; “T1”; “T2”."]

    def test_an_ask_with_no_room_gets_no_line(self, observed, real_db):
        receipt = _raise(observed, "e-7")
        before = len(_lines(observed))
        observed.svc.cancel(receipt["id"], actor=observed.svc.Actor(email=OWNER))
        assert _new_lines(observed, before) == []

    def test_a_failed_line_never_fails_the_ending(self, observed, real_db, monkeypatch):
        item = _room_ask(observed, real_db, "e-8")

        def _boom(*a, **k):
            raise RuntimeError("insert failed")

        monkeypatch.setattr(observed.rooms, "_post_system", _boom)
        ending = observed.svc.cancel(item["id"], actor=observed.svc.Actor(email=OWNER))
        assert ending.rows and ending.observers_ok


def test_main_registers_the_rooms_ending_observer():
    """The production wiring (tdd question 4): delete the `main.py` call and
    this goes red. Pinned by AST — importing `main` is too heavy for the island."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"
    calls = [n for n in ast.walk(ast.parse(src.read_text()))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "register_ask_observers"]
    assert calls, "main.py must register shared_sessions' ask ending observer"


# ---------------------------------------------------------------------------
# 2. The return turn
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402


@pytest.fixture()
def turn(observed, monkeypatch):
    """The dispatch seam stubbed: one recorder for every dispatch, room turn
    or ordinary wake alike, so a test can tell which one ran."""
    import services.docker_utils as docker_utils
    import services.platform_audit_service as pas
    import services.pull_pilot as pull_pilot
    import services.task_execution_service as tes
    import services.operator_resume_service as ors

    calls, audit = [], []
    state = {"reply": "On it — paying the vendor now.", "raise": None, "status": "success"}

    async def _dispatch(**kw):
        calls.append(kw)
        if state["raise"] is not None and kw.get("triggered_by") == "room":
            raise state["raise"]
        reply = state["reply"] if kw.get("triggered_by") == "room" and not calls[1:] else "ok"
        return SimpleNamespace(status=state["status"], response=reply,
                               execution_id=f"exec-{len(calls)}", error=None, error_code=None,
                               session_id="sess-1")

    async def _running(agent):
        return "running"

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)

    monkeypatch.setattr(tes, "dispatch_and_await_terminal", _dispatch)
    monkeypatch.setattr(docker_utils, "agent_container_state_async", _running)
    monkeypatch.setattr(pull_pilot, "pull_owns_dispatch", lambda *a, **k: False)
    monkeypatch.setattr(pas, "platform_audit_service", _Audit())
    observed.state["opted_in"] = True
    return SimpleNamespace(w=observed, calls=calls, audit=audit, state=state, ors=ors)


def _answer(t, item, actor=OWNER, response="approve", notes="ship it"):
    return asyncio.run(t.ors.maybe_dispatch_resume(
        item, response=response, response_text=notes, responded_by_email=actor))


def _triggers(t):
    return [c["triggered_by"] for c in t.calls]


class TestReturnTurn:
    def test_an_answer_continues_the_agent_in_the_room_as_a_room_turn(self, turn, real_db):
        w = turn.w
        item = _room_ask(w, real_db, "t-1", title="Pay the vendor")
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["room"]
        call = turn.calls[0]
        assert call["source_channel"] == "room" and call["source_channel_chat_id"] == w.room
        assert call["conversation_key"] == f"room:{w.room}"
        assert "An ask you raised in this room ended" in call["message"]
        assert "answer: approve" in call["message"] and "notes: ship it" in call["message"]
        # the gate reads the person's words too (ent#751)
        assert "approve" in call["request_text"] and "ship it" in call["request_text"]
        # the reply lands in the room as the agent
        from shared_sessions import db as room_db
        msgs = room_db.get_messages(w.room)
        assert msgs[-1]["sender_kind"] == "agent" and msgs[-1]["sender_identity"] == w.agent
        assert msgs[-1]["content"] == "On it — paying the vendor now."
        assert _new_lines(w, before) == []   # the return turn posts no ending line
        row = w.db.get_operator_queue_item(item["id"])
        assert row["wake_state"] == "woken"
        assert [a["details"]["status"] for a in turn.audit
                if a.get("event_action") == "operator_resume_dispatch"] == ["success"]

    def test_a_seated_answerer_is_the_turns_client_a_stranger_is_not(self, turn, real_db):
        w = turn.w
        _answer(turn, _room_ask(w, real_db, "t-2a"), actor=OWNER)
        assert turn.calls[-1]["source_user_email"] == OWNER
        assert turn.calls[-1]["source_channel_client"] == OWNER
        _answer(turn, _room_ask(w, real_db, "t-2b"), actor="stranger-1135@example.com")
        assert turn.calls[-1]["source_user_email"] is None
        assert turn.calls[-1]["source_channel_client"] is None

    def test_a_closed_room_falls_back_to_todays_wake(self, turn, real_db):
        w = turn.w
        item = _room_ask(w, real_db, "t-3")
        w.rooms.close_room(w.person, w.room)
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["operator_response"]
        assert f"{w.agent} continues outside the room: the room is closed." in _new_lines(w, before)

    def test_an_agent_that_left_falls_back_to_todays_wake(self, turn, real_db):
        w = turn.w
        item = _room_ask(w, real_db, "t-4")
        w.rooms.remove_participant(w.person, w.room, w.agent)
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["operator_response"]
        assert (f"{w.agent} continues outside the room: {w.agent} has left the room."
                in _new_lines(w, before))

    @pytest.mark.parametrize("reason,words", [("max_messages", "message"), ("max_cost", "cost")])
    def test_over_budget_falls_back_and_says_so(self, turn, real_db, monkeypatch, reason, words):
        w = turn.w
        item = _room_ask(w, real_db, f"t-5-{reason}")
        monkeypatch.setattr(w.rooms, "_budget_exceeded_reason", lambda room: reason)
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["operator_response"]
        assert (f"{w.agent} continues outside the room: the room is over its {words} budget."
                in _new_lines(w, before))

    def test_a_spent_wake_cap_falls_back_once(self, turn, real_db, monkeypatch):
        import services.rate_limiter as rl
        w = turn.w
        item = _room_ask(w, real_db, "t-6")
        monkeypatch.setattr(rl, "check", lambda key, *a, **k: not key.startswith("room_wake:"))
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["operator_response"]
        new = _new_lines(w, before)
        assert any("reached the wake limit" in line for line in new)
        # the cap line alone reads as "nobody woke"; the agent does, outside
        assert (f"{w.agent} continues outside the room: its wake limit here is spent."
                in new)

    def test_a_busy_room_lock_falls_back_with_the_busy_clause(self, turn, real_db, monkeypatch):
        w = turn.w
        item = _room_ask(w, real_db, "t-7")

        async def _busy(*a, **k):
            return w.rooms.LOCK_BUSY

        monkeypatch.setattr(w.rooms, "_wake_agent", _busy)
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["operator_response"]
        assert (f"{w.agent} continues outside the room: it is still busy with an earlier "
                "turn here." in _new_lines(w, before))

    def test_a_gate_held_return_is_woken_and_never_rewoken_outside(self, turn, real_db):
        from services.skill_gate_errors import SkillGateError
        w = turn.w
        item = _room_ask(w, real_db, "t-8")
        turn.state["raise"] = SkillGateError("needs approval")
        _answer(turn, item)
        assert _triggers(turn) == ["room"]          # no ordinary wake after a hold
        assert w.db.get_operator_queue_item(item["id"])["wake_state"] == "woken"
        assert [a["details"]["status"] for a in turn.audit
                if a.get("event_action") == "operator_resume_dispatch"] == ["gate_held"]

    def test_a_room_dispatch_that_raises_is_owed_never_rewoken_outside(self, turn, real_db):
        """A raise may come after the turn started (a claim or adapter timeout):
        no second, ordinary wake — the wake stays owed, as when the ordinary
        dispatch raises, and the next start re-offers it once."""
        w = turn.w
        item = _room_ask(w, real_db, "t-8r")
        turn.state["raise"] = TimeoutError("claim budget spent")
        _answer(turn, item)
        assert _triggers(turn) == ["room"]
        assert w.db.get_operator_queue_item(item["id"])["wake_state"] == "pending"
        assert [a["details"]["status"] for a in turn.audit
                if a.get("event_action") == "operator_resume_dispatch"] == ["dispatch_error"]

    def test_a_replayed_answer_runs_one_room_turn(self, turn, real_db):
        w = turn.w
        item = _room_ask(w, real_db, "t-9")
        _answer(turn, item)
        _answer(turn, item)
        assert _triggers(turn) == ["room"]

    def test_the_turn_runs_on_an_empty_delta(self, turn, real_db):
        """The ask block IS the content: no unread transcript still runs."""
        w = turn.w
        item = _room_ask(w, real_db, "t-10")
        from shared_sessions import db as room_db
        top = max(m["seq"] for m in room_db.get_messages(w.room))
        room_db.advance_read_cursor(w.room, w.agent, top, "sess-0")
        _answer(turn, item)
        assert _triggers(turn) == ["room"]

    def test_an_ask_with_no_room_keeps_todays_wake(self, turn, real_db):
        receipt = _raise(turn.w, "t-11")
        _answer(turn, turn.w.db.get_operator_queue_item(receipt["id"]))
        assert _triggers(turn) == ["operator_response"]

    def test_the_replys_mentions_are_held_to_the_moderators_reach(self, turn, real_db, monkeypatch):
        w = turn.w
        item = _room_ask(w, real_db, "t-12")
        turn.state["reply"] = f"@{w.other} please take the invoice."
        monkeypatch.setattr(w.rooms, "_reach_principal", lambda room_id: None)
        before = len(_lines(w))
        _answer(turn, item)
        assert _triggers(turn) == ["room"]          # the mention woke nobody
        assert f"{w.other} could not be reached (no longer shared with the sender)." \
            in _new_lines(w, before)

    def test_a_reachable_mention_cascades(self, turn, real_db):
        w = turn.w
        item = _room_ask(w, real_db, "t-13")
        turn.state["reply"] = f"@{w.other} please take the invoice."
        _answer(turn, item)
        assert _triggers(turn) == ["room", "room"]
        assert turn.calls[1]["agent_name"] == w.other


class TestEndingReturn:
    def _end(self, t, items, disposition="cancelled", batch_id=None):
        return asyncio.run(t.ors.maybe_dispatch_ending(
            items, disposition=disposition, disposed_by_email=OWNER, reason="not now",
            batch_id=batch_id))

    def test_a_cancel_continues_the_agent_in_its_room(self, turn, real_db):
        item = _room_ask(turn.w, real_db, "x-1")
        self._end(turn, [item])
        assert _triggers(turn) == ["room"]
        assert "You raised them in this room" in turn.calls[0]["message"]

    def test_asks_from_two_places_get_one_ordinary_wake(self, turn, real_db):
        in_room = _room_ask(turn.w, real_db, "x-2a")
        receipt = _raise(turn.w, "x-2b")
        elsewhere = turn.w.db.get_operator_queue_item(receipt["id"])
        self._end(turn, [in_room, elsewhere])
        assert _triggers(turn) == ["operator_ending"]

    def test_a_bulk_sweep_gets_one_ordinary_wake(self, turn, real_db):
        items = [_room_ask(turn.w, real_db, f"x-3-{n}") for n in range(2)]
        self._end(turn, items, batch_id="b-9")
        assert _triggers(turn) == ["operator_ending"]

    def test_an_ending_whose_room_dispatch_raises_is_owed_never_rewoken_outside(
            self, turn, real_db):
        item = _room_ask(turn.w, real_db, "x-4")
        turn.state["raise"] = TimeoutError("claim budget spent")
        self._end(turn, [item])
        assert _triggers(turn) == ["room"]
        assert turn.w.db.get_operator_queue_item(item["id"])["wake_state"] == "pending"

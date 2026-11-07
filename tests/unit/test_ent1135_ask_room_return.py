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

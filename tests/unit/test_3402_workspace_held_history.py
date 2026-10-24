"""
A Workspace turn the skill gate held never reaches the agent again (trinity#3402).

The gate reads only what the client typed this turn, by design — reading the
history would re-hold every later turn after one mention. But the held
`/pay-invoice …` and its "Not run…" notice stayed in the thread, and every cold
turn (every turn, on a runtime with no `--resume`) replayed them to the agent
as conversation, where it could act on a request nobody approved.

Pinned here, each through its own layer:
  * ``client_portal.service.portal_chat`` — the next cold turn's message has
    neither the held request nor its notice, whatever the decision; a turn the
    gate REFUSED is left out the same way; the thread the client reads keeps both;
  * ``client_portal.voice.build_portal_context_summary`` — the voice model's
    opening context leaves them out;
  * ``client_portal.db.get_platform_rows_since_last_reply`` — a held turn's
    notice is not "the last reply the live session wrote", so a call's spoken
    rows before it still reach the next resumed turn;
  * ``db.migrations`` ``held_gate_turns`` — rows held before the marker existed
    (#3166 → this fix) are marked once, from the gate record naming their turn.

Real schema (`db_harness`) and the real gate (the `execute_task` backstop); only
the agent's own reply is stubbed.

Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent751_gate_entries import FIN, world  # noqa: E402,F401

import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from services.execution_envelope import TaskExecutionResult  # noqa: E402

pytestmark = pytest.mark.unit

CLIENT = "client-3402@example.com"
SESSION = "ps_3402"
HELD = "/pay-invoice 100 EUR to ACME"
NOTICE_HEAD = "Not run:"
REPLY = "Two invoices are overdue: ACME and Initech."
MODEL = "claude-sonnet-4-6"

_EXECUTE_KEYS = ("agent_name", "message", "triggered_by", "source_channel",
                 "source_channel_chat_id", "source_channel_client", "source_user_email",
                 "timeout_seconds", "images", "system_prompt", "execution_id", "model",
                 "request_text", "gate_requester")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ws(world, monkeypatch):
    """A Workspace thread on FIN for a signed-in person who is NOT its approver.

    Each turn runs the real `execute_task` up to admission — so the gate's
    backstop holds a gated turn for real and writes its record — and the
    agent's answer is stubbed at admission."""
    # Imported here: the unit conftest pops this module before every test.
    import services.task_execution_service as tes
    from client_portal import db as portal_db
    from client_portal import service as svc
    from utils.helpers import utc_now_iso

    monkeypatch.setattr(tes, "dispatch_breaker_active", lambda _n: False)

    async def _answer(self, **kw):
        return False, TaskExecutionResult(execution_id=kw["execution_id"], status="success",
                                          response=REPLY)

    monkeypatch.setattr(tes.TaskExecutionService, "_admission_gate", _answer)

    turns: list = []

    async def _run_turn(*_a, **kw):
        turns.append(kw)
        result = await tes.TaskExecutionService().execute_task(
            **{k: kw[k] for k in _EXECUTE_KEYS if k in kw})
        return SimpleNamespace(result=result, real_uuid=None)

    async def _ready(_name):
        return "ready"

    async def _no_files(*_a, **_k):
        return "", []

    monkeypatch.setattr(svc, "_run_sync_turn_and_clear_marker", _run_turn)
    monkeypatch.setattr(svc, "agent_on_roster", lambda *a, **k: True)
    monkeypatch.setattr(svc, "_agent_availability", _ready)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda *a, **k: None)
    monkeypatch.setattr(svc, "collect_inbox_context", _no_files)
    monkeypatch.setattr(svc, "_title_plan", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_refuse_turn_during_voice_call", lambda *a, **k: None)

    portal_db.create_portal_session(SESSION, FIN, CLIENT, utc_now_iso())

    def send(text, **kw):
        """One streaming-path turn: the row is pre-created, as `start_portal_turn` does."""
        from db.write_params import TaskExecutionFields
        row = db.create_task_execution(
            agent_name=FIN, message=text, triggered_by="public",
            fields=TaskExecutionFields(source_user_email=CLIENT, source_channel="portal",
                                       source_channel_chat_id=SESSION))
        return _run(svc.portal_chat(FIN, text, CLIENT, session_id=SESSION,
                                    execution_id=row.id, resolved_model=MODEL,
                                    turn_timeout_seconds=60, gate_is_person=True, **kw))

    return SimpleNamespace(send=send, turns=turns, svc=svc, portal_db=portal_db, world=world)


def _last_message(ws):
    assert ws.turns, "no turn reached the agent"
    return ws.turns[-1]["cold_message"]


# ---------------------------------------------------------------------------
# The cold replay
# ---------------------------------------------------------------------------

def test_the_next_cold_turn_is_not_handed_the_held_request_or_its_notice(ws):
    ws.send("which invoices are overdue?")
    held = ws.send(HELD)
    assert held["response"].startswith(NOTICE_HEAD)       # held: the notice is the reply
    assert db.count_pending_gate_requests(FIN) == 1

    ws.send("thanks — anything else?")
    sent = _last_message(ws)
    assert "which invoices are overdue?" in sent and REPLY in sent   # history kept
    assert HELD not in sent, "the held request was replayed to the agent"
    assert NOTICE_HEAD not in sent


@pytest.mark.parametrize("decision", ["denied", "dispatched"])
def test_it_stays_out_whatever_was_decided(ws, decision):
    """An approved request runs as its own execution, and its result reaches the
    thread as its own row — the held text is never part of a later turn."""
    ws.send(HELD)
    (pending,) = db.list_pending_gate_requests(FIN)
    if decision == "dispatched":
        assert db.claim_gate_request_for_dispatch(pending["request_id"], "ex-approved-3402")
    assert db.transition_gate_request(pending["request_id"], decision)

    ws.send("and now?")
    sent = _last_message(ws)
    assert HELD not in sent and NOTICE_HEAD not in sent


def test_a_turn_the_gate_refused_stays_out_too(ws):
    from client_portal.service import ClientPortalError

    ws.world.state["gates"] = {
        "pay-invoice": _GATE.SkillGate(approver="primary"),
        "send-wire": _GATE.SkillGate(approver="approver"),
    }
    with pytest.raises(ClientPortalError) as info:
        ws.send("/pay-invoice 1 and /send-wire 2")
    assert info.value.category == "gated"

    ws.send("ok, just the balance please")
    sent = _last_message(ws)
    assert "/send-wire 2" not in sent, "the refused request was replayed to the agent"


def test_a_held_turn_with_no_pre_created_row_is_left_out_by_its_mark(ws, monkeypatch):
    """The synchronous path whose pre-create failed: the rows carry no turn id,
    so no gate record can name them — only the mark keeps them out."""
    monkeypatch.setattr(ws.svc, "_precreate_sync_execution", lambda *a, **k: None)
    monkeypatch.setattr(ws.svc, "resolve_turn_model", lambda *a, **k: MODEL)
    out = _run(ws.svc.portal_chat(FIN, HELD, CLIENT, session_id=SESSION,
                                  turn_timeout_seconds=60, gate_is_person=True))
    assert out["response"].startswith(NOTICE_HEAD)
    assert db.count_pending_gate_requests(FIN) == 1

    ws.send("and now?")
    sent = _last_message(ws)
    assert HELD not in sent and NOTICE_HEAD not in sent


def test_the_thread_the_client_reads_still_shows_both(ws):
    ws.send(HELD)
    rows = ws.svc.get_history(FIN, CLIENT, session_id=SESSION)["messages"]
    assert [m["content"] for m in rows if m["role"] == "user"] == [HELD]
    assert any(m["role"] == "assistant" and m["content"].startswith(NOTICE_HEAD) for m in rows)
    # …and the reply poll, which finds a turn's answer by its execution id.
    polled = ws.svc.get_history(FIN, CLIENT, session_id=SESSION, limit=5)["messages"]
    assert any(m["content"].startswith(NOTICE_HEAD) for m in polled)


def test_resending_a_refused_message_that_then_runs_is_conversation_again(ws):
    """The retry reuses the refused turn's row (#3166 dedupe): it stands for the
    new turn now, so the old verdict must not keep it out once that turn ran."""
    from client_portal.service import ClientPortalError

    ws.world.state["gates"] = {
        "pay-invoice": _GATE.SkillGate(approver="primary"),
        "send-wire": _GATE.SkillGate(approver="approver"),
    }
    text = "/pay-invoice 1 and /send-wire 2"
    with pytest.raises(ClientPortalError):
        ws.send(text)
    ws.world.state["gates"] = {}                 # the owner removed the gates
    ws.send(text)                                # same words: the row is reused, and it runs

    ws.send("done?")
    assert text in _last_message(ws)


# ---------------------------------------------------------------------------
# The voice model's context
# ---------------------------------------------------------------------------

def test_the_voice_summary_leaves_the_held_turn_out(ws):
    from client_portal.voice import build_portal_context_summary

    ws.send("which invoices are overdue?")
    ws.send(HELD)
    summary = build_portal_context_summary(FIN, CLIENT, SESSION)
    assert "which invoices are overdue?" in summary
    assert HELD not in summary and NOTICE_HEAD not in summary


# ---------------------------------------------------------------------------
# Rows written before the marker: the gate record still names their turn
# ---------------------------------------------------------------------------

def _row(portal_db, role, content, at, *, source=None, call=None, execution_id=None,
         gate_hold=None):
    import uuid
    portal_db.add_portal_message(uuid.uuid4().hex, FIN, CLIENT, role, content, None, at,
                                 session_id=SESSION, source=source, voice_call_id=call,
                                 execution_id=execution_id, gate_hold=gate_hold)


def _gate_record(request_id, *, origin=None, state="pending"):
    if state == "self_approved":
        assert db.record_self_approved_run(
            request_id=request_id, agent_name=FIN, skills=["pay-invoice"], request_text=HELD,
            requester_email=CLIENT, triggered_by="public",
            dispatched_execution_id=f"ex-run-{request_id}")
        return
    db.create_gate_request(request_id=request_id, agent_name=FIN, skills=["pay-invoice"],
                           request_text=HELD, requester_kind="person",
                           requester_key=f"person:{CLIENT}", origin_execution_id=origin,
                           dispatch={})
    if state != "pending":
        assert db.transition_gate_request(request_id, state)


def test_the_migration_marks_a_turn_held_before_the_marker_existed(ws, db_backend):
    """Rows from #3166 → this fix carry their turn's id and no mark: the
    registered `held_gate_turns` migration marks them once, from the gate record
    naming that turn — an exact match, never a guess from the text."""
    if db_backend != "sqlite":
        pytest.skip("the SQLite runner's entry; the PostgreSQL twin shares its SQL")
    import os
    import sqlite3

    from db import migrations

    _row(ws.portal_db, "user", "which invoices are overdue?", "2026-10-20T10:00:00.000000Z",
         execution_id="ex-1")
    _row(ws.portal_db, "assistant", REPLY, "2026-10-20T10:00:05.000000Z", execution_id="ex-1")
    _row(ws.portal_db, "user", HELD, "2026-10-20T10:01:00.000000Z", execution_id="ex-held")
    _row(ws.portal_db, "assistant", f"{NOTICE_HEAD} … Request gate-old is waiting.",
         "2026-10-20T10:01:01.000000Z", execution_id="ex-held")
    _row(ws.portal_db, "user", "an old row with no turn", "2026-10-20T10:02:00.000000Z")
    _gate_record("gate-old", origin="ex-held")
    _gate_record("gate-task", origin=None)              # a record naming no turn marks nothing
    assert len(ws.portal_db.get_portal_thread_window(FIN, CLIENT, SESSION).rows) == 5

    conn = sqlite3.connect(os.environ["TRINITY_DB_PATH"])
    migrate = dict(migrations.MIGRATIONS)["held_gate_turns"]
    for _ in range(2):                                   # idempotent
        migrate(conn.cursor(), conn)
        conn.commit()
    conn.close()

    window = ws.portal_db.get_portal_thread_window(FIN, CLIENT, SESSION).rows
    assert [r["content"] for r in window] == ["which invoices are overdue?", REPLY,
                                              "an old row with no turn"]
    every = ws.portal_db.get_portal_thread_window(FIN, CLIENT, SESSION, include_held=True).rows
    assert len(every) == 5


def test_the_voice_delta_cursor_skips_a_held_notice(ws):
    pdb = ws.portal_db
    # A call the live session already heard (before its last real reply): a
    # cursor that moved off that reply would hand it over again.
    _row(pdb, "user", "an earlier call", "2026-10-20T09:59:00.000000Z", source="voice", call="call-0")
    _row(pdb, "assistant", REPLY, "2026-10-20T10:00:00.000000Z", execution_id="ex-1")
    _row(pdb, "user", "can you hear me?", "2026-10-20T10:00:10.000000Z", source="voice", call="call-1")
    _row(pdb, "assistant", "yes, loud and clear", "2026-10-20T10:00:11.000000Z",
         source="voice", call="call-1")
    _row(pdb, "user", HELD, "2026-10-20T10:01:00.000000Z", execution_id="ex-held",
         gate_hold="gate-v")
    _row(pdb, "assistant", f"{NOTICE_HEAD} … Request gate-v is waiting.",
         "2026-10-20T10:01:01.000000Z", execution_id="ex-held", gate_hold="gate-v")

    delta = pdb.get_platform_rows_since_last_reply(FIN, CLIENT, SESSION)
    assert [r["content"] for r in delta] == ["can you hear me?", "yes, loud and clear"]


def test_a_reply_from_before_execution_ids_is_still_the_cursor(ws):
    pdb = ws.portal_db
    _row(pdb, "user", "call me", "2026-10-20T09:00:00.000000Z", source="voice", call="call-0")
    _row(pdb, "assistant", "an old typed reply", "2026-10-20T09:30:00.000000Z")   # NULL execution_id
    _row(pdb, "user", "after it", "2026-10-20T09:40:00.000000Z", source="voice", call="call-2")

    delta = pdb.get_platform_rows_since_last_reply(FIN, CLIENT, SESSION)
    assert [r["content"] for r in delta] == ["after it"]

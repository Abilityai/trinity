"""trinity-enterprise#637 — a schedule that names a user can write that user's memory.

The delivery half (ent#498) already carried a schedule's output to a person's
Main chat; the WRITE half of the same seat — its memory — did not exist, and
`write_user_memory` refused every `schedule` trigger. Five properties carry the
weight, one per acceptance criterion:

1. a scheduled run addressed to a person (ent#498's stamp) may write THAT
   person's memory; a scheduled run with no address is refused as before, and
   nothing else changed for user-facing triggers (AC 1 + AC 5);
2. the seat is read off the row, never sent by the caller, and there is ONE
   write boundary — `db.write_public_user_memory_agent_notes` — for every
   trigger (AC 2: the ent#419 gate lands on it once);
3. every write is recorded with what/when/which run, and the person can undo
   it, latest-first (AC 3);
4. the seat is the single `source_channel_client` on the row, so one run can
   only reach one seat (AC 4);
5. the run READS the seat's memory before it can write — the caller prompt
   carries the memory block on both dispatch branches — so the whole-blob
   replace has something to replace.

Every behavioural test EXECUTES the path with the DB / portal boundary stubbed
or against a real SQLite file; the DDL and revision presence are pinned by
text, as in test_ent498 (their live consumer is the migration runner).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _execution(**over):
    base = dict(
        id="e1", agent_name="analyst", triggered_by="schedule",
        source_user_email=None, source_channel="portal",
        source_channel_chat_id="sess-1", source_channel_client="seat@example.com",
        schedule_id="sch-1",
    )
    base.update(over)
    return SimpleNamespace(**base)


# --- 1. the seat resolver ------------------------------------------------------

def test_a_scheduled_run_addressed_to_a_person_has_that_seat():
    from services.schedule_seat_memory import seat_for_execution
    assert seat_for_execution(_execution()) == "seat@example.com"


def test_the_seat_is_normalised_like_the_roster_stores_it():
    from services.schedule_seat_memory import seat_for_execution
    assert seat_for_execution(_execution(source_channel_client="  Seat@Example.COM ")) == "seat@example.com"


@pytest.mark.parametrize("over", [
    dict(source_channel=None, source_channel_client=None),          # AC 5: no address
    dict(source_channel="telegram"),                                 # a channel row, not a seat
    dict(triggered_by="manual", schedule_id="__manual__", source_channel=None,
         source_channel_client=None),                                # a /task run, not a schedule
    dict(triggered_by="manual", schedule_id=None),                   # no schedule behind it
    dict(triggered_by="agent"),
    dict(triggered_by="mcp"),
    dict(triggered_by="public", source_channel="portal"),            # a chat turn resolves via source_user_email, not here
    dict(source_channel_client=""),
], ids=["no-address", "channel-row", "task-run", "manual-no-schedule", "agent", "mcp", "portal-chat", "blank-client"])
def test_everything_that_is_not_an_addressed_schedule_has_no_seat(over):
    from services.schedule_seat_memory import seat_for_execution
    assert seat_for_execution(_execution(**over)) is None


@pytest.mark.parametrize("trigger", ["schedule", "manual", "webhook"])
def test_every_way_a_schedule_fires_serves_the_same_seat(trigger):
    """Cron, Run now and a webhook all address the seat; the operator who
    pressed Run now is not the addressee, so their `source_user_email` is not
    consulted."""
    from services.schedule_seat_memory import seat_for_execution
    row = _execution(triggered_by=trigger, source_user_email="operator@example.com")
    assert seat_for_execution(row) == "seat@example.com"


def test_a_missing_execution_has_no_seat():
    from services.schedule_seat_memory import seat_for_execution
    assert seat_for_execution(None) is None


# --- 2. the write boundary ----------------------------------------------------

def _router(monkeypatch, execution, *, recorded):
    import routers.public_memory as pm
    monkeypatch.setattr(pm, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(pm.db, "get_execution", lambda _id: execution)

    def _write(agent, email, notes, *, execution_id, triggered_by, schedule_id=None):
        recorded.append(dict(agent=agent, email=email, notes=notes, execution_id=execution_id,
                             triggered_by=triggered_by, schedule_id=schedule_id))
        return {"write_id": "w1", "previous_notes": "", "new_notes": notes}
    monkeypatch.setattr(pm.db, "write_public_user_memory_agent_notes", _write)
    return pm


def _call(pm, agent="analyst", execution_id="e1", text="Open loop: budget sign-off."):
    from models import WriteUserMemoryRequest
    body = WriteUserMemoryRequest(execution_id=execution_id, memory_text=text)
    return asyncio.run(pm.write_user_memory(agent, body, current_user=SimpleNamespace(username="analyst-key")))


def test_an_addressed_scheduled_run_writes_the_seats_memory(monkeypatch):
    """AC 1. The seat comes off the ROW — the request body carries no email."""
    recorded = []
    pm = _router(monkeypatch, _execution(), recorded=recorded)
    out = _call(pm)
    assert out["success"] is True
    assert out["user_email"] == "seat@example.com"
    assert out["write_id"] == "w1"
    assert recorded == [dict(agent="analyst", email="seat@example.com",
                             notes="Open loop: budget sign-off.", execution_id="e1",
                             triggered_by="schedule", schedule_id="sch-1")]


def test_a_scheduled_run_with_no_address_is_refused_as_before(monkeypatch):
    """AC 5. Behaviour is unchanged for a schedule that names no user — and the
    refusal SAYS why, because "not a user-facing session" is no longer the
    whole truth."""
    recorded = []
    pm = _router(monkeypatch, _execution(source_channel=None, source_channel_client=None), recorded=recorded)
    with pytest.raises(HTTPException) as ei:
        _call(pm)
    assert ei.value.status_code == 422
    assert "names no one" in ei.value.detail
    assert recorded == []


@pytest.mark.parametrize("trigger", ["manual", "mcp", "agent", "loop"])
def test_other_triggers_are_still_refused(monkeypatch, trigger):
    recorded = []
    pm = _router(monkeypatch, _execution(triggered_by=trigger, source_channel=None,
                                          source_channel_client=None), recorded=recorded)
    with pytest.raises(HTTPException) as ei:
        _call(pm)
    assert ei.value.status_code == 422
    assert recorded == []


def test_a_chat_turn_still_resolves_its_user_from_source_user_email(monkeypatch):
    """The user-facing path is untouched: a portal chat turn is
    `triggered_by="public"` with `source_user_email`, and that — not the
    channel client — is what it writes for."""
    recorded = []
    pm = _router(monkeypatch, _execution(triggered_by="public", source_user_email="person@example.com",
                                          source_channel="portal", source_channel_client="person@example.com",
                                          schedule_id=None), recorded=recorded)
    out = _call(pm)
    assert out["user_email"] == "person@example.com"
    assert recorded[0]["triggered_by"] == "public"
    assert recorded[0]["schedule_id"] is None


def test_the_seat_wins_over_a_stray_source_user_email_on_a_scheduled_row(monkeypatch):
    """Defensive: if some future writer stamps `source_user_email` on a seat
    run (an operator's address, say), the memory still goes to the SEAT — the
    person the run is addressed to — never to whoever's email happened to ride
    the row."""
    recorded = []
    pm = _router(monkeypatch, _execution(source_user_email="operator@example.com"), recorded=recorded)
    _call(pm)
    assert recorded[0]["email"] == "seat@example.com"


def test_there_is_one_write_boundary_for_every_trigger():
    """AC 2. The ent#419 gate goes on `write_public_user_memory_agent_notes`
    and covers scheduled writes by construction: the router must not keep a
    second writer around for one trigger."""
    src = (REPO / "src/backend/routers/public_memory.py").read_text()
    assert src.count("db.write_public_user_memory_agent_notes(") == 1
    assert "db.update_public_user_memory_agent_notes(" not in src


# --- 3. the write history + undo, against a real SQLite file --------------------

@pytest.fixture
def real_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import db.engine as engine_module
    import db.tables as tables
    import db.public_links as pl
    from db.public_links import PublicLinkOperations

    engine = create_engine(f"sqlite:///{tmp_path / 'mem.db'}")
    tables.public_user_memory.create(engine)
    tables.public_user_memory_writes.create(engine)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    monkeypatch.setattr(pl, "get_engine", lambda: engine)
    return PublicLinkOperations()


def test_every_write_is_recorded_with_what_when_and_which_run(real_db):
    """AC 3, the SEE half."""
    first = real_db.write_user_memory_agent_notes(
        "analyst", "Seat@Example.com", "Open loop: budget.",
        execution_id="e1", triggered_by="schedule", schedule_id="sch-1")
    second = real_db.write_user_memory_agent_notes(
        "analyst", "seat@example.com", "Open loop: budget. Commitment: Friday memo.",
        execution_id="e2", triggered_by="schedule", schedule_id="sch-1")
    assert first["previous_notes"] == "" and second["previous_notes"] == "Open loop: budget."

    writes = real_db.list_user_memory_writes("analyst", "seat@example.com")
    assert [w["execution_id"] for w in writes] == ["e2", "e1"]          # newest first
    assert writes[0]["triggered_by"] == "schedule"
    assert writes[0]["schedule_id"] == "sch-1"
    assert writes[0]["previous_notes"] == "Open loop: budget."
    assert writes[0]["new_notes"].endswith("Friday memo.")
    assert writes[0]["undone_at"] is None
    assert real_db.get_or_create_user_memory("analyst", "seat@example.com")["agent_notes"].endswith("Friday memo.")


def test_a_write_never_touches_the_conversation_summary_section(real_db):
    """#895's split survives the new writer: the summariser's section is
    carried through a seat write untouched."""
    real_db.update_user_memory_conversation_summary("analyst", "seat@example.com", "Talked about Q3.")
    real_db.write_user_memory_agent_notes("analyst", "seat@example.com", "Notes v1",
                                          execution_id="e1", triggered_by="schedule")
    rec = real_db.get_or_create_user_memory("analyst", "seat@example.com")
    assert rec == {**rec, "agent_notes": "Notes v1", "conversation_summary": "Talked about Q3."}


def test_undo_restores_what_the_notes_were_before_that_write(real_db):
    """AC 3, the UNDO half."""
    real_db.write_user_memory_agent_notes("analyst", "seat@example.com", "v1",
                                          execution_id="e1", triggered_by="schedule")
    w2 = real_db.write_user_memory_agent_notes("analyst", "seat@example.com", "v2 (wrong)",
                                               execution_id="e2", triggered_by="schedule")
    assert real_db.undo_user_memory_write("analyst", "seat@example.com", w2["write_id"],
                                          undone_by="seat@example.com") == "undone"
    assert real_db.get_or_create_user_memory("analyst", "seat@example.com")["agent_notes"] == "v1"
    writes = real_db.list_user_memory_writes("analyst", "seat@example.com")
    assert writes[0]["id"] == w2["write_id"] and writes[0]["undone_at"] is not None
    # Undo again: already undone — not a second revert.
    assert real_db.undo_user_memory_write("analyst", "seat@example.com", w2["write_id"],
                                          undone_by="seat@example.com") == "already_undone"


def test_undo_is_latest_first_so_a_revert_never_discards_an_unseen_change(real_db):
    w1 = real_db.write_user_memory_agent_notes("analyst", "seat@example.com", "v1",
                                               execution_id="e1", triggered_by="schedule")
    w2 = real_db.write_user_memory_agent_notes("analyst", "seat@example.com", "v2",
                                               execution_id="e2", triggered_by="public")
    assert real_db.undo_user_memory_write("analyst", "seat@example.com", w1["write_id"],
                                          undone_by="seat@example.com") == "not_latest"
    assert real_db.get_or_create_user_memory("analyst", "seat@example.com")["agent_notes"] == "v2"
    # Undo the later one first, then the earlier one becomes the latest open write.
    assert real_db.undo_user_memory_write("analyst", "seat@example.com", w2["write_id"],
                                          undone_by="seat@example.com") == "undone"
    assert real_db.undo_user_memory_write("analyst", "seat@example.com", w1["write_id"],
                                          undone_by="seat@example.com") == "undone"
    assert real_db.get_or_create_user_memory("analyst", "seat@example.com")["agent_notes"] == ""


def test_undo_cannot_reach_another_persons_write(real_db):
    """The id space is per (agent, viewer): someone else's write id reads as
    nonexistent, never as forbidden (Invariant #8)."""
    w = real_db.write_user_memory_agent_notes("analyst", "alice@example.com", "alice v1",
                                              execution_id="e1", triggered_by="schedule")
    assert real_db.undo_user_memory_write("analyst", "bob@example.com", w["write_id"],
                                          undone_by="bob@example.com") == "not_found"
    assert real_db.get_or_create_user_memory("analyst", "alice@example.com")["agent_notes"] == "alice v1"


# --- 3b. the Workspace projection -------------------------------------------

def test_the_person_sees_a_run_now_fire_as_a_scheduled_run(monkeypatch):
    """Found live: a Run-now fire is `triggered_by='manual'`, and keying the
    label on the trigger rendered the seat's write as "In a conversation". The
    boundary records `schedule_id` only for a seat run, so that is the key."""
    from client_portal import agent_page
    monkeypatch.setattr(agent_page.db, "get_or_create_public_user_memory",
                        lambda a, e: {"agent_notes": "v2", "updated_at": "t2"})
    monkeypatch.setattr(agent_page.db, "list_public_user_memory_writes", lambda a, e, n: [
        {"id": "w2", "execution_id": "e2", "triggered_by": "manual", "schedule_id": "sch-1",
         "previous_notes": "v1", "new_notes": "v2", "written_at": "t2", "undone_at": None},
        {"id": "w1", "execution_id": "e1", "triggered_by": "public", "schedule_id": None,
         "previous_notes": "", "new_notes": "v1", "written_at": "t1", "undone_at": None},
    ])
    monkeypatch.setattr(agent_page.db, "get_agent_schedule_names", lambda a: {"sch-1": "Daily brief"})
    page = agent_page.memory("analyst", "seat@example.com")
    assert page["notes"] == "v2"
    assert [w["kind"] for w in page["writes"]] == ["scheduled_run", "conversation"]
    assert page["writes"][0]["schedule_name"] == "Daily brief"
    assert [w["undoable"] for w in page["writes"]] == [True, False]      # latest only


def test_undo_refusals_are_named_and_a_miss_is_the_uniform_404(monkeypatch):
    from client_portal import agent_page
    for outcome, code, status in (("not_found", "not_found", 404),
                                  ("already_undone", "already_undone", 409),
                                  ("not_latest", "not_latest", 409)):
        monkeypatch.setattr(agent_page.db, "undo_public_user_memory_write", lambda *a, **k: outcome)
        with pytest.raises(agent_page.MemoryUndoRefused) as ei:
            agent_page.undo_memory_write("analyst", "seat@example.com", "w1")
        assert (ei.value.code, ei.value.status_code) == (code, status)


# --- 4. one run, one seat ---------------------------------------------------

def test_a_companion_serving_two_seats_keeps_them_apart(real_db, monkeypatch):
    """AC 4. Two schedules on one agent, one per seat: each run's seat is the
    single client on ITS row, and memory is keyed on (agent, email)."""
    import routers.public_memory as pm
    from models import WriteUserMemoryRequest
    from services.schedule_seat_memory import seat_for_execution

    runs = {"e-alice": _execution(id="e-alice", source_channel_client="alice@example.com", schedule_id="s-a"),
            "e-bob": _execution(id="e-bob", source_channel_client="bob@example.com", schedule_id="s-b")}
    monkeypatch.setattr(pm, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(pm.db, "get_execution", lambda _id: runs[_id])
    monkeypatch.setattr(pm.db, "write_public_user_memory_agent_notes",
                        real_db.write_user_memory_agent_notes)
    for eid, text in (("e-alice", "Alice's loops"), ("e-bob", "Bob's loops")):
        asyncio.run(pm.write_user_memory("analyst", WriteUserMemoryRequest(execution_id=eid, memory_text=text),
                                         current_user=SimpleNamespace(username="k")))
    assert seat_for_execution(runs["e-alice"]) != seat_for_execution(runs["e-bob"])
    assert real_db.get_or_create_user_memory("analyst", "alice@example.com")["agent_notes"] == "Alice's loops"
    assert real_db.get_or_create_user_memory("analyst", "bob@example.com")["agent_notes"] == "Bob's loops"


# --- 5. the run reads before it writes -----------------------------------------

def test_the_seat_prompt_carries_the_memory_block_and_the_seat_note(monkeypatch):
    import services.schedule_seat_memory as ssm
    import database as database_mod
    monkeypatch.setattr(database_mod.db, "get_or_create_public_user_memory",
                        lambda a, e: {"agent_notes": "Open loop: budget.", "conversation_summary": ""})
    prompt = ssm.build_seat_caller_prompt("analyst", "seat@example.com")
    assert "## What you know about this user" in prompt
    assert "Open loop: budget." in prompt
    assert "write_user_memory" in prompt
    assert "complete" in prompt          # whole-blob semantics stated where the write is invited


def test_an_empty_seat_memory_still_gets_the_seat_note(monkeypatch):
    import services.schedule_seat_memory as ssm
    import database as database_mod
    monkeypatch.setattr(database_mod.db, "get_or_create_public_user_memory",
                        lambda a, e: {"agent_notes": "", "conversation_summary": ""})
    prompt = ssm.build_seat_caller_prompt("analyst", "seat@example.com")
    assert "What you know about this user" not in prompt
    assert "write_user_memory" in prompt


def test_an_unreadable_memory_yields_no_prompt_rather_than_inviting_a_blind_write(monkeypatch):
    import services.schedule_seat_memory as ssm
    import database as database_mod

    def _boom(a, e):
        raise RuntimeError("db down")
    monkeypatch.setattr(database_mod.db, "get_or_create_public_user_memory", _boom)
    assert ssm.build_seat_caller_prompt("analyst", "seat@example.com") is None


def test_the_seat_prompt_reaches_execute_task_on_both_dispatch_branches(monkeypatch):
    """The stamp succeeded → the memory rides `system_prompt` into
    `execute_task`, sync and async alike. No address → `system_prompt=None`,
    byte-identical to before (AC 5)."""
    import routers.internal as internal
    from models import InternalTaskExecutionRequest

    calls = []

    class _Svc:
        async def execute_task(self, **kw):
            calls.append(kw)
            return SimpleNamespace(execution_id=kw["execution_id"], status="completed", response="ok",
                                   cost=0, context_used=0, context_max=0, session_id=None, error=None)

    import services.cleanup_service as cleanup_service
    monkeypatch.setattr(cleanup_service, "is_startup_recovery_complete", lambda: True)
    monkeypatch.setattr(internal, "get_task_execution_service", lambda: _Svc())
    monkeypatch.setattr(internal.idempotency_service, "begin", lambda *a, **k: SimpleNamespace(replay=False, execution_id=None, in_flight=False, snapshot=None))
    monkeypatch.setattr(internal.idempotency_service, "attach_execution", lambda *a, **k: None)
    monkeypatch.setattr(internal.idempotency_service, "complete", lambda *a, **k: None)
    monkeypatch.setattr(internal.idempotency_service, "fail", lambda *a, **k: None)
    monkeypatch.setattr(internal.platform_audit_service, "log", AsyncMock())
    monkeypatch.setattr(internal.schedule_workspace_delivery, "resolve_and_stamp", lambda *a: "sess-1")
    monkeypatch.setattr(internal.schedule_seat_memory, "build_seat_caller_prompt",
                        lambda agent, email: f"SEAT PROMPT for {email}")

    def _req(**over):
        base = dict(agent_name="analyst", message="Write the brief", triggered_by="schedule",
                    execution_id="e1", schedule_id="sch-1", schedule_name="Daily brief")
        base.update(over)
        return InternalTaskExecutionRequest(**base)

    # sync, addressed
    asyncio.run(internal.execute_task_internal(_req(deliver_to_workspace_email="seat@example.com"),
                                               idempotency_key=None))
    assert calls[-1]["system_prompt"] == "SEAT PROMPT for seat@example.com"
    # sync, no address
    asyncio.run(internal.execute_task_internal(_req(execution_id="e2"), idempotency_key=None))
    assert calls[-1]["system_prompt"] is None
    # async, addressed — the background coroutine carries it too
    asyncio.run(internal._execute_task_internal_background(
        _Svc(), _req(execution_id="e3", deliver_to_workspace_email="seat@example.com"),
        system_prompt="SEAT PROMPT for seat@example.com"))
    assert calls[-1]["system_prompt"] == "SEAT PROMPT for seat@example.com"


# --- both migration tracks ------------------------------------------------------

def test_the_table_is_in_the_sqlite_ddl_the_metadata_mirror_and_the_cleanup_registry():
    schema = (REPO / "src/backend/db/schema.py").read_text()
    tables = (REPO / "src/backend/db/tables.py").read_text()
    cleanup = (REPO / "src/backend/db/agent_cleanup.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS public_user_memory_writes" in schema
    assert 'public_user_memory_writes = Table(' in tables
    assert 'AgentRef("public_user_memory_writes"' in cleanup


def test_the_sqlite_runner_carries_the_migration():
    mig = (REPO / "src/backend/db/migrations.py").read_text()
    assert '("public_user_memory_writes_table", _migrate_public_user_memory_writes_table)' in mig


def test_the_alembic_revision_exists_and_chains_off_the_head():
    rev = REPO / "src/backend/migrations/versions/0066_public_user_memory_writes.py"
    body = rev.read_text()
    assert 'revision = "0066_public_user_memory_writes"' in body
    # Chained off #2920's revision (the three 2026-09-21 schema PRs are stacked
    # so `dev` never carries two heads off 0064).
    assert 'down_revision = "0065_agent_skills_delivery_status"' in body
    assert 'has_table("public_user_memory_writes")' in body
    assert "DROP TABLE IF EXISTS public_user_memory_writes" in body


def test_the_migration_graph_still_has_exactly_one_head():
    import subprocess
    out = subprocess.run(
        ["python3", str(REPO / "scripts/ci/check_alembic_heads.py"),
         str(REPO / "src/backend/migrations/versions")],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr

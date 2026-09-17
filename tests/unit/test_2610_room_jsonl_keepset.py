"""#2610 — room participants must be in the JSONL reaper's keep set.

A room stores a resumable Claude handle per (agent, room) in
``enterprise_room_participants.cached_session_id`` and passes it to
``execute_task(resume_session_id=...)``. The reaper's keep set was built from
``agent_sessions`` and ``enterprise_portal_sessions`` only, so every room
handle was an orphan by construction: reaped once past the 1h age guard, on
the next 6h sweep. The next mention of any agent in the room then failed with
``No conversation found with session ID: <uuid>`` — observed in production on
a room left overnight, every participant failing on its own id.

Same class as ent#358 one surface over, which is why the fail-closed rule is
re-asserted here: a keep-set read that raises must ABORT the sweep, never reap
against a partial set.

The other half of these tests is the boundary of what may be kept. The keep-set
predicate must equal the WAKE predicate in ``shared_sessions.service._wake_agent``
(a left participant and a non-open room both return early there, and neither is
reversible — ``close_room`` is one-way and ``add_participant`` is a no-op that
never clears ``left_at``). Keeping more than that would trade this bug for an
unbounded disk leak: rooms expire, so every room eventually closes, and a
keep set that ignored status would pin every JSONL those rooms ever wrote.
"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


AGENT = "agent-a"


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    """Fresh sqlite carrying the OSS tables the module reads + the room tables."""
    db_file = tmp_path / "trinity-rooms-2610.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_ownership, users, schedule_executions
    m.create_all(get_engine(), tables=[agent_ownership, users, schedule_executions])

    from conftest import ensure_schema_tables
    ensure_schema_tables("enterprise_rooms", "enterprise_room_participants",
                         "enterprise_room_messages")

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          email="alice@example.com",
                                          created_at="t", updated_at="t"))
        for name in ("agent-a", "agent-b"):
            conn.execute(insert(agent_ownership).values(
                agent_name=name, owner_id=1, created_at="t"))
    yield str(db_file)


def _room(room_id: str, status: str = "open") -> None:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_rooms (id, name, topic, created_by, status, "
            "max_messages, max_cost_usd, expires_at, created_at) "
            "VALUES (:id, 'R', NULL, 'alice', :st, 60, NULL, NULL, 't')"
        ), {"id": room_id, "st": status})


def _participant(room_id: str, kind: str, identity: str, cached: str | None,
                 left_at: str | None = None) -> None:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_room_participants "
            "(room_id, kind, identity, role, joined_at, left_at, last_read_seq, "
            " cached_session_id) "
            "VALUES (:r, :k, :i, 'member', 't', :left, 0, :sid)"
        ), {"r": room_id, "k": kind, "i": identity, "left": left_at, "sid": cached})


# ---------------------------------------------------------------------------
# The accessor
# ---------------------------------------------------------------------------

def test_an_open_rooms_agent_handle_is_live(rooms_db):
    """The bug in one line: this id was invisible to the reaper."""
    from shared_sessions import db as rooms

    _room("room_1")
    _participant("room_1", "agent", AGENT, "aaaaaaaa-1111-2222-3333-444444444444")

    assert rooms.list_active_claude_session_ids(AGENT) == [
        "aaaaaaaa-1111-2222-3333-444444444444"
    ]


def test_a_departed_participants_handle_is_not_live(rooms_db):
    """`_wake_agent` returns early on `left_at`, and nothing ever clears it —
    `add_participant` is an idempotent no-op on conflict. So the handle can
    never be resumed again and must not pin its JSONL forever."""
    from shared_sessions import db as rooms

    _room("room_1")
    _participant("room_1", "agent", AGENT, "dddddddd-1111-2222-3333-444444444444",
                 left_at="2026-01-01T00:00:00Z")

    assert rooms.list_active_claude_session_ids(AGENT) == []


def test_a_closed_rooms_handle_is_not_live(rooms_db):
    """`_wake_agent` returns early unless the room is open, and `close_room` is
    a one-way CAS. Keeping these would leak: every room expires eventually."""
    from shared_sessions import db as rooms

    _room("room_closed", status="closed")
    _participant("room_closed", "agent", AGENT,
                 "cccccccc-1111-2222-3333-444444444444")

    assert rooms.list_active_claude_session_ids(AGENT) == []


def test_only_agent_participants_contribute(rooms_db):
    """`identity` is POLYMORPHIC — an agent name, a user id, or a verified
    email depending on the sibling `kind` (the ent#443 rule). A human whose
    username happens to equal an agent name must not inject a handle into that
    agent's keep set."""
    from shared_sessions import db as rooms

    _room("room_1")
    _participant("room_1", "user", AGENT, "eeeeeeee-1111-2222-3333-444444444444")

    assert rooms.list_active_claude_session_ids(AGENT) == []


def test_another_agents_handle_is_not_returned(rooms_db):
    """The sweep is per-agent and reaps inside ONE container, so a sibling's id
    must not appear — it would be a no-op here but a silent leak there."""
    from shared_sessions import db as rooms

    _room("room_1")
    _participant("room_1", "agent", "agent-b",
                 "bbbbbbbb-1111-2222-3333-444444444444")

    assert rooms.list_active_claude_session_ids(AGENT) == []


def test_participants_with_no_handle_yet_are_skipped(rooms_db):
    """A participant that has never run a turn has a NULL handle; it must not
    reach the keep set as a None the reaper would compare against a filename."""
    from shared_sessions import db as rooms

    _room("room_1")
    _participant("room_1", "agent", AGENT, None)

    assert rooms.list_active_claude_session_ids(AGENT) == []


def test_one_agent_in_several_rooms_keeps_every_handle(rooms_db):
    """The handle is per-(agent, room), so an agent in three rooms has three
    live JSONLs. Returning only one would reap the other two."""
    from shared_sessions import db as rooms

    ids = set()
    for n in (1, 2, 3):
        _room(f"room_{n}")
        sid = f"0000000{n}-1111-2222-3333-444444444444"
        ids.add(sid)
        _participant(f"room_{n}", "agent", AGENT, sid)

    assert set(rooms.list_active_claude_session_ids(AGENT)) == ids


# ---------------------------------------------------------------------------
# The reaper wiring — an accessor nothing calls fixes nothing
# ---------------------------------------------------------------------------

def test_the_reaper_keeps_a_live_room_jsonl(monkeypatch):
    """Three old files past the age guard: one held by a Session-tab row, one by
    a room, one by nobody. Only the orphan may be deleted."""
    from services import session_cleanup_service as cleanup
    from client_portal import db as portal_db
    from shared_sessions import db as rooms_db_mod
    from database import db as core_db

    session_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    room_uuid = "11111111-2222-3333-4444-555555555555"
    orphan_uuid = "99999999-9999-9999-9999-999999999999"

    monkeypatch.setattr(core_db, "list_active_claude_session_ids",
                        lambda agent: [session_uuid])
    monkeypatch.setattr(portal_db, "list_active_claude_session_ids",
                        lambda agent: [])
    monkeypatch.setattr(rooms_db_mod, "list_active_claude_session_ids",
                        lambda agent: [room_uuid])

    old = "1000000000"
    listing = "\n".join(f"{u}.jsonl {old}"
                        for u in (session_uuid, room_uuid, orphan_uuid))
    removed = []

    async def _fake_exec(container, cmd, timeout=30):
        if cmd.startswith("rm -f"):
            removed.append(cmd)
            return {"exit_code": 0, "output": ""}
        return {"exit_code": 0, "output": listing}

    monkeypatch.setattr(cleanup, "execute_command_in_container", _fake_exec)

    per = _run(cleanup.SessionCleanupService()._sweep_agent(AGENT))

    assert per["deleted"] == 1
    assert any(orphan_uuid in c for c in removed)
    assert not any(room_uuid in c for c in removed), (
        "a live room's JSONL was reaped — every agent in that room will fail "
        "its next mention with 'No conversation found with session ID'"
    )


def test_the_reaper_skips_the_sweep_when_the_room_keep_set_cannot_load(monkeypatch):
    """Fail-closed, the ent#358 rule: skipping a cycle costs disk, reaping
    against a partial keep set costs every room its memory."""
    from services import session_cleanup_service as cleanup
    from client_portal import db as portal_db
    from shared_sessions import db as rooms_db_mod
    from database import db as core_db

    monkeypatch.setattr(core_db, "list_active_claude_session_ids",
                        lambda agent: ["session-tab-uuid"])
    monkeypatch.setattr(portal_db, "list_active_claude_session_ids",
                        lambda agent: [])

    def _boom(agent):
        raise RuntimeError("db down")

    monkeypatch.setattr(rooms_db_mod, "list_active_claude_session_ids", _boom)

    reached = {"container": False}

    async def _fake_exec(container, cmd, timeout=30):
        reached["container"] = True
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(cleanup, "execute_command_in_container", _fake_exec)

    per = _run(cleanup.SessionCleanupService()._sweep_agent(AGENT))

    assert reached["container"] is False, "must not list (or reap) files blind"
    assert per["errors"] == 1
    assert per["deleted"] == 0

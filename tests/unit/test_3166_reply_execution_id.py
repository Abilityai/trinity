"""A Workspace reply names the turn that produced it (#3166).

Two turns on one thread (the same chat open in two tabs) could show each other's
answers: the client accepted "the newest reply that differs from the baseline",
and nothing tied that reply to the turn it was waiting on. Each persisted row
now carries the execution id of the turn that wrote it, so the client accepts
only its own turn's reply, and a reload can place each reply under its own
question.

Pinned here:
  * the reply row carries the DISPATCHED execution id — the one the client
    watches — on both the streaming and the synchronous path, and on the
    skill-approval notice;
  * the user row carries it too, including the synchronous path (where the
    execution is created after the user row is written) and a retry (where the
    existing row is reused instead of written again);
  * the history read returns it, and the response model declares it (a field
    the model does not declare is stripped on the wire);
  * the SQLite migration adds the column, idempotently.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import uuid
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-3166.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-3166-logs"))

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "client@example.com"
SESSION = "sess-3166"


class _Result:
    status = "success"
    response = "the reply"
    error = None
    error_code = None
    session_id = None
    cost = 0.01


@pytest.fixture()
def chat(monkeypatch):
    """The REAL `portal_chat` and `_persist_user_turn`, boundaries stubbed."""
    from client_portal import db as portal_db
    from client_portal import service as svc
    from services import session_turn_service as sts

    state = types.SimpleNamespace(inserted=[], stamped=[], history=[], turn_raises=None)

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)

    async def _availability(name):
        return "ready"

    monkeypatch.setattr(svc, "_agent_availability", _availability)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda a, e: None)
    monkeypatch.setattr(svc, "_spawn_title_generation", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_precreate_sync_execution", lambda *a, **kw: "exec-sync")
    monkeypatch.setattr(svc, "mark_turn_inflight", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "clear_turn_inflight", lambda *a, **kw: None, raising=False)

    async def _no_inbox(agent, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _no_inbox)

    def _add(msg_id, agent_name, client_email, role, content, cost, now, **kw):
        state.inserted.append({"id": msg_id, "role": role, "content": content,
                               "execution_id": kw.get("execution_id")})

    monkeypatch.setattr(portal_db, "add_portal_message", _add)
    monkeypatch.setattr(portal_db, "set_portal_message_execution_id",
                        lambda mid, eid: state.stamped.append((mid, eid)))
    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages", lambda *a, **kw: list(state.history))
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id", lambda sid: None)
    monkeypatch.setattr(portal_db, "update_cached_claude_session_id", lambda sid, u: None)

    monkeypatch.setattr(sts, "supports_session_resume", lambda a: True)
    monkeypatch.setattr(sts, "resolve_turn_timeout", lambda a: 600)

    async def _turn(**kwargs):
        if state.turn_raises:
            raise state.turn_raises
        return sts.ResumableTurn(result=_Result(), real_uuid=None)

    monkeypatch.setattr(sts, "run_resumable_turn", _turn)
    return svc, state


def _rows(state, role):
    return [r for r in state.inserted if r["role"] == role]


def _user_execution_id(state):
    """The execution id the user row ends up carrying: written, or stamped after."""
    user = _rows(state, "user")
    if not user:
        return None
    stamped = [eid for mid, eid in state.stamped if mid == user[-1]["id"]]
    return stamped[-1] if stamped else user[-1]["execution_id"]


def test_streaming_turn_stamps_the_dispatched_id_on_both_rows(chat):
    svc, state = chat
    asyncio.run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION, execution_id="exec-stream"))

    assert [r["execution_id"] for r in _rows(state, "assistant")] == ["exec-stream"]
    assert _user_execution_id(state) == "exec-stream"


def test_sync_turn_stamps_the_precreated_id_on_both_rows(chat):
    svc, state = chat
    asyncio.run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))

    assert [r["execution_id"] for r in _rows(state, "assistant")] == ["exec-sync"]
    # The user row is written before the execution exists on this path, so it
    # is stamped once the id is known.
    assert _user_execution_id(state) == "exec-sync"


def test_skill_approval_notice_carries_the_turn_id(chat):
    from services.skill_gate_errors import SkillApprovalRequired

    svc, state = chat
    state.turn_raises = SkillApprovalRequired(request_id="r1", agent_name=AGENT, skills=["s"],
                                              approver_role="approver", expires_at=None)
    asyncio.run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION, execution_id="exec-gate"))

    assert [r["execution_id"] for r in _rows(state, "assistant")] == ["exec-gate"]


def test_retry_moves_the_existing_user_row_to_the_new_turn(chat):
    """A retry reuses the failed turn's user row; it must now name the retry,
    or the retry's reply has no question to sit under after a reload."""
    svc, state = chat
    state.history = [{"id": "u-old", "role": "user", "content": "hello", "source": None,
                      "execution_id": "exec-failed"}]
    asyncio.run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION, execution_id="exec-retry"))

    assert _rows(state, "user") == []          # not written twice
    assert ("u-old", "exec-retry") in state.stamped


def test_history_model_declares_execution_id():
    from client_portal.models import PortalHistoryMessage

    assert "execution_id" in PortalHistoryMessage.model_fields
    assert PortalHistoryMessage(role="assistant", content="x").execution_id is None
    assert PortalHistoryMessage(role="assistant", content="x",
                                execution_id="e1").execution_id == "e1"


@pytest.fixture()
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3166-real.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata, enterprise_portal_messages, enterprise_portal_sessions
    metadata.create_all(get_engine(), tables=[enterprise_portal_messages, enterprise_portal_sessions])
    from client_portal import db as pdb
    from utils.helpers import utc_now_iso
    pdb.create_portal_session(SESSION, AGENT, EMAIL, utc_now_iso())
    return pdb


def test_both_history_reads_return_the_execution_id(real_db):
    from utils.helpers import utc_now_iso

    pdb = real_db
    uid = uuid.uuid4().hex
    pdb.add_portal_message(uid, AGENT, EMAIL, "user", "q", None, utc_now_iso(),
                           session_id=SESSION)
    pdb.set_portal_message_execution_id(uid, "e1")
    pdb.add_portal_message(uuid.uuid4().hex, AGENT, EMAIL, "assistant", "a", None, utc_now_iso(),
                           session_id=SESSION, execution_id="e1")
    pdb.add_portal_message(uuid.uuid4().hex, AGENT, EMAIL, "assistant", "report", None,
                           utc_now_iso(), session_id=SESSION)

    narrow = pdb.get_portal_messages(AGENT, EMAIL, limit=8, session_id=SESSION)
    assert [m["execution_id"] for m in narrow] == ["e1", "e1", None]
    window = pdb.get_portal_thread_window(AGENT, EMAIL, SESSION)
    assert [m["execution_id"] for m in window.rows] == ["e1", "e1", None]


def test_sqlite_migration_adds_the_column_idempotently(tmp_path):
    from db.migrations import _migrate_portal_messages_execution_id

    conn = sqlite3.connect(tmp_path / "m.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE enterprise_portal_messages (id TEXT PRIMARY KEY, role TEXT)")
    _migrate_portal_messages_execution_id(cur, conn)
    _migrate_portal_messages_execution_id(cur, conn)   # second run is a no-op
    cols = [r[1] for r in cur.execute("PRAGMA table_info(enterprise_portal_messages)")]
    assert cols.count("execution_id") == 1


# --- A second turn on the thread waits for the first (#3166 follow-on) --------
#
# Only one turn runs on a thread at a time. A second one waited at most 30s for
# the thread's lock and was then refused with a 429, so any first turn longer
# than 30s made the second fail. A Workspace turn now waits as long as the lock
# can be held (the room path's rule, #3114), and the client's wait budget grows
# by the same amount so it does not give up on a turn that is still queued.


def test_portal_turn_asks_to_wait_for_the_lock(chat, monkeypatch):
    from services import session_turn_service as sts

    svc, state = chat
    seen = {}

    async def _turn(**kwargs):
        seen.update(kwargs)
        return sts.ResumableTurn(result=_Result(), real_uuid=None)

    monkeypatch.setattr(sts, "run_resumable_turn", _turn)
    asyncio.run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION, execution_id="e1"))
    assert seen.get("wait_for_lock") is True


def test_run_resumable_turn_waits_as_long_as_the_lock_lives(monkeypatch):
    from services import session_turn_service as sts
    from services import task_execution_service as tes

    locks = []

    class _Lock:
        def __init__(self, *a, ttl_seconds=None, wait_seconds=None, **kw):
            locks.append({"ttl": ttl_seconds, "wait": wait_seconds})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    async def _dispatch(**kwargs):
        return _Result()

    monkeypatch.setattr(sts, "ResumeLock", _Lock)
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", _dispatch)
    monkeypatch.setattr(sts, "supports_session_resume", lambda a: True)

    for flag in (True, False):
        asyncio.run(sts.run_resumable_turn(agent_name=AGENT, session_key="k", message="m",
                                           cached_uuid=None, triggered_by="public",
                                           lock_ttl=900, wait_for_lock=flag))
    assert locks == [{"ttl": 900, "wait": 900}, {"ttl": 900, "wait": None}]

"""
Public links and Telegram groups never replay a gated request to the agent (trinity#3402).

Both surfaces store the sender's message BEFORE the gate decides, and every later
turn's context is rebuilt from those rows — the public link's `Previous
conversation`, the group's history block, and the MEM-001 summary. A held or
refused `/pay-invoice …` (and the notice answering it) rode along in all three.

A row is now marked (`public_chat_messages.gate_hold`):
  * when it is WRITTEN, if its text invokes a skill gated on that agent — these
    senders can never self-approve, so such a message never runs as itself; that
    also covers group chatter the gate never sees (an untagged message is
    recorded as context and never dispatched) and a turn built while an earlier
    one is still inside the gate's check;
  * when the gate HOLDS or REFUSES the turn — with the request id or the reason,
    which also covers a hold the reply quote caused.
Every prompt read leaves marked rows out by default; the visitor's own history
still shows them.

Targets, each through its own layer:
  * ``services.public_chat_service.run_public_chat`` / ``_execute_public_chat_background``
    over the real schema (`db_harness`);
  * ``routers.public.get_public_chat_history`` — the visitor's view;
  * ``services.platform_prompt_service.summarize_user_memory_background`` — MEM-001;
  * ``adapters.message_router.ChannelMessageRouter`` — the observe path, a tagged
    group turn, and the group history block;
  * ``db.public_chat`` — the default and the marker's write rule; ``db.migrations``.

Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import count as _count, db_backend  # noqa: E402,F401

import services.skill_gate_service as _GATE  # noqa: E402
from services.skill_gate_errors import SkillApprovalRequired, SkillGateRefused  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "finance-3402"
HELD = "/pay-invoice 100 EUR to ACME"
NOTICE_HEAD = "Not run:"


def _held(request_id="gate-3402-public"):
    return SkillApprovalRequired(request_id=request_id, agent_name=AGENT, skills=["pay-invoice"],
                                 approver_role="primary", expires_at=None)


@pytest.fixture
def gated(monkeypatch):
    """`pay-invoice` is gated on AGENT (the ent#753 map, stubbed)."""
    monkeypatch.setattr(_GATE, "list_skill_gates",
                        lambda agent: {"pay-invoice": _GATE.SkillGate()} if agent == AGENT else {})


def _unreadable_for(monkeypatch, reads):
    """The gate map cannot be read for the next `reads` reads — a transient
    database blip — and then answers as before."""
    healthy = _GATE.list_skill_gates
    left = {"n": reads}

    def _read(agent):
        if left["n"]:
            left["n"] -= 1
            raise RuntimeError("database is locked")
        return healthy(agent)

    monkeypatch.setattr(_GATE, "list_skill_gates", _read)


# ---------------------------------------------------------------------------
# Public links — the real service over the real schema
# ---------------------------------------------------------------------------

@pytest.fixture
def public(db_backend, gated, monkeypatch):
    # Imported here: the unit conftest pops this module before every test.
    import services.task_execution_service as tes
    from database import db
    from services import public_chat_service as pcs

    link = db.create_public_link(AGENT, "owner-3402", name="Finance")
    link = link if isinstance(link, dict) else link.model_dump()
    monkeypatch.setattr(pcs, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(pcs, "build_public_channel_caller_prompt", lambda *a, **k: None)

    sent: list = []
    outcome = {"next": None}

    async def _dispatch(**kw):
        sent.append(kw)
        nxt, outcome["next"] = outcome["next"], None
        if nxt is not None:
            raise nxt
        return SimpleNamespace(status="success", response="Two invoices are overdue.", cost=0.0,
                               error=None, execution_id=kw.get("execution_id") or "ex-pub")

    monkeypatch.setattr(tes, "dispatch_and_await_terminal", _dispatch)

    def send(text, *, raise_=None, async_mode=False):
        outcome["next"] = raise_
        req = SimpleNamespace(session_token=None, session_id="anon-3402", files=None,
                              message=text, async_mode=async_mode)
        return asyncio.run(pcs.run_public_chat(link, req, "203.0.113.7"))

    def rows(include_held=True):
        session = db.get_public_chat_session_by_identifier(link["id"], "anon-3402")
        return db.get_recent_public_chat_messages(session.id, limit=50, include_held=include_held)

    return SimpleNamespace(db=db, pcs=pcs, link=link, send=send, sent=sent, rows=rows)


def test_the_next_turn_of_a_public_link_is_not_handed_a_held_request(public):
    public.send("which invoices are overdue?")
    out = public.send(HELD, raise_=_held())
    assert out.response.startswith(NOTICE_HEAD)

    public.send("thanks")
    context = public.sent[-1]["message"]
    assert "which invoices are overdue?" in context
    assert HELD not in context, "the held request was replayed to the agent"
    assert NOTICE_HEAD not in context
    marks = {r.content[:12]: r.gate_hold for r in public.rows()}
    assert marks[HELD[:12]] == "gate-3402-public"


def test_a_refused_public_turn_stays_out_and_says_why_on_the_row(public):
    public.send(HELD, raise_=SkillGateRefused(429, "approval_rate_limited", "Too many gated requests."))
    public.send("ok")
    assert HELD not in public.sent[-1]["message"]
    assert {r.gate_hold for r in public.rows() if r.role == "user" and r.content == HELD} == {
        "refused:approval_rate_limited"}


def test_a_message_naming_a_gated_skill_is_marked_before_the_gate_decides(public):
    """A second message built while the first is still being checked must not
    replay it: the row is marked the moment it is written."""
    seen = {}

    async def _peek(**kw):
        seen["rows"] = [(r.content, r.gate_hold) for r in public.rows()]
        raise _held("gate-3402-peek")

    import services.task_execution_service as tes
    with patch.object(tes, "dispatch_and_await_terminal", _peek):
        public.send(HELD)
    assert seen["rows"] == [(HELD, "named")]


@pytest.mark.parametrize("unreadable_reads, expected", [
    (1, (None, True)),                          # the map is back by dispatch: the turn runs
    (2, ("refused:gate_unavailable", False)),   # still unreadable: the backstop refuses it
], ids=["back-by-dispatch", "still-unreadable"])
def test_an_unreadable_gate_map_never_hides_a_message_for_good(public, monkeypatch,
                                                               unreadable_reads, expected):
    """The mark written with the row fails OPEN: nothing ever clears `named`, so
    a turn that then runs would lose its message from every later prompt while
    its reply stayed. The verdict marks a turn the gate stops — still unreadable
    at dispatch, the backstop refuses it."""
    import services.task_execution_service as tes

    _unreadable_for(monkeypatch, unreadable_reads)
    run = tes.dispatch_and_await_terminal          # the fixture's stub

    async def _backstop_first(**kw):
        _GATE.read_gates(AGENT)                    # what the dispatch-time backstop reads first
        return await run(**kw)

    monkeypatch.setattr(tes, "dispatch_and_await_terminal", _backstop_first)
    public.send("which invoices are overdue?")
    (row,) = [r for r in public.rows() if r.role == "user"]
    public.send("thanks")
    assert (row.gate_hold, "which invoices are overdue?" in public.sent[-1]["message"]) == expected


def test_an_async_public_turn_hands_the_background_the_stored_message(public, monkeypatch):
    spawned = AsyncMock()
    monkeypatch.setattr(public.pcs, "_execute_public_chat_background", spawned)
    out = public.send(HELD, async_mode=True)
    assert out["status"] == "accepted"
    (user_row,) = [r for r in public.rows() if r.role == "user"]
    assert spawned.call_args.kwargs["user_message_id"] == user_row.id


def test_the_async_background_marks_the_request_and_the_notice(public):
    db = public.db
    session = db.get_or_create_public_chat_session(public.link["id"], "anon-bg", "anonymous")
    user_row = db.add_public_chat_message(session.id, "user", HELD)
    import services.task_execution_service as tes

    async def _hold(**_kw):
        raise _held("gate-3402-async")

    with patch.object(tes, "dispatch_and_await_terminal", _hold):
        asyncio.run(public.pcs._execute_public_chat_background(
            agent_name=AGENT, context_prompt="ctx", source_email="anonymous (203.0.113.7)",
            execution_id="ex-bg", chat_session_id=session.id, session_identifier="anon-bg",
            identifier_type="anonymous", request_text=HELD, user_message_id=user_row.id))

    rows = db.get_recent_public_chat_messages(session.id, include_held=True)
    assert [(r.role, r.gate_hold) for r in rows] == [("user", "gate-3402-async"),
                                                       ("assistant", "gate-3402-async")]
    assert rows[1].content.startswith(NOTICE_HEAD)
    assert db.get_recent_public_chat_messages(session.id) == []


def test_the_visitor_still_sees_their_message_and_the_notice(public, monkeypatch):
    from routers import public as public_router

    public.send(HELD, raise_=_held())
    monkeypatch.setattr(public_router, "_get_client_ip", lambda request: "203.0.113.7")
    monkeypatch.setattr(public_router, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(public_router, "_validate_public_link", lambda token: public.link)
    out = asyncio.run(public_router.get_public_chat_history(
        "tok", None, session_token=None, session_id="anon-3402"))
    contents = [m["content"] for m in out.messages]
    assert HELD in contents and any(c.startswith(NOTICE_HEAD) for c in contents)


def test_mem001_never_summarises_a_held_request(public, monkeypatch):
    from services import platform_prompt_service as pps
    from services import settings_service

    db = public.db
    session = db.get_or_create_public_chat_session(public.link["id"], "v@example.com", "email")
    for role, content in (("user", "what is my balance?"), ("assistant", "It is 10 EUR.")):
        db.add_public_chat_message(session.id, role, content, sender_email="v@example.com")
    held_user = db.add_public_chat_message(session.id, "user", HELD, sender_email="v@example.com")
    notice = db.add_public_chat_message(session.id, "assistant", f"{NOTICE_HEAD} waiting",
                                        sender_email="v@example.com")
    db.mark_public_chat_messages_held([held_user.id, notice.id], "gate-mem")

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"content": [{"text": "- likes short answers"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["prompt"] = json["messages"][0]["content"]
            return _Resp()

    monkeypatch.setattr(settings_service, "get_anthropic_api_key", lambda: "test-anthropic-key-placeholder")
    monkeypatch.setattr(pps.httpx, "AsyncClient", _Client)
    asyncio.run(pps.summarize_user_memory_background(AGENT, "v@example.com", session.id))
    assert "what is my balance?" in captured["prompt"]
    assert HELD not in captured["prompt"] and NOTICE_HEAD not in captured["prompt"]


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_held_rows_are_left_out_by_default_and_a_verdict_never_unmarks(db_backend):
    from database import DatabaseManager, db

    session = db.get_or_create_public_chat_session("link-store", "anon-store", "anonymous")
    plain = db.add_public_chat_message(session.id, "user", "hello")
    named = db.add_public_chat_message(session.id, "user", HELD, gate_hold="named")
    assert db.mark_public_chat_messages_held([named.id], "gate-x") == 1      # verdict overwrites `named`
    assert db.mark_public_chat_messages_held([named.id], "refused:other") == 0   # …but never a verdict
    assert db.mark_public_chat_messages_held([plain.id], "gate-y") == 1

    assert db.get_recent_public_chat_messages(session.id) == []
    every = db.get_recent_public_chat_messages(session.id, include_held=True)
    assert {(r.content, r.gate_hold) for r in every} == {("hello", "gate-y"), (HELD, "gate-x")}
    assert "Previous conversation" not in db.build_public_chat_context(session.id, "next")
    for name in ("mark_public_chat_messages_held", "get_recent_public_chat_messages",
                 "add_public_chat_message"):
        assert callable(getattr(DatabaseManager, name, None)), name


# The shapes from before this change, written out: a fresh schema built from
# `db/schema.py` already has the new columns, and dropping them again needs
# SQLite's DROP COLUMN, which cannot rewrite these tables' DDL on every SQLite
# build (a comment sits before their last column).
_OLD_SHAPE = (
    "CREATE TABLE public_chat_messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
    "role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT NOT NULL, cost REAL, "
    "sender_email TEXT, sender_label TEXT)",
    "CREATE TABLE enterprise_portal_messages (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, "
    "client_email TEXT NOT NULL, session_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, "
    "cost REAL, created_at TEXT NOT NULL, source TEXT, voice_call_id TEXT, attachments TEXT, "
    "execution_id TEXT)",
)


def test_the_registered_migration_upgrades_the_old_shape_twice_safely(tmp_path):
    """Run through the MIGRATIONS registry on the OLD shape (the 2026-10-01
    learning): the columns and the table appear, and the backfill marks a
    Workspace turn a gate record names — and nothing else — exactly once."""
    import sqlite3

    from db import migrations
    from db.schema import TABLES

    conn = sqlite3.connect(tmp_path / "old.db")
    for ddl in _OLD_SHAPE:
        conn.execute(ddl)
    conn.execute(TABLES["skill_gate_requests"])
    rows = [("u-held", "user", HELD, "ex-held"), ("n-held", "assistant", "Not run: …", "ex-held"),
            ("u-ok", "user", "hello", "ex-ok"), ("old", "user", "no turn id", None)]
    for mid, role, content, eid in rows:
        conn.execute("INSERT INTO enterprise_portal_messages (id, agent_name, client_email, "
                     "session_id, role, content, created_at, execution_id) "
                     "VALUES (?, 'fin', 'c@example.com', 'ps', ?, ?, '2026-10-20T10:00:00Z', ?)",
                     (mid, role, content, eid))
    for rid, origin in (("gate-old", "ex-held"), ("gate-noorigin", None)):
        conn.execute("INSERT INTO skill_gate_requests (request_id, agent_name, skills, request_text, "
                     "requester_kind, requester_key, origin_execution_id, dispatch, state, created_at) "
                     "VALUES (?, 'fin', '[]', '', 'person', 'person:c', ?, '{}', 'pending', 't')",
                     (rid, origin))
    conn.commit()

    migrate = dict(migrations.MIGRATIONS)["held_gate_turns"]
    for _ in range(2):                     # re-running is a no-op
        migrate(conn.cursor(), conn)
        conn.commit()

    for table in ("public_chat_messages", "enterprise_portal_messages"):
        assert "gate_hold" in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'room_gate_holds'").fetchone()
    marks = dict(conn.execute("SELECT id, gate_hold FROM enterprise_portal_messages"))
    conn.close()
    assert marks == {"u-held": "gate-old", "n-held": "gate-old", "u-ok": None, "old": None}


def test_the_alembic_revision_carries_the_same_change():
    """A pin for the PostgreSQL track, whose live consumer is CI's real
    `alembic upgrade head` (`pg-migrations`); single-head is `check_alembic_heads`."""
    versions = _BACKEND / "migrations" / "versions"
    hits = [p for p in versions.glob("*.py") if "room_gate_holds" in p.read_text()]
    assert len(hits) == 1, f"expected one Alembic revision creating room_gate_holds, found {hits}"
    src = hits[0].read_text()
    for needle in ("ADD COLUMN IF NOT EXISTS gate_hold", "CREATE TABLE IF NOT EXISTS room_gate_holds",
                   "op.execute(HELD_PORTAL_ROWS_BACKFILL)",
                   "DROP TABLE IF EXISTS room_gate_holds", "DROP COLUMN IF EXISTS gate_hold"):
        assert needle in src, needle


# ---------------------------------------------------------------------------
# Telegram groups — the router (the ent#600 harness)
# ---------------------------------------------------------------------------

import adapters.message_router as _MR  # noqa: E402
from adapters.base import NormalizedMessage  # noqa: E402
from adapters.message_router import ChannelMessageRouter  # noqa: E402


def _adapter():
    a = MagicMock()
    a.channel_type = "telegram"
    a.get_agent_name = AsyncMock(return_value=AGENT)
    for name in ("enrich_message", "record_inbound_activity", "set_group_verified",
                 "prompt_group_auth", "prompt_auth", "indicate_processing", "indicate_progress",
                 "indicate_done", "send_response", "on_response_sent", "note_untagged_seen"):
        setattr(a, name, AsyncMock(return_value=None))
    a.handle_verification = AsyncMock(return_value=True)
    a.resolve_verified_email = AsyncMock(return_value=None)
    a.is_group_verified = AsyncMock(return_value=True)
    a.group_context_enabled = AsyncMock(return_value=True)
    a.get_bot_token = MagicMock(return_value="tok")
    a.get_rate_key = MagicMock(return_value="rk")
    a.get_session_identifier = MagicMock(return_value="77:group:-100")
    a.get_source_identifier = MagicMock(return_value="telegram:77:9")
    return a


def _group(text, *, observe_only=False, untagged=False, quoted=None):
    raw = {"from": {"id": 9, "first_name": "Alice"}}
    if quoted:
        raw["reply_to_message"] = {"from": {"id": 5, "first_name": "Eve"}, "text": quoted}
    return NormalizedMessage(
        sender_id="9", text=text, channel_id="-100", thread_id="42", timestamp="1",
        metadata={"is_group": True, "chat_title": "Builders", "username": "alice", "bot_id": "77",
                  "agent_name": AGENT, "sender_display_name": "Alice", "sender_username": "alice",
                  "observe_only": observe_only, "untagged": untagged,
                  "raw_message": raw, "progress_ack_eligible": not observe_only})


@contextmanager
def _router_env(execute):
    db = MagicMock()
    db.get_access_policy.return_value = {"require_email": False, "open_access": True,
                                         "group_auth_mode": "none"}
    db.get_or_create_public_chat_session.return_value = {"id": "s1", "message_count": 3}
    db.get_recent_public_chat_messages.return_value = []
    db.add_public_chat_message.return_value = SimpleNamespace(id="row-step7")
    db.get_or_create_public_user_memory.return_value = {}
    service = MagicMock()
    service.execute_task = execute
    with patch.object(_MR, "db", db), \
         patch.object(_MR, "get_agent_container", return_value=MagicMock(status="running")), \
         patch.object(_MR, "get_task_execution_service", return_value=service), \
         patch.object(_MR, "_check_rate_limit", return_value=True), \
         patch.object(_MR, "process_voice", new=AsyncMock(return_value="")), \
         patch.object(_MR, "format_user_memory_block", return_value=None), \
         patch.object(_MR, "summarize_user_memory_background", new=AsyncMock()):
        yield db


def _turn(message, execute):
    with _router_env(execute) as db:
        asyncio.run(ChannelMessageRouter()._handle_message_inner(_adapter(), message))
    return db


def test_a_held_group_turn_marks_the_stored_message_with_the_request(gated):
    db = _turn(_group(f"@bot {HELD}"), AsyncMock(side_effect=_held("gate-3402-group")))
    db.mark_public_chat_messages_held.assert_called_once_with(["row-step7"], "gate-3402-group")


def test_a_hold_caused_by_the_reply_quote_still_marks_the_message(gated):
    """The sender's own words name nothing gated; the quoted message did."""
    db = _turn(_group("@bot do what she said", quoted=HELD),
               AsyncMock(side_effect=_held("gate-3402-quote")))
    db.mark_public_chat_messages_held.assert_called_once_with(["row-step7"], "gate-3402-quote")
    assert "gate_hold" not in db.add_public_chat_message.call_args.kwargs     # not pre-marked


def test_untagged_group_chatter_naming_a_gated_skill_is_marked_when_recorded(gated):
    db = _turn(_group(f"someone please {HELD}", observe_only=True, untagged=True), AsyncMock())
    assert db.add_public_chat_message.call_args.kwargs.get("gate_hold") == "named"


def test_ordinary_chatter_is_recorded_exactly_as_before(gated):
    db = _turn(_group("standup moved to 11", observe_only=True, untagged=True), AsyncMock())
    assert "gate_hold" not in db.add_public_chat_message.call_args.kwargs


def test_chatter_recorded_while_the_gate_map_is_unreadable_is_stored_unmarked(gated, monkeypatch):
    """Chatter is never dispatched, so no verdict would ever clear a mark the
    blip wrote: the group would lose it for good."""
    _unreadable_for(monkeypatch, 1)
    db = _turn(_group("standup moved to 11", observe_only=True, untagged=True), AsyncMock())
    assert db.add_public_chat_message.called
    assert "gate_hold" not in db.add_public_chat_message.call_args.kwargs


def test_the_group_history_block_leaves_marked_rows_out(db_backend):
    from database import db

    session = db.get_or_create_public_chat_session(AGENT, "77:group:-100", "telegram")
    db.add_public_chat_message(session.id, "user", "standup moved to 11", sender_label="Bob (@bob)")
    db.add_public_chat_message(session.id, "user", f"someone please {HELD}",
                               sender_label="Eve (@eve)", gate_hold="named")
    block = ChannelMessageRouter._group_history_block(session.id, AGENT)
    assert "standup moved to 11" in block
    assert HELD not in block

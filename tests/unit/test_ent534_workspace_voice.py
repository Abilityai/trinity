"""trinity-enterprise#534 — Workspace voice mode: the orb takes the conversation.

The Workspace's real-time voice call is the platform's existing realtime voice
session started from, and written back into, a Workspace thread. The
properties pinned here are the ones both independent plan reviews found the
obvious build gets wrong:

  * **Write-as-you-go, on the live worker.** Save-at-end double-writes under two
    uvicorn workers (a `/stop` landing off-worker reconstructs an EMPTY session
    from Redis) and loses the call on a restart. Each spoken turn is inserted
    when the provider reports it; a call with no turns writes nothing.
  * **Grouped by call id, not by an opener row.** The history window is 100
    rows and a 30-minute call is more, so the grouping key rides on EVERY row.
  * **Strictly increasing stamps.** `get_portal_messages` orders by
    `created_at` alone; two rows of one turn arrive in one tick.
  * **One uniform 404.** Off-roster, a foreign thread and a portal-token client
    are indistinguishable at the start route (Invariant #8).
  * **The session outlives the provider connection.** Compression + resumption
    are requested, a `go_away` is a reconnect, and the cap ends the call WITH
    a reason, set before `end_session` cancels the watchdog's own task.
  * **The read side is a parameter of the principal.** A platform user reads
    every canvas audience in the Workspace; a client stays `roster`.

Runs the persistence tests against a throwaway sqlite carrying the real
`enterprise_portal_messages` / `enterprise_portal_sessions` tables (with the
new columns), so the invariant under test is the schema's own.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

ALICE = "alice@example.com"
AGENT = "scribe"
THREAD = "thread-1"
CALL = "vs_call_1"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Harness — the real portal tables on a throwaway sqlite (ent#523's pattern)
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent534.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    )
    engine = get_engine()
    oss_metadata.create_all(engine, tables=[enterprise_portal_messages, enterprise_portal_sessions])
    yield engine


class _Session(types.SimpleNamespace):
    """A voice session as the persistence helpers see it — plain fields."""

    def __init__(self, **kw):
        base = dict(
            session_id=CALL, agent_name=AGENT, client_email=ALICE,
            portal_session_id=THREAD, max_duration=1800,
        )
        base.update(kw)
        super().__init__(**base)


# ---------------------------------------------------------------------------
# Schema — both tracks, additive
# ---------------------------------------------------------------------------

def test_columns_are_declared_on_every_track():
    from db import schema, tables
    ddl = schema.TABLES["enterprise_portal_messages"]
    assert "source TEXT" in ddl and "voice_call_id TEXT" in ddl
    cols = {c.name for c in tables.enterprise_portal_messages.columns}
    assert {"source", "voice_call_id"} <= cols
    from db import migrations
    names = [name for name, _fn in migrations.MIGRATIONS] if hasattr(migrations, "MIGRATIONS") else []
    src = (_BACKEND / "db" / "migrations.py").read_text()
    assert '("portal_messages_voice_source", _migrate_portal_messages_voice_source)' in src
    alembic = (_BACKEND / "migrations" / "versions" / "0056_portal_messages_voice_source.py").read_text()
    assert 'down_revision = "0055_portal_session_main_chat"' in alembic
    assert "ADD COLUMN IF NOT EXISTS source TEXT" in alembic
    assert "ADD COLUMN IF NOT EXISTS voice_call_id TEXT" in alembic
    assert names == [] or "portal_messages_voice_source" in names


# ---------------------------------------------------------------------------
# The roster capability field
# ---------------------------------------------------------------------------

def test_realtime_voice_is_never_offered_to_a_portal_token_client(monkeypatch):
    import config
    from client_portal.voice import realtime_voice_capability
    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    cap = realtime_voice_capability(False)
    assert cap.available is False and cap.reason is None


def test_realtime_voice_names_the_reason_for_a_platform_user(monkeypatch):
    import config
    from client_portal.voice import (
        REASON_DISABLED, REASON_NO_KEY, realtime_voice_capability,
    )
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    assert realtime_voice_capability(True).reason == REASON_DISABLED
    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert realtime_voice_capability(True).reason == REASON_NO_KEY
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    cap = realtime_voice_capability(True)
    assert cap.available is True and cap.reason is None
    # Provider-neutral copy (ent#354): the reason is about voice, not the vendor.
    assert "gemini" not in (REASON_DISABLED + REASON_NO_KEY).lower()


def test_the_roster_carries_the_field_and_the_model_defaults_closed():
    from client_portal.models import PortalRoster, PortalRealtimeVoice
    roster = PortalRoster(agents=[])
    assert roster.realtime_voice == PortalRealtimeVoice(available=False, reason=None)
    src = (_BACKEND / "client_portal" / "service.py").read_text()
    assert "realtime_voice=realtime_voice_capability(include_owned)" in src


# ---------------------------------------------------------------------------
# The start path — one uniform 404, then the session with the Workspace's shape
# ---------------------------------------------------------------------------

@pytest.fixture()
def start_env(monkeypatch):
    """Stub everything past the gates so `start_workspace_voice` is testable."""
    import client_portal.voice as wv
    import client_portal.service as svc
    import config
    # Patch where the code RESOLVES, not where it lives (learnings 2026-08-10):
    # `client_portal/voice.py` imports these function-locally, which reads
    # `sys.modules[...]` at call time — the package attribute can be a
    # different module object once an earlier test has replaced one.
    gemini_voice = sys.modules["services.gemini_voice"] if "services.gemini_voice" in sys.modules else __import__("services.gemini_voice", fromlist=["x"])
    voice_prompt_service = sys.modules["services.voice_prompt_service"] if "services.voice_prompt_service" in sys.modules else __import__("services.voice_prompt_service", fromlist=["x"])

    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "WORKSPACE_VOICE_MAX_DURATION", 1800)
    monkeypatch.setattr(svc, "agent_on_roster", lambda agent, email, inc: agent == AGENT)
    monkeypatch.setattr(wv.db, "get_portal_session",
                        lambda sid, agent, email: {"id": sid} if sid == THREAD else None)
    monkeypatch.setattr(wv.db, "get_portal_messages", lambda *a, **k: [
        {"role": "user", "content": "hello there", "source": None},
        {"role": "system", "content": "Main was reset.", "source": None},
        {"role": "assistant", "content": "hi", "source": "voice", "voice_call_id": "vs_old"},
    ])
    monkeypatch.setattr(voice_prompt_service, "get_voice_system_prompt", AsyncMock(return_value="You are Scribe."))
    created = types.SimpleNamespace(session_id="vs_new")
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(gemini_voice.voice_service, "create_session", create)
    import database
    monkeypatch.setattr(database.db, "get_voice_name", lambda agent: "Kore", raising=False)
    return create


def _start(**over):
    from client_portal.voice import start_workspace_voice
    kwargs = dict(agent_name=AGENT, email=ALICE, is_platform=True, portal_session_id=THREAD,
                  user_id=7, user_label="alice")
    kwargs.update(over)
    return _run(start_workspace_voice(**kwargs))


def test_off_roster_foreign_thread_and_portal_token_are_one_uniform_404(start_env):
    from client_portal.service import ClientPortalError
    details = set()
    for over in (dict(agent_name="other"), dict(portal_session_id="not-mine"), dict(is_platform=False)):
        with pytest.raises(ClientPortalError) as exc:
            _start(**over)
        assert exc.value.status_code == 404
        details.add(exc.value.detail)
    assert details == {"Conversation not found"}
    start_env.assert_not_called()


def test_unavailable_voice_is_a_503_after_the_gates(start_env, monkeypatch):
    import config
    from client_portal.service import ClientPortalError
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(ClientPortalError) as exc:
        _start()
    assert exc.value.status_code == 503
    # The gates still come first: an off-roster caller on an unavailable
    # instance learns nothing beyond the 404.
    with pytest.raises(ClientPortalError) as exc2:
        _start(agent_name="other")
    assert exc2.value.status_code == 404


def test_the_session_is_created_with_the_workspace_shape(start_env):
    result = _start()
    kwargs = start_env.call_args.kwargs
    assert kwargs["portal_session_id"] == THREAD
    assert kwargs["client_email"] == ALICE
    assert kwargs["chat_session_id"] is None
    assert kwargs["workspace_mode"] is True
    assert kwargs["max_duration"] == 1800
    # An internal user's call is an operator surface; `roster` would only add
    # exposure now that platform principals read every audience (V8).
    assert kwargs["canvas_audience"] == "operator"
    assert kwargs["user_id"] == 7
    prompt = kwargs["system_prompt"]
    assert prompt.startswith("You are Scribe.")
    assert "## Conversation so far:" in prompt
    assert "User: hello there" in prompt
    assert "Assistant (voice): hi" in prompt          # an earlier call's rows are labelled
    assert "Main was reset." not in prompt             # platform lines are not a party
    assert "## Visual Canvas" in prompt                # the panel tools are described
    assert result == {
        "voice_session_id": "vs_new",
        "websocket_url": "/ws/voice/vs_new",
        "portal_session_id": THREAD,
        "max_duration_seconds": 1800,
    }


def test_the_route_is_under_the_portal_principal_and_refuses_a_portal_token_first():
    """The route inherits every Workspace gate by construction; a portal-token
    principal is a uniform 404 before any roster or limiter work."""
    from fastapi import HTTPException
    from client_portal import router
    from client_portal.portal_auth import PortalPrincipal
    from client_portal.models import PortalVoiceStartRequest
    with pytest.raises(HTTPException) as exc:
        _run(router.portal_voice_start(
            AGENT, PortalVoiceStartRequest(portal_session_id=THREAD), request=MagicMock(),
            principal=PortalPrincipal("client@example.com", False), token="t",
        ))
    assert exc.value.status_code == 404
    src = (_BACKEND / "client_portal" / "router.py").read_text()
    body = src[src.index("async def portal_voice_start"):src.index("async def portal_stt")]
    assert "Depends(get_portal_principal)" in body
    assert body.index("_require_roster(") < body.index("rate_limiter.enforce(")
    assert "portal_voice_start:" in body
    # The OSS voice router does not grow a portal variant — one start path per surface.
    voice_src = (_BACKEND / "routers" / "voice.py").read_text()
    assert "portal_session_id" not in voice_src[voice_src.index("async def voice_start"):voice_src.index("async def voice_stop")]


# ---------------------------------------------------------------------------
# Write-as-you-go persistence
# ---------------------------------------------------------------------------

def _rows(engine, thread=THREAD):
    from sqlalchemy import text
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT role, content, source, voice_call_id, created_at FROM enterprise_portal_messages "
            "WHERE session_id = :s ORDER BY created_at"), {"s": thread}).mappings()]


def _mk_thread(engine, sid=THREAD):
    from client_portal import db as pdb
    pdb.create_portal_session(sid, AGENT, ALICE, "2026-09-07T10:00:00Z")


def test_each_turn_is_a_row_with_the_call_id_and_the_call_closes_with_one_label(portal_db):
    from client_portal import db as pdb
    from client_portal.voice import persist_voice_call_end, persist_voice_turn
    _mk_thread(portal_db)
    s = _Session()
    assert persist_voice_turn(s, "user", "  what's on the board? ") is True
    assert persist_voice_turn(s, "assistant", "three items") is True
    assert persist_voice_turn(s, "assistant", "   ") is False       # nothing said, nothing written
    rows = _rows(portal_db)
    assert [(r["role"], r["content"]) for r in rows] == [("user", "what's on the board?"), ("assistant", "three items")]
    assert {r["source"] for r in rows} == {"voice"}
    assert {r["voice_call_id"] for r in rows} == {CALL}
    n = persist_voice_call_end(s, duration_seconds=250)
    assert n == 3
    rows = _rows(portal_db)
    assert rows[-1]["role"] == "system" and rows[-1]["content"] == "Voice call · 4 min"
    assert rows[-1]["voice_call_id"] == CALL and rows[-1]["source"] == "voice"
    sess = pdb.get_portal_session(THREAD, AGENT, ALICE)
    assert sess["message_count"] == 3                                  # the ent#457 pairing
    assert sess["title"]                                               # named from the first spoken line
    # Closing twice writes nothing more.
    assert persist_voice_call_end(s, duration_seconds=250) == 3
    assert len(_rows(portal_db)) == 3


def test_a_call_with_no_turns_leaves_no_tombstone(portal_db):
    from client_portal import db as pdb
    from client_portal.voice import persist_voice_call_end
    _mk_thread(portal_db)
    assert persist_voice_call_end(_Session(), duration_seconds=3) == 0
    assert _rows(portal_db) == []
    assert pdb.get_portal_session(THREAD, AGENT, ALICE)["message_count"] == 0


def test_a_reconstructed_cross_worker_session_never_writes(portal_db):
    """The off-worker `/stop` holds a session rebuilt from Redis: no turns were
    ever reported to it, so the end path writes nothing — the phantom
    "Voice call · 0 min" both reviews predicted cannot exist."""
    from client_portal.voice import persist_voice_call_end
    _mk_thread(portal_db)
    rebuilt = _Session()                # no `_turns_saved` attribute at all
    assert persist_voice_call_end(rebuilt, duration_seconds=900) == 0
    assert _rows(portal_db) == []


def test_stamps_are_strictly_increasing_even_within_one_tick(portal_db, monkeypatch):
    import client_portal.voice as wv
    from datetime import datetime, timezone
    frozen = datetime(2026, 9, 7, 10, 0, 0, 123456, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen
    monkeypatch.setattr(wv, "datetime", _Frozen)
    _mk_thread(portal_db)
    s = _Session()
    wv.persist_voice_turn(s, "user", "a")
    wv.persist_voice_turn(s, "assistant", "b")
    wv.persist_voice_turn(s, "user", "c")
    stamps = [r["created_at"] for r in _rows(portal_db)]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3
    assert [r["content"] for r in _rows(portal_db)] == ["a", "b", "c"]


def test_spoken_text_is_scrubbed_like_every_free_text_sink(portal_db, monkeypatch):
    from services import runtime_secret_scrub as scrub
    from client_portal.voice import persist_voice_turn
    monkeypatch.setattr(scrub, "get_staged_values", lambda: ["sk-verysecret"])
    _mk_thread(portal_db)
    persist_voice_turn(_Session(), "assistant", "the key is sk-verysecret ok")
    content = _rows(portal_db)[0]["content"]
    assert "sk-verysecret" not in content


def test_the_call_label_says_how_it_ended():
    from client_portal.voice import call_label
    assert call_label(12) == "Voice call · 1 min"
    assert call_label(1800, "cap", 1800) == "Voice call · 30 min · ended at the 30-minute limit"
    assert call_label(70, "error", 1800, "The voice provider returned an error.") == \
        "Voice call · 1 min · ended early: The voice provider returned an error."
    assert call_label(70, "provider_closed", 1800).endswith("ended early: the voice connection was lost")


# ---------------------------------------------------------------------------
# The readers — history context, dedup, the history model
# ---------------------------------------------------------------------------

def test_history_context_labels_spoken_rows_and_budgets_a_long_call():
    from client_portal.service import _format_history_context, _VOICE_CONTEXT_ROWS_PER_CALL
    rows = [{"role": "user", "content": "typed first", "source": None}]
    for i in range(20):
        rows.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"spoken {i}",
                     "source": "voice", "voice_call_id": "c1"})
    rows.append({"role": "system", "content": "Voice call · 9 min", "source": "voice", "voice_call_id": "c1"})
    rows.append({"role": "assistant", "content": "typed reply", "source": None})
    out = _format_history_context(rows)
    lines = out.splitlines()
    assert lines[1] == "Client: typed first"
    assert lines[2] == f"[{20 - _VOICE_CONTEXT_ROWS_PER_CALL} earlier spoken turns of a voice call omitted]"
    spoken = [l for l in lines if "(voice)" in l]
    assert len(spoken) == _VOICE_CONTEXT_ROWS_PER_CALL
    assert spoken[-1] == "You (voice): spoken 19"
    assert "Voice call · 9 min" not in out            # the platform's label is not a party
    assert lines[-1] == "You: typed reply"


def test_a_spoken_last_line_does_not_swallow_the_same_words_typed(monkeypatch):
    import client_portal.service as svc
    writes = []
    monkeypatch.setattr(svc.db, "get_portal_messages", lambda *a, **k: [
        {"role": "user", "content": "show me the plan", "source": "voice", "voice_call_id": "c1"}])
    monkeypatch.setattr(svc.db, "add_portal_message", lambda *a, **k: writes.append((a, k)))
    monkeypatch.setattr(svc.db, "touch_portal_session", lambda *a, **k: None)
    svc._persist_user_turn(AGENT, ALICE, THREAD, "show me the plan")
    assert len(writes) == 1                                           # a new message, not a retry
    # …while a genuinely repeated TYPED row is still deduplicated.
    monkeypatch.setattr(svc.db, "get_portal_messages", lambda *a, **k: [
        {"role": "user", "content": "show me the plan", "source": None}])
    svc._persist_user_turn(AGENT, ALICE, THREAD, "show me the plan")
    assert len(writes) == 1


def test_history_rows_carry_the_grouping_key_to_the_client(portal_db):
    from client_portal import db as pdb
    from client_portal.models import PortalHistoryMessage
    _mk_thread(portal_db)
    pdb.add_portal_message("m1", AGENT, ALICE, "user", "hi", None, "2026-09-07T10:00:01Z",
                           session_id=THREAD, source="voice", voice_call_id=CALL)
    pdb.add_portal_message("m2", AGENT, ALICE, "user", "typed", None, "2026-09-07T10:00:02Z", session_id=THREAD)
    rows = pdb.get_portal_messages(AGENT, ALICE, session_id=THREAD)
    assert rows[0]["source"] == "voice" and rows[0]["voice_call_id"] == CALL
    assert rows[1]["source"] is None and rows[1]["voice_call_id"] is None
    m = PortalHistoryMessage(**{k: rows[0][k] for k in ("id", "role", "content", "cost", "created_at", "source", "voice_call_id")})
    assert m.source == "voice" and m.voice_call_id == CALL
    assert PortalHistoryMessage(role="user", content="x").source is None


# ---------------------------------------------------------------------------
# The voice service — the session outlives the connection, the cap has words
# ---------------------------------------------------------------------------

def _voice_module():
    """The real service where the SDK is installed; the stubbed one otherwise
    (test_voice_tools' shape), so this file runs in both environments."""
    try:
        import google.genai  # noqa: F401
    except ImportError:
        pytest.skip("google-genai not installed; lifetime tests need the real types")
    from services import gemini_voice
    return gemini_voice


def test_every_session_asks_for_compression_and_resumption():
    gv = _voice_module()
    from google.genai import types as t
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p", workspace_mode=True)
    s._resumption_handle = "h-1"
    cfg = gv.GeminiVoiceService()._build_live_config(s)
    assert isinstance(cfg.context_window_compression, t.ContextWindowCompressionConfig)
    assert cfg.context_window_compression.sliding_window is not None
    assert isinstance(cfg.session_resumption, t.SessionResumptionConfig)
    assert cfg.session_resumption.handle == "h-1"
    assert len(cfg.tools) == 2                                         # run_task + the panel verbs


def test_go_away_is_a_reconnect_and_the_resumption_handle_is_kept():
    gv = _voice_module()
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p")
    s._active = True
    frames = [
        types.SimpleNamespace(session_resumption_update=types.SimpleNamespace(new_handle="h-2", resumable=True),
                              go_away=None, tool_call=None, server_content=None),
        types.SimpleNamespace(session_resumption_update=None,
                              go_away=types.SimpleNamespace(time_left="10s"), tool_call=None, server_content=None),
    ]

    class _Turn:
        def __init__(self, items): self._items = list(items)
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._items: raise StopAsyncIteration
            return self._items.pop(0)

    s._gemini_session = types.SimpleNamespace(receive=lambda: _Turn(frames))
    _run(gv.GeminiVoiceService()._receive_audio_loop(s))
    assert s._resumption_handle == "h-2"
    assert s._go_away is True
    assert s._active is True                                           # a go_away is not the end of the call


def test_a_new_connection_leg_resumes_the_turn_a_go_away_interrupted():
    """Review I1: the leg after a mid-turn go_away must not overwrite the
    mirrored partial text with the first new chunk."""
    gv = _voice_module()
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p")
    s._active = True
    s._partial_user_text = "what is "
    seen = []

    async def on_turn(role, text): seen.append((role, text))
    s._on_turn = on_turn
    content = types.SimpleNamespace(
        model_turn=None, turn_complete=True,
        input_transcription=types.SimpleNamespace(text="two plus two"),
        output_transcription=None,
    )
    frame = types.SimpleNamespace(session_resumption_update=None, go_away=None, tool_call=None, server_content=content)

    class _Turn:
        def __init__(self, items): self._items = list(items)
        def __aiter__(self): return self
        async def __anext__(self):
            if not self._items:
                s._active = False
                raise StopAsyncIteration
            return self._items.pop(0)

    s._gemini_session = types.SimpleNamespace(receive=lambda: _Turn([frame]))
    _run(gv.GeminiVoiceService()._receive_audio_loop(s))
    assert seen == [("user", "what is two plus two")]


def test_the_send_loop_stops_at_a_go_away_without_ending_the_call():
    gv = _voice_module()
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p")
    s._active = True
    s._go_away = True
    _run(asyncio.wait_for(gv.GeminiVoiceService()._send_audio_loop(s), 1))
    assert s._active is True


def test_the_cap_warns_out_loud_then_ends_with_a_reason(monkeypatch):
    gv = _voice_module()
    svc = gv.GeminiVoiceService()
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p", max_duration=1800)
    s._active = True
    sent = []
    s._gemini_session = types.SimpleNamespace(send_realtime_input=AsyncMock(side_effect=lambda **k: sent.append(k)))
    svc._sessions["vs"] = s
    sleeps = []

    async def fake_sleep(n): sleeps.append(n)
    monkeypatch.setattr(gv.asyncio, "sleep", fake_sleep)
    _run(svc._timeout_watchdog(s))
    assert sleeps == [1800 - gv.CAP_WARNING_LEAD_SECONDS, gv.CAP_WARNING_LEAD_SECONDS]
    assert sent and "text" in sent[0] and "thirty seconds" in sent[0]["text"]
    assert s.end_reason == "cap"
    assert "30-minute" in s.end_message
    assert s._active is False


def test_a_completed_turn_reaches_the_front_door_and_the_transcript():
    gv = _voice_module()
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p")
    seen = []

    async def on_turn(role, text): seen.append((role, text))
    s._on_turn = on_turn
    _run(gv.GeminiVoiceService()._record_turn(s, "user", "hello"))
    assert seen == [("user", "hello")]
    assert [(e.role, e.text) for e in s.transcript] == [("user", "hello")]

    async def boom(role, text): raise RuntimeError("sink down")
    s._on_turn = boom
    _run(gv.GeminiVoiceService()._record_turn(s, "assistant", "still recorded"))   # never takes the loop down
    assert s.transcript[-1].text == "still recorded"


def test_end_before_turn_complete_still_records_the_last_exchange():
    """Found live: End pressed one second after the answer landed recorded
    NOTHING, because the bridge only recorded a turn at `turn_complete`."""
    gv = _voice_module()
    svc = gv.GeminiVoiceService()
    s = gv.VoiceSession(session_id="vs", agent_name=AGENT, chat_session_id=None, user_id=1,
                        user_email="u", system_prompt="p")
    s._active = True
    seen = []

    async def on_turn(role, text): seen.append((role, text))
    s._on_turn = on_turn
    s._partial_user_text = "what is two plus two"
    s._partial_assistant_text = "2 plus 2 is 4."
    svc._sessions["vs"] = s
    ended = _run(svc.end_session("vs"))
    assert ended is s
    assert seen == [("user", "what is two plus two"), ("assistant", "2 plus 2 is 4.")]
    assert [e.text for e in s.transcript] == ["what is two plus two", "2 plus 2 is 4."]
    assert s._partial_user_text == "" and s._partial_assistant_text == ""
    # A second end has nothing left to flush.
    _run(svc.end_session("vs"))
    assert len(seen) == 2


def test_redis_metadata_round_trips_the_workspace_fields():
    gv = _voice_module()
    svc = gv.GeminiVoiceService()
    store = {}

    class _Redis:
        async def setex(self, key, ttl, value): store[key] = (ttl, value)
        async def get(self, key): return store.get(key, (None, None))[1]
        async def delete(self, key): store.pop(key, None)
    svc._redis = _Redis()
    created = _run(svc.create_session(
        agent_name=AGENT, chat_session_id=None, user_id=7, user_email="alice", system_prompt="p",
        workspace_mode=True, max_duration=1800, portal_session_id=THREAD, client_email=ALICE,
        canvas_audience="operator",
    ))
    ttl, _ = store[f"voice_session:{created.session_id}"]
    assert ttl == 1860                                                 # spans the call plus grace
    svc._sessions.clear()                                              # the other worker
    rebuilt = _run(svc.get_session(created.session_id))
    assert rebuilt.portal_session_id == THREAD
    assert rebuilt.client_email == ALICE
    assert rebuilt.canvas_audience == "operator"
    assert rebuilt.max_duration == 1800
    assert rebuilt.chat_session_id is None
    assert rebuilt.transcript == []                                    # …and has no turns to write


def test_the_transcript_save_claim_is_a_setnx_and_fails_open():
    gv = _voice_module()
    svc = gv.GeminiVoiceService()

    class _Redis:
        def __init__(self): self.keys = set()
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in self.keys: return None
            self.keys.add(key); return True
    svc._redis = _Redis()
    assert _run(svc.claim_transcript_save("vs")) is True
    assert _run(svc.claim_transcript_save("vs")) is False

    class _Down:
        async def set(self, *a, **k): raise ConnectionError("redis down")
    svc._redis = _Down()
    assert _run(svc.claim_transcript_save("vs2")) is True


# ---------------------------------------------------------------------------
# The OSS voice router — Agent Detail keeps save-at-end, once; Workspace never
# ---------------------------------------------------------------------------

def test_save_transcript_writes_once_and_skips_an_empty_session():
    from routers import voice
    session = types.SimpleNamespace(transcript=[], session_id="vs")
    assert voice._save_transcript(session) == 0
    session = types.SimpleNamespace(
        transcript=[types.SimpleNamespace(role="user", text="hi")], session_id="vs",
        chat_session_id="cs", agent_name=AGENT, user_id=1, user_email="u",
    )
    calls = []
    real_db = voice.db
    voice.db = types.SimpleNamespace(add_chat_message=lambda **k: calls.append(k))
    try:
        assert voice._save_transcript(session) == 1
        assert voice._save_transcript(session) == 0                    # the same-worker double call
    finally:
        voice.db = real_db
    assert len(calls) == 1


def test_the_websocket_persists_a_workspace_call_through_the_portal_module_only():
    src = (_BACKEND / "routers" / "voice.py").read_text()
    ws = src[src.index("async def voice_websocket"):src.index("async def _get_voice_system_prompt")]
    # JWT before the session lookup (no 4004-vs-4001 oracle).
    assert ws.index("jwt.decode(token") < ws.index("voice_service.get_session(voice_session_id)")
    # Turn-by-turn persistence is registered only for a portal-bound session…
    assert "from client_portal.voice import persist_voice_turn" in ws
    assert "on_turn=on_turn" in ws
    # …the end path closes the call there, and the Agent Detail path claims first.
    assert "persist_voice_call_end(" in ws
    assert "await _claim_save(voice_session_id)" in ws
    # The `saved` frame follows the write and precedes the close.
    assert ws.index("persist_voice_call_end(") < ws.index('"type": "saved"') < ws.index("await websocket.close()")
    stop = src[src.index("async def voice_stop"):src.index("async def voice_status")]
    assert 'getattr(session, "portal_session_id", None)' in stop


def test_the_status_frame_carries_the_end_reason():
    src = (_BACKEND / "routers" / "voice.py").read_text()
    ws = src[src.index("async def on_status(state: str)"):src.index("portal_session_id = getattr(session")]
    assert 'frame["reason"] = getattr(session, "end_reason", None)' in ws
    assert 'frame["message"] = getattr(session, "end_message", None)' in ws


# ---------------------------------------------------------------------------
# Config + compose — the knob reaches the container
# ---------------------------------------------------------------------------

def test_the_cap_knob_is_surface_scoped_and_wired_through_compose():
    import config
    assert config.WORKSPACE_VOICE_MAX_DURATION == 1800 or isinstance(config.WORKSPACE_VOICE_MAX_DURATION, int)
    root = _BACKEND.parent.parent
    for f in ("docker-compose.yml", "docker-compose.prod.yml", ".env.example"):
        assert "WORKSPACE_VOICE_MAX_DURATION" in (root / f).read_text(), f

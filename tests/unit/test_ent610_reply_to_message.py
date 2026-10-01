"""Reply to one message from the Workspace (ent#610 PR A, sign-off round 8).

The Inbox pane's arrow opens a chat with a "replying to" chip above the
composer (Codex-style) and the send carries `reply_to_message_id`. What must
hold, and what this suite pins through the REAL prompt composition:

1. The quote actually reaches the agent — on a resumed turn AND on the cold
   message (the resume-failure retry), because a reply the agent never sees is
   worse than no reply feature: the person believes they gave context.
2. It is resolved SERVER-side from the id, never trusted as client text — so a
   client cannot put words in the agent's mouth as "your earlier message".
3. Only a message in THIS caller's thread with THIS agent qualifies. Anything
   else is a uniform 422 (never "exists but not yours"), and it fails LOUD
   rather than silently dropping the context the person thinks they sent.
4. The stored user message stays exactly what they typed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent610-reply.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent610-reply-logs")
)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "bob@example.com"
SESSION = "sess-1"
CACHED_UUID = "11111111-2222-3333-4444-555555555555"
FRESH_UUID = "99999999-8888-7777-6666-555555555555"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for TaskExecutionResult — only the fields portal_chat reads."""

    def __init__(self, status="success", response="ok", error=None, session_id=FRESH_UUID):
        self.status = status
        self.response = response
        self.error = error
        self.session_id = session_id
        self.cost = 0.01


class _Recorder:
    """Captures every execute_task call so a test can assert on the sequence."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def execute_task(self, **kwargs):
        self.calls.append(kwargs)
        return self._results.pop(0) if self._results else _Result()


@pytest.fixture(autouse=True)
def _pin_container_state(monkeypatch):
    """#2196: pin the container-state seam for every test in this module.

    `portal_chat` gained a liveness gate (it had none, and its 502 fired only
    AFTER the user's message was durably written). Without this fixture the gate
    would consult the developer's real Docker — where these fixture agents have
    no container — so the module would pass in a Docker-less CI container and
    fail on every workstation, or the reverse. Patched on the consuming module's
    own attribute, which is why that read has a named seam at all.
    """
    from client_portal import service as svc

    async def _map(names):
        return {n: "ready" for n in names}

    async def _one(name):
        return "ready"

    monkeypatch.setattr(svc, "_availability_map", _map)
    monkeypatch.setattr(svc, "_agent_availability", _one)


@pytest.fixture()
def portal(monkeypatch):
    """`client_portal.service` with its DB, roster and execution stack stubbed.

    Everything faked here is a boundary the turn logic calls out to; the
    composition and resume decisions under test are the real code.
    """
    from client_portal import service as svc
    from client_portal import db as portal_db
    from services import session_turn_service

    state = types.SimpleNamespace(
        cached=None,
        cleared=False,
        failures=0,
        cached_writes=[],
        history=[],
        recorder=None,
    )

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda a, e: None)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: SESSION)
    monkeypatch.setattr(svc, "_spawn_title_generation", lambda *a, **kw: None)

    async def _no_inbox(agent, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _no_inbox)

    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages",
                        lambda *a, **kw: state.history)
    # #2694: the cold replay reads the thread as a window of typed turns; the
    # rows a test stages are the window.
    monkeypatch.setattr(portal_db, "get_portal_thread_window",
                        lambda *a, **kw: portal_db.ThreadWindow(rows=list(state.history), truncated=False))
    monkeypatch.setattr(portal_db, "get_platform_rows_since_last_reply", lambda *a, **kw: [])
    monkeypatch.setattr(portal_db, "add_portal_message", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id",
                        lambda sid: state.cached)

    def _clear(sid):
        state.cleared = True
        state.cached = None

    def _mark(sid):
        state.failures += 1
        return state.failures

    monkeypatch.setattr(portal_db, "clear_cached_claude_session_id", _clear)
    monkeypatch.setattr(portal_db, "mark_resume_failure", _mark)
    monkeypatch.setattr(portal_db, "update_cached_claude_session_id",
                        lambda sid, uuid: state.cached_writes.append((sid, uuid)))

    # Claude-like runtime unless a test says otherwise; no Docker in a unit test.
    monkeypatch.setattr(session_turn_service, "supports_session_resume", lambda a: True)
    monkeypatch.setattr(session_turn_service, "resolve_lock_ttl", lambda a: 60)

    class _NoLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(session_turn_service, "ResumeLock", _NoLock)

    def _install(results):
        state.recorder = _Recorder(results)
        monkeypatch.setattr(
            session_turn_service,
            "get_task_execution_service",
            lambda: state.recorder,
            raising=False,
        )
        # run_resumable_turn imports the accessor lazily from the module, so the
        # patch has to land where it looks it up.
        import services.task_execution_service as tes
        monkeypatch.setattr(tes, "get_task_execution_service", lambda: state.recorder)
        return state.recorder

    state.install = _install
    return svc, state


def _run(coro):
    """`asyncio.run`, not `get_event_loop().run_until_complete`.

    The latter passes when this file runs alone and fails in a full suite: a
    sibling module that closes the global loop leaves `get_event_loop()`
    handing back a closed one. A fresh loop per call has no such dependency on
    what ran before.
    """
    return asyncio.run(coro)




from client_portal.service import ClientPortalError  # noqa: E402

OTHER_AGENT = "recon"
STRANGER = "stranger@nowhere.test"
MSG = "msg-9"


def _row(**over):
    row = {"id": MSG, "agent_name": AGENT, "client_email": EMAIL, "session_id": SESSION,
           "role": "assistant", "content": "Heads up: the API returned 429 twice.\n\nSecond para."}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Resolution: server-side, owner-scoped, loud
# ---------------------------------------------------------------------------


def test_no_reply_id_means_no_context(portal):
    svc, _ = portal
    assert svc.reply_context(AGENT, EMAIL, SESSION, None) == ""


def test_the_agents_message_in_your_thread_becomes_a_quoted_block(portal, monkeypatch):
    svc, _ = portal
    monkeypatch.setattr(svc.db, "get_portal_message", lambda mid: _row())
    ctx = svc.reply_context(AGENT, EMAIL, SESSION, MSG)
    assert "replying to your earlier message" in ctx
    assert "> Heads up: the API returned 429 twice." in ctx
    assert "> Second para." in ctx
    assert ctx.endswith("\n\n")


def test_email_case_does_not_matter(portal, monkeypatch):
    svc, _ = portal
    monkeypatch.setattr(svc.db, "get_portal_message", lambda mid: _row(client_email=EMAIL.upper()))
    assert svc.reply_context(AGENT, EMAIL, SESSION, MSG)


@pytest.mark.parametrize("row", [
    None,                                   # no such message
    _row(client_email=STRANGER),            # someone else's conversation
    _row(agent_name=OTHER_AGENT),           # another agent's thread
    _row(session_id="sess-other"),          # another of your own threads
    _row(content="   "),                    # nothing to quote
])
def test_anything_else_is_one_uniform_refusal(portal, monkeypatch, row):
    svc, _ = portal
    monkeypatch.setattr(svc.db, "get_portal_message", lambda mid: row)
    with pytest.raises(ClientPortalError) as e:
        svc.reply_context(AGENT, EMAIL, SESSION, MSG)
    assert e.value.status_code == 422
    assert e.value.detail == svc.REPLY_TARGET_REFUSED


def test_a_reply_needs_a_named_thread(portal, monkeypatch):
    svc, _ = portal
    monkeypatch.setattr(svc.db, "get_portal_message", lambda mid: _row())
    with pytest.raises(ClientPortalError):
        svc.reply_context(AGENT, EMAIL, None, MSG)


@pytest.mark.parametrize("bad", ["", 12345, "x" * 65])
def test_a_malformed_id_is_refused_before_any_read(portal, monkeypatch, bad):
    svc, _ = portal
    def _never(mid):
        raise AssertionError("read a malformed id")
    monkeypatch.setattr(svc.db, "get_portal_message", _never)
    with pytest.raises(ClientPortalError):
        svc.reply_context(AGENT, EMAIL, SESSION, bad)


def test_a_long_message_is_capped(portal, monkeypatch):
    svc, _ = portal
    monkeypatch.setattr(svc.db, "get_portal_message", lambda mid: _row(content="a" * 9000))
    ctx = svc.reply_context(AGENT, EMAIL, SESSION, MSG)
    assert len(ctx) < svc.REPLY_QUOTE_MAX_CHARS + 300
    assert "…" in ctx


# ---------------------------------------------------------------------------
# Composition: the block reaches the agent on both turn shapes
# ---------------------------------------------------------------------------

CTX = "[Client Portal] The user is replying to your earlier message in this conversation:\n> friday\n\n"


def test_a_resumed_turn_carries_the_reply_before_the_message(portal):
    svc, state = portal
    state.cached = CACHED_UUID
    rec = state.install([_Result()])
    _run(svc.portal_chat(AGENT, "move it to monday", EMAIL, SESSION, reply_context=CTX))
    sent = rec.calls[0]["message"]
    assert CTX in sent
    assert sent.index(CTX) < sent.index("move it to monday")


def test_the_cold_message_carries_it_too(portal):
    svc, state = portal
    state.cached = None
    state.history = [{"role": "assistant", "content": "friday"}]
    rec = state.install([_Result()])
    _run(svc.portal_chat(AGENT, "move it to monday", EMAIL, SESSION, reply_context=CTX))
    assert CTX in rec.calls[0]["message"]


def test_the_stored_user_message_is_exactly_what_they_typed(portal, monkeypatch):
    svc, state = portal
    from client_portal import db as portal_db
    written = []
    monkeypatch.setattr(portal_db, "add_portal_message", lambda *a, **kw: written.append((a, kw)))
    state.install([_Result()])
    _run(svc.portal_chat(AGENT, "move it to monday", EMAIL, SESSION, reply_context=CTX))
    user_rows = [w for w in written if "user" in w[0] or w[1].get("role") == "user"]
    assert user_rows, written
    flat = repr(user_rows)
    assert "move it to monday" in flat
    assert "replying to" not in flat


# ---------------------------------------------------------------------------
# Wiring: both turn paths resolve and forward it (the ent#555 lesson — a flag
# honoured by one path comes back exactly when the other one is taken)
# ---------------------------------------------------------------------------


def test_the_request_model_accepts_a_bounded_id():
    from client_portal.models import PortalChatRequest
    assert PortalChatRequest(message="hi", reply_to_message_id="m1").reply_to_message_id == "m1"
    with pytest.raises(Exception):
        PortalChatRequest(message="hi", reply_to_message_id="x" * 65)


def test_both_routes_resolve_it_and_the_stream_forwards_it():
    import inspect
    from client_portal import router, service
    src = inspect.getsource(router)
    assert src.count("service.reply_context(") == 2
    assert "reply_context" in inspect.signature(service.start_portal_turn).parameters
    assert "reply_context=reply_context" in inspect.getsource(service.start_portal_turn)

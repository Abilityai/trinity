"""
Unit tests for voice WebSocket + REST ownership gates (#600).

Verifies that `/ws/voice/{voice_session_id}` and `POST .../voice/stop` reject
attempts to attach to a session the JWT user does not own. The bug was
introduced by #581: the WS handler validated the JWT signature but discarded
the payload, so any authenticated user could hijack any session whose
128-bit id they observed (logs, browser inspection, XSS).

Also the Workspace live-call lease (#2700, `TestWorkspaceLiveCallMarker`): the
audio bridge is the only writer of `portal_voice_active:{thread}` — it arms the
lease at connect and releases it on every exit — because the behavioural bridge
harness (the importlib load of `routers/voice.py`, `_FakeWebSocket`, the stubbed
voice service) lives in this file and nowhere else.

Issue: https://github.com/abilityai/trinity/issues/600
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest


# Point the backend at an ephemeral SQLite file BEFORE any backend module
# imports — otherwise database.py tries to mkdir /data on import.
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_voice_auth.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))
# Fix cross-module SECRET_KEY mismatch: test_voice_tools.py's _stub_config()
# replaces sys.modules["config"] with a stub that hardcodes this same value.
# Setting it here ensures the real config (loaded during collection) and the
# stub (activated during tests) both sign/verify JWTs with the same key.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests")

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Stub passlib so dependencies.py imports without bcrypt installed.
# We never call into hashing here — only need the import path to resolve.
def _stub_passlib():
    if "passlib" in sys.modules:
        return
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")

    class _CryptContext:
        def __init__(self, **kw):
            pass

        def hash(self, pw):
            return f"stub${pw}"

        def verify(self, pw, hashed):
            return hashed == f"stub${pw}"

    context.CryptContext = _CryptContext
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context


_stub_passlib()


def _run(coro):
    return asyncio.run(coro)


# ── Stub services.gemini_voice (avoids dragging google.genai into the test) ──

def _stub_voice_service():
    mod = types.ModuleType("services.gemini_voice")

    class _FakeVoiceSession:
        # #2700: `portal_session_id` makes a fake Workspace-bound, and the
        # bridge's close-out reads `end_reason`/`end_message` WITHOUT getattr
        # defaults, so a portal-bound fake without them raises AttributeError
        # inside the `finally`. The None/1800 defaults keep every pre-existing
        # bridge test byte-identical.
        def __init__(self, session_id, agent_name, user_id, user_email="u@example.com",
                     portal_session_id=None, max_duration=1800,
                     end_reason=None, end_message=None):
            self.session_id = session_id
            self.agent_name = agent_name
            self.user_id = user_id
            self.user_email = user_email
            self.chat_session_id = "cs_test"
            self.transcript = []
            self._duration_seconds = 0.0
            self.panel_state = {"type": "empty", "content": "", "title": None, "updated_at": None}
            self.portal_session_id = portal_session_id
            self.max_duration = max_duration
            self.end_reason = end_reason
            self.end_message = end_message

    class _FakeVoiceService:
        def __init__(self):
            self._sessions: dict = {}
            self.is_available = MagicMock(return_value=True)
            self.create_session = AsyncMock()
            self.connect_and_stream = AsyncMock()
            self.send_audio = AsyncMock()
            # get_session and remove_session are now async (fix for #704)
            self.remove_session = AsyncMock(side_effect=lambda sid: self._sessions.pop(sid, None))
            self.get_session = AsyncMock(side_effect=lambda sid: self._sessions.get(sid))

        def add(self, session):
            self._sessions[session.session_id] = session

        async def end_session(self, sid):
            return self._sessions.get(sid)

    mod.VoiceSession = _FakeVoiceSession
    mod.voice_service = _FakeVoiceService()
    mod.WORKSPACE_PANEL_INSTRUCTIONS = ""
    sys.modules["services.gemini_voice"] = mod
    return mod.voice_service, _FakeVoiceSession


def _stub_docker_service():
    mod = types.ModuleType("services.docker_service")
    mod.docker_client = None
    mod.get_agent_container = MagicMock(return_value=None)
    # services/__init__.py re-exports docker_client + several helpers; if the
    # stub omits any of them, `from services import ...` chains downstream
    # of database migrations explode with ImportError.
    mod.get_agent_status_from_container = MagicMock(return_value=None)
    mod.list_all_agents = MagicMock(return_value=[])
    mod.get_agent_by_name = MagicMock(return_value=None)
    mod.get_next_available_port = MagicMock(return_value=2222)
    sys.modules["services.docker_service"] = mod


def _stub_template_service():
    mod = types.ModuleType("services.template_service")
    mod.get_github_template = MagicMock()
    mod.clone_github_repo = MagicMock()
    mod.extract_agent_credentials = MagicMock()
    mod.generate_credential_files = MagicMock()
    sys.modules["services.template_service"] = mod


def _stub_platform_audit():
    mod = types.ModuleType("services.platform_audit_service")
    audit = MagicMock()
    audit.log = AsyncMock()
    mod.platform_audit_service = audit

    class _AuditEventType:
        EXECUTION = "execution"

    class _AuditActorType:
        USER = "user"

    mod.AuditEventType = _AuditEventType
    mod.AuditActorType = _AuditActorType
    sys.modules["services.platform_audit_service"] = mod


# Shared service modules whose stubs (installed below for voice.py's import
# chain) are INCOMPLETE — the template_service stub omits is_trinity_compatible,
# the docker_service stub omits execute_command_in_container. If they leak past
# this file they break later unit files whose import chains pull the real symbols
# (`ImportError: cannot import name '…' (unknown location)`, e.g. test_fork_to_own,
# test_73_stats_cache). We snapshot them here and restore right after voice.py
# imports. The `_STUBBED_MODULE_NAMES` list + `_restore_sys_modules` fixture below
# are the snapshot/restore pattern tests/lint_sys_modules.py recognises (#762;
# precedent: test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "services.docker_service",
    "services.template_service",
    "services.platform_audit_service",
]
_voice_auth_pre_stub = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}

_voice_service, _FakeVoiceSession = _stub_voice_service()
_stub_docker_service()
_stub_template_service()
_stub_platform_audit()


# Load voice.py directly via importlib instead of `from routers import voice`.
# Going through routers/__init__.py drags in 50+ unrelated routers (agents,
# slack, telegram, …) which need docker_service, twilio, slack_sdk, etc.
# We only need the voice handlers.
import importlib.util as _ilu  # noqa: E402

_voice_path = _BACKEND / "routers" / "voice.py"
_spec = _ilu.spec_from_file_location("routers.voice", str(_voice_path))
voice_router = _ilu.module_from_spec(_spec)
# Pre-register so relative imports inside voice.py (none right now) would work.
sys.modules["routers.voice"] = voice_router
_spec.loader.exec_module(voice_router)

# voice.py has captured what it needs from the stubs; restore the real shared
# service modules so later unit files see the real ones, not our stubs (see the
# snapshot above). This is the load-bearing cross-file fix — it runs at import
# time, which a per-test fixture cannot.
for _name, _orig in _voice_auth_pre_stub.items():
    if _orig is not None:
        sys.modules[_name] = _orig
    else:
        sys.modules.pop(_name, None)


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Per-test snapshot/restore safety net for the stubbed shared modules, and
    the lint-recognised marker (with `_STUBBED_MODULE_NAMES` above) for #762."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


from fastapi import HTTPException  # noqa: E402
from jose import jwt  # noqa: E402
from config import SECRET_KEY, ALGORITHM  # noqa: E402


# ── Test helpers ─────────────────────────────────────────────────────────────

def _make_jwt(username: str) -> str:
    return jwt.encode({"sub": username, "mode": "prod"}, SECRET_KEY, algorithm=ALGORITHM)


class _FakeWebSocket:
    """Minimal WebSocket that records accept/close/send activity."""

    def __init__(self, queue=None):
        self.accepted = False
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.sent = []
        self._queue = list(queue or [])

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_text(self):
        if not self._queue:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return self._queue.pop(0)


@pytest.fixture(autouse=True)
def _reset_voice_service():
    _voice_service._sessions.clear()
    _voice_service.connect_and_stream.reset_mock()
    yield
    _voice_service._sessions.clear()


@pytest.fixture
def alice_session():
    s = _FakeVoiceSession("vs_alice", agent_name="alice-agent", user_id=1, user_email="alice@example.com")
    _voice_service.add(s)
    return s


def _patch_db(monkeypatch, users_by_username):
    """Stub voice_router.db.get_user_by_username to return canned dicts."""
    fake_db = MagicMock()
    fake_db.get_user_by_username = MagicMock(side_effect=lambda u: users_by_username.get(u))
    monkeypatch.setattr(voice_router, "db", fake_db)
    return fake_db


# ── WebSocket auth tests ─────────────────────────────────────────────────────

class TestVoiceWebSocketAuth:

    def test_no_token_rejects_4001(self, alice_session, monkeypatch):
        ws = _FakeWebSocket()
        _run(voice_router.voice_websocket(ws, "vs_alice", token=None))
        assert ws.close_code == 4001
        assert ws.accepted is False

    def test_invalid_token_rejects_4001(self, alice_session, monkeypatch):
        ws = _FakeWebSocket()
        _run(voice_router.voice_websocket(ws, "vs_alice", token="garbage.not.jwt"))
        assert ws.close_code == 4001
        assert ws.accepted is False

    def test_unknown_session_rejects_4004(self, monkeypatch):
        ws = _FakeWebSocket()
        _patch_db(monkeypatch, {"alice": {"id": 1, "role": "user"}})
        token = _make_jwt("alice")
        _run(voice_router.voice_websocket(ws, "vs_does_not_exist", token=token))
        assert ws.close_code == 4004
        assert ws.accepted is False

    def test_unknown_user_rejects_4001(self, alice_session, monkeypatch):
        ws = _FakeWebSocket()
        _patch_db(monkeypatch, {})
        token = _make_jwt("ghost")
        _run(voice_router.voice_websocket(ws, "vs_alice", token=token))
        assert ws.close_code == 4001
        assert ws.accepted is False

    def test_owner_passes_auth_gate(self, alice_session, monkeypatch):
        """Alice connecting to her own session reaches accept()."""
        ws = _FakeWebSocket()
        _patch_db(monkeypatch, {"alice": {"id": 1, "role": "user"}})
        token = _make_jwt("alice")
        _run(voice_router.voice_websocket(ws, "vs_alice", token=token))
        assert ws.accepted is True
        assert ws.closed is True  # closed at end of finally — but we got past the gate

    def test_other_user_rejected_4003(self, alice_session, monkeypatch):
        """Bob holding a valid JWT cannot attach to Alice's session."""
        ws = _FakeWebSocket()
        _patch_db(monkeypatch, {"bob": {"id": 2, "role": "user"}})
        token = _make_jwt("bob")
        _run(voice_router.voice_websocket(ws, "vs_alice", token=token))
        assert ws.close_code == 4003
        assert ws.accepted is False

    def test_admin_bypasses_ownership(self, alice_session, monkeypatch):
        """Admins can attach to any session for support purposes."""
        ws = _FakeWebSocket()
        _patch_db(monkeypatch, {"root": {"id": 99, "role": "admin"}})
        token = _make_jwt("root")
        _run(voice_router.voice_websocket(ws, "vs_alice", token=token))
        assert ws.accepted is True

    def test_token_missing_sub_rejects_4001(self, alice_session, monkeypatch):
        ws = _FakeWebSocket()
        token = jwt.encode({"mode": "prod"}, SECRET_KEY, algorithm=ALGORITHM)
        _run(voice_router.voice_websocket(ws, "vs_alice", token=token))
        assert ws.close_code == 4001
        assert ws.accepted is False


# ── voice_stop ownership tests ──────────────────────────────────────────────

class _FakeUser:
    def __init__(self, id, role="user", email="u@example.com", username="u"):
        self.id = id
        self.role = role
        self.email = email
        self.username = username


class TestVoiceStopAuth:

    def test_unknown_session_404(self, monkeypatch):
        req = voice_router.VoiceStopRequest(voice_session_id="vs_missing")
        with pytest.raises(HTTPException) as exc:
            _run(voice_router.voice_stop(req, name="alice-agent", current_user=_FakeUser(1)))
        assert exc.value.status_code == 404

    def test_other_agent_403(self, alice_session, monkeypatch):
        """Path agent doesn't match the session's agent — reject."""
        req = voice_router.VoiceStopRequest(voice_session_id="vs_alice")
        with pytest.raises(HTTPException) as exc:
            _run(voice_router.voice_stop(req, name="bob-agent", current_user=_FakeUser(1)))
        assert exc.value.status_code == 403

    def test_other_user_403(self, alice_session, monkeypatch):
        """JWT user doesn't own the session — reject even with correct path agent."""
        req = voice_router.VoiceStopRequest(voice_session_id="vs_alice")
        with pytest.raises(HTTPException) as exc:
            _run(voice_router.voice_stop(req, name="alice-agent", current_user=_FakeUser(2)))
        assert exc.value.status_code == 403

    def test_owner_succeeds(self, alice_session, monkeypatch):
        req = voice_router.VoiceStopRequest(voice_session_id="vs_alice")
        # _save_transcript would touch db — stub it to a no-op.
        monkeypatch.setattr(voice_router, "_save_transcript", lambda s: 0)
        result = _run(voice_router.voice_stop(req, name="alice-agent", current_user=_FakeUser(1)))
        assert result.messages_saved == 0

    def test_admin_bypasses_ownership(self, alice_session, monkeypatch):
        req = voice_router.VoiceStopRequest(voice_session_id="vs_alice")
        monkeypatch.setattr(voice_router, "_save_transcript", lambda s: 0)
        result = _run(voice_router.voice_stop(req, name="alice-agent", current_user=_FakeUser(99, role="admin")))
        assert result.messages_saved == 0


# ── GET /panel ownership tests ───────────────────────────────────────────────

class TestVoicePanelAuth:
    """Tests for GET /api/agents/{name}/voice/{session_id}/panel ownership gate.

    ent#536: the panel IS the agent's default canvas, so the body is the canvas
    row (or an empty canvas shape) rather than an in-memory panel_state.
    """

    @pytest.fixture(autouse=True)
    def _canvas_row(self, monkeypatch):
        """Fake the canvas read so the ownership gate is what is under test."""
        self.rows = {}

        def _empty(name, canvas_id="main"):
            return {"agent_name": name, "canvas_id": canvas_id, "blocks": [],
                    "updated_at": None, "stale": False}

        fake_service = types.SimpleNamespace(
            empty_canvas=_empty,
            decorate=lambda rows, name: rows,
        )
        fake_db = types.SimpleNamespace(
            get_agent_canvas=lambda name, canvas_id, audience=None: self.rows.get((name, canvas_id)),
        )
        monkeypatch.setattr(voice_router, "canvas_service", fake_service, raising=False)
        monkeypatch.setattr(voice_router, "db", fake_db, raising=False)
        monkeypatch.setattr(voice_router, "DEFAULT_CANVAS_ID", "main", raising=False)

    def test_missing_session_returns_empty_state(self, monkeypatch):
        """Non-existent session_id returns an empty canvas (200), not 404."""
        result = _run(voice_router.get_voice_panel(
            session_id="vs_does_not_exist",
            name="alice-agent",
            current_user=_FakeUser(1),
        ))
        assert result["canvas_id"] == "main"
        assert result["blocks"] == []

    def test_owner_gets_the_canvas(self, alice_session, monkeypatch):
        """Session owner gets the agent's default canvas back."""
        self.rows[("alice-agent", "main")] = {
            "agent_name": "alice-agent", "canvas_id": "main", "updated_at": "ts",
            "blocks": [{"id": "voice", "kind": "markdown", "payload": {"markdown": "# Hello"}}],
        }
        result = _run(voice_router.get_voice_panel(
            session_id="vs_alice",
            name="alice-agent",
            current_user=_FakeUser(1),
        ))
        assert result["canvas_id"] == "main"
        assert result["blocks"][0]["payload"]["markdown"] == "# Hello"

    def test_no_canvas_yet_is_an_empty_canvas_not_404(self, alice_session, monkeypatch):
        result = _run(voice_router.get_voice_panel(
            session_id="vs_alice",
            name="alice-agent",
            current_user=_FakeUser(1),
        ))
        assert result["blocks"] == []

    def test_wrong_agent_name_403(self, alice_session, monkeypatch):
        """Session belongs to alice-agent but path says bob-agent — reject."""
        with pytest.raises(HTTPException) as exc:
            _run(voice_router.get_voice_panel(
                session_id="vs_alice",
                name="bob-agent",
                current_user=_FakeUser(1),
            ))
        assert exc.value.status_code == 403

    def test_other_user_403(self, alice_session, monkeypatch):
        """Bob cannot read Alice's panel."""
        with pytest.raises(HTTPException) as exc:
            _run(voice_router.get_voice_panel(
                session_id="vs_alice",
                name="alice-agent",
                current_user=_FakeUser(2),
            ))
        assert exc.value.status_code == 403

    def test_admin_reads_any_panel(self, alice_session, monkeypatch):
        """Admin can read any user's panel."""
        self.rows[("alice-agent", "main")] = {
            "agent_name": "alice-agent", "canvas_id": "main", "updated_at": "ts",
            "blocks": [{"id": "voice", "kind": "html", "payload": {"html": "<b>data</b>"}}],
        }
        result = _run(voice_router.get_voice_panel(
            session_id="vs_alice",
            name="alice-agent",
            current_user=_FakeUser(99, role="admin"),
        ))
        assert result["blocks"][0]["kind"] == "html"


# ── Audit attribution tests (#705) ──────────────────────────────────────────

class TestVoiceAuditAttribution:
    """
    Tests that on_tool_call uses actor_user= (SimpleNamespace) not the legacy
    actor_type/actor_id/actor_email kwargs that caused TypeError (fix for #705).
    """

    def test_on_tool_call_audit_uses_actor_user_not_legacy_kwargs(self):
        """
        Fix #705: the on_tool_call closure inside voice_websocket must pass
        actor_user= to audit.log(), not the legacy actor_type=/actor_id=/actor_email=
        kwargs that caused a TypeError and silently suppressed audit records.

        Tests the source directly — the regression was a kwargs mismatch; source
        inspection is the most reliable check and avoids async scheduling complexity.
        """
        import inspect
        source = inspect.getsource(voice_router.voice_websocket)

        # Must NOT contain the legacy invalid kwargs
        assert "actor_type=" not in source, (
            "Legacy actor_type= kwarg found in voice_websocket — this causes TypeError (#705)"
        )
        assert "actor_id=" not in source, (
            "Legacy actor_id= kwarg found in voice_websocket — this causes TypeError (#705)"
        )
        assert "actor_email=" not in source, (
            "Legacy actor_email= kwarg found in voice_websocket — this causes TypeError (#705)"
        )

        # Must use the correct actor_user= kwarg
        assert "actor_user=" in source, (
            "actor_user= is missing from audit.log() call in voice_websocket"
        )
        assert "SimpleNamespace" in source, (
            "SimpleNamespace wrapper is missing from actor_user= in voice_websocket"
        )


# ── The Workspace live-call lease (#2700) ────────────────────────────────────

def _install_fake_portal_voice(monkeypatch):
    """A recording fake `client_portal.voice` for the bridge's local imports.

    `routers/voice.py` imports `persist_voice_turn` unconditionally whenever
    `portal_session_id` is truthy, so a fake missing it raises ImportError
    before the arm ever runs. Installed with `monkeypatch.setitem` — the
    per-test-scoped, lint-blessed pattern (`tests/lint_sys_modules.py`); the
    module-level `_STUBBED_MODULE_NAMES` list snapshots and RESTORES, it does
    not install, so it is not the right tool here.

    Note what this fake costs: every assertion in this class runs against it,
    so the real constants and the real compare-and-delete are pinned in
    `test_2694_voice_delta_context.py` instead, and
    `test_the_bridge_resolves_the_real_marker_helpers` there is what asserts the
    real modules resolve each other.
    """
    rec = types.SimpleNamespace(calls=[], renews=[], persisted=[],
                                renew_cancelled=False, gemini_cancelled=False)
    mod = types.ModuleType("client_portal.voice")
    mod.VOICE_MARKER_LEASE_SECONDS = 60
    mod.VOICE_MARKER_TICK_SECONDS = 15.0
    mod.VOICE_MARKER_SLACK_SECONDS = 120

    def _mark(psid, ttl, *, owner):
        rec.calls.append(("mark", psid, ttl, owner))

    def _clear(psid, *, owner):
        rec.calls.append(("clear", psid, owner))

    async def _renew(psid, *, owner, max_seconds):
        rec.renews.append((psid, owner, max_seconds))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            rec.renew_cancelled = True
            raise

    mod.mark_voice_call_active = _mark
    mod.clear_voice_call_active = _clear
    mod.renew_voice_call_marker = _renew
    mod.persist_voice_turn = lambda session, role, text: rec.persisted.append(("turn", role, text))
    mod.persist_voice_call_end = lambda session, d, r, m: (rec.persisted.append(("end", d, r, m)), 1)[1]
    monkeypatch.setitem(sys.modules, "client_portal.voice", mod)
    return rec


class _YieldingWebSocket(_FakeWebSocket):
    """`_FakeWebSocket.receive_text` pops a queue without a single `await`, so a
    bridge driven by it never yields to the loop and its `create_task`ed
    children never get to start. A real socket always yields; this one does too,
    which is what lets these tests observe the renewer and the gemini task at
    all (#2700)."""

    async def receive_text(self):
        await asyncio.sleep(0)
        return await super().receive_text()


class TestWorkspaceLiveCallMarker:
    """#2700 — the party that opens the effect closes it.

    `/voice/start` used to arm the thread's live-call marker for the whole call
    cap (1920 s) before the audio socket that is the only thing able to clear it
    existed, so a start whose socket never opened refused every typed turn in
    that thread for ~32 minutes. The bridge is the only writer now: it arms a
    60 s lease at connect inside the same `try` whose `finally` releases it,
    renews it while it lives, and releases it unconditionally, last, and only if
    the lease is still its own.
    """

    PSID = "thread-2700"

    def _portal_session(self, monkeypatch, **kw):
        s = _FakeVoiceSession("vs_p", agent_name="alice-agent", user_id=1,
                              portal_session_id=self.PSID, **kw)
        _voice_service.add(s)
        _patch_db(monkeypatch, {"alice": {"id": 1, "role": "user"}})
        return s

    def _drive(self, ws, sid="vs_p", user="alice"):
        """Run the bridge to completion, then let cancellations land. Returns
        the tasks still pending afterwards — the leak pin."""
        token = _make_jwt(user)

        async def _go():
            await voice_router.voice_websocket(ws, sid, token=token)
            for _ in range(4):
                await asyncio.sleep(0)
            return [t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()]

        return _run(_go())

    def test_the_bridge_arms_a_lease_at_connect_and_releases_it_on_close(self, monkeypatch):
        """The whole shape in one run: armed at connect with the call's own id
        as the owner and the lease TTL (not the cap), a renewer started and
        bounded at the call's `max_duration` + slack, and released last."""
        rec = _install_fake_portal_voice(monkeypatch)
        self._portal_session(monkeypatch, max_duration=1800)
        ws = _YieldingWebSocket(queue=['{"type": "end"}'])
        pending = self._drive(ws)
        assert ws.accepted is True
        assert rec.calls == [("mark", self.PSID, 60, "vs_p"), ("clear", self.PSID, "vs_p")]
        assert rec.renews == [(self.PSID, "vs_p", 1800 + 120)]
        assert rec.renew_cancelled is True          # cancelled by the bridge, not by loop teardown
        assert pending == []                        # and it does not outlive the bridge
        assert ("end", 0.0, None, None) in rec.persisted

    def test_the_marker_is_released_even_when_the_session_already_ended(self, monkeypatch):
        """Trap C. `end_session` returns None — the exact case the REST `/stop`
        clear was added for — and the old clear sat inside `if ended:` →
        `if ended.portal_session_id:`, so a bridge that armed and then got None
        back re-stranded the marker: the same defect, moved. The release is
        keyed on the local captured before the `try` instead."""
        rec = _install_fake_portal_voice(monkeypatch)
        self._portal_session(monkeypatch)
        monkeypatch.setattr(_voice_service, "end_session", AsyncMock(return_value=None))
        ws = _YieldingWebSocket(queue=['{"type": "end"}'])
        assert self._drive(ws) == []
        assert rec.calls == [("mark", self.PSID, 60, "vs_p"), ("clear", self.PSID, "vs_p")]
        assert rec.persisted == []                  # nothing to close, nothing written

    def test_the_marker_is_released_when_the_close_path_raises(self, monkeypatch):
        """A raising close-out used to skip the gemini cancel, the `saved` frame
        and the socket close as well. It is logged, not propagated, the tail is
        always reached — and the release is behind it all."""
        rec = _install_fake_portal_voice(monkeypatch)
        self._portal_session(monkeypatch)
        monkeypatch.setattr(_voice_service, "end_session", AsyncMock(side_effect=RuntimeError("boom")))

        async def _never(*a, **kw):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                rec.gemini_cancelled = True
                raise

        monkeypatch.setattr(_voice_service, "connect_and_stream", _never)
        ws = _YieldingWebSocket(queue=['{"type": "end"}'])
        assert self._drive(ws) == []                            # and no raise escaped
        assert rec.calls == [("mark", self.PSID, 60, "vs_p"), ("clear", self.PSID, "vs_p")]
        assert rec.gemini_cancelled is True
        assert any(f.get("type") == "saved" for f in ws.sent)
        assert ws.closed is True

    def test_the_marker_is_released_when_the_bridge_is_cancelled(self, monkeypatch):
        """Process-level cancellation (a shutdown, a worker reload) still runs
        the `finally`; the release is synchronous, so it runs there too."""
        rec = _install_fake_portal_voice(monkeypatch)
        self._portal_session(monkeypatch)

        class _SlowWebSocket(_YieldingWebSocket):
            async def receive_text(self):
                await asyncio.sleep(3600)

        ws = _SlowWebSocket()
        token = _make_jwt("alice")

        async def _go():
            task = asyncio.create_task(voice_router.voice_websocket(ws, "vs_p", token=token))
            for _ in range(3):
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        _run(_go())
        assert ("mark", self.PSID, 60, "vs_p") in rec.calls
        assert ("clear", self.PSID, "vs_p") in rec.calls

    def test_an_agent_detail_call_never_arms_a_thread_marker(self, monkeypatch):
        """A GUARD, not a regression test — it passes against the pre-#2700 code
        too. It exists so a later edit cannot move the arm, the renewer or the
        release outside the `if portal_session_id:`, which would make a
        non-portal (Agent Detail / VoIP) bridge import `client_portal` and write
        a thread key for a thread that does not exist."""
        rec = _install_fake_portal_voice(monkeypatch)
        s = _FakeVoiceSession("vs_alice", agent_name="alice-agent", user_id=1)
        assert s.portal_session_id is None
        _voice_service.add(s)
        _patch_db(monkeypatch, {"alice": {"id": 1, "role": "user"}})
        ws = _YieldingWebSocket(queue=['{"type": "end"}'])
        assert self._drive(ws, sid="vs_alice") == []
        assert (rec.calls, rec.renews, rec.persisted) == ([], [], [])

    def test_the_rest_stop_releases_the_marker_for_a_stop_that_beats_the_socket(self, monkeypatch):
        """An API-ONLY guard (D9): the Workspace UI passes `restStop: false` and
        never calls `/stop`, so this is not a second live clear path and the
        safety case does not lean on it. It stays because it is idempotent,
        owner-matched, and reachable by a direct API client."""
        rec = _install_fake_portal_voice(monkeypatch)
        self._portal_session(monkeypatch)
        req = voice_router.VoiceStopRequest(voice_session_id="vs_p")
        result = _run(voice_router.voice_stop(req, name="alice-agent", current_user=_FakeUser(1)))
        assert result.messages_saved == 0                 # a portal-bound stop writes nothing
        assert rec.calls == [("clear", self.PSID, "vs_p")]


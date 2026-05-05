"""
Unit tests for voice WebSocket + REST ownership gates (#600).

Verifies that `/ws/voice/{voice_session_id}` and `POST .../voice/stop` reject
attempts to attach to a session the JWT user does not own. The bug was
introduced by #581: the WS handler validated the JWT signature but discarded
the payload, so any authenticated user could hijack any session whose
128-bit id they observed (logs, browser inspection, XSS).

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
        def __init__(self, session_id, agent_name, user_id, user_email="u@example.com"):
            self.session_id = session_id
            self.agent_name = agent_name
            self.user_id = user_id
            self.user_email = user_email
            self.chat_session_id = "cs_test"
            self.transcript = []
            self._duration_seconds = 0.0

    class _FakeVoiceService:
        def __init__(self):
            self._sessions: dict = {}
            self.is_available = MagicMock(return_value=True)
            self.create_session = MagicMock()
            self.connect_and_stream = AsyncMock()
            self.send_audio = AsyncMock()
            self.remove_session = MagicMock(side_effect=lambda sid: self._sessions.pop(sid, None))

        def add(self, session):
            self._sessions[session.session_id] = session

        def get_session(self, sid):
            return self._sessions.get(sid)

        async def end_session(self, sid):
            return self._sessions.get(sid)

    mod.VoiceSession = _FakeVoiceSession
    mod.voice_service = _FakeVoiceService()
    sys.modules["services.gemini_voice"] = mod
    return mod.voice_service, _FakeVoiceSession


def _stub_docker_service():
    mod = types.ModuleType("services.docker_service")
    mod.get_agent_container = MagicMock(return_value=None)
    sys.modules["services.docker_service"] = mod


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


_voice_service, _FakeVoiceSession = _stub_voice_service()
_stub_docker_service()
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

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
        def __init__(self, session_id, agent_name, user_id, user_email="u@example.com"):
            self.session_id = session_id
            self.agent_name = agent_name
            self.user_id = user_id
            self.user_email = user_email
            self.chat_session_id = "cs_test"
            self.transcript = []
            self._duration_seconds = 0.0
            self.panel_state = {"type": "empty", "content": "", "title": None, "updated_at": None}

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
    """Tests for GET /api/agents/{name}/voice/{session_id}/panel ownership gate."""

    def test_missing_session_returns_empty_state(self, monkeypatch):
        """Non-existent session_id returns empty state (200), not 404."""
        result = _run(voice_router.get_voice_panel(
            session_id="vs_does_not_exist",
            name="alice-agent",
            current_user=_FakeUser(1),
        ))
        assert result["type"] == "empty"
        assert result["content"] == ""

    def test_owner_gets_panel_state(self, alice_session, monkeypatch):
        """Session owner gets the panel state back."""
        # Inject some panel state on the fake session
        alice_session.panel_state = {
            "type": "markdown", "content": "# Hello", "title": None, "updated_at": "ts"
        }
        result = _run(voice_router.get_voice_panel(
            session_id="vs_alice",
            name="alice-agent",
            current_user=_FakeUser(1),
        ))
        assert result["type"] == "markdown"
        assert result["content"] == "# Hello"

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
        """Bob cannot read Alice's panel state."""
        with pytest.raises(HTTPException) as exc:
            _run(voice_router.get_voice_panel(
                session_id="vs_alice",
                name="alice-agent",
                current_user=_FakeUser(2),
            ))
        assert exc.value.status_code == 403

    def test_admin_reads_any_panel(self, alice_session, monkeypatch):
        """Admin can read any user's panel state."""
        alice_session.panel_state = {
            "type": "html", "content": "<b>data</b>", "title": "T", "updated_at": "ts"
        }
        result = _run(voice_router.get_voice_panel(
            session_id="vs_alice",
            name="alice-agent",
            current_user=_FakeUser(99, role="admin"),
        ))
        assert result["type"] == "html"


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

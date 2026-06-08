"""
Regression tests for #1073 — VoIP Media Streams WS 403'd on answer.

Bug: the auth ticket was passed in the `<Stream url>` query string. Twilio
Media Streams does NOT forward query params on that WebSocket, so the handler
saw `ticket=None`, closed *before* `accept()` (HTTP 403 handshake failure →
Twilio error 31920), and every outbound call dropped ~1s after answer.

Fix (`adapters/transports/twilio_media_stream.py`):
  - `accept()` the transport FIRST (the ticket arrives in the first `start`
    frame, only readable after the handshake completes);
  - read `start.customParameters.ticket` (a Media Streams `<Parameter>`),
    capturing the `streamSid` from that same frame;
  - validate the ticket scope, then consume the staged intent and create the
    Gemini session — exactly as before, just after accept;
  - a query-string ticket is still honored as a fallback for non-Twilio /
    diagnostic clients.

The module pulls heavy leaf imports (gemini_voice, database, config, the audio
codec). Following the `test_1069` precedent, we exec the module file directly
with ONLY those leaves stubbed, saving/restoring the exact sys.modules keys we
touch (NOT patch.dict — that wipes the whole snapshot and splits crypto/jose
identity for sibling tests). Async handlers are driven with `asyncio.run`, the
same pattern `test_voip_service.py` uses.

Module under test: src/backend/adapters/transports/twilio_media_stream.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path as _FsPath
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _find_backend_root() -> _FsPath:
    candidates = [
        _FsPath(__file__).resolve().parent.parent.parent / "src" / "backend",  # host
        _FsPath("/app"),  # trinity-backend container
    ]
    env_override = os.environ.get("TRINITY_BACKEND_PATH")
    if env_override:
        candidates.insert(0, _FsPath(env_override))
    for c in candidates:
        if (c / "adapters" / "transports" / "twilio_media_stream.py").exists():
            return c
    raise RuntimeError("Cannot locate backend source tree (set TRINITY_BACKEND_PATH)")


_BACKEND = _find_backend_root()
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Modules this test stubs into sys.modules during the import-time module load
# below — restored synchronously inside `_load_media_stream_module()` and, as a
# belt-and-suspenders guard, snapshot/restored around every test by the autouse
# fixture. This named-helper pair is the sanctioned exemption from the
# tests/lint_sys_modules.py ban on bare sys.modules mutation (Issue #762),
# matching the precedent in tests/unit/test_telegram_webhook_backfill.py.
_STUBBED_MODULE_NAMES = [
    "adapters",
    "adapters.transports",
    "adapters.transports.voip_audio",
    "adapters.transports.twilio_media_stream",
    "config",
    "database",
    "services",
    "services.gemini_voice",
    "services.voip_service",
    "services.ws_ticket_service",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after.

    The heavy-leaf stubs are installed and removed inside
    `_load_media_stream_module()` at import time; this fixture guards against
    any per-test re-stub leaking into sibling test files in the same session.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_media_stream_module() -> types.ModuleType:
    """Exec twilio_media_stream.py with its heavy leaf imports stubbed.

    Stubs are installed and removed by saving/restoring only the specific
    sys.modules keys we touch, so real modules other tests rely on are left
    untouched after load. The loaded module keeps references to the stub
    objects via its globals; tests then patch those globals directly.
    """
    def _pkg(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []  # mark as a package so dotted submodule imports resolve
        return m

    adapters_pkg = _pkg("adapters")
    transports_pkg = _pkg("adapters.transports")

    audio_mod = types.ModuleType("adapters.transports.voip_audio")
    audio_mod.FRAME_BYTES = 160
    audio_mod.pcm24k_to_ulaw8k = lambda pcm, state: (b"", state)
    audio_mod.ulaw8k_to_pcm16k = lambda mulaw, state: (b"", state)
    audio_mod.pop_frames = lambda buf, n: []

    config_mod = types.ModuleType("config")
    config_mod.REDIS_URL = "redis://test:test@redis:6379"

    database_mod = types.ModuleType("database")
    database_mod.db = MagicMock()

    services_pkg = _pkg("services")

    gemini_mod = types.ModuleType("services.gemini_voice")
    gemini_mod.voice_service = MagicMock()

    voip_svc_mod = types.ModuleType("services.voip_service")
    voip_svc_mod.voip_service = MagicMock()
    voip_svc_mod.intent_key = lambda call_id: f"voip_intent:{call_id}"

    ws_ticket_mod = types.ModuleType("services.ws_ticket_service")
    ws_ticket_mod.consume_ticket = lambda t: None

    path = _BACKEND / "adapters" / "transports" / "twilio_media_stream.py"
    spec = importlib.util.spec_from_file_location(
        "adapters.transports.twilio_media_stream", str(path)
    )
    mod = importlib.util.module_from_spec(spec)

    stubs = {
        "adapters": adapters_pkg,
        "adapters.transports": transports_pkg,
        "adapters.transports.voip_audio": audio_mod,
        "adapters.transports.twilio_media_stream": mod,
        "config": config_mod,
        "database": database_mod,
        "services": services_pkg,
        "services.gemini_voice": gemini_mod,
        "services.voip_service": voip_svc_mod,
        "services.ws_ticket_service": ws_ticket_mod,
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, original in saved.items():
            if original is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = original
    return mod


MS = _load_media_stream_module()


# ── Test doubles ──────────────────────────────────────────────────────────────

class _FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in driven from a frame list."""

    def __init__(self, frames):
        self._frames = [json.dumps(f) if isinstance(f, dict) else f for f in frames]
        self.accepted = False
        self.closed = None        # (code, reason) once closed
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._frames:
            return self._frames.pop(0)
        # No more frames — emulate the PSTN leg hanging up.
        raise MS.WebSocketDisconnect(code=1000)

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


class _FakeSession:
    def __init__(self):
        self.session_id = "vs_test"
        self.transcript = []


class _FakeVoiceService:
    def __init__(self):
        self.create_calls = []

    async def create_session(self, **kwargs):
        self.create_calls.append(kwargs)
        return _FakeSession()

    async def connect_and_stream(self, vs_id, **kwargs):
        return  # complete immediately so the handler tears down cleanly

    async def end_session(self, vs_id):
        return

    async def remove_session(self, vs_id):
        return


class _FakeRedis:
    def __init__(self, intent_json):
        self._intent = intent_json
        self.getdel_keys = []

    async def getdel(self, key):
        self.getdel_keys.append(key)
        return self._intent

    async def set(self, *a, **k):
        return True


_INTENT = {
    "agent_name": "cornelius-m",
    "chat_session_id": "cs_1",
    "user_id": 7,
    "user_email": "owner@example.com",
    "system_prompt": "SYS",
    "to_number": "+14155551234",
    "process_transcript": True,
}


def _start_frame(ticket=None, stream_sid="MZ_streamsid"):
    start = {"streamSid": stream_sid}
    if ticket is not None:
        start["customParameters"] = {"ticket": ticket}
    return {"event": "start", "start": start, "streamSid": stream_sid}


def _wire(monkeypatch, *, ticket_scope, redis_intent):
    """Patch the loaded module's globals with cooperative fakes.

    Returns (fake_voice, fake_redis, consume_calls).
    """
    consume_calls = []

    def _consume(t):
        consume_calls.append(t)
        return {"sub": "voip", "scope": ticket_scope} if ticket_scope else None

    fake_voice = _FakeVoiceService()
    fake_redis = _FakeRedis(redis_intent)

    async def _get_redis():
        return fake_redis

    async def _finalize(*a, **k):  # avoid importing routers.voice in teardown
        return

    monkeypatch.setattr(MS, "consume_ticket", _consume)
    monkeypatch.setattr(MS, "voice_service", fake_voice)
    monkeypatch.setattr(MS, "_get_redis", _get_redis)
    monkeypatch.setattr(MS, "_finalize", _finalize)
    return fake_voice, fake_redis, consume_calls


# ── _parse_start_frame (pure) ─────────────────────────────────────────────────

class TestParseStartFrame:
    def test_extracts_ticket_and_stream_sid_from_custom_parameters(self):
        data = _start_frame(ticket="TKT_abc", stream_sid="MZ123")
        ticket, sid = MS._parse_start_frame(data)
        assert ticket == "TKT_abc"
        assert sid == "MZ123"

    def test_missing_custom_parameters_yields_none_ticket(self):
        ticket, sid = MS._parse_start_frame({"event": "start", "start": {"streamSid": "MZ9"}})
        assert ticket is None
        assert sid == "MZ9"

    def test_top_level_stream_sid_fallback(self):
        ticket, sid = MS._parse_start_frame({"event": "start", "streamSid": "MZ_top"})
        assert ticket is None
        assert sid == "MZ_top"


# ── handle_media_stream — the #1073 fix ───────────────────────────────────────

class TestHandleMediaStreamTicketFromStartFrame:
    def test_twilio_start_parameter_authenticates_and_creates_session(self, monkeypatch):
        """Happy path: ticket arrives as a customParameter in the `start` frame
        (Twilio's transport), the handshake is accepted, the ticket is consumed
        with the call-bound scope, and the Gemini session is created."""
        call_id = "voip_call1"
        fake_voice, fake_redis, consume_calls = _wire(
            monkeypatch, ticket_scope=f"voip:{call_id}", redis_intent=json.dumps(_INTENT)
        )
        # Record the bridge instance so we can assert the streamSid was seeded.
        created = {}
        real_bridge = MS._CallBridge

        class _RecordingBridge(real_bridge):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                created["bridge"] = self

        monkeypatch.setattr(MS, "_CallBridge", _RecordingBridge)

        ws = _FakeWebSocket([
            {"event": "connected", "protocol": "Call"},
            _start_frame(ticket="TKT_good", stream_sid="MZ_seed"),
            {"event": "stop"},
        ])

        # No query-string ticket → Twilio path (read from start frame).
        asyncio.run(MS.handle_media_stream(ws, call_id, None))

        assert ws.accepted is True
        assert consume_calls == ["TKT_good"]                  # ticket read from <Parameter>
        assert len(fake_voice.create_calls) == 1              # session created
        assert fake_voice.create_calls[0]["agent_name"] == "cornelius-m"
        assert fake_redis.getdel_keys == [f"voip_intent:{call_id}"]
        assert created["bridge"]._stream_sid == "MZ_seed"     # streamSid carried over
        # Auth passed → the connection was NOT rejected with a ticket/intent code.
        assert ws.closed is None or ws.closed[0] not in (4001, 4004)

    def test_missing_ticket_parameter_rejected_after_accept_without_session(self, monkeypatch):
        """A `start` frame with no ticket parameter must be rejected (close 4001)
        AFTER accept — and must never create a session or consume the intent."""
        call_id = "voip_call2"
        fake_voice, fake_redis, consume_calls = _wire(
            monkeypatch, ticket_scope=None, redis_intent=json.dumps(_INTENT)
        )
        ws = _FakeWebSocket([
            {"event": "connected", "protocol": "Call"},
            _start_frame(ticket=None),   # no customParameters.ticket
        ])

        asyncio.run(MS.handle_media_stream(ws, call_id, None))

        assert ws.accepted is True                 # accept happens regardless (#1073)
        assert ws.closed is not None and ws.closed[0] == 4001
        assert consume_calls == []                 # None ticket short-circuits consume
        assert fake_voice.create_calls == []       # no session
        assert fake_redis.getdel_keys == []        # intent never touched

    def test_wrong_scope_ticket_rejected(self, monkeypatch):
        """A ticket bound to a different call must fail the scope check."""
        call_id = "voip_call3"
        fake_voice, fake_redis, consume_calls = _wire(
            monkeypatch, ticket_scope="voip:SOME_OTHER_CALL", redis_intent=json.dumps(_INTENT)
        )
        ws = _FakeWebSocket([_start_frame(ticket="TKT_other")])

        asyncio.run(MS.handle_media_stream(ws, call_id, None))

        assert consume_calls == ["TKT_other"]
        assert ws.closed is not None and ws.closed[0] == 4001
        assert fake_voice.create_calls == []

    def test_no_start_frame_times_out_into_rejection(self, monkeypatch):
        """Handshake completes but the client never sends a `start` frame →
        bounded read returns None → reject without hanging the worker."""
        call_id = "voip_call4"
        fake_voice, _, consume_calls = _wire(
            monkeypatch, ticket_scope=f"voip:{call_id}", redis_intent=json.dumps(_INTENT)
        )
        ws = _FakeWebSocket([])  # receive_text() immediately raises disconnect

        asyncio.run(MS.handle_media_stream(ws, call_id, None))

        assert ws.accepted is True
        assert ws.closed is not None and ws.closed[0] == 4001
        assert consume_calls == []
        assert fake_voice.create_calls == []

    def test_query_string_ticket_fallback_still_works(self, monkeypatch):
        """Non-Twilio / diagnostic clients can still present the ticket in the
        query string; the handler uses it directly and skips the start-frame read."""
        call_id = "voip_call5"
        fake_voice, fake_redis, consume_calls = _wire(
            monkeypatch, ticket_scope=f"voip:{call_id}", redis_intent=json.dumps(_INTENT)
        )
        ws = _FakeWebSocket([{"event": "stop"}])

        asyncio.run(MS.handle_media_stream(ws, call_id, "QS_TICKET"))

        assert ws.accepted is True
        assert consume_calls == ["QS_TICKET"]          # query-string ticket honored
        assert len(fake_voice.create_calls) == 1
        assert fake_redis.getdel_keys == [f"voip_intent:{call_id}"]

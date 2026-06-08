"""
Unit tests for VoIP service helpers (VOIP-001, #1056).

Covers the pure, security-relevant helpers in `services/voip_service.py`:
  - normalize_e164: destination-number validation (first gate on PSTN spend)
  - VoipService._wss_base: HTTP(S) public base → ws(s):// origin
  - VoipService.build_stream_twiml: well-formed, XML-escaped <Connect><Stream>

`services.voip_service` pulls `ws_ticket_service → routers.auth → passlib`,
which isn't in the host test env, so we stub `services.ws_ticket_service`
before import (sanctioned `_STUBBED_MODULE_NAMES` + `_restore_sys_modules`
pattern; precedent: tests/unit/test_904_agent_call_limiter.py).

Module: src/backend/services/voip_service.py
"""

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Stub the heavy ws_ticket_service import chain (routers.auth → passlib), and
# the twilio SDK modules the happy-path test injects (so neither leaks across tests).
_STUBBED_MODULE_NAMES = [
    "services.ws_ticket_service",
    "twilio", "twilio.rest", "twilio.base", "twilio.base.exceptions",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _import_voip_service():
    stub = types.ModuleType("services.ws_ticket_service")
    stub.mint_ticket = lambda subject, *, scope="user", ttl_seconds=None: "STUB_TICKET"
    sys.modules["services.ws_ticket_service"] = stub
    import importlib
    if "services.voip_service" in sys.modules:
        return importlib.reload(sys.modules["services.voip_service"])
    return importlib.import_module("services.voip_service")


# ---------------------------------------------------------------------------
# normalize_e164
# ---------------------------------------------------------------------------

class TestNormalizeE164:
    def test_valid_passes_through(self):
        vs = _import_voip_service()
        assert vs.normalize_e164("+14155551234") == "+14155551234"

    def test_strips_formatting_and_tel_scheme(self):
        vs = _import_voip_service()
        assert vs.normalize_e164("tel:+1 (415) 555-1234") == "+14155551234"

    @pytest.mark.parametrize("bad", ["", "14155551234", "+0123", "not-a-number", "+1-555-CALL"])
    def test_rejects_invalid(self, bad):
        vs = _import_voip_service()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            vs.normalize_e164(bad)
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# _wss_base
# ---------------------------------------------------------------------------

class TestWssBase:
    def test_https_becomes_wss(self):
        vs = _import_voip_service()
        assert vs.VoipService._wss_base("https://agent.example.com/") == "wss://agent.example.com"

    def test_http_becomes_ws(self):
        vs = _import_voip_service()
        assert vs.VoipService._wss_base("http://localhost:8000") == "ws://localhost:8000"

    def test_bare_host_assumes_tls(self):
        vs = _import_voip_service()
        assert vs.VoipService._wss_base("agent.example.com") == "wss://agent.example.com"


# ---------------------------------------------------------------------------
# build_stream_twiml
# ---------------------------------------------------------------------------

class TestBuildTwiml:
    def test_call_id_in_path_ticket_in_parameter(self):
        vs = _import_voip_service()
        svc = vs.VoipService()
        twiml = svc.build_stream_twiml("voip_abc123", "TICKET_xyz", "https://agent.example.com")
        # Parseable XML
        import xml.etree.ElementTree as ET
        root = ET.fromstring(twiml)
        assert root.tag == "Response"
        stream = root.find("./Connect/Stream")
        assert stream is not None
        url = stream.attrib["url"]
        # call_id stays in the PATH; no query string (Twilio drops it — #1073).
        assert url == "wss://agent.example.com/api/voip/voice/voip_abc123"
        # The ticket travels as a <Parameter> child — Twilio forwards these in
        # the `start` event's customParameters; it does NOT forward the query
        # string on the <Stream url> WebSocket.
        param = stream.find("./Parameter")
        assert param is not None
        assert param.attrib["name"] == "ticket"
        assert param.attrib["value"] == "TICKET_xyz"

    def test_ticket_never_in_query_string_regression_1073(self):
        """#1073: a query-string ticket is silently dropped by Twilio Media
        Streams, so the WS handshake 403s on answer. Guard we never emit one."""
        vs = _import_voip_service()
        svc = vs.VoipService()
        twiml = svc.build_stream_twiml("voip_abc", "TKT", "https://h.example.com")
        assert "?ticket=" not in twiml
        assert "/api/voip/voice/voip_abc?" not in twiml

    def test_attributes_are_escaped_against_injection(self):
        """A ticket containing quotes/angle-brackets must not break out of its
        attribute (quoteattr escaping). Defense-in-depth even though
        call_id/ticket are server-generated tokens."""
        vs = _import_voip_service()
        svc = vs.VoipService()
        payload = '"/><Hangup/><Parameter value="'
        twiml = svc.build_stream_twiml("voip_x", payload, "https://h.example.com")
        # Still well-formed (escaping worked) — no injected Hangup element.
        import xml.etree.ElementTree as ET
        root = ET.fromstring(twiml)
        assert root.find("./Connect/Hangup") is None
        stream = root.find("./Connect/Stream")
        assert stream is not None
        # The malicious payload survives intact as the literal attribute value.
        param = stream.find("./Parameter")
        assert param is not None
        assert param.attrib["value"] == payload


# ---------------------------------------------------------------------------
# place_outbound_call — abuse-control gates (flag / binding / cap / rate)
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.staged = {}
    async def setex(self, k, ttl, v):
        self.staged[k] = v
    async def delete(self, k):
        self.staged.pop(k, None)


def _mk_service(monkeypatch, *, available=True, binding=None, call_count=0):
    """Build a VoipService with all external collaborators mocked."""
    vs = _import_voip_service()
    svc = vs.VoipService()
    monkeypatch.setattr(svc, "is_available", lambda: available)

    monkeypatch.setattr(vs.db, "get_voip_binding", lambda a: binding)
    monkeypatch.setattr(vs.db, "get_voip_auth_token", lambda a: "decrypted-token")
    monkeypatch.setattr(vs.db, "count_voip_calls_since", lambda a, hours=24: call_count)
    monkeypatch.setattr(vs.db, "create_voip_call_log", lambda **kw: None)
    monkeypatch.setattr(vs.db, "update_voip_call_status", lambda *a, **k: None)
    monkeypatch.setattr(
        vs.db, "get_or_create_chat_session",
        lambda agent_name, user_id, user_email: types.SimpleNamespace(id="cs_test"),
    )
    fake_redis = _FakeRedis()
    async def _get_redis():
        return fake_redis
    monkeypatch.setattr(svc, "_get_redis", _get_redis)
    async def _build_prompt(agent_name, to_number, context):
        return "SYS PROMPT"
    monkeypatch.setattr(svc, "_build_call_system_prompt", _build_prompt)
    # rate limiter: no-op by default (allow); tests override to simulate 429.
    monkeypatch.setattr(vs.rate_limiter, "enforce", lambda **kw: None)
    return vs, svc, fake_redis


_BINDING = {"agent_name": "a", "account_sid": "AC" + "0" * 32,
            "from_number": "+14155550100", "enabled": True, "daily_call_cap": 50}


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestPlaceOutboundCallGates:
    def test_flag_off_404(self, monkeypatch):
        import asyncio
        from fastapi import HTTPException
        vs, svc, _ = _mk_service(monkeypatch, available=False)
        with pytest.raises(HTTPException) as e:
            _run(svc.place_outbound_call("a", "+14155551234", 1, "u@e.com", "https://h.example.com"))
        assert e.value.status_code == 404

    def test_no_binding_400(self, monkeypatch):
        from fastapi import HTTPException
        vs, svc, _ = _mk_service(monkeypatch, binding=None)
        with pytest.raises(HTTPException) as e:
            _run(svc.place_outbound_call("a", "+14155551234", 1, "u@e.com", "https://h.example.com"))
        assert e.value.status_code == 400

    def test_over_daily_cap_429(self, monkeypatch):
        from fastapi import HTTPException
        vs, svc, _ = _mk_service(monkeypatch, binding=_BINDING, call_count=50)
        with pytest.raises(HTTPException) as e:
            _run(svc.place_outbound_call("a", "+14155551234", 1, "u@e.com", "https://h.example.com"))
        assert e.value.status_code == 429

    def test_rate_limit_429(self, monkeypatch):
        from fastapi import HTTPException, status
        vs, svc, _ = _mk_service(monkeypatch, binding=_BINDING, call_count=0)
        def _deny(**kw):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate")
        monkeypatch.setattr(vs.rate_limiter, "enforce", _deny)
        with pytest.raises(HTTPException) as e:
            _run(svc.place_outbound_call("a", "+14155551234", 1, "u@e.com", "https://h.example.com"))
        assert e.value.status_code == 429

    def test_happy_path_stages_intent_and_dials(self, monkeypatch):
        vs, svc, fake_redis = _mk_service(monkeypatch, binding=_BINDING, call_count=0)
        dialed = {}
        async def _dial(client, to_number, from_number, twiml):
            dialed["to"] = to_number
            dialed["from"] = from_number
            dialed["twiml"] = twiml
            return types.SimpleNamespace(sid="CA_fake")
        monkeypatch.setattr(svc, "_dial", _dial)
        # Avoid importing the real twilio SDK in the try-block.
        import sys as _sys, types as _types
        tw = _types.ModuleType("twilio"); tw_rest = _types.ModuleType("twilio.rest")
        tw_rest.Client = lambda sid, token: object()
        tw_base = _types.ModuleType("twilio.base"); tw_exc = _types.ModuleType("twilio.base.exceptions")
        tw_exc.TwilioRestException = Exception
        _sys.modules.update({"twilio": tw, "twilio.rest": tw_rest,
                             "twilio.base": tw_base, "twilio.base.exceptions": tw_exc})

        result = _run(svc.place_outbound_call(
            "a", "tel:+1 (415) 555-1234", 7, "owner@e.com", "https://h.example.com",
            context="say hi", process_transcript=True,
        ))
        assert result["status"] == "ringing"
        assert result["to_number"] == "+14155551234"
        assert result["twilio_call_sid"] == "CA_fake"
        # Intent staged under the returned call_id, with the right fields.
        key = f"voip_intent:{result['call_id']}"
        assert key in fake_redis.staged
        import json as _json
        intent = _json.loads(fake_redis.staged[key])
        assert intent["agent_name"] == "a"
        assert intent["to_number"] == "+14155551234"
        assert intent["process_transcript"] is True
        assert intent["chat_session_id"] == "cs_test"
        # TwiML dialed from the binding's from_number, carries the call_id.
        assert dialed["from"] == "+14155550100"
        assert result["call_id"] in dialed["twiml"]

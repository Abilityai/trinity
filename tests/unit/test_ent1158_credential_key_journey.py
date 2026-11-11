"""trinity-enterprise#1158 — the journey: an agent asks a person for a
credential that belongs to an agent on another Trinity, and the value lands
there under the key an admin registered for that Trinity. The asking agent
never holds that key, and its own `.env` is never read nor written.

One path end to end, through the real pieces in the order a deployment runs
them:

1. an admin registers the other Trinity as an outbound A2A endpoint with its
   key (`a2a_outbound.upsert_endpoint` — real validation, a real AES-256-GCM
   envelope round trip);
2. the asking agent raises the ask through the real route coroutine
   (`routers.operator_queue.raise_my_ask` → `ask_service.raise_ask`) naming
   `destination.instance` → 201;
3. the person answers through the real Workspace answer path
   (`client_portal.asks.service.provide_credential` → the real credential
   sink) → the remote instance receives ONE merge inject under the entry key;
   the ask is answered `provided`; exactly one `credential_instance_delivery`
   audit row; the asking agent's `.env` is byte-identical.

And the two variants the plan names:

* the admin clears the entry's key after the ask was raised → the person's
  answer is refused `destination_instance_no_credential`, nothing is sent,
  the ask stays pending;
* the older `key_env` form, with no such variable in the asking agent's
  `.env` → the raise itself is refused `destination_key_env_missing`, before
  a person types anything.

Stood in for: the remote instance (an `httpx.MockTransport` behind the pinned
client, as in `test_ent1109_credential_handover.py`), the asking agent's
internal server (an in-memory `.env`), DNS, Docker, Redis (fakeredis), the
`system_settings` row (an in-memory dict under the real encrypt/decrypt) and
the audit/websocket side channels.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import httpx
import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

OWNER = "owner-1158j@example.com"
ASKER = "asking-agent-1158j"         # the agent that raises the ask
TARGET = "target-agent"              # the agent the credential is for, on the other Trinity
HOST = "remote.example.com"
ORIGIN = f"https://{HOST}"
ADDRESS = "93.184.216.34"
ENTRY = "instance-x"                 # the admin's registry entry for that Trinity
ENTRY_KEY = "trinity_mcp_ENTRY_KEY_am91cm5leS1lbnRyeS1rZXk"
NAME = "TELEGRAM_BOT_TOKEN"
VALUE = "7421:AAH-journey-bot-token-value"
KEY_ENV = "REMOTE_TRINITY_KEY"
ASKER_ENV = 'OTHER="keep me"\n'
ACTION = "credential_instance_delivery"


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


def _resolver(host, port, *a, **k):
    if host == HOST:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ADDRESS, port))]
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _streaming(resp):
    if not resp.is_stream_consumed:
        return resp
    return httpx.Response(resp.status_code, headers=resp.headers, stream=httpx.ByteStream(resp.content))


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _AgentServer:
    """The asking agent's internal server: an in-memory `.env` behind the
    read and inject endpoints. Records every call so the journey can show
    which paths touched it."""

    def __init__(self, env_text):
        self.env_text = env_text
        self.calls = []

    def client(self, agent_name, **kw):
        server = self

        class _Ctx:
            async def __aenter__(self):
                return server

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url))
        return _Resp(200, {"files": {".env": self.env_text}, "files_b64": {}, "missing": []})

    async def post(self, url, json=None, **kw):
        self.calls.append(("POST", url))
        self.env_text = json["files"][".env"]
        return _Resp(200, {"status": "success", "files_written": [".env"]})


class _Instance:
    """The other Trinity's backend over the pinned transport (a real
    `httpx.AsyncClient` over a `MockTransport`). Records what it was sent."""

    def __init__(self):
        self.calls = []

    def client(self, *a, **k):
        inst = self

        async def _handler(request):
            inst.calls.append({"url": str(request.url),
                               "json": json.loads(request.content or b"null"),
                               "headers": request.headers})
            return _streaming(httpx.Response(200, json={"status": "success"}))

        return httpx.AsyncClient(transport=httpx.MockTransport(_handler), follow_redirects=False)


class _FakeEnc:
    def encrypt(self, d):
        return "ENV:" + json.dumps(d)

    def decrypt(self, s):
        return json.loads(s[len("ENV:"):])


@pytest.fixture
def journey(monkeypatch):
    """Everything around the real registry, route, ask sink, answer path and
    credential sink. The `system_settings` row is a dict here so a registered
    endpoint never outlives the test; the envelope inside it is real."""
    from database import db as real_db
    import client_portal.service as portal_service
    import services.ask_service as svc
    import services.credential_sink as sink
    import services.operator_queue_service as oqs
    import services.runtime_secret_scrub as scrub
    import utils.url_validation as url_validation
    from services import a2a_client, a2a_outbound, a2a_outbound_service, assignment_provider
    from services.rate_limiter import RateLimitResult

    # -- the registry: real upsert, real encryption, an in-memory settings row
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    settings = {}
    monkeypatch.setattr(real_db, "get_setting_value",
                        lambda key, default=None: settings.get(key, default), raising=False)
    monkeypatch.setattr(real_db, "set_setting",
                        lambda key, value: settings.__setitem__(key, value), raising=False)
    validate = functools.partial(url_validation.validate_a2a_endpoint_url, resolver=_resolver)
    monkeypatch.setattr(url_validation, "validate_a2a_endpoint_url", validate)
    monkeypatch.setattr(a2a_client, "validate_a2a_endpoint_url", validate)
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: True)
    a2a_outbound.clear_provider()
    assignment_provider.clear_provider()

    # -- the ask sink's side channels
    audit = []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "_owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", True))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))

    # -- the credential sink's world
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(scrub, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(scrub, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(scrub, "_redis_down_until", 0.0, raising=False)
    monkeypatch.setattr(sink, "get_breaker_redis", lambda: fake)
    asker = _AgentServer(ASKER_ENV)
    monkeypatch.setattr(sink, "_agent_client", asker.client)
    instance = _Instance()
    monkeypatch.setattr(sink, "_destination_client", instance.client)
    monkeypatch.setattr(sink, "get_agent_container", lambda name: MagicMock())
    monkeypatch.setattr(sink, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status="running"))
    monkeypatch.setattr(sink, "platform_audit_service", _Audit())
    monkeypatch.setattr(sink.credential_requirements_service, "invalidate_report_cache",
                        lambda name: None)

    yield SimpleNamespace(db=real_db, outbound=a2a_outbound, settings=settings, audit=audit,
                          asker=asker, instance=instance, fake=fake)
    a2a_outbound.clear_provider()
    assignment_provider.clear_provider()


def _register(j, credential=ENTRY_KEY):
    """What the admin does under Settings → Integrations → Outbound A2A endpoints."""
    return j.outbound.upsert_endpoint(ENTRY, f"{ORIGIN}/a2a/agents/front-desk", credential)


def _body(request_id, destination):
    return {"request_id": request_id, "type": "credential", "title": f"Needs {NAME}",
            "question": "For the agent there to post to Telegram.",
            "context": {"credential_name": NAME, "credential_destination": destination}}


def _raise_over_the_route(body):
    """The asking agent's `ask_for_credential` call as the backend receives it."""
    from fastapi import Response
    from models import OperatorAskCreate, User
    from routers import operator_queue as r

    user = User(id=7, username="op", email="op@example.com", role="admin",
                mcp_scope="agent", agent_name=ASKER)
    response = Response()
    receipt = asyncio.run(r.raise_my_ask(
        body=OperatorAskCreate(**body), response=response, name=ASKER,
        current_user=user, x_trinity_execution_id=None))
    return response.status_code, receipt


def _provide(uid):
    from client_portal.asks import service
    return asyncio.run(service.provide_credential(uid, OWNER, False, VALUE,
                                                  actor_ip="127.0.0.1",
                                                  endpoint="/api/enterprise/client-portal/asks/x/credential"))


def _rows(j, action=ACTION):
    return [a for a in j.audit if a.get("event_action") == action]


# ===========================================================================
# The main path
# ===========================================================================


def test_an_admin_registered_instance_carries_the_value_without_the_asking_agent_holding_a_key(journey):
    j = journey

    # 1. the admin registers the other Trinity with its key — stored encrypted
    record = _register(j)
    assert record["has_credentials"] is True
    assert ENTRY_KEY not in json.dumps(j.settings), "the registry row must hold an envelope, not the key"

    # 2. the asking agent names the registered instance — 201, nothing read from it
    status, receipt = _raise_over_the_route(_body("j-main", {"instance": ENTRY, "agent_name": TARGET}))
    assert (status, receipt["status"]) == (201, "created")
    assert receipt["credential_destination"] == {"agent_name": TARGET, "host": HOST}
    assert j.asker.calls == [], "an instance destination must not read the asking agent's .env at raise"
    stored = j.db.get_operator_queue_item(receipt["id"])["context"]["credential_destination"]
    assert stored == {"instance": ENTRY, "endpoint_id": record["id"],
                      "instance_url": ORIGIN, "agent_name": TARGET}

    # 3. the person answers — one merge inject on the other Trinity, under the entry key
    out = _provide(receipt["id"])
    assert len(j.instance.calls) == 1
    call = j.instance.calls[0]
    assert call["url"] == f"https://{ADDRESS}/api/agents/{TARGET}/credentials/inject"
    assert call["headers"]["Host"] == HOST
    assert call["headers"]["Authorization"] == f"Bearer {ENTRY_KEY}"
    assert call["json"] == {"mode": "merge", "env": {NAME: VALUE}}

    # the ask is answered, one audit row with names only
    row = j.db.get_operator_queue_item(receipt["id"])
    assert row["response"] == "provided" and out.answer == "provided"
    rows = _rows(j)
    assert len(rows) == 1
    assert rows[0]["details"]["outcome"] == "delivered"
    assert rows[0]["details"]["instance"] == ENTRY
    assert rows[0]["details"]["destination"] == {"host": HOST, "agent_name": TARGET}

    # the asking agent's .env: never read, never written, byte-identical
    assert j.asker.calls == []
    assert j.asker.env_text == ASKER_ENV
    assert (j.fake.hlen("secret_scrub:staged") or 0) == 0
    everything = json.dumps(j.audit, default=str) + json.dumps(row, default=str) \
        + json.dumps(receipt, default=str) + json.dumps(out.model_dump(), default=str)
    assert ENTRY_KEY not in everything and VALUE not in everything


# ===========================================================================
# Variant 1 — the admin clears the entry's key between raise and answer
# ===========================================================================


def test_a_key_cleared_after_the_raise_refuses_the_answer_by_name_and_sends_nothing(journey):
    from client_portal.asks import service

    j = journey
    _register(j)
    status, receipt = _raise_over_the_route(_body("j-cleared", {"instance": ENTRY, "agent_name": TARGET}))
    assert status == 201

    j.outbound.upsert_endpoint(ENTRY, f"{ORIGIN}/a2a/agents/front-desk", clear_credential=True)

    with pytest.raises(service.AskError) as info:
        _provide(receipt["id"])
    e = info.value
    assert (e.status_code, e.code) == (422, "destination_instance_no_credential"), (e.code, e.detail)
    assert e.data.get("reason") == "no_credential"
    assert j.instance.calls == []
    assert j.db.get_operator_queue_item(receipt["id"])["status"] == "pending"
    assert [r["details"]["outcome"] for r in _rows(j)] == ["destination_instance_no_credential"]
    assert j.asker.calls == [] and j.asker.env_text == ASKER_ENV


# ===========================================================================
# Variant 2 — the older key_env form, with no such variable: refused at raise
# ===========================================================================


def test_a_key_env_destination_without_the_variable_is_refused_before_a_person_types(journey):
    from fastapi import HTTPException

    j = journey
    _register(j)
    body = _body("j-keyenv", {"instance_url": ORIGIN, "agent_name": TARGET, "key_env": KEY_ENV})
    with pytest.raises(HTTPException) as info:
        _raise_over_the_route(body)
    assert info.value.status_code == 422
    assert info.value.detail["code"] == "destination_key_env_missing"
    assert "destination.instance" in info.value.detail["message"]
    # the variable was looked for in the asking agent's own .env, and nothing was stored
    assert ("GET" in [c[0] for c in j.asker.calls])
    assert j.asker.env_text == ASKER_ENV
    assert j.instance.calls == []

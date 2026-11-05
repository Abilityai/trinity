"""trinity-enterprise#1109 — a credential ask with a DESTINATION: the value a
person types into the Workspace secret field lands on another instance, in
the agent it is for, and never on the instance that asked.

The ent#1066 sink wrote the value into the ASKING agent's `.env`. A hub Regent
asks on behalf of an agent it is building elsewhere, so the ask now carries
`context.credential_destination = {instance_url, agent_name, key_env}`:
where the agent is, which agent, and the NAME of the variable in the asking
agent's own `.env` that holds its key to that instance. The sink reads that
key by name through the asking agent's read endpoint, re-validates the URL
at delivery time, pins the connection, and POSTs a MERGE inject to the
destination; after a 2xx nothing of the value rests here (the scrub staging
is removed). Every failure is a named plain retry.

Pinned, through the real ask sink, the real credential sink and the real
inject router over the real SQLite; the agents' HTTP servers, the destination
instance, DNS, Docker, Redis (fakeredis) and the audit sink are stood in for:

* the ask: a destination is validated at raise time by name and stored
  normalised; the receipt and the Workspace projection say where in plain
  words and never name the key variable; a destination on another kind is
  refused;
* the delivery: the key read by name from the asking agent, the merge inject
  on the destination under that key, nothing written on the asking agent,
  nothing staged afterwards, the audit row `credential_delivered` with host
  and agent only;
* every refusal: named, the ask pending, the value and the key in no log, no
  audit row, no refusal text; unstaged when nothing could have been written;
* the receiving side: `mode: "merge"` on the owner inject route patches one
  line and keeps every other byte, the whole-file default is unchanged;
* the manifest: `credentials:` names are parsed, validated and previewed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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

# Bound once, at collection, like `test_ent719_credential_and_key_routes_human_only.py`:
# the override key below and the route's own `Depends(get_current_user)` must be
# the same function object, and a module first imported inside a test is not
# kept across tests on this island.
import dependencies  # noqa: E402
import routers.credentials as r_credentials  # noqa: E402
from models import User  # noqa: E402

OWNER = "owner-1109@example.com"
ASKER = "regent-1109"            # the agent that asks (the hub Regent)
TARGET = "lopez-digest"          # the agent the credential is for, elsewhere
HOST = "member.example.com"
INSTANCE = f"https://{HOST}"
ADDRESS = "93.184.216.34"
NAME = "TELEGRAM_BOT_TOKEN"
KEY_ENV = "MEMBER_TRINITY_KEY"
KEY = "trinity_mcp_k3y_Zm9yLW1lbWJlci1pbnN0YW5jZQ"
VALUE = "7421:AAH-bot-token-value-with\"quote\\and\\backslash"
DEST = {"instance_url": INSTANCE, "agent_name": TARGET, "key_env": KEY_ENV}


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


def _record(addr, port=0):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))


def _resolver(host, port, *a, **k):
    if host == HOST:
        return [_record(ADDRESS, port)]
    if host == "private.example.com":
        return [_record("10.0.0.5", port)]
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _AgentServer:
    """An agent's internal HTTP server as the sink reaches it: an in-memory
    `.env`, a read endpoint and an inject endpoint. Records every call."""

    def __init__(self, env_text=""):
        self.env_text = env_text
        self.calls = []

    def client(self, agent_name):
        server = self

        class _Ctx:
            async def __aenter__(self):
                return server

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url))
        if self.env_text is None:
            return _Resp(200, {"files": {}, "files_b64": {}, "missing": [".env"]})
        return _Resp(200, {"files": {".env": self.env_text}, "files_b64": {}, "missing": []})

    async def post(self, url, json=None, **kw):
        self.calls.append(("POST", url))
        self.env_text = json["files"][".env"]
        return _Resp(200, {"status": "success", "files_written": [".env"]})


class _Instance:
    """The DESTINATION instance's backend as the sink reaches it over the
    pinned transport. Records the request it saw; answers as told."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.error = None

    def client(self, *a, **k):
        inst = self

        class _Ctx:
            async def __aenter__(self):
                return inst

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def post(self, url, json=None, headers=None, extensions=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {}),
                           "extensions": dict(extensions or {})})
        if self.error:
            raise self.error
        if self.status != 200:
            return _Resp(self.status, {"detail": "nope"})
        return _Resp(200, {"status": "success", "files_written": [".env"]})


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    from services import credential_destination
    monkeypatch.setattr(credential_destination, "_resolver", lambda: _resolver)


class _FakeEnc:
    def encrypt(self, d):
        return "ENV:" + json.dumps(d)

    def decrypt(self, s):
        return json.loads(s[len("ENV:"):])


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real ask sink; the world around it stubbed as the ent#1066 suite stubs it."""
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit, sent, events = [], [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_observers", [events.append])
    monkeypatch.setattr(svc, "_owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", True))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, sent=sent, events=events, db=real_db)
    assignment_provider.clear_provider()


@pytest.fixture
def world(real_db, monkeypatch):
    """The real credential sink over the real SQLite: the asking agent's
    server holds the key under KEY_ENV; the destination instance records what
    it is sent; every side channel recorded so a test can grep it."""
    import services.ask_service as svc
    import services.credential_sink as sink
    import services.runtime_secret_scrub as scrub

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(scrub, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(scrub, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(scrub, "_redis_down_until", 0.0, raising=False)
    monkeypatch.setattr(sink, "get_breaker_redis", lambda: fake)

    asker = _AgentServer(f'OTHER="keep me"\n{KEY_ENV}="{KEY}"\n')
    monkeypatch.setattr(sink, "_agent_client", asker.client)
    instance = _Instance()
    monkeypatch.setattr(sink, "_destination_client", instance.client)
    status = {"status": "running"}
    monkeypatch.setattr(sink, "get_agent_container", lambda name: MagicMock())
    monkeypatch.setattr(sink, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status=status["status"]))

    audit, events = [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    monkeypatch.setattr(sink, "platform_audit_service", _Audit())
    monkeypatch.setattr(sink.credential_requirements_service, "invalidate_report_cache",
                        lambda name: None)
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [events.append])
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda a: False, raising=False)

    return SimpleNamespace(db=real_db, sink=sink, scrub=scrub, fake=fake, asker=asker,
                           instance=instance, status=status, audit=audit, events=events)


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "credential", "title": f"Needs {NAME}",
         "question": "For the digest agent to post to Telegram.",
         "context": {"credential_name": NAME, "credential_destination": dict(DEST)}}
    b.update(over)
    return b


def _raise(ask, body, agent=ASKER):
    return ask.svc.raise_ask(agent, body, raised_by="agent", channel="mcp")


def _refused_raise(ask, body, code):
    with pytest.raises(ask.svc.AskRejected) as info:
        _raise(ask, body)
    assert (info.value.status_code, info.value.code) == (422, code), (
        info.value.status_code, info.value.code, info.value.message)
    return info.value


def _pending(real_db, rid, destination=DEST, name=NAME, **over):
    context = {"credential_name": name}
    if destination is not None:
        context["credential_destination"] = dict(destination)
    item = {"id": rid, "type": "credential", "status": "pending", "priority": "medium",
            "title": f"Needs {name}", "question": "to post the digest", "options": None,
            "context": context, "created_at": "2026-11-04T10:00:00Z",
            "addressed_to_email": OWNER}
    item.update(over)
    return real_db.create_operator_queue_item(ASKER, item)


def _provide(uid, value=VALUE, email=OWNER, is_platform=False):
    from client_portal.asks import service
    return asyncio.run(service.provide_credential(uid, email, is_platform, value,
                                                  actor_ip="127.0.0.1",
                                                  endpoint="/api/enterprise/client-portal/asks/x/credential"))


def _refused(uid, status, code, **kw):
    from client_portal.asks import service
    with pytest.raises(service.AskError) as info:
        _provide(uid, **kw)
    assert (info.value.status_code, info.value.code) == (status, code), (
        info.value.status_code, info.value.code, info.value.detail)
    return info.value


def _staged(world):
    return world.fake.hlen("secret_scrub:staged") or 0


# ===========================================================================
# 1. The ask carries a destination
# ===========================================================================


class TestTheAsk:
    def test_a_destination_is_stored_normalised_and_the_receipt_says_where_in_plain_words(self, ask):
        receipt = _raise(ask, _body("d-1"))
        assert receipt["status"] == "created"
        row = ask.db.get_operator_queue_item(receipt["id"])
        stored = row["context"]["credential_destination"]
        assert stored == {"instance_url": INSTANCE, "agent_name": TARGET, "key_env": KEY_ENV}
        # the receipt names the agent and the host — the key's variable never
        assert receipt["credential_destination"] == {"agent_name": TARGET, "host": HOST}
        assert KEY_ENV not in json.dumps(receipt)

    def test_a_trailing_slash_and_upper_case_host_are_normalised(self, ask):
        body = _body("d-2")
        body["context"]["credential_destination"]["instance_url"] = f"https://{HOST.upper()}/"
        receipt = _raise(ask, body)
        row = ask.db.get_operator_queue_item(receipt["id"])
        assert row["context"]["credential_destination"]["instance_url"] == INSTANCE

    def test_a_receipt_without_a_destination_carries_none(self, ask):
        body = _body("d-3")
        body["context"].pop("credential_destination")
        receipt = _raise(ask, body)
        assert receipt["credential_destination"] is None

    @pytest.mark.parametrize("url,reason", [
        (f"http://{HOST}", "endpoint_not_https"),
        ("https://private.example.com", "endpoint_private_address"),
        ("https://nowhere.invalid", "endpoint_dns_failure"),
        (f"https://user:pw@{HOST}", "endpoint_invalid"),
        (f"{INSTANCE}/api", "path"),
        ("", "missing"),
        (42, "not_a_string"),
    ])
    def test_a_bad_instance_url_is_refused_by_name(self, ask, url, reason):
        body = _body("d-url")
        body["context"]["credential_destination"]["instance_url"] = url
        e = _refused_raise(ask, body, "destination_url_invalid")
        assert e.extra.get("reason") == reason

    @pytest.mark.parametrize("agent", ["", "../etc", "has space", "x" * 65, 7])
    def test_a_bad_agent_name_is_refused_by_name(self, ask, agent):
        body = _body("d-agent")
        body["context"]["credential_destination"]["agent_name"] = agent
        _refused_raise(ask, body, "destination_agent_invalid")

    @pytest.mark.parametrize("key_env,reason", [
        ("not a name", "not_a_name"), ("TRINITY_MCP_API_KEY", "reserved"),
        ("K" * 65, "too_long"), (None, "not_a_name"),
    ])
    def test_a_bad_key_variable_is_refused_by_name(self, ask, key_env, reason):
        body = _body("d-key")
        body["context"]["credential_destination"]["key_env"] = key_env
        e = _refused_raise(ask, body, "destination_key_env_invalid")
        assert e.extra.get("reason") == reason

    @pytest.mark.parametrize("shape", ["text", [1], {"instance_url": INSTANCE}])
    def test_a_destination_of_the_wrong_shape_is_refused(self, ask, shape):
        body = _body("d-shape")
        body["context"]["credential_destination"] = shape
        _refused_raise(ask, body, "destination_invalid")

    def test_a_destination_on_a_question_is_refused(self, ask):
        body = _body("d-kind", type="question", options=None)
        body["context"] = {"credential_destination": dict(DEST)}
        _refused_raise(ask, body, "destination_on_non_credential")

    def test_the_workspace_projection_says_where_and_never_names_the_key_variable(self, ask):
        from client_portal.asks import service
        receipt = _raise(ask, _body("d-proj"))
        row = ask.db.get_operator_queue_item(receipt["id"])
        out = service._project(row, viewer_email=OWNER)
        assert out.credential_name == NAME
        assert out.credential_destination == {"agent_name": TARGET, "host": HOST}
        assert KEY_ENV not in json.dumps(out.model_dump(), default=str)

    def test_the_wake_says_the_credential_went_elsewhere_and_is_not_in_the_environment(self, ask):
        from services import operator_resume_service as ors
        receipt = _raise(ask, _body("d-wake"))
        row = ask.db.get_operator_queue_item(receipt["id"])
        row = dict(row, response="provided", status="answered")
        text = ors._framed_message(row, "provided", None)
        assert TARGET in text and HOST in text
        assert "not in your environment" in text
        assert KEY_ENV not in text


# ===========================================================================
# 2. The delivery
# ===========================================================================


class TestDelivery:
    def test_the_value_lands_on_the_destination_under_the_asking_agents_key_and_nowhere_here(
            self, world, caplog):
        caplog.set_level(logging.DEBUG)
        uid = _pending(world.db, "del-1")
        out = _provide(uid)

        # one merge inject on the destination, pinned to the resolved address,
        # the registered host in Host + SNI, the key read by name as the bearer
        assert len(world.instance.calls) == 1
        call = world.instance.calls[0]
        assert call["url"] == f"https://{ADDRESS}/api/agents/{TARGET}/credentials/inject"
        assert call["headers"]["Host"] == HOST
        assert call["headers"]["Authorization"] == f"Bearer {KEY}"
        assert call["extensions"]["sni_hostname"] == HOST
        assert call["json"] == {"mode": "merge", "env": {NAME: VALUE}}
        # the asking agent was READ for its key and never written
        assert [m for m, _ in world.asker.calls] == ["GET"]
        assert NAME not in world.asker.env_text
        # nothing rests here after delivery
        assert _staged(world) == 0
        # the ask ended provided, by name
        row = world.db.get_operator_queue_item(uid)
        assert row["disposition"] == "answered" and row["response"] == "provided"
        assert out.answer == "provided"
        # the audit row: name, ask, destination — never the value or the key
        delivered = [a for a in world.audit if a.get("event_action") == "credential_delivered"]
        assert len(delivered) == 1
        details = delivered[0]["details"]
        assert details["name"] == NAME and details["ask_id"] == uid
        assert details["destination"] == {"host": HOST, "agent_name": TARGET}
        everything = caplog.text + json.dumps(world.audit, default=str) + json.dumps(row, default=str) \
            + json.dumps(out.model_dump(), default=str)
        assert VALUE not in everything and KEY not in everything and KEY_ENV not in json.dumps(out.model_dump())

    def test_the_destination_is_read_from_the_stored_row_not_from_the_request(self, world):
        uid = _pending(world.db, "del-2", destination={"instance_url": f"https://{HOST}:8443",
                                                         "agent_name": TARGET, "key_env": KEY_ENV})
        _provide(uid)
        call = world.instance.calls[0]
        assert call["url"] == f"https://{ADDRESS}:8443/api/agents/{TARGET}/credentials/inject"
        assert call["headers"]["Host"] == f"{HOST}:8443"

    def test_a_row_without_a_destination_still_writes_the_asking_agent(self, world):
        uid = _pending(world.db, "del-3", destination=None)
        _provide(uid)
        assert world.instance.calls == []
        assert f'{NAME}=' in world.asker.env_text
        assert any(a.get("event_action") == "chat_provide" for a in world.audit)

    def test_a_missing_key_is_a_named_409_with_no_remote_call_and_nothing_staged(self, world):
        world.asker.env_text = 'OTHER="keep me"\n'
        uid = _pending(world.db, "del-4")
        e = _refused(uid, 409, "destination_key_missing")
        assert world.instance.calls == []
        assert _staged(world) == 0
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"
        assert KEY_ENV not in f"{e.detail} {json.dumps(e.data, default=str)}"

    def test_an_asking_agent_that_is_not_running_is_the_same_409_as_before(self, world):
        world.status["status"] = "stopped"
        uid = _pending(world.db, "del-5")
        _refused(uid, 409, "agent_not_running")
        assert world.instance.calls == [] and _staged(world) == 0

    def test_a_connect_failure_is_a_503_and_unstages(self, world):
        world.instance.error = httpx.ConnectError("boom")
        uid = _pending(world.db, "del-6")
        e = _refused(uid, 503, "destination_unreachable")
        assert _staged(world) == 0
        assert "nothing was stored" in e.detail.lower()
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_timeout_after_the_request_left_keeps_the_value_staged_and_says_so(self, world):
        world.instance.error = httpx.ReadTimeout("slow")
        uid = _pending(world.db, "del-7")
        e = _refused(uid, 503, "destination_unreachable")
        assert _staged(world) == 1
        assert "may or may not" in e.detail
        assert e.data.get("may_have_written") is True

    @pytest.mark.parametrize("status,code", [
        (401, "destination_key_refused"), (403, "destination_key_refused"),
        (404, "destination_agent_missing"), (400, "destination_refused"),
        (500, "destination_refused"),
    ])
    def test_a_refusal_from_the_destination_is_named_and_unstaged(self, world, caplog, status, code):
        caplog.set_level(logging.DEBUG)
        world.instance.status = status
        uid = _pending(world.db, f"del-{status}")
        e = _refused(uid, 409, code)
        if code == "destination_refused":
            assert e.data.get("status") == status
        assert _staged(world) == 0
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"
        text = f"{e.detail} {json.dumps(e.data, default=str)} {caplog.text}"
        assert VALUE not in text and KEY not in text
        assert not [a for a in world.audit if a.get("event_action") == "credential_delivered"]

    def test_a_url_that_no_longer_validates_at_delivery_time_is_refused(self, world, monkeypatch):
        from services import credential_destination
        monkeypatch.setattr(credential_destination, "_resolver",
                            lambda: (lambda h, p, *a, **k: [_record("10.0.0.9", p)]))
        uid = _pending(world.db, "del-8")
        _refused(uid, 422, "destination_url_invalid")
        assert world.instance.calls == [] and _staged(world) == 0

    def test_the_scrub_is_armed_before_the_key_is_read(self, world, monkeypatch):
        monkeypatch.setattr(world.scrub, "get_breaker_redis", lambda: None)
        uid = _pending(world.db, "del-9")
        _refused(uid, 503, "scrub_unavailable")
        assert world.asker.calls == [] and world.instance.calls == []


# ===========================================================================
# 3. The receiving side: mode "merge" on the owner inject route
# ===========================================================================


@pytest.fixture
def inject_app(real_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import services.credential_sink as sink

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(sink, "get_breaker_redis", lambda: fake)
    agent = _AgentServer('OTHER="keep me"\n# note\nFOO=bar\n')
    monkeypatch.setattr(sink, "_agent_client", agent.client)
    monkeypatch.setattr(sink, "get_agent_container", lambda name: MagicMock())
    monkeypatch.setattr(sink, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status="running"))
    monkeypatch.setattr(r_credentials, "get_agent_container", lambda name: MagicMock())
    monkeypatch.setattr(r_credentials, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status="running"))
    monkeypatch.setattr(r_credentials, "agent_httpx_client", agent.client)
    monkeypatch.setattr(r_credentials, "enforce_rate_limit", lambda *a, **k: None, raising=False)
    audit = []

    async def _log(**kw):
        audit.append(kw)

    monkeypatch.setattr(r_credentials.platform_audit_service, "log", _log)
    monkeypatch.setattr(r_credentials.credential_requirements_service, "invalidate_report_cache",
                        lambda name: None)

    app = FastAPI()
    app.include_router(r_credentials.router)
    person = User(id=1, username="owner", email=OWNER, role="admin")
    app.dependency_overrides[dependencies.get_current_user] = lambda: person
    app.dependency_overrides[dependencies.get_owned_agent_by_name] = lambda: TARGET
    yield SimpleNamespace(client=TestClient(app), agent=agent, audit=audit)
    app.dependency_overrides.clear()


class TestMergeInject:
    def test_merge_sets_the_named_lines_and_keeps_every_other_byte(self, inject_app):
        r = inject_app.client.post(f"/api/agents/{TARGET}/credentials/inject",
                                   json={"mode": "merge", "env": {NAME: VALUE, "FOO": "baz"}})
        assert r.status_code == 200, r.text
        assert r.json()["files_written"] == [".env"]
        env = inject_app.agent.env_text
        assert env.startswith('OTHER="keep me"\n# note\nFOO="baz"\n')
        assert env.count(f"{NAME}=") == 1 and "keep me" in env
        assert [m for m, _ in inject_app.agent.calls] == ["GET", "POST"]
        row = inject_app.audit[-1]
        assert row["event_action"] == "inject"
        assert row["details"]["mode"] == "merge" and sorted(row["details"]["names"]) == sorted([NAME, "FOO"])
        assert VALUE not in json.dumps(inject_app.audit, default=str)

    def test_merge_with_files_or_without_env_is_a_named_400(self, inject_app):
        r = inject_app.client.post(f"/api/agents/{TARGET}/credentials/inject",
                                   json={"mode": "merge", "env": {NAME: VALUE}, "files": {".env": "X=1\n"}})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "merge_takes_env"
        r = inject_app.client.post(f"/api/agents/{TARGET}/credentials/inject",
                                   json={"mode": "merge"})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "merge_takes_env"
        assert inject_app.agent.calls == []

    @pytest.mark.parametrize("env", [{"not a name": "v"}, {NAME: "two\nlines"}, {NAME: ""}])
    def test_a_bad_name_or_value_is_refused_before_the_agent_is_read(self, inject_app, env):
        r = inject_app.client.post(f"/api/agents/{TARGET}/credentials/inject",
                                   json={"mode": "merge", "env": env})
        assert r.status_code in (400, 422), r.text
        assert inject_app.agent.calls == []

    def test_the_default_mode_is_the_whole_file_write_unchanged(self, inject_app):
        r = inject_app.client.post(f"/api/agents/{TARGET}/credentials/inject",
                                   json={"files": {".env": "ONLY=this\n"}})
        assert r.status_code == 200, r.text
        assert inject_app.agent.env_text == "ONLY=this\n"
        assert [m for m, _ in inject_app.agent.calls] == ["POST"]


# ===========================================================================
# 4. The manifest: credentials are names
# ===========================================================================


class TestManifestCredentials:
    def _manifest(self, creds):
        return (
            "name: digest\n"
            "agents:\n"
            "  digest:\n"
            "    template: local:digest\n"
            f"    credentials: {creds}\n"
        )

    def test_credential_names_are_parsed_kept_and_not_warned_about(self):
        from services import system_service
        manifest = system_service.parse_manifest(self._manifest("[TELEGRAM_BOT_TOKEN, SLACK_TOKEN]"))
        assert manifest.agents["digest"].credentials == ["TELEGRAM_BOT_TOKEN", "SLACK_TOKEN"]
        assert manifest.unknown_agent_keys == {}
        assert not [w for w in system_service.validate_manifest(manifest) if "credentials" in w]

    @pytest.mark.parametrize("creds", ["[not a name]", "[1]", "[TRINITY_MCP_API_KEY]"])
    def test_a_bad_credentials_entry_is_refused_by_name(self, creds):
        from services import system_service
        with pytest.raises(ValueError, match="credentials"):
            system_service.parse_manifest(self._manifest(creds))

    @pytest.mark.parametrize("creds", ["{}", "{OPENAI_API_KEY: sk-value}", "TELEGRAM_BOT_TOKEN"])
    def test_the_older_map_form_stays_an_unknown_key_warned_and_dropped(self, creds):
        # The shape this parser never read keeps its exact pre-1109 fate (the
        # ent#695 golden and the ent#1030 pins): unknown, warned, no value read.
        from services import system_service
        manifest = system_service.parse_manifest(self._manifest(creds))
        assert manifest.agents["digest"].credentials is None
        assert manifest.unknown_agent_keys == {"digest": ["credentials"]}
        warnings = system_service.validate_manifest(manifest)
        assert any(w.startswith("Agent 'digest': ignored unknown key(s): credentials.") for w in warnings)
        assert "sk-value" not in " ".join(warnings)

    def test_the_preview_lists_the_names_per_member(self):
        from services import system_service
        manifest = system_service.parse_manifest(self._manifest("[TELEGRAM_BOT_TOKEN]"))
        extras = system_service._configured_preview(manifest.agents["digest"])
        assert extras["credentials"] == ["TELEGRAM_BOT_TOKEN"]
        bare = system_service.parse_manifest(self._manifest("[]"))
        assert "credentials" not in system_service._configured_preview(bare.agents["digest"])

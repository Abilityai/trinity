"""trinity-enterprise#1158 — a credential ask's destination may NAME a
registered outbound instance instead of carrying the asking agent's own key.

ent#1109's destination is `{instance_url, agent_name, key_env}`: the key to
the other Trinity is read by name from the asking agent's own `.env`, which an
agent has no sanctioned way to write. The second shape,

    {"instance": "<registered outbound endpoint name or id>", "agent_name": "…"}

names an entry an admin registered under Settings → Integrations → Outbound
A2A endpoints; the platform delivers under the key stored for that entry, so
the asking agent never holds a copy.

Pinned here (checkpoints 1 and 2 of the plan — the raise half, the answer path,
then delivery: the resolve point, raise/delivery parity, one audit row):

* the answer path fails CLOSED: a stored destination this Trinity cannot read
  is a 409 `destination_unreadable` and nothing is written anywhere — before
  this, the value fell through to the asking agent's own `.env`;
* the shape union: exactly one of the two shapes, named refusals for both,
  neither, and a bad `instance`; `destination_of` reads both shapes back and
  every reader of it renders an `instance` row;
* the raise-time refusals, read from the entry's METADATA through the same
  resolver an A2A call uses (`a2a_outbound.resolve_endpoint`): unknown,
  no credential, aauth, payment token; the stored row keeps the entry's id,
  its registered name and the origin of its URL — never its credential.

Real ask sink over the real SQLite; the registry, the switch, the roster and
the audit/websocket side channels are stood in for.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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

OWNER = "owner-1158@example.com"
ASKER = "asking-agent-1158"          # the agent that raises the ask
TARGET = "target-agent"              # the agent the credential is for, elsewhere
HOST = "remote.example.com"
ORIGIN = f"https://{HOST}"
ENTRY = "instance-remote"            # the admin's registry entry for that Trinity
ENTRY_ID = "ep-remote"
SECRET = "trinity_mcp_ENTRY_KEY_c2VudGluZWwtZW50cnk"   # the entry's stored credential
NAME = "TELEGRAM_BOT_TOKEN"
VALUE = "7421:AAH-a-bot-token-value"


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


def _entry(**over):
    rec = {"id": ENTRY_ID, "name": ENTRY, "url": f"{ORIGIN}/a2a/agents/front-desk",
           "credential": SECRET}
    rec.update(over)
    return rec


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """The admin's outbound registry (the OSS `system_settings` list, read
    through the real provider seam) holds one entry with a stored key, and the
    outbound switch is ON. A test edits either."""
    from services import a2a_outbound, a2a_outbound_service
    records = [_entry()]
    switch = {"on": True}
    a2a_outbound.clear_provider()
    monkeypatch.setattr(a2a_outbound, "_load_endpoint_records", lambda: [dict(r) for r in records])
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: switch["on"])
    yield SimpleNamespace(records=records, switch=switch)
    a2a_outbound.clear_provider()


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real ask sink; the world around it stubbed as the ent#1109 suite stubs it."""
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit = []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            pass

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "_owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", True))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, db=real_db)
    assignment_provider.clear_provider()


@pytest.fixture
def sink_calls(monkeypatch, real_db):
    """`credential_sink.set_env_value` recorded instead of run: the answer
    path's decision is what is pinned here, not the delivery."""
    import services.ask_service as svc
    import services.credential_sink as sink

    calls = []

    async def _record(agent, name, value, **kw):
        calls.append({"agent": agent, "name": name, **kw})

    class _Audit:
        async def log(self, **kw):
            return "evt"

    monkeypatch.setattr(sink, "set_env_value", _record)
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda a: False, raising=False)
    return calls


def _dest(**over):
    d = {"instance": ENTRY, "agent_name": TARGET}
    d.update(over)
    return d


def _body(request_id, destination=None):
    return {"request_id": request_id, "type": "credential", "title": f"Needs {NAME}",
            "question": "For the agent there to post to Telegram.",
            "context": {"credential_name": NAME,
                        "credential_destination": _dest() if destination is None else destination}}


def _raise(ask, body, agent=ASKER):
    return ask.svc.raise_ask(agent, body, raised_by="agent", channel="mcp")


def _refused_raise(ask, body, code, agent=ASKER):
    with pytest.raises(ask.svc.AskRejected) as info:
        _raise(ask, body, agent=agent)
    assert (info.value.status_code, info.value.code) == (422, code), (
        info.value.status_code, info.value.code, info.value.message)
    return info.value


_RAW = object()


def _pending(real_db, rid, destination=_RAW):
    context = {"credential_name": NAME}
    if destination is not _RAW:
        context["credential_destination"] = destination
    item = {"id": rid, "type": "credential", "status": "pending", "priority": "medium",
            "title": f"Needs {NAME}", "question": "to post the digest", "options": None,
            "context": context, "created_at": "2026-11-09T10:00:00Z",
            "addressed_to_email": OWNER}
    return real_db.create_operator_queue_item(ASKER, item)


def _provide(uid):
    from client_portal.asks import service
    return asyncio.run(service.provide_credential(uid, OWNER, False, VALUE,
                                                  actor_ip="127.0.0.1",
                                                  endpoint="/api/enterprise/client-portal/asks/x/credential"))


STORED = {"instance": ENTRY, "endpoint_id": ENTRY_ID, "instance_url": ORIGIN, "agent_name": TARGET}


# ===========================================================================
# 1. The answer path fails closed on a destination it cannot read (C5)
# ===========================================================================


class TestUnreadableDestinationFailsClosed:
    @pytest.mark.parametrize("stored", [
        None,                                                    # key present, value null
        {},                                                      # key present, empty
        "https://remote.example.com",                            # not an object
        {"instance_url": ORIGIN, "agent_name": TARGET},          # neither shape complete
        {**STORED, "key_env": "SOME_KEY"},                       # both shapes at once
        {"instance": ENTRY, "instance_url": ORIGIN, "agent_name": TARGET},  # no endpoint_id
        {**STORED, "agent_name": "../escape"},                   # a name the routes refuse
        {"future_shape": {"anything": 1}, "agent_name": TARGET},  # a shape a later version writes
    ])
    def test_a_destination_that_does_not_parse_is_a_409_and_nothing_is_written(
            self, real_db, sink_calls, stored):
        from client_portal.asks import service
        uid = _pending(real_db, f"unread-{abs(hash(json.dumps(stored, default=str))) % 10**8}",
                       destination=stored)
        with pytest.raises(service.AskError) as info:
            _provide(uid)
        assert (info.value.status_code, info.value.code) == (409, "destination_unreadable")
        assert "nothing was stored" in info.value.detail
        assert "again" in info.value.detail
        # never fell through to the asking agent's own `.env`, nor anywhere else
        assert sink_calls == []
        assert real_db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_row_without_a_destination_still_writes_the_asking_agent(self, real_db, sink_calls):
        uid = _pending(real_db, "unread-none")
        _provide(uid)
        assert len(sink_calls) == 1
        assert sink_calls[0]["agent"] == ASKER and sink_calls[0]["destination"] is None

    def test_an_instance_row_is_handed_to_the_sink_as_stored(self, real_db, sink_calls):
        uid = _pending(real_db, "unread-instance", destination=dict(STORED))
        _provide(uid)
        assert len(sink_calls) == 1
        assert sink_calls[0]["destination"] == STORED

    def test_a_key_env_row_is_handed_to_the_sink_as_stored(self, real_db, sink_calls):
        legacy = {"instance_url": ORIGIN, "agent_name": TARGET, "key_env": "REMOTE_KEY"}
        uid = _pending(real_db, "unread-keyenv", destination=dict(legacy))
        _provide(uid)
        assert sink_calls[0]["destination"] == legacy


# ===========================================================================
# 2. The shape union
# ===========================================================================


class TestShapes:
    def test_carries_destination_is_about_the_key_not_its_value(self):
        from services import credential_destination as cd
        assert cd.carries_destination({"credential_destination": None})
        assert cd.carries_destination({"credential_destination": {}})
        assert cd.carries_destination({"credential_destination": dict(STORED)})
        assert not cd.carries_destination({"credential_name": NAME})
        assert not cd.carries_destination(None)
        assert not cd.carries_destination("credential_destination")

    def test_destination_of_reads_both_shapes_back(self):
        from services import credential_destination as cd
        legacy = {"instance_url": ORIGIN, "agent_name": TARGET, "key_env": "REMOTE_KEY"}
        assert cd.destination_of({"credential_destination": legacy}) == legacy
        assert cd.destination_of({"credential_destination": dict(STORED)}) == STORED
        # extra keys are not carried forward
        assert cd.destination_of({"credential_destination": {**STORED, "note": "x"}}) == STORED

    @pytest.mark.parametrize("stored", [
        {**STORED, "key_env": "REMOTE_KEY"},
        {k: v for k, v in STORED.items() if k != "endpoint_id"},
        {k: v for k, v in STORED.items() if k != "instance_url"},
        {**STORED, "instance": ""},
        {**STORED, "instance": "x" * 201},
        {**STORED, "endpoint_id": 7},
    ])
    def test_destination_of_reads_a_broken_instance_row_as_none(self, stored):
        from services import credential_destination as cd
        assert cd.destination_of({"credential_destination": stored}) is None

    def test_both_shapes_at_once_is_ambiguous(self, ask):
        e = _refused_raise(ask, _body("s-both", _dest(instance_url=ORIGIN, key_env="REMOTE_KEY")),
                           "destination_invalid")
        assert e.extra.get("reason") == "ambiguous_shape"
        e = _refused_raise(ask, _body("s-both-2", _dest(key_env="REMOTE_KEY")), "destination_invalid")
        assert e.extra.get("reason") == "ambiguous_shape"

    @pytest.mark.parametrize("destination", [
        {"agent_name": TARGET},
        {"instance_url": ORIGIN, "agent_name": TARGET},
        {"instance": ENTRY},
    ])
    def test_neither_shape_complete_is_a_missing_field(self, ask, destination):
        e = _refused_raise(ask, _body("s-missing", destination), "destination_invalid")
        assert e.extra.get("reason") == "missing_field"

    @pytest.mark.parametrize("instance", ["", 7, None, "x" * 201, "inst\nance", "inst\x00ance",
                                          "inst\x7fance"])
    def test_a_bad_instance_is_refused_by_name(self, ask, instance):
        e = _refused_raise(ask, _body("s-inst", _dest(instance=instance)),
                           "destination_instance_invalid")
        assert "Settings → Integrations → Outbound A2A endpoints" in e.message

    def test_an_instance_of_exactly_the_registry_bound_is_accepted(self, ask, registry):
        registry.records.append(_entry(id="ep-long", name="y" * 200))
        receipt = _raise(ask, _body("s-bound", _dest(instance="y" * 200)))
        assert receipt["status"] == "created"

    @pytest.mark.parametrize("agent", ["", "../x", "a/b", "-lead", "x" * 65])
    def test_a_bad_agent_name_is_refused_on_the_instance_shape_too(self, ask, agent):
        _refused_raise(ask, _body("s-agent", _dest(agent_name=agent)), "destination_agent_invalid")


# ===========================================================================
# 3. Raise-time refusals, read from the entry's metadata
# ===========================================================================


class TestRaiseAgainstTheRegistry:
    def test_the_stored_row_keeps_the_entry_id_name_and_origin_never_its_key(self, ask):
        receipt = _raise(ask, _body("r-ok"))
        assert receipt["credential_destination"] == {"agent_name": TARGET, "host": HOST}
        row = ask.db.get_operator_queue_item(receipt["id"])
        assert row["context"]["credential_destination"] == STORED
        everything = json.dumps(row, default=str) + json.dumps(receipt, default=str) \
            + json.dumps(ask.audit, default=str)
        assert SECRET not in everything

    def test_the_entry_is_matched_by_id_or_case_insensitive_name(self, ask):
        for i, ref in enumerate([ENTRY_ID, ENTRY.upper(), ENTRY]):
            receipt = _raise(ask, _body(f"r-ref-{i}", _dest(instance=ref)))
            row = ask.db.get_operator_queue_item(receipt["id"])
            # the REGISTERED name is stored, whatever spelling the agent used
            assert row["context"]["credential_destination"] == STORED

    def test_the_origin_keeps_a_non_default_port_and_drops_the_path(self, ask, registry):
        registry.records[0] = _entry(url=f"https://{HOST.upper()}:8443/a2a/x?y=1")
        receipt = _raise(ask, _body("r-port"))
        row = ask.db.get_operator_queue_item(receipt["id"])
        assert row["context"]["credential_destination"]["instance_url"] == f"https://{HOST}:8443"
        assert receipt["credential_destination"]["host"] == f"{HOST}:8443"

    @pytest.mark.parametrize("ref", ["no-such-entry", "ep-nope"])
    def test_an_unknown_entry_is_refused_and_told_where_to_register(self, ask, ref):
        e = _refused_raise(ask, _body("r-unknown", _dest(instance=ref)), "destination_not_approved")
        assert e.extra.get("reason") == "instance_unknown"
        assert "Settings → Integrations → Outbound A2A endpoints" in e.message

    @pytest.mark.parametrize("over,reason", [
        ({"credential": None}, "no_credential"),
        ({"credential": ""}, "no_credential"),
        ({"credential": None, "auth_scheme": "aauth"}, "aauth"),
        ({"credential_kind": "payment_token"}, "payment_token"),
    ])
    def test_an_entry_without_a_bearer_key_is_the_named_422(self, ask, registry, over, reason):
        registry.records[0] = _entry(**over)
        e = _refused_raise(ask, _body(f"r-nokey-{reason}"), "destination_instance_no_credential")
        assert e.extra.get("reason") == reason
        assert "Settings → Integrations → Outbound A2A endpoints" in e.message
        assert "admin" in e.message
        assert SECRET not in e.message

    def test_outbound_switched_off_is_named_before_the_registry(self, ask, registry):
        registry.switch["on"] = False
        e = _refused_raise(ask, _body("r-off", _dest(instance="no-such-entry")),
                           "destination_outbound_disabled")
        assert e.extra.get("reason") == "outbound_disabled"

    def test_the_entry_is_resolved_for_the_asking_agent_through_the_effective_provider(
            self, ask, registry):
        """The reach is `resolve_endpoint(<asking agent>, ref)` — the same call
        an A2A call makes — so a provider that scopes entries per agent scopes
        this too, and an out-of-reach entry is the same refusal as an unknown one."""
        from services import a2a_outbound
        seen = []

        class _Scoped:
            def resolve_endpoint(self, agent_name, ref):
                seen.append((agent_name, ref))
                if agent_name != ASKER:
                    return None
                return a2a_outbound.ResolvedEndpoint(
                    id="p1", name="partner", url="https://partner.example.com/a2a",
                    credential=SECRET)

            def list_endpoints(self, agent_name):
                return []

        a2a_outbound.register_provider(_Scoped())
        receipt = _raise(ask, _body("r-prov", _dest(instance="partner")))
        assert receipt["credential_destination"]["host"] == "partner.example.com"
        assert seen[-1] == (ASKER, "partner")
        e = _refused_raise(ask, _body("r-prov-2", _dest(instance="partner")),
                           "destination_not_approved", agent="other-agent")
        assert e.extra.get("reason") == "instance_unknown"

    def test_a_provider_error_is_the_unknown_refusal_and_carries_no_exception_text(
            self, ask, registry):
        from services import a2a_outbound

        class _Broken:
            def resolve_endpoint(self, agent_name, ref):
                raise RuntimeError(f"backend down {SECRET}")

            def list_endpoints(self, agent_name):
                return []

        a2a_outbound.register_provider(_Broken())
        e = _refused_raise(ask, _body("r-broken"), "destination_not_approved")
        assert e.extra.get("reason") == "instance_unknown"
        assert SECRET not in e.message and "backend down" not in e.message

    def test_instance_entry_is_metadata_only(self):
        from services import credential_destination as cd
        meta = cd.instance_entry(ASKER, ENTRY)
        assert meta == {"id": ENTRY_ID, "name": ENTRY, "url": f"{ORIGIN}/a2a/agents/front-desk",
                        "auth_scheme": "bearer", "credential_kind": "api_key",
                        "has_credential": True}
        assert SECRET not in json.dumps(meta)
        assert cd.instance_entry(ASKER, "no-such-entry") is None

    def test_raise_time_never_resolves_the_host(self, ask, monkeypatch):
        import socket

        def _boom(*a, **k):
            raise AssertionError("raise time must not resolve a host")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        assert _raise(ask, _body("r-nodns"))["status"] == "created"

    def test_the_key_env_shape_is_unchanged(self, ask, registry):
        registry.records.append({"id": "ep-legacy", "name": "legacy", "url": "https://legacy.example.com"})
        legacy = {"instance_url": "https://legacy.example.com/", "agent_name": TARGET,
                  "key_env": "REMOTE_KEY"}
        receipt = _raise(ask, _body("r-legacy", legacy))
        row = ask.db.get_operator_queue_item(receipt["id"])
        assert row["context"]["credential_destination"] == {
            "instance_url": "https://legacy.example.com", "agent_name": TARGET,
            "key_env": "REMOTE_KEY"}

    def test_a_userinfo_refusal_no_longer_points_at_key_env(self, ask):
        legacy = {"instance_url": f"https://u:p@{HOST}", "agent_name": TARGET, "key_env": "REMOTE_KEY"}
        e = _refused_raise(ask, _body("r-userinfo", legacy), "destination_url_invalid")
        assert e.extra.get("reason") == "userinfo"
        assert "key_env" not in e.message


# ===========================================================================
# 4. Every reader of `destination_of` renders an instance row
# ===========================================================================


class TestReadersRenderAnInstanceRow:
    def test_the_workspace_projection_says_where_and_never_names_the_entry(self, ask):
        from client_portal.asks import service
        receipt = _raise(ask, _body("v-proj"))
        row = ask.db.get_operator_queue_item(receipt["id"])
        out = service._project(row, viewer_email=OWNER)
        assert out.credential_destination == {"agent_name": TARGET, "host": HOST}
        dumped = json.dumps(out.model_dump(), default=str)
        assert ENTRY not in dumped and ENTRY_ID not in dumped and SECRET not in dumped

    def test_the_agents_own_view_says_where(self, ask):
        receipt = _raise(ask, _body("v-agent"))
        row = ask.db.get_operator_queue_item(receipt["id"])
        assert ask.svc._destination_view(row) == {"agent_name": TARGET, "host": HOST}

    def test_the_wake_says_the_credential_went_elsewhere(self, ask):
        from services import operator_resume_service as ors
        receipt = _raise(ask, _body("v-wake"))
        row = ask.db.get_operator_queue_item(receipt["id"])
        row = dict(row, response="provided", status="answered")
        text = ors._framed_message(row, "provided", None)
        assert TARGET in text and HOST in text
        assert "not in your environment" in text
        assert SECRET not in text


# ===========================================================================
# Checkpoint 2 — delivery: the key is resolved at the resolve point, by the
# entry's id, every time; one audit row per delivery.
# ===========================================================================

import functools  # noqa: E402
import logging  # noqa: E402
import socket  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import fakeredis  # noqa: E402
import httpx  # noqa: E402

ADDRESS = "93.184.216.34"
ROTATED = "trinity_mcp_ROTATED_KEY_cm90YXRlZC1lbnRyeS1rZXk"
ACTION = "credential_instance_delivery"


def _resolver(host, port, *a, **k):
    if host in (HOST, "other.example.com"):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ADDRESS, port))]
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _streaming(resp):
    if not resp.is_stream_consumed:
        return resp
    return httpx.Response(resp.status_code, headers=resp.headers, stream=httpx.ByteStream(resp.content))


class _Instance:
    """The remote instance's backend over the pinned transport: a real
    `httpx.AsyncClient` over a `MockTransport`. Records what it was sent."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.error = None

    def client(self, *a, **k):
        inst = self

        async def _handler(request):
            inst.calls.append({"url": str(request.url),
                               "json": json.loads(request.content or b"null"),
                               "headers": request.headers})
            if inst.error:
                raise inst.error
            if inst.status != 200:
                return _streaming(httpx.Response(inst.status, json={"detail": "nope"}))
            return _streaming(httpx.Response(200, json={"status": "success"}))

        return httpx.AsyncClient(transport=httpx.MockTransport(_handler), follow_redirects=False)


class _FakeEnc:
    def encrypt(self, d):
        return "ENV:" + json.dumps(d)

    def decrypt(self, s):
        return json.loads(s[len("ENV:"):])


@pytest.fixture
def world(real_db, monkeypatch):
    """The real credential sink over the real SQLite. The asking agent's own
    server is a trap: an `instance` delivery must never read or write it."""
    import services.ask_service as svc
    import services.credential_sink as sink
    import services.runtime_secret_scrub as scrub
    from services import a2a_client
    from utils.url_validation import validate_a2a_endpoint_url

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(scrub, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(scrub, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(scrub, "_redis_down_until", 0.0, raising=False)
    monkeypatch.setattr(sink, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(a2a_client, "validate_a2a_endpoint_url",
                        functools.partial(validate_a2a_endpoint_url, resolver=_resolver))

    asker_calls = []

    def _asker_client(agent_name):
        asker_calls.append(agent_name)
        raise AssertionError("an instance delivery must not reach the asking agent's server")

    monkeypatch.setattr(sink, "_agent_client", _asker_client)

    async def _no_env_read(*a, **k):
        asker_calls.append("read_env")
        raise AssertionError("an instance delivery must not read the asking agent's .env")

    monkeypatch.setattr(sink, "_read_env", _no_env_read)
    instance = _Instance()
    monkeypatch.setattr(sink, "_destination_client", instance.client)
    monkeypatch.setattr(sink, "get_agent_container", lambda name: MagicMock())
    monkeypatch.setattr(sink, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status="running"))

    audit = []
    audit_fails = {"on": False}

    class _Audit:
        async def log(self, **kw):
            if audit_fails["on"]:
                raise RuntimeError("audit store down")
            audit.append(kw)
            return "evt"

    monkeypatch.setattr(sink, "platform_audit_service", _Audit())
    monkeypatch.setattr(sink.credential_requirements_service, "invalidate_report_cache",
                        lambda name: None)
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda a: False, raising=False)
    return SimpleNamespace(db=real_db, sink=sink, fake=fake, instance=instance, audit=audit,
                           audit_fails=audit_fails, asker_calls=asker_calls)


def _refused(uid, status, code):
    from client_portal.asks import service
    with pytest.raises(service.AskError) as info:
        _provide(uid)
    assert (info.value.status_code, info.value.code) == (status, code), (
        info.value.status_code, info.value.code, info.value.detail)
    return info.value


def _staged(world):
    return world.fake.hlen("secret_scrub:staged") or 0


def _rows(world, action=ACTION):
    return [a for a in world.audit if a.get("event_action") == action]


# -- 3. raise / delivery parity ---------------------------------------------


_ENTRY_STATES = {
    "absent": None,
    "no_credential": {"credential": None},
    "empty_credential": {"credential": ""},
    "aauth": {"credential": None, "auth_scheme": "aauth"},
    "payment_token": {"credential_kind": "payment_token"},
    "ok": {},
}


class TestRaiseDeliveryParity:
    """One table, both readers: the raise-time twin (`instance_entry` +
    `entry_problem`) and the delivery resolve point (`_resolve_instance_key`)
    must answer every entry state with the same code and reason."""

    @pytest.mark.parametrize("state", sorted(_ENTRY_STATES))
    def test_raise_and_delivery_agree(self, registry, state):
        from services import credential_destination as cd
        from services import credential_sink as sink
        over = _ENTRY_STATES[state]
        registry.records.clear()
        if over is not None:
            registry.records.append(_entry(**over))

        raised = cd.entry_problem(cd.instance_entry(ASKER, ENTRY_ID))
        try:
            key, ep_id, ep_name = sink._resolve_instance_key(ASKER, dict(STORED))
            delivered = None
        except sink.CredentialSinkError as exc:
            delivered = exc

        if raised is None:
            assert delivered is None, (delivered.code, delivered.data)
            assert (key, ep_id, ep_name) == (SECRET, ENTRY_ID, ENTRY)
        else:
            assert delivered is not None, f"raise refused {raised.code}, delivery did not"
            assert (delivered.status_code, delivered.code, delivered.data.get("reason")) \
                == (422, raised.code, raised.reason)
            assert SECRET not in delivered.message

    def test_delivery_resolves_by_the_stored_id_not_the_name(self, registry):
        """A remove-and-re-add under the same name gets a new id: never followed."""
        from services import credential_sink as sink
        registry.records[0] = _entry(id="ep-readded")
        with pytest.raises(sink.CredentialSinkError) as info:
            sink._resolve_instance_key(ASKER, dict(STORED))
        assert (info.value.code, info.value.data.get("reason")) == ("destination_not_approved",
                                                                    "instance_unknown")

    def test_the_resolve_point_asks_for_the_asking_agent(self, registry):
        from services import a2a_outbound
        from services import credential_sink as sink
        seen = []

        class _Scoped:
            def resolve_endpoint(self, agent_name, ref):
                seen.append((agent_name, ref))
                return None

            def list_endpoints(self, agent_name):
                return []

        a2a_outbound.register_provider(_Scoped())
        with pytest.raises(sink.CredentialSinkError) as info:
            sink._resolve_instance_key(ASKER, dict(STORED))
        assert seen == [(ASKER, ENTRY_ID)]
        assert info.value.data.get("reason") == "instance_unknown"

    def test_a_provider_error_is_the_unknown_refusal_without_its_text(self, registry):
        from services import a2a_outbound
        from services import credential_sink as sink

        class _Broken:
            def resolve_endpoint(self, agent_name, ref):
                raise RuntimeError(f"backend down {SECRET}")

            def list_endpoints(self, agent_name):
                return []

        a2a_outbound.register_provider(_Broken())
        with pytest.raises(sink.CredentialSinkError) as info:
            sink._resolve_instance_key(ASKER, dict(STORED))
        assert info.value.data.get("reason") == "instance_unknown"
        assert SECRET not in info.value.message and "backend down" not in info.value.message

    def test_a_moved_entry_is_refused_before_its_credential_is_looked_at(self, registry):
        from services import credential_sink as sink
        registry.records[0] = _entry(url="https://other.example.com/a2a", credential=None)
        with pytest.raises(sink.CredentialSinkError) as info:
            sink._resolve_instance_key(ASKER, dict(STORED))
        assert (info.value.code, info.value.data.get("reason")) == ("destination_not_approved",
                                                                    "instance_moved")

    def test_a_header_unsafe_entry_credential_is_refused_at_delivery_only(self, registry):
        from services import credential_sink as sink
        bad = "trinity_mcp_bad kéy"
        registry.records[0] = _entry(credential=bad)
        with pytest.raises(sink.CredentialSinkError) as info:
            sink._resolve_instance_key(ASKER, dict(STORED))
        assert (info.value.status_code, info.value.code) == (422, "destination_key_invalid")
        assert bad not in info.value.message


# -- 4. delivery --------------------------------------------------------------


class TestInstanceDelivery:
    def test_the_value_lands_under_the_entry_key_and_the_asking_agent_is_never_touched(
            self, world, monkeypatch, caplog):
        from services import a2a_client
        caplog.set_level(logging.DEBUG)
        seen = {}
        real = a2a_client._read_capped

        def _spy(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)

        monkeypatch.setattr(a2a_client, "_read_capped", _spy)
        uid = _pending(world.db, "d-ok", destination=dict(STORED))
        out = _provide(uid)

        assert len(world.instance.calls) == 1
        call = world.instance.calls[0]
        assert call["url"] == f"https://{ADDRESS}/api/agents/{TARGET}/credentials/inject"
        assert call["headers"]["Host"] == HOST
        assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
        assert call["json"] == {"mode": "merge", "env": {NAME: VALUE}}
        assert seen.get("secret") == SECRET
        # the asking agent's .env was never read nor written
        assert world.asker_calls == []
        assert _staged(world) == 0
        row = world.db.get_operator_queue_item(uid)
        assert row["response"] == "provided" and out.answer == "provided"
        everything = caplog.text + json.dumps(world.audit, default=str) \
            + json.dumps(row, default=str) + json.dumps(out.model_dump(), default=str)
        assert SECRET not in everything and VALUE not in everything

    def test_a_key_rotated_on_the_entry_after_the_ask_is_the_one_used(self, world, registry):
        uid = _pending(world.db, "d-rot", destination=dict(STORED))
        registry.records[0] = _entry(credential=ROTATED)
        _provide(uid)
        assert world.instance.calls[0]["headers"]["Authorization"] == f"Bearer {ROTATED}"

    def test_an_entry_repointed_at_another_host_is_refused_and_nothing_is_sent(
            self, world, registry):
        uid = _pending(world.db, "d-moved", destination=dict(STORED))
        registry.records[0] = _entry(url="https://other.example.com/a2a")
        e = _refused(uid, 422, "destination_not_approved")
        assert e.data.get("reason") == "instance_moved"
        assert "again" in e.detail and "nothing was stored" in e.detail
        assert world.instance.calls == [] and _staged(world) == 0
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    @pytest.mark.parametrize("change,code,reason", [
        (lambda r: r.clear(), "destination_not_approved", "instance_unknown"),
        (lambda r: r.__setitem__(0, _entry(credential=None)),
         "destination_instance_no_credential", "no_credential"),
        (lambda r: r.__setitem__(0, _entry(credential_kind="payment_token")),
         "destination_instance_no_credential", "payment_token"),
    ])
    def test_an_entry_changed_after_the_ask_is_a_named_refusal_with_nothing_sent(
            self, world, registry, change, code, reason):
        uid = _pending(world.db, f"d-chg-{reason}", destination=dict(STORED))
        change(registry.records)
        e = _refused(uid, 422, code)
        assert e.data.get("reason") == reason
        assert "nothing was stored" in e.detail and "admin" in e.detail
        assert world.instance.calls == [] and _staged(world) == 0

    def test_a_refused_key_names_the_registered_key_not_the_asking_agent(self, world):
        world.instance.status = 401
        uid = _pending(world.db, "d-401", destination=dict(STORED))
        e = _refused(uid, 409, "destination_key_refused")
        assert "registered for that Trinity" in e.detail
        assert "agent that asked holds" not in e.detail

    def test_the_sentinel_key_reaches_no_body_log_or_audit_row_on_any_refusal(
            self, world, registry, caplog):
        caplog.set_level(logging.DEBUG)
        texts = []
        for i, (over, status, code) in enumerate([
            ({"credential_kind": "payment_token"}, 422, "destination_instance_no_credential"),
            ({"url": "https://other.example.com/a2a"}, 422, "destination_not_approved"),
            ({"credential": SECRET + " x"}, 422, "destination_key_invalid"),
        ]):
            registry.records[0] = _entry(**over)
            e = _refused(_pending(world.db, f"d-sent-{i}", destination=dict(STORED)), status, code)
            texts.append(f"{e.detail} {json.dumps(e.data, default=str)}")
        registry.records[0] = _entry()
        world.instance.status = 403
        e = _refused(_pending(world.db, "d-sent-403", destination=dict(STORED)), 409,
                     "destination_key_refused")
        texts.append(f"{e.detail} {json.dumps(e.data, default=str)}")
        everything = " ".join(texts) + caplog.text + json.dumps(world.audit, default=str)
        assert SECRET not in everything and VALUE not in everything


# -- 5. one audit row per delivery --------------------------------------------


class TestOneAuditRowPerDelivery:
    def test_a_delivery_writes_exactly_one_row_with_names_only(self, world):
        uid = _pending(world.db, "a-ok", destination=dict(STORED))
        _provide(uid)
        rows = _rows(world)
        assert len(rows) == 1
        row = rows[0]
        assert row["actor_email"] == OWNER
        assert (row["target_type"], row["target_id"]) == ("agent", ASKER)
        assert row["details"] == {
            "name": NAME, "ask_id": uid, "instance": ENTRY, "endpoint_id": ENTRY_ID,
            "destination": {"host": HOST, "agent_name": TARGET},
            "outcome": "delivered", "may_have_written": True}
        # the ent#1109 pair is for key_env destinations only
        assert _rows(world, "credential_delivery_attempt") == []
        assert _rows(world, "credential_delivered") == []

    @pytest.mark.parametrize("arm,status,code,may_have_written", [
        ("switch_off", 422, "destination_outbound_disabled", False),
        ("removed", 422, "destination_not_approved", False),
        ("moved", 422, "destination_not_approved", False),
        ("no_credential", 422, "destination_instance_no_credential", False),
        ("key_invalid", 422, "destination_key_invalid", False),
        ("dns", 422, "destination_url_invalid", False),
        ("refused", 409, "destination_key_refused", False),
        ("timeout", 503, "destination_unreachable", True),
        ("5xx", 503, "destination_unconfirmed", True),
    ])
    def test_each_refusal_writes_exactly_one_row_naming_its_outcome(
            self, world, registry, monkeypatch, arm, status, code, may_have_written):
        uid = _pending(world.db, f"a-{arm}", destination=dict(STORED))
        if arm == "switch_off":
            registry.switch["on"] = False
        elif arm == "removed":
            registry.records.clear()
        elif arm == "moved":
            registry.records[0] = _entry(url="https://other.example.com/a2a")
        elif arm == "no_credential":
            registry.records[0] = _entry(credential=None)
        elif arm == "key_invalid":
            registry.records[0] = _entry(credential="bad key")
        elif arm == "dns":
            from services import a2a_client
            from utils.url_validation import validate_a2a_endpoint_url
            monkeypatch.setattr(a2a_client, "validate_a2a_endpoint_url", functools.partial(
                validate_a2a_endpoint_url,
                resolver=lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                 ("10.0.0.9", p))]))
        elif arm == "refused":
            world.instance.status = 403
        elif arm == "timeout":
            world.instance.error = httpx.ReadTimeout("slow")
        elif arm == "5xx":
            world.instance.status = 503
        _refused(uid, status, code)
        rows = _rows(world)
        assert len(rows) == 1, [a.get("event_action") for a in world.audit]
        details = rows[0]["details"]
        assert details["outcome"] == code
        assert details["may_have_written"] is may_have_written
        # the stored name and id when the entry did not resolve
        assert (details["instance"], details["endpoint_id"]) == (ENTRY, ENTRY_ID)
        assert details["destination"] == {"host": HOST, "agent_name": TARGET}
        assert set(details) == {"name", "ask_id", "instance", "endpoint_id", "destination",
                                "outcome", "may_have_written"}
        assert _rows(world, "credential_delivery_attempt") == []
        assert SECRET not in json.dumps(world.audit, default=str)

    def test_a_failed_audit_write_does_not_undo_the_delivery(self, world):
        world.audit_fails["on"] = True
        uid = _pending(world.db, "a-fail", destination=dict(STORED))
        out = _provide(uid)
        assert out.answer == "provided" and len(world.instance.calls) == 1

    def test_a_trace_is_logged_before_the_request_leaves(self, world, caplog):
        caplog.set_level(logging.INFO)
        world.instance.error = httpx.ReadTimeout("slow")
        uid = _pending(world.db, "a-trace", destination=dict(STORED))
        _refused(uid, 503, "destination_unreachable")
        lines = [r.getMessage() for r in caplog.records if r.name == "services.credential_sink"]
        assert any(uid in line and NAME in line and HOST in line for line in lines), lines

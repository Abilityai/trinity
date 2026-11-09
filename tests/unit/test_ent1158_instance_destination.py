"""trinity-enterprise#1158 — a credential ask's destination may NAME a
registered outbound instance instead of carrying the asking agent's own key.

ent#1109's destination is `{instance_url, agent_name, key_env}`: the key to
the other Trinity is read by name from the asking agent's own `.env`, which an
agent has no sanctioned way to write. The second shape,

    {"instance": "<registered outbound endpoint name or id>", "agent_name": "…"}

names an entry an admin registered under Settings → Integrations → Outbound
A2A endpoints; the platform delivers under the key stored for that entry, so
the asking agent never holds a copy.

Pinned here (checkpoint 1 of the plan — the raise half and the answer path):

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

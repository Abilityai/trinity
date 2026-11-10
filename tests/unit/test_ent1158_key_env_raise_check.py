"""trinity-enterprise#1158 §2 — a `key_env` destination is checked when the ask
is RAISED, not only when a person has already typed the value.

A credential ask whose destination names `key_env` (the variable in the asking
agent's own `.env` that holds its key to the other Trinity) used to fail only
at delivery, `destination_key_missing`, after the person had typed the secret.
The raise route now reads the asking agent's `.env` (off the event loop, with
a 5 s budget) and tells the agent first:

* ONE classifier, `credential_sink.classify_key`, decides ok / missing /
  invalid for both the raise check and delivery, so the two cannot drift
  (whitespace-only or header-unsafe is `invalid` on both sides — T7);
* `key_env_state` returns only the label, never the key, and None when it
  cannot tell (agent not running, unreadable file, agent server error,
  timeout) — the raise then proceeds and delivery stays the authoritative
  check (T2 = fail open);
* the route passes the label to `raise_ask(destination_key_state=)` only for a
  `key_env` destination; an `instance` destination is never read;
* inside `raise_ask` the label is applied LAST — after every earlier refusal
  and after the replay — so a key refusal never hides a refusal that would
  also block the ask, and a retry still gets its first receipt.

Harness: the real ask sink over the real per-process SQLite, the real
credential sink with the asking agent's HTTP server and Docker stood in for,
and the real route coroutine with `raise_ask` recorded.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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

OWNER = "owner-1158k@example.com"
ASKER = "asking-agent-1158k"
TARGET = "target-agent"
HOST = "remote.example.com"
INSTANCE = f"https://{HOST}"
NAME = "TELEGRAM_BOT_TOKEN"
KEY_ENV = "REMOTE_TRINITY_KEY"
KEY = "trinity_mcp_k3y_c2VudGluZWwta2V5LTExNTg"
DEST = {"instance_url": INSTANCE, "agent_name": TARGET, "key_env": KEY_ENV}


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _AgentServer:
    """The asking agent's internal server as the sink reaches it: an in-memory
    `.env` behind the read endpoint. Records the client's timeout and calls."""

    def __init__(self, env_text=""):
        self.env_text = env_text
        self.calls = []
        self.timeouts = []
        self.status = 200
        self.error = None
        self.hang = False
        self.b64 = False

    def client(self, agent_name, **kw):
        server = self
        server.timeouts.append(kw.get("timeout"))

        class _Ctx:
            async def __aenter__(self):
                return server

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url))
        if self.hang:
            await asyncio.sleep(30)
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return _Resp(self.status, {"detail": "nope"})
        if self.b64:
            return _Resp(200, {"files": {}, "files_b64": {".env": "//4="}, "missing": []})
        if self.env_text is None:
            return _Resp(200, {"files": {}, "files_b64": {}, "missing": [".env"]})
        return _Resp(200, {"files": {".env": self.env_text}, "files_b64": {}, "missing": []})

    async def post(self, url, json=None, **kw):  # pragma: no cover - never reached here
        self.calls.append(("POST", url))
        raise AssertionError("the raise check never writes the agent's file")


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def approved(monkeypatch):
    """The admin's outbound registry holds the remote instance; the switch is ON."""
    from services import a2a_outbound, a2a_outbound_service
    records = [{"id": "ep-remote", "name": "remote", "url": INSTANCE}]
    a2a_outbound.clear_provider()
    monkeypatch.setattr(a2a_outbound, "_load_endpoint_records", lambda: [dict(r) for r in records])
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: True)
    yield SimpleNamespace(records=records)
    a2a_outbound.clear_provider()


@pytest.fixture
def sink(monkeypatch):
    """The real credential sink; the asking agent's server and Docker stood in."""
    import services.credential_sink as s
    server = _AgentServer(f'OTHER="keep me"\n{KEY_ENV}="{KEY}"\n')
    monkeypatch.setattr(s, "_agent_client", server.client)
    docker = {"status": "running", "threads": []}

    def _container(name):
        docker["threads"].append(threading.get_ident())
        return MagicMock()

    monkeypatch.setattr(s, "get_agent_container", _container)
    monkeypatch.setattr(s, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status=docker["status"]))
    return SimpleNamespace(mod=s, server=server, docker=docker)


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real ask sink; the world around it stubbed as the ent#1109 suite does."""
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit, sent = [], []
    rate = {"ok": True}

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "_owner_email", lambda agent: OWNER)
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", True))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(rate["ok"], 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, db=real_db, rate=rate)
    assignment_provider.clear_provider()


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "credential", "title": f"Needs {NAME}",
         "question": "For the digest agent to post to Telegram.",
         "context": {"credential_name": NAME, "credential_destination": dict(DEST)}}
    b.update(over)
    return b


def _raise(ask, body, **kw):
    return ask.svc.raise_ask(ASKER, body, raised_by="agent", channel="mcp", **kw)


def _refused(ask, body, status, code, **kw):
    with pytest.raises(ask.svc.AskRejected) as info:
        _raise(ask, body, **kw)
    assert (info.value.status_code, info.value.code) == (status, code), (
        info.value.status_code, info.value.code, info.value.message)
    return info.value


def _rows(ask, rid):
    return ask.db.get_operator_queue_item_for_agent_by_request_id(ASKER, rid)


# ---------------------------------------------------------------------------
# 1. One classifier for raise and delivery
# ---------------------------------------------------------------------------

#: (the asking agent's .env, the label both sides must agree on)
_CASES = [
    (f'{KEY_ENV}="{KEY}"\n', "ok"),
    (f'{KEY_ENV}=old-key-value\nOTHER=x\n{KEY_ENV}="{KEY}"\n', "ok"),      # last wins
    (f'{KEY_ENV}="{KEY}"\n{KEY_ENV}=\n', "missing"),                    # last is empty
    ('OTHER="keep me"\n', "missing"),
    (None, "missing"),                                                  # no file at all
    ("", "missing"),
    (f'{KEY_ENV}="   "\n', "invalid"),                                   # whitespace-only (T7)
    (f'{KEY_ENV}="has space-in-it"\n', "invalid"),
    (f'{KEY_ENV}="trinity_mcp_kéy"\n', "invalid"),                       # non-ASCII
]


class TestClassifier:

    @pytest.mark.parametrize("env_text,label", _CASES)
    def test_the_raise_check_and_delivery_agree(self, sink, env_text, label):
        s = sink.mod
        sink.server.env_text = env_text
        assert asyncio.run(s.key_env_state(ASKER, KEY_ENV)) == label
        if label == "ok":
            assert asyncio.run(s._read_key_by_name(ASKER, KEY_ENV)) == KEY
        else:
            with pytest.raises(s.CredentialSinkError) as info:
                asyncio.run(s._read_key_by_name(ASKER, KEY_ENV))
            assert info.value.code == f"destination_key_{label}"

    @pytest.mark.parametrize("env_text,label", _CASES)
    def test_classify_key_is_the_shared_leaf(self, sink, env_text, label):
        found_label, key = sink.mod.classify_key(env_text or "", KEY_ENV)
        assert found_label == label
        assert key == (KEY if label == "ok" else None)


# ---------------------------------------------------------------------------
# 2. key_env_state: a label or None, off the loop, bounded
# ---------------------------------------------------------------------------

class TestKeyEnvState:

    def test_it_returns_a_label_and_never_the_key(self, sink):
        out = asyncio.run(sink.mod.key_env_state(ASKER, KEY_ENV))
        assert out == "ok" and KEY not in repr(out)
        assert sink.server.calls == [("GET", f"http://agent-{ASKER}:8000/api/credentials/read")]

    def test_the_agent_client_gets_the_5s_budget(self, sink):
        assert sink.mod.KEY_ENV_STATE_TIMEOUT_SECONDS == 5
        asyncio.run(sink.mod.key_env_state(ASKER, KEY_ENV))
        assert sink.server.timeouts == [5]

    def test_the_docker_lookup_runs_off_the_event_loop(self, sink):
        loop_thread = {}

        async def _go():
            loop_thread["id"] = threading.get_ident()
            return await sink.mod.key_env_state(ASKER, KEY_ENV)

        assert asyncio.run(_go()) == "ok"
        assert sink.docker["threads"] and loop_thread["id"] not in sink.docker["threads"]

    @pytest.mark.parametrize("setup", [
        lambda w: w.server.__setattr__("status", 500),
        lambda w: w.server.__setattr__("error", httpx.ConnectError("refused")),
        lambda w: w.server.__setattr__("b64", True),            # not UTF-8 → unreadable
        lambda w: w.docker.__setitem__("status", "exited"),
    ], ids=["http_500", "connect_error", "not_utf8", "not_running"])
    def test_it_cannot_tell_and_says_none(self, sink, setup):
        setup(sink)
        assert asyncio.run(sink.mod.key_env_state(ASKER, KEY_ENV)) is None

    def test_a_hanging_agent_is_none_within_the_budget(self, sink, monkeypatch):
        monkeypatch.setattr(sink.mod, "KEY_ENV_STATE_TIMEOUT_SECONDS", 0.2)
        sink.server.hang = True

        async def _go():
            return await asyncio.wait_for(sink.mod.key_env_state(ASKER, KEY_ENV), 3)

        assert asyncio.run(_go()) is None


# ---------------------------------------------------------------------------
# 3. raise_ask applies the label last
# ---------------------------------------------------------------------------

class TestRaiseAsk:

    def test_missing_is_a_named_422_and_nothing_is_created(self, ask):
        e = _refused(ask, _body("kr-missing"), 422, "destination_key_env_missing",
                     destination_key_state="missing")
        assert "destination.instance" in e.message
        assert "destination.key_env" in e.message
        assert KEY_ENV not in e.message and KEY not in e.message
        assert _rows(ask, "kr-missing") is None

    def test_invalid_is_a_named_422_and_nothing_is_created(self, ask):
        e = _refused(ask, _body("kr-invalid"), 422, "destination_key_invalid",
                     destination_key_state="invalid")
        assert KEY_ENV not in e.message and KEY not in e.message
        assert _rows(ask, "kr-invalid") is None

    @pytest.mark.parametrize("state", ["ok", None])
    def test_ok_or_cannot_tell_creates_the_ask(self, ask, state):
        rid = f"kr-pass-{state}"
        receipt = _raise(ask, _body(rid), destination_key_state=state)
        assert receipt["status"] == "created"
        assert _rows(ask, rid) is not None

    def test_callers_that_pass_nothing_are_unchanged(self, ask):
        """Gate and file callers never pass the kwarg: the default is None."""
        assert _raise(ask, _body("kr-default"))["status"] == "created"

    def test_a_replay_gets_its_first_receipt_whatever_the_key_state(self, ask):
        first = _raise(ask, _body("kr-replay"), destination_key_state="ok")
        again = _raise(ask, _body("kr-replay"), destination_key_state="missing")
        assert again["status"] == "replayed"
        assert again["id"] == first["id"]

    def test_an_unapproved_destination_wins(self, ask, approved):
        approved.records.clear()
        _refused(ask, _body("kr-unapproved"), 422, "destination_not_approved",
                 destination_key_state="missing")

    def test_the_title_cap_wins(self, ask):
        _refused(ask, _body("kr-title", title="x" * 5000), 422, "title_too_long",
                 destination_key_state="missing")

    def test_the_rate_cap_wins(self, ask):
        ask.rate["ok"] = False
        _refused(ask, _body("kr-rate"), 429, "rate_limited", destination_key_state="missing")

    def test_the_deadline_floor_wins(self, ask):
        _refused(ask, _body("kr-deadline", expires_at="2020-01-01T00:00:00Z"), 422,
                 "invalid_expires_at", destination_key_state="missing")

    def test_no_person_wins(self, ask, monkeypatch):
        monkeypatch.setattr(ask.svc, "_owner_email", lambda agent: None)
        _refused(ask, _body("kr-noperson"), 422, "no_person",
                 destination_key_state="missing")

    def test_an_instance_destination_ignores_a_stray_label(self, ask, monkeypatch):
        """Only a key_env destination is subject to the check; an instance
        destination's key is the registry entry's, resolved at delivery."""
        from services import credential_destination
        monkeypatch.setattr(credential_destination, "validate",
                            lambda d, agent_name: {"instance": "instance-remote",
                                                   "agent_name": TARGET})
        body = _body("kr-instance")
        body["context"]["credential_destination"] = {"instance": "instance-remote",
                                                      "agent_name": TARGET}
        assert _raise(ask, body, destination_key_state="missing")["status"] == "created"


# ---------------------------------------------------------------------------
# 4. The route reads the .env and passes the label through
# ---------------------------------------------------------------------------

class TestRoute:

    @pytest.fixture
    def route(self, monkeypatch):
        from fastapi import Response
        from models import OperatorAskCreate, User
        from routers import operator_queue as r
        from services import ask_service, credential_sink

        seen, reads = {}, []

        def _raise_ask(agent, ask, **kw):
            seen.update(kw, agent=agent, ask=ask)
            return {"status": "created"}

        async def _state(agent, key_env):
            reads.append((agent, key_env))
            return "missing"

        monkeypatch.setattr(ask_service, "raise_ask", _raise_ask)
        monkeypatch.setattr(credential_sink, "key_env_state", _state)
        user = User(id=7, username="op", email="op@example.com", role="admin",
                    mcp_scope="agent", agent_name=ASKER)

        def call(body):
            return asyncio.run(r.raise_my_ask(
                body=OperatorAskCreate(**body), response=Response(), name=ASKER,
                current_user=user, x_trinity_execution_id=None))

        return SimpleNamespace(call=call, seen=seen, reads=reads)

    def test_a_key_env_destination_is_read_and_passed_through(self, route):
        route.call(_body("rt-key"))
        assert route.reads == [(ASKER, KEY_ENV)]
        assert route.seen["destination_key_state"] == "missing"

    def test_an_instance_destination_is_never_read(self, route):
        body = _body("rt-inst")
        body["context"]["credential_destination"] = {"instance": "instance-remote",
                                                      "agent_name": TARGET}
        route.call(body)
        assert route.reads == []
        assert route.seen.get("destination_key_state") is None

    @pytest.mark.parametrize("body", [
        _body("rt-q", type="question", context={"note": "x"}),
        _body("rt-nodest", context={"credential_name": NAME}),
        _body("rt-badname", context={"credential_name": NAME, "credential_destination":
                                     {**DEST, "key_env": "lower case bad"}}),
        _body("rt-notdict", context={"credential_name": NAME, "credential_destination": "x"}),
    ], ids=["not_credential", "no_destination", "shape_invalid_key_env", "not_an_object"])
    def test_nothing_else_is_read(self, route, body):
        route.call(body)
        assert route.reads == []
        assert route.seen.get("destination_key_state") is None

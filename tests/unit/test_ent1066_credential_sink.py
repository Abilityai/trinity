"""trinity-enterprise#1066 — the secret sink: the Workspace secret field's one
write path, `client_portal.asks.service.provide_credential` →
`services/credential_sink.set_env_value` → the ask sink by name.

What a person types into the field goes to exactly one place — one line of the
agent's `.env`, written through the agent's own read + inject endpoints — and
to nothing a reader sees: not a log line, not the audit row, not the ask row,
not a refusal's message, not the agent's wake prompt. Those are the claims, so
every test here holds the value and greps every sink for it.

Order inside the sink, each step a named refusal, each refusal leaving the
agent untouched:

1. the name (the ask policy, re-read at answer time: a file-channel row
   validates nothing) and the value (`invalid_value`: empty, below the scrub's
   floor, over 32 KiB, or carrying a line break / NUL);
2. STAGE into the identity scrub FIRST and fail CLOSED (`scrub_unavailable`,
   503) — no delivery without the scrub armed;
3. the agent must be running (`agent_not_running`, 409, naming the state);
4. one writer per agent at a time (`env_busy`, 409);
5. read `.env`, set ONE line (`agent_env_patch.set_keys`: in place, duplicates
   collapsed, every other byte kept), refuse a file over 256 KiB
   (`env_too_large`), inject it back (`agent_unreachable`, 503, on either leg);
6. audit `credentials` / `chat_provide` with the NAME and the ask id, drop the
   requirements report cache, and answer the ask `provided` through the sink.

A compare-and-set lost AFTER the write is 409 `already_resolved` with
`credential_written: true` — nothing is rolled back (re-asking is cheap,
un-writing is not).

Driven through the real service and the real sink over the real SQLite; the
agent's HTTP server, Docker, Redis (fakeredis) and the audit sink are stood in
for. The agent's own `.env` reader is loaded by file path from the image to
prove the round trip.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import httpx
import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_lua`
import _ent695_lua as lua  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "owner-1066-sink@example.com"
AGENT = "agent-1066-sink"
NAME = "VAULT_SERVICE_TOKEN"
VALUE = "vst_Qm9yaW5nLXRlc3Qtc2VjcmV0LXZhbHVl_with\"quote\\and\\backslash"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)


class _FakeEnc:
    def encrypt(self, d: dict) -> str:
        return "ENV:" + json.dumps(d)

    def decrypt(self, s) -> dict:
        if not isinstance(s, str) or not s.startswith("ENV:"):
            raise ValueError("bad envelope")
        return json.loads(s[len("ENV:"):])


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _AgentServer:
    """The agent's internal HTTP server as the sink reaches it: an in-memory
    `.env`, a read endpoint and an inject endpoint. Records every call."""

    def __init__(self, env_text=""):
        self.env_text = env_text
        self.calls = []
        self.read_status = 200
        self.inject_status = 200
        self.read_error = None
        self.legacy_read = False

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
        if self.read_error:
            raise self.read_error
        if self.read_status != 200:
            return _Resp(self.read_status, {"detail": "nope"})
        if self.env_text is None:
            # the agent positively confirms there is no file (ent#1066)
            body = {"files": {}, "files_b64": {}, "missing": [".env"]}
            if self.legacy_read:
                body.pop("missing")   # an image older than the `missing` list
            return _Resp(200, body)
        return _Resp(200, {"files": {".env": self.env_text}, "files_b64": {}, "missing": []})

    async def post(self, url, json=None, **kw):
        self.calls.append(("POST", url))
        if self.inject_status != 200:
            return _Resp(self.inject_status, {"detail": "nope"})
        self.env_text = json["files"][".env"]
        return _Resp(200, {"status": "success", "files_written": [".env"]})


@pytest.fixture
def world(real_db, monkeypatch):
    """The real sink and the real ask sink over the real SQLite; Redis, the
    agent, Docker and the audit sink stood in for; every side channel
    recorded so a test can grep it for the value."""
    import services.ask_service as svc
    import services.credential_sink as sink
    import services.runtime_secret_scrub as scrub

    fake = lua.with_lua(fakeredis.FakeStrictRedis(decode_responses=True))
    monkeypatch.setattr(scrub, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(scrub, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(scrub, "_redis_down_until", 0.0, raising=False)
    monkeypatch.setattr(sink, "get_breaker_redis", lambda: fake)

    agent = _AgentServer('OTHER="keep me"\n# a comment\nFOO=bar\n')
    monkeypatch.setattr(sink, "_agent_client", agent.client)
    status = {"container": MagicMock(), "status": "running"}
    monkeypatch.setattr(sink, "get_agent_container", lambda name: status["container"])
    monkeypatch.setattr(sink, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status=status["status"]))

    audit, events, invalidated = [], [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    monkeypatch.setattr(sink, "platform_audit_service", _Audit())
    monkeypatch.setattr(sink.credential_requirements_service, "invalidate_report_cache",
                        lambda name: invalidated.append(name))
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", None)
    monkeypatch.setattr(svc, "_observers", [events.append])
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda a: False, raising=False)

    return SimpleNamespace(db=real_db, sink=sink, scrub=scrub, fake=fake, agent=agent,
                           status=status, audit=audit, events=events, invalidated=invalidated)


@pytest.fixture
def agent_reader():
    path = _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
    spec = importlib.util.spec_from_file_location("_execution_env_ent1066_sink", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pending(real_db, rid, name=NAME, **over):
    item = {"id": rid, "type": "credential", "status": "pending", "priority": "medium",
            "title": f"Needs {name}", "question": "to open the vault", "options": None,
            "context": {"credential_name": name}, "created_at": "2026-11-01T10:00:00Z",
            "addressed_to_email": OWNER}
    item.update(over)
    return real_db.create_operator_queue_item(AGENT, item)


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
# 1. The write
# ===========================================================================


class TestTheWrite:
    def test_one_line_is_added_and_every_other_byte_kept(self, world, agent_reader, tmp_path):
        uid = _pending(world.db, "w-1")
        out = _provide(uid)
        assert world.agent.env_text == (
            'OTHER="keep me"\n# a comment\nFOO=bar\n' + f'{NAME}="{VALUE.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"\n'
        )
        env = tmp_path / ".env"
        env.write_text(world.agent.env_text)
        parsed = agent_reader.parse_env_file(env)
        assert parsed[NAME] == VALUE            # the agent reads back exactly what was typed
        assert parsed["OTHER"] == "keep me" and parsed["FOO"] == "bar"
        assert [c[0] for c in world.agent.calls] == ["GET", "POST"]
        assert out.status == "answered" and out.kind == "credential" and out.credential_name == NAME

    def test_an_existing_line_is_replaced_in_place_and_duplicates_collapse(self, world, agent_reader, tmp_path):
        world.agent.env_text = f'{NAME}=old-one\nKEEP=1\n{NAME}="old-two"\n'
        uid = _pending(world.db, "w-2")
        _provide(uid)
        env = tmp_path / ".env"
        env.write_text(world.agent.env_text)
        assert world.agent.env_text.count(f"{NAME}=") == 1
        assert "old-one" not in world.agent.env_text and "old-two" not in world.agent.env_text
        assert world.agent.env_text.startswith(f'{NAME}="')
        assert agent_reader.parse_env_file(env) == {NAME: VALUE, "KEEP": "1"}

    def test_a_missing_env_file_is_created(self, world):
        world.agent.env_text = None
        uid = _pending(world.db, "w-3")
        _provide(uid)
        assert world.agent.env_text.startswith(f'{NAME}="')

    def test_an_omitted_env_the_agent_did_not_confirm_absent_is_never_treated_as_empty(self, world):
        """An agent image older than the read endpoint's `missing` list omits a
        file it could not read exactly as it omits one that is not there; the
        sink cannot tell them apart, so it writes nothing."""
        world.agent.env_text = None
        world.agent.legacy_read = True
        uid = _pending(world.db, "w-3-legacy")
        err = _refused(uid, 409, "env_unreadable")
        assert err.data == {"reason": "unconfirmed"}
        assert [c[0] for c in world.agent.calls] == ["GET"]
        assert world.agent.env_text is None
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_the_ask_ends_answered_by_name_and_the_agent_is_told_where_to_read(self, world):
        from services import operator_resume_service as ors
        uid = _pending(world.db, "w-4")
        out = _provide(uid)
        row = world.db.get_operator_queue_item(uid)
        assert (row["disposition"], row["disposed_by"], row["disposed_by_email"]) == (
            "answered", "person", OWNER)
        assert row["response"] == "provided" and row["response_text"] is None
        assert [e.disposition for e in world.events] == ["answered"]
        assert out.resume_requested is False and out.answer == "provided"
        wake = ors._framed_message(row, row["response"], row["response_text"], to_person=True)
        assert NAME in wake and VALUE not in wake
        assert world.invalidated == [AGENT]

    def test_the_audit_row_carries_the_name_and_the_ask_id_only(self, world):
        uid = _pending(world.db, "w-5")
        _provide(uid)
        rows = [a for a in world.audit if a.get("event_action") == "chat_provide"]
        assert len(rows) == 1
        a = rows[0]
        assert a["target_type"] == "agent" and a["target_id"] == AGENT
        assert a["actor_email"] == OWNER and a["actor_ip"] == "127.0.0.1"
        assert a["details"] == {"name": NAME, "ask_id": uid, "files": [".env"]}

    def test_the_lock_is_released_after_the_write(self, world):
        uid = _pending(world.db, "w-6")
        _provide(uid)
        assert world.fake.get(f"credential_sink:lock:{AGENT}") is None


# ===========================================================================
# 1b. The agent's own read + inject endpoints, over a real file
# ===========================================================================


@pytest.fixture
def real_agent(world, tmp_path, monkeypatch):
    """The sink talking to the agent image's REAL `/api/credentials/read` and
    `/api/credentials/inject` (the base-image router, mounted over a tmp home),
    so what reaches the file is decided by the code that ships, not a stand-in."""
    from fastapi import FastAPI
    from agent_server.routers import credentials as cred_router

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cred_router, "_HOME", home)
    monkeypatch.setattr(cred_router, "sync_process_env", lambda path: None)
    monkeypatch.setattr(cred_router, "refresh_credential_values", lambda: None)
    app = FastAPI()
    app.include_router(cred_router.router)

    def client(agent_name):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr(world.sink, "_agent_client", client)
    return SimpleNamespace(home=home, env=home / ".env")


class TestTheFileIsNeverAssumedEmpty:
    """The write is read-modify-write of the whole file. Only a file the agent
    positively reports absent may be treated as empty; a file it could not
    hand back as text is refused by name, and every byte of it is kept."""

    OTHERS = b'OTHER="keep me"\n# a comment\nFOO=bar\nLATIN1=caf\xe9\n'

    def test_a_non_utf8_env_with_other_keys_keeps_every_byte(self, world, real_agent):
        real_agent.env.write_bytes(self.OTHERS)
        uid = _pending(world.db, "ne-latin1")
        err = _refused(uid, 409, "env_unreadable")
        assert err.data == {"reason": "not_utf8"}
        assert real_agent.env.read_bytes() == self.OTHERS
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"
        assert not [a for a in world.audit if a.get("event_action") == "chat_provide"]

    def test_an_env_the_agent_could_not_read_keeps_every_byte(self, world, real_agent, monkeypatch):
        good = b'OTHER="keep me"\nFOO=bar\n'
        real_agent.env.write_bytes(good)
        real_agent.env.chmod(0o000)
        if os.geteuid() == 0:   # root reads through a 000 mode; fail the read the same way
            real_read = Path.read_bytes

            def denied(self):
                if self == real_agent.env:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_read(self)
            monkeypatch.setattr(Path, "read_bytes", denied)
        try:
            uid = _pending(world.db, "ne-unreadable")
            err = _refused(uid, 409, "env_unreadable")
            assert err.data == {"reason": "unconfirmed"}
        finally:
            real_agent.env.chmod(0o600)
        assert real_agent.env.read_bytes() == good
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_file_the_agent_reports_absent_is_created(self, world, real_agent, agent_reader):
        assert not real_agent.env.exists()
        uid = _pending(world.db, "ne-absent")
        _provide(uid)
        assert agent_reader.parse_env_file(real_agent.env) == {NAME: VALUE}

    def test_a_readable_file_is_patched_in_place_through_the_real_endpoints(self, world, real_agent,
                                                                            agent_reader):
        real_agent.env.write_bytes(b'OTHER="keep me"\nFOO=bar\n')
        uid = _pending(world.db, "ne-text")
        _provide(uid)
        assert real_agent.env.read_bytes().startswith(b'OTHER="keep me"\nFOO=bar\n')
        assert agent_reader.parse_env_file(real_agent.env) == {
            "OTHER": "keep me", "FOO": "bar", NAME: VALUE}


# ===========================================================================
# 2. The value is nowhere a reader looks
# ===========================================================================


class TestTheValueIsNowhere:
    def test_a_successful_write_leaks_the_value_to_no_side_channel(self, world, caplog,
                                                                   agent_reader, tmp_path):
        caplog.set_level(logging.DEBUG)
        uid = _pending(world.db, "n-1")
        out = _provide(uid)
        env = tmp_path / ".env"
        env.write_text(world.agent.env_text)
        assert agent_reader.parse_env_file(env)[NAME] == VALUE   # the one place it belongs
        assert VALUE not in caplog.text
        assert VALUE not in json.dumps(world.audit, default=str)
        assert VALUE not in json.dumps(world.db.get_operator_queue_item(uid), default=str)
        assert VALUE not in json.dumps(out.model_dump(), default=str)
        # the scrub has it staged — the guarantee for every persisted transcript
        assert VALUE in world.scrub.get_staged_values()

    @pytest.mark.parametrize("arm", ["stopped", "unreachable", "inject_refused", "busy", "too_large"])
    def test_every_refusal_after_staging_names_the_step_and_never_the_value(self, world, caplog, arm):
        caplog.set_level(logging.DEBUG)
        if arm == "stopped":
            world.status["status"] = "stopped"
        elif arm == "unreachable":
            world.agent.read_error = httpx.ConnectError("boom")
        elif arm == "inject_refused":
            world.agent.inject_status = 400
        elif arm == "busy":
            world.fake.set(f"credential_sink:lock:{AGENT}", "someone-else", ex=30)
        elif arm == "too_large":
            world.agent.env_text = 'X="' + "y" * (256 * 1024) + '"\n'
        from client_portal.asks import service
        uid = _pending(world.db, f"n-{arm}")
        with pytest.raises(service.AskError) as info:
            _provide(uid)
        text = f"{info.value.code} {info.value.detail} {json.dumps(info.value.data, default=str)}"
        assert VALUE not in text and VALUE not in caplog.text
        assert VALUE not in json.dumps(world.audit, default=str)
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"


# ===========================================================================
# 3. Refusals, in the sink's order
# ===========================================================================


class TestInvalidValue:
    @pytest.mark.parametrize("bad", [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("short", id="below-the-scrub-floor"),
        pytest.param("line-one-xxxx\nline-two", id="newline"),
        pytest.param("line-one-xxxx\rline-two", id="carriage-return"),
        pytest.param("nul-in-the-middle\x00xxxx", id="nul"),
        pytest.param("x" * (32 * 1024 + 1), id="over-32k"),
    ])
    def test_is_refused_before_anything_is_staged_or_sent(self, world, bad):
        uid = _pending(world.db, "iv-" + str(abs(hash(bad)))[:8])
        err = _refused(uid, 422, "invalid_value", value=bad)
        assert bad.strip() not in err.detail or not bad.strip()
        assert world.agent.calls == [] and _staged(world) == 0
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_name_the_policy_refuses_is_refused_at_answer_time_too(self, world):
        """A row can arrive through the file channel, which validates nothing;
        the sink re-reads the name and refuses what the policy refuses."""
        uid = _pending(world.db, "iv-name", name="PATH")
        _refused(uid, 422, "invalid_credential_name")
        assert world.agent.calls == [] and _staged(world) == 0


class TestStagingFailsClosed:
    def test_a_down_scrub_store_refuses_delivery_and_never_reaches_the_agent(self, world, monkeypatch):
        monkeypatch.setattr(world.scrub, "get_breaker_redis", lambda: None)
        uid = _pending(world.db, "st-down")
        _refused(uid, 503, "scrub_unavailable")
        assert world.agent.calls == []
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_failing_stage_write_refuses_delivery(self, world, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("redis exploded")
        monkeypatch.setattr(world.fake, "hset", boom)
        uid = _pending(world.db, "st-boom")
        _refused(uid, 503, "scrub_unavailable")
        assert world.agent.calls == []

    def test_the_stage_happens_before_the_agent_is_consulted(self, world, monkeypatch):
        """Stage FIRST: by the time the agent's state is read, the scrub
        already holds the value."""
        seen = []
        real_state = world.sink._running_state

        def state(agent_name):
            seen.append(VALUE in world.scrub.get_staged_values())
            return real_state(agent_name)
        monkeypatch.setattr(world.sink, "_running_state", state)
        uid = _pending(world.db, "st-order")
        _provide(uid)
        assert seen == [True]


class TestARefusedWriteLeavesNothingStaged:
    """"Nothing was stored" is true of the scrub too: a refusal that left the
    agent's file untouched removes the value this call staged. Only the inject
    leg — where the agent may have written before failing — keeps it staged,
    and says so."""

    @pytest.mark.parametrize("arm,status,code", [
        ("stopped", 409, "agent_not_running"),
        ("busy", 409, "env_busy"),
        ("lock_store", 503, "scrub_unavailable"),
        ("read_unreachable", 503, "agent_unreachable"),
        ("read_refused", 503, "agent_unreachable"),
        ("unconfirmed", 409, "env_unreadable"),
        ("too_large", 422, "env_too_large"),
    ])
    def test_every_refusal_before_the_inject_unstages_the_value(self, world, monkeypatch,
                                                                 arm, status, code):
        if arm == "stopped":
            world.status["status"] = "stopped"
        elif arm == "busy":
            world.fake.set(f"credential_sink:lock:{AGENT}", "someone-else", ex=30)
        elif arm == "lock_store":
            real_set = world.fake.set

            def flaky(key, *a, **k):
                if key.startswith("credential_sink:lock:"):
                    raise RuntimeError("redis exploded")
                return real_set(key, *a, **k)
            monkeypatch.setattr(world.fake, "set", flaky)
        elif arm == "read_unreachable":
            world.agent.read_error = httpx.ConnectError("boom")
        elif arm == "read_refused":
            world.agent.read_status = 500
        elif arm == "unconfirmed":
            world.agent.env_text = None
            world.agent.legacy_read = True
        elif arm == "too_large":
            world.agent.env_text = 'X="' + "y" * (256 * 1024) + '"\n'
        uid = _pending(world.db, f"un-{arm}")
        err = _refused(uid, status, code)
        assert "nothing was stored" in err.detail.lower()
        assert VALUE not in world.scrub.get_staged_values()
        assert _staged(world) == 0
        assert not [c for c in world.agent.calls if c[0] == "POST"]

    @pytest.mark.parametrize("arm", ["inject_unreachable", "inject_refused"])
    def test_an_inject_that_failed_keeps_the_value_staged_and_does_not_say_nothing_was_stored(
            self, world, arm):
        if arm == "inject_unreachable":
            async def boom(url, json=None, **kw):
                raise httpx.ReadTimeout("the agent may have written before timing out")
            world.agent.post = boom
        else:
            world.agent.inject_status = 500
        uid = _pending(world.db, f"un-{arm}")
        err = _refused(uid, 503, "agent_unreachable")
        assert "nothing was stored" not in err.detail.lower()
        assert VALUE in world.scrub.get_staged_values()

    def test_a_value_already_staged_by_someone_else_stays_staged(self, world):
        """Removal is of what THIS call added: the same value staged earlier
        (a successful write, a vault fetch) keeps its protection."""
        world.scrub.stage_secret(AGENT, VALUE)
        world.status["status"] = "stopped"
        uid = _pending(world.db, "un-already")
        _refused(uid, 409, "agent_not_running")
        assert VALUE in world.scrub.get_staged_values()


class TestAgentState:
    def test_a_stopped_agent_is_a_named_409_and_nothing_is_written(self, world):
        world.status["status"] = "exited"
        uid = _pending(world.db, "ag-stopped")
        err = _refused(uid, 409, "agent_not_running")
        assert err.data == {"status": "exited"}
        assert world.agent.calls == []
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_missing_container_is_the_same_409(self, world):
        world.status["container"] = None
        uid = _pending(world.db, "ag-missing")
        err = _refused(uid, 409, "agent_not_running")
        assert err.data == {"status": "missing"}

    def test_an_unreachable_agent_is_a_503_on_the_read(self, world):
        world.agent.read_error = httpx.ConnectError("boom")
        uid = _pending(world.db, "ag-unreach")
        _refused(uid, 503, "agent_unreachable")
        assert [c[0] for c in world.agent.calls] == ["GET"]

    def test_a_refused_read_is_a_503(self, world):
        world.agent.read_status = 500
        uid = _pending(world.db, "ag-read500")
        _refused(uid, 503, "agent_unreachable")

    def test_a_refused_inject_is_a_503_and_the_ask_stays_pending(self, world):
        world.agent.inject_status = 400
        uid = _pending(world.db, "ag-inject400")
        _refused(uid, 503, "agent_unreachable")
        assert world.db.get_operator_queue_item(uid)["status"] == "pending"
        assert not [a for a in world.audit if a.get("event_action") == "chat_provide"]


class TestOneWriterPerAgent:
    def test_a_held_lock_is_a_named_409_and_the_file_is_not_read(self, world):
        world.fake.set(f"credential_sink:lock:{AGENT}", "another-writer", ex=30)
        uid = _pending(world.db, "lk-busy")
        _refused(uid, 409, "env_busy")
        assert world.agent.calls == []
        assert world.fake.get(f"credential_sink:lock:{AGENT}") == "another-writer"  # not ours; untouched

    def test_a_lock_store_error_fails_closed(self, world, monkeypatch):
        """The same store the scrub uses — a lock that cannot be taken is a
        refusal, never a write under no lock."""
        real_set = world.fake.set

        def flaky(key, *a, **k):
            if key.startswith("credential_sink:lock:"):
                raise RuntimeError("redis exploded")
            return real_set(key, *a, **k)
        monkeypatch.setattr(world.fake, "set", flaky)
        uid = _pending(world.db, "lk-err")
        _refused(uid, 503, "scrub_unavailable")
        assert world.agent.calls == []

    def test_two_sequential_writes_both_land_and_keep_each_other(self, world, agent_reader, tmp_path):
        a = _pending(world.db, "lk-a", name="FIRST_TOKEN")
        b = _pending(world.db, "lk-b", name="SECOND_TOKEN")
        _provide(a, value="first-value-xxxxxxxx")
        _provide(b, value="second-value-xxxxxxx")
        env = tmp_path / ".env"
        env.write_text(world.agent.env_text)
        parsed = agent_reader.parse_env_file(env)
        assert parsed["FIRST_TOKEN"] == "first-value-xxxxxxxx"
        assert parsed["SECOND_TOKEN"] == "second-value-xxxxxxx"
        assert parsed["OTHER"] == "keep me"


class TestEnvCap:
    def test_a_file_that_would_exceed_the_cap_is_refused_before_inject(self, world):
        world.agent.env_text = 'X="' + "y" * (256 * 1024) + '"\n'
        uid = _pending(world.db, "cap-1")
        _refused(uid, 422, "env_too_large")
        assert [c[0] for c in world.agent.calls] == ["GET"]


# ===========================================================================
# 4. The ask side of the service
# ===========================================================================


class TestTheAskSide:
    def test_a_question_cannot_be_answered_through_the_field(self, world):
        uid = _pending(world.db, "as-q", type="question", options=None, context={})
        _refused(uid, 404, "not_found")
        assert world.agent.calls == []

    def test_a_stranger_gets_the_uniform_404(self, world):
        uid = _pending(world.db, "as-stranger")
        _refused(uid, 404, "not_found", email="stranger@example.com")
        assert world.agent.calls == []

    def test_an_ended_ask_is_refused_before_any_write(self, world):
        uid = _pending(world.db, "as-ended")
        world.db.cancel_operator_queue_item(uid, disposed_by_email=OWNER)
        _refused(uid, 400, "already_resolved")
        assert world.agent.calls == [] and _staged(world) == 0

    def test_an_expired_ask_is_refused_before_any_write(self, world):
        uid = _pending(world.db, "as-expired", expires_at="2020-01-01T00:00:00Z")
        _refused(uid, 409, "expired")
        assert world.agent.calls == []

    def test_a_diverged_ask_is_refused_before_any_write(self, world):
        uid = _pending(world.db, "as-diverged")
        world.db.set_operator_queue_sync_state(uid, "changed", "the agent rewrote it",
                                               "2026-11-01T10:00:00Z")
        _refused(uid, 409, "item_diverged")
        assert world.agent.calls == []

    def test_a_cas_lost_after_the_write_reports_the_write_and_rolls_nothing_back(self, world, monkeypatch):
        uid = _pending(world.db, "as-race")
        real_set = world.sink.set_env_value

        async def write_then_lose(agent_name, name, value, **kw):
            await real_set(agent_name, name, value, **kw)
            world.db.cancel_operator_queue_item(uid, disposed_by_email="someone@example.com")
        monkeypatch.setattr(world.sink, "set_env_value", write_then_lose)
        err = _refused(uid, 409, "already_resolved")
        assert err.data == {"credential_written": True}
        assert world.agent.env_text.startswith(f'OTHER="keep me"\n# a comment\nFOO=bar\n{NAME}="')
        assert [a for a in world.audit if a.get("event_action") == "chat_provide"]
        assert world.db.get_operator_queue_item(uid)["status"] == "cancelled"


# ===========================================================================
# 5. The module never spells the value into a string
# ===========================================================================


# Measuring a value (its type, length, line structure, bytes) and handing it to
# the scrub (stage / unstage), the file patcher, the sink's own checker or its
# delivery step are the only moves the module may make with it.
_ALLOWED_VALUE_CALLEES = {"len", "isinstance", "strip", "encode", "splitlines",
                          "stage_secret", "unstage_secret", "set_keys", "_check_value",
                          "_deliver", "set_env_value"}


def test_the_sink_module_never_interpolates_the_value():
    """An AST guard over `services/credential_sink.py`: the name `value` may be
    measured, staged, patched into the file or passed on — never formatted,
    logged, concatenated or raised. A new leak path fails here before it ships."""
    src = (Path(_BACKEND) / "services" / "credential_sink.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    offences = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id == "value" and isinstance(node.ctx, ast.Load)):
            continue
        cur = node
        verdict = None
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.JoinedStr):
                verdict = "f-string"
                break
            if isinstance(cur, ast.BinOp) and isinstance(cur.op, (ast.Add, ast.Mod)):
                verdict = "concatenation"
                break
            if isinstance(cur, ast.Call):
                fn = cur.func
                name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                if name == "format" or (isinstance(fn, ast.Attribute)
                                        and isinstance(fn.value, ast.Name) and fn.value.id == "logger"):
                    verdict = f"call {name}"
                elif name not in _ALLOWED_VALUE_CALLEES:
                    verdict = f"call {name}"
                break
            if isinstance(cur, (ast.Raise,)):
                verdict = "raise"
                break
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
        if verdict:
            offences.append((node.lineno, verdict))
    assert not offences, offences

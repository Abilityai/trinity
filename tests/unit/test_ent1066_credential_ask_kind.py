"""trinity-enterprise#1066 — the `credential` ask kind.

An agent that needs a secret asks for it BY NAME: one ask of kind `credential`
whose `context.credential_name` says which environment variable the person is
filling. The value never travels through an ask: the only thing that may end a
credential ask is the Workspace secret field's sink (`via_credential_sink`), and
every other door — the operator's respond box, the Workspace answer route, a
direct `ask_service.answer` — is refused by name. The agent is woken with the
NAME only.

What is pinned here, driven through the real sink over the real SQLite:

* the kind is raised with a valid name, no options, and exactly one resolved
  person (`no_person` otherwise — a secret field has one reader);
* the name is a variable name the agent's own `.env` reader would set, at most
  64 characters, and never one the platform or the agent runtime reserves
  (`credential_ask_policy`, held in parity with the agent image's
  `execution_env` protected and routing sets);
* the answer sink refuses a credential row answered any way but the field, on
  both doors, and a dismiss still ends it;
* the Workspace projection carries `credential_name` and nothing else of the
  context; the wake prompt names the variable and never a value.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

OWNER = "owner-1066@example.com"
NAME = "VAULT_SERVICE_TOKEN"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real sink over the real SQLite; the world around it stubbed the way
    the ent#611 suite stubs it: audit + broadcast recorded, the owner lookup,
    the workspace thread, the wake opt-in and the rate limiter."""
    import json as _json
    from types import SimpleNamespace
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit, sent, events, state = [], [], [], {"owner": OWNER}

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(_json.loads(message))

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_observers", [events.append])
    monkeypatch.setattr(svc, "_owner_email", lambda agent: state["owner"])
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", True))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: False, raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, sent=sent, events=events, state=state, db=real_db)
    assignment_provider.clear_provider()


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Every Workspace reader is on every roster here; the fence is ent#364's."""
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "credential", "title": f"Needs {NAME}",
         "question": "To open your vault on your behalf.",
         "context": {"credential_name": NAME}}
    b.update(over)
    return b


def _raise(ask, agent, body):
    return ask.svc.raise_ask(agent, body, raised_by="agent", channel="mcp")


def _rejected(ask, status, code):
    class _Ctx:
        def __enter__(self):
            self._cm = pytest.raises(ask.svc.AskRejected)
            self._info = self._cm.__enter__()
            return self._info

        def __exit__(self, *exc):
            ok = self._cm.__exit__(*exc)
            assert (self._info.value.status_code, self._info.value.code) == (status, code), (
                self._info.value.status_code, self._info.value.code, self._info.value.message)
            return ok
    return _Ctx()


def _pending_credential(real_db, agent, rid, name=NAME, **over):
    """A pending credential row as the sink stores one."""
    item = {"id": rid, "type": "credential", "status": "pending", "priority": "medium",
            "title": f"Needs {name}", "question": "to open the vault", "options": None,
            "context": {"credential_name": name}, "created_at": "2026-11-01T10:00:00Z",
            "addressed_to_email": OWNER}
    item.update(over)
    return real_db.create_operator_queue_item(agent, item)


# ===========================================================================
# 1. Raising the kind
# ===========================================================================


class TestRaise:
    AGENT = "agent-1066-raise"

    def test_a_credential_ask_is_created_by_name_with_no_options(self, ask):
        receipt = _raise(ask, self.AGENT, _body("cred-ok"))
        assert receipt["status"] == "created"
        row = ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "cred-ok")
        assert row["type"] == "credential"
        assert row["context"]["credential_name"] == NAME
        assert row["options"] in (None, [])
        assert row["addressed_to_email"] == OWNER   # exactly one person reads the field

    def test_options_are_refused(self, ask):
        with _rejected(ask, 422, "invalid_options"):
            _raise(ask, self.AGENT, _body("cred-opts", options=["paste it", "later"]))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "cred-opts") is None

    @pytest.mark.parametrize("name", [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("not a name", id="spaces"),
        pytest.param("1ABC", id="leading-digit"),
        pytest.param("MY-KEY", id="hyphen"),
        pytest.param("A" * 65, id="over-64"),
        pytest.param(123, id="not-a-string"),
        pytest.param("PATH", id="agent-protected"),
        pytest.param("LD_PRELOAD", id="agent-protected-loader"),
        pytest.param("GIT_CONFIG_KEY_0", id="agent-protected-prefix"),
        pytest.param("HTTPS_PROXY", id="agent-routing"),
        pytest.param("ANTHROPIC_BASE_URL", id="runtime-endpoint"),
        pytest.param("TRINITY_MCP_API_KEY", id="platform-written"),
        pytest.param("ANTHROPIC_API_KEY", id="platform-auth"),
        pytest.param("CLAUDE_CODE_OAUTH_TOKEN", id="subscription-token"),
    ])
    def test_a_name_the_agent_cannot_take_is_refused_and_nothing_is_written(self, ask, name):
        context = {} if name is None else {"credential_name": name}
        with _rejected(ask, 422, "invalid_credential_name") as info:
            _raise(ask, self.AGENT, _body("cred-bad", context=context))
        # the refusal names the rule, never echoes a value
        assert "credential_name" in info.value.message
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "cred-bad") is None

    def test_a_lower_case_name_the_agent_reader_accepts_is_accepted(self, ask):
        receipt = _raise(ask, self.AGENT, _body("cred-lower", context={"credential_name": "my_token"}))
        assert receipt["status"] == "created"

    def test_the_operator_role_has_no_person_and_is_refused(self, ask):
        with _rejected(ask, 422, "no_person"):
            _raise(ask, self.AGENT, _body("cred-op", to="operator"))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "cred-op") is None

    def test_an_agent_with_no_owner_and_no_primary_is_refused(self, ask):
        ask.state["owner"] = None
        with _rejected(ask, 422, "no_person"):
            _raise(ask, self.AGENT, _body("cred-noowner"))

    def test_a_role_several_people_fill_is_refused(self, ask):
        """A secret field has ONE reader; a shared ask would hand it to whoever
        answers first and record it against all of them."""
        from services import assignment_provider

        class _Two:
            def people_for(self, agent, role):
                return {"emails": ["a@example.com", "b@example.com"]}

        assignment_provider.register_provider(_Two())
        with _rejected(ask, 422, "no_person"):
            _raise(ask, self.AGENT, _body("cred-shared"))

    def test_a_replay_returns_the_first_receipt(self, ask):
        first = _raise(ask, self.AGENT, _body("cred-replay"))
        again = _raise(ask, self.AGENT, _body("cred-replay"))
        assert first["status"] == "created" and again["status"] == "replayed"


class TestReservedNamesMirrorTheAgentImage:
    """The policy leaf lists what the agent runtime itself refuses from `.env`
    (`execution_env.PROTECTED_KEYS`, its prefixes, the routing and endpoint
    names) plus the platform-written keys. A drift fails here, not on a person
    who typed a secret that the agent then silently ignored."""

    @pytest.fixture(scope="class")
    def agent_env(self):
        path = _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
        spec = importlib.util.spec_from_file_location("_execution_env_ent1066", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_every_agent_protected_name_is_reserved(self, agent_env):
        from services import credential_ask_policy as policy
        agent_side = set(agent_env.PROTECTED_KEYS) | set(agent_env.ROUTING_KEYS)
        for names in agent_env.RUNTIME_ENDPOINT_KEYS.values():
            agent_side |= set(names)
        missing = agent_side - set(policy.RESERVED_ENV_NAMES)
        assert not missing, sorted(missing)

    def test_the_prefix_families_match(self, agent_env):
        from services import credential_ask_policy as policy
        assert tuple(policy.RESERVED_ENV_NAME_PREFIXES) == tuple(agent_env.PROTECTED_KEY_PREFIXES)

    def test_the_platform_written_keys_are_reserved(self):
        from services import credential_ask_policy as policy
        for name in ("TRINITY_MCP_API_KEY", "TRINITY_BACKEND_URL", "TRINITY_AGENT_NAME",
                     "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            assert policy.credential_name_problem(name) == "reserved", name

    def test_a_plain_name_has_no_problem(self):
        from services import credential_ask_policy as policy
        assert policy.credential_name_problem(NAME) is None


# ===========================================================================
# 2. Only the secret sink answers it
# ===========================================================================


class TestAnswerRefusal:
    AGENT = "agent-1066-answer"

    def test_the_sink_refuses_an_answer_that_did_not_come_through_the_field(self, ask):
        uid = _pending_credential(ask.db, self.AGENT, "ans-1")
        item = ask.db.get_operator_queue_item(uid)
        with pytest.raises(ask.svc.CredentialAnswerRefused):
            ask.svc.answer(item, response="ops_xxxxxxxxxxxxxxxxxxxxxxxxxx", response_text=None,
                           actor=ask.svc.Actor(email=OWNER))
        row = ask.db.get_operator_queue_item(uid)
        assert row["status"] == "pending" and row["response"] is None
        assert ask.events == []

    def test_the_field_answers_by_name_only(self, ask):
        uid = _pending_credential(ask.db, self.AGENT, "ans-2")
        item = ask.db.get_operator_queue_item(uid)
        ending = ask.svc.answer(item, response="provided", response_text=None,
                                actor=ask.svc.Actor(email=OWNER), via_credential_sink=True)
        row = ask.db.get_operator_queue_item(uid)
        assert row["disposition"] == "answered" and row["response"] == "provided"
        assert row["response_text"] is None
        assert [e.disposition for e in ask.events] == ["answered"]
        assert ending.rows[0]["id"] == uid

    def test_a_dismiss_still_ends_a_credential_ask(self, ask):
        from client_portal.asks import service as portal
        uid = _pending_credential(ask.db, self.AGENT, "ans-dismiss")
        out = portal.dismiss_ask(uid, OWNER, False)
        assert out.status == "dismissed" and out.kind == "credential"

    def test_the_workspace_answer_route_refuses_it_by_name(self, ask):
        from client_portal.asks import service as portal
        uid = _pending_credential(ask.db, self.AGENT, "ans-portal")
        with pytest.raises(portal.AskError) as info:
            portal.answer_ask(uid, OWNER, False, "ops_xxxxxxxxxxxxxxxxxxxxxxxxxx", None)
        assert (info.value.status_code, info.value.code) == (422, "credential_answer_via_field")
        assert ask.db.get_operator_queue_item(uid)["status"] == "pending"


_PRINCIPAL = {"user": None}
_ROUTES_APP = None


def _operator_client():
    """The real operator-queue router, `get_current_user` overridden by walking
    the routes' own dependant trees (the ent#611 suite's shape)."""
    global _ROUTES_APP
    from fastapi.testclient import TestClient
    if _ROUTES_APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _ROUTES_APP = app
    return TestClient(_ROUTES_APP, raise_server_exceptions=True)


class TestOperatorDoor:
    AGENT = "agent-1066-operator"

    def test_the_operator_respond_box_cannot_answer_a_credential_ask(self, ask, monkeypatch):
        from models import User
        from routers import operator_queue as r
        monkeypatch.setattr(r, "_websocket_manager", None)
        _PRINCIPAL["user"] = User(id=7, username="op", email="op@example.com", role="admin")
        uid = _pending_credential(ask.db, self.AGENT, "op-1")
        res = _operator_client().post(f"/api/operator-queue/{uid}/respond",
                                      json={"response": "ops_xxxxxxxxxxxxxxxxxxxxxxxxxx"})
        assert res.status_code == 422, res.text
        assert res.json()["detail"]["code"] == "credential_answer_via_field"
        assert "ops_xxxxxxxxxxxxxxxxxxxxxxxxxx" not in res.text
        assert ask.db.get_operator_queue_item(uid)["status"] == "pending"


# ===========================================================================
# 3. What the person and the agent are told
# ===========================================================================


class TestProjectionAndWake:
    AGENT = "agent-1066-view"

    def test_the_workspace_sees_the_kind_and_the_name_and_nothing_else_of_the_context(self, ask):
        from client_portal.asks import service as portal
        uid = _pending_credential(ask.db, self.AGENT, "view-1",
                                  context={"credential_name": NAME, "secret": "do-not-leak"})
        asks = [a for a in portal.list_asks(OWNER, False, self.AGENT) if a.id == uid]
        assert len(asks) == 1
        view = asks[0]
        assert view.kind == "credential" and view.credential_name == NAME
        dumped = view.model_dump()
        assert "context" not in dumped and "do-not-leak" not in str(dumped)

    def test_the_wake_names_the_variable_and_never_a_value(self):
        from services import operator_resume_service as ors
        item = {"id": "uuid-1", "type": "credential", "title": f"Needs {NAME}",
                "question": "to open the vault", "context": {"credential_name": NAME}}
        text = ors._framed_message(item, "provided", None, to_person=True)
        assert f"{NAME} is now set in your environment" in text
        assert "never repeat its value" in text
        assert "answer: provided" in text
        # the framed data fence carries no free-text note for this kind
        assert "notes:" not in text

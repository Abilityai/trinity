"""trinity-enterprise#1354 — connect a Claude subscription from a browser, no terminal.

The instance runs the sign-in `claude setup-token` runs, with the instance as
the client: it makes the link (PKCE, the verifier kept in Redis for this person
only), the person signs in at Claude and copies the code the page shows, and
the instance exchanges that code for the long-lived subscription token and
registers it as the `primary` subscription through the same path a pasted
token takes.

Pinned here, through the real router on a throwaway app (no network: the token
endpoint is a fake; Redis is an in-memory stand-in):

* the link carries Claude Code's sign-in parameters and an S256 challenge of a
  verifier the instance kept;
* a valid paste is exchanged with that verifier and registered as `primary`,
  and the response is the registration (the token itself is never returned);
* a half-copied, foreign, reused or expired code is refused by name, and
  nothing is stored;
* Anthropic refusing the code is refused by name, nothing stored;
* only a person who is an admin can start or finish it;
* neither the code nor the token reaches a log line.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import logging
import types
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.subscriptions as subs_router  # noqa: E402
from dependencies import get_current_user  # noqa: E402

pytestmark = pytest.mark.unit

TOKEN = "sk-ant-oat01-" + "t" * 40
CODE = "authcode-" + "c" * 30


def _live(name):
    return importlib.import_module(name)


class _Redis:
    def __init__(self):
        self.data = {}
        self.ttl = {}

    def setex(self, key, ttl, value):
        self.data[key] = value
        self.ttl[key] = ttl

    def getdel(self, key):
        return self.data.pop(key, None)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def sign_in(monkeypatch):
    svc = _live("services.claude_sign_in_service")
    store = _Redis()
    monkeypatch.setattr(svc, "get_redis_client", lambda: store)
    calls = []
    answer = {"resp": _Resp(200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 31536000})}

    async def post(url, body):
        calls.append((url, body))
        return answer["resp"]

    monkeypatch.setattr(svc, "_post_token", post)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "0" * 64)
    registered = []
    ss = _live("services.subscription_service")   # the router imports it at call time
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: False)
    monkeypatch.setattr(ss, "connect_agents_to_first_credential", lambda sid: registered.append(sid) or 1)

    async def no_sweep(**kw):
        return None

    monkeypatch.setattr(ss, "adopt_for_credentialless_agents", no_sweep)
    uid = _admin_user_id()
    monkeypatch.setattr(subs_router.db, "get_user_by_username", lambda u: {"id": uid})
    db = _live("database").db
    stale = db.get_subscription_by_name("primary")
    if stale:
        db.delete_subscription(stale.id)
    yield types.SimpleNamespace(svc=svc, store=store, calls=calls, answer=answer, registered=registered, db=db)
    sub = db.get_subscription_by_name("primary")
    if sub:
        db.delete_subscription(sub.id)


def _admin_user_id():
    from db_models import UserCreate
    db = _live("database").db
    user = db.get_user_by_username("ent1354-admin")
    if not user:
        db.create_user(UserCreate(username="ent1354-admin", role="admin", email="ent1354@example.com"))
        user = db.get_user_by_username("ent1354-admin")
    return user["id"]


def _user(role="admin", uid=1, **kw):
    base = dict(id=uid, username=f"{role}{uid}", role=role, email=f"{role}{uid}@example.com",
                agent_name=None, connector_agent=None, mcp_scope=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _client(user=None):
    app = FastAPI()
    app.include_router(subs_router.router)
    app.dependency_overrides[get_current_user] = lambda: user or _user()
    return TestClient(app, raise_server_exceptions=True)


def _start(client):
    r = client.post("/api/subscriptions/claude-sign-in")
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    return url, q


class TestTheLink:
    def test_the_link_is_claude_codes_sign_in_with_a_kept_verifier(self, sign_in):
        url, q = _start(_client())
        assert url.startswith("https://claude.com/cai/oauth/authorize?")
        assert q["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
        assert q["redirect_uri"] == "https://platform.claude.com/oauth/code/callback"
        assert q["scope"] == "user:inference"
        assert q["response_type"] == "code" and q["code"] == "true"
        assert q["code_challenge_method"] == "S256"
        kept = sign_in.svc._load_for_test(sign_in.store, q["state"])
        digest = hashlib.sha256(kept["verifier"].encode()).digest()
        assert q["code_challenge"] == base64.urlsafe_b64encode(digest).decode().rstrip("=")
        assert kept["user_id"] == 1
        assert 0 < sign_in.store.ttl[sign_in.svc._key(q["state"])] <= 1800

    def test_each_link_is_its_own(self, sign_in):
        client = _client()
        assert _start(client)[1]["state"] != _start(client)[1]["state"]


class TestThePaste:
    def test_a_valid_code_is_exchanged_and_registered_as_primary(self, sign_in):
        client = _client()
        _, q = _start(client)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"  {CODE}#{q['state']} \n"})
        assert r.status_code == 200, r.text
        assert TOKEN not in r.text
        assert r.json()["name"] == "primary" and r.json()["connected_agents"] == 1
        sub = sign_in.db.get_subscription_by_name("primary")
        assert sub is not None and sign_in.registered == [sub.id]
        assert sign_in.db.get_subscription_token(sub.id) == TOKEN
        (url, body), = sign_in.calls
        assert url == "https://platform.claude.com/v1/oauth/token"
        digest = hashlib.sha256(body["code_verifier"].encode()).digest()
        assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == q["code_challenge"]
        assert body["grant_type"] == "authorization_code" and body["code"] == CODE
        assert body["state"] == q["state"] and body["client_id"] == q["client_id"]
        assert body["redirect_uri"] == q["redirect_uri"]
        assert body["expires_in"] == 31536000

    def test_a_code_is_used_once(self, sign_in):
        client = _client()
        _, q = _start(client)
        assert client.post("/api/subscriptions/claude-sign-in/exchange",
                           json={"code": f"{CODE}#{q['state']}"}).status_code == 200
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "sign_in_expired"
        assert len(sign_in.registered) == 1 and len(sign_in.calls) == 1

    @pytest.mark.parametrize("pasted", ["", "   ", CODE, f"#{'s' * 20}", f"{CODE}#"])
    def test_a_half_copied_code_is_refused_by_name(self, sign_in, pasted):
        client = _client()
        _start(client)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": pasted})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "code_incomplete"
        assert "fresh" in r.json()["detail"]["message"].lower() or "again" in r.json()["detail"]["message"].lower()
        assert sign_in.calls == [] and sign_in.registered == []

    def test_a_code_from_no_link_of_ours_is_refused(self, sign_in):
        r = _client().post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#notours"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "sign_in_expired"
        assert sign_in.calls == [] and sign_in.registered == []

    def test_another_persons_link_does_not_finish_for_you(self, sign_in):
        _, q = _start(_client(_user(uid=1)))
        r = _client(_user(uid=2)).post("/api/subscriptions/claude-sign-in/exchange",
                                       json={"code": f"{CODE}#{q['state']}"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "sign_in_expired"
        assert sign_in.calls == [] and sign_in.registered == []

    @pytest.mark.parametrize("status,code", [(400, "code_rejected"), (401, "code_rejected"),
                                             (429, "anthropic_unavailable"), (503, "anthropic_unavailable")])
    def test_anthropic_refusing_is_named_and_nothing_is_stored(self, sign_in, status, code):
        sign_in.answer["resp"] = _Resp(status, {"error": {"message": "nope"}})
        client = _client()
        _, q = _start(client)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        assert r.json()["detail"]["code"] == code
        assert r.status_code in (422, 502)
        assert sign_in.registered == []

    def test_an_answer_without_a_subscription_token_is_not_stored(self, sign_in):
        sign_in.answer["resp"] = _Resp(200, {"access_token": "something-else"})
        client = _client()
        _, q = _start(client)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        assert r.status_code == 502 and r.json()["detail"]["code"] == "unexpected_answer"
        assert "something-else" not in r.text
        assert sign_in.registered == []

    def test_the_code_survives_a_missing_encryption_key(self, sign_in, monkeypatch):
        """Checked before the exchange: a 503 must not spend the person's code."""
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        client = _client()
        _, q = _start(client)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        assert r.status_code == 503 and sign_in.calls == []

    def test_neither_code_nor_token_is_logged(self, sign_in, caplog):
        caplog.set_level(logging.DEBUG)
        client = _client()
        _, q = _start(client)
        client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        sign_in.answer["resp"] = _Resp(401, {"error": {"message": CODE}})
        _, q = _start(client)
        client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        text = caplog.text
        assert CODE not in text and TOKEN not in text and q["state"] not in text


class TestWhoMay:
    @pytest.mark.parametrize("user", [
        _user(role="creator"),
        _user(agent_name="some-agent", mcp_scope="agent"),
    ])
    def test_only_a_person_who_is_admin(self, sign_in, user):
        client = _client(user)
        assert client.post("/api/subscriptions/claude-sign-in").status_code == 403
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#x"})
        assert r.status_code == 403
        assert sign_in.store.data == {} and sign_in.calls == []


class TestMustBeAPerson:
    """`assert_person` on the exchange route is its own check, not a by-product
    of the admin gate: these principals pass `assert_admin` (trinity-system and
    a request vouched by an agent are admins), so only the person rule stops
    them, and with it gone they would register `primary`."""

    @pytest.mark.parametrize("user", [
        _user(mcp_scope="system"),
        _user(vouched_source_agent="some-agent"),
    ], ids=["system", "vouched-by-an-agent"])
    def test_an_admin_who_is_not_a_person_cannot_exchange_or_start(self, sign_in, user):
        from dependencies import assert_admin
        assert_admin(user)                                   # the admin gate lets it through …
        _, q = _start(_client())                             # a live link, made by a person
        client = _client(user)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        assert r.status_code == 403                          # … the person rule does not
        assert sign_in.calls == [] and sign_in.registered == []
        assert sign_in.db.get_subscription_by_name("primary") is None
        assert client.post("/api/subscriptions/claude-sign-in").status_code == 403


SECRET = "zq9-pasted-" + "p" * 24


@pytest.mark.parametrize("case", [
    "incomplete", "no-link", "foreign", "rejected", "unavailable", "unexpected", "no-encryption", "api-key",
])
def test_the_pasted_code_is_in_no_refusal(sign_in, monkeypatch, case):
    """Every refusal the exchange route can give, with a recognisable paste:
    the paste (code or state half) never comes back in the answer."""
    client = _client()
    _, q = _start(client)
    pasted = f"{SECRET}#{q['state']}"
    if case == "incomplete":
        pasted = SECRET
    elif case == "no-link":
        pasted = f"{SECRET}#{SECRET}-state"
    elif case == "foreign":
        _, q2 = _start(_client(_user(uid=2)))
        pasted = f"{SECRET}#{q2['state']}"
    elif case == "rejected":
        sign_in.answer["resp"] = _Resp(400, {"error": {"message": SECRET}})
    elif case == "unavailable":
        sign_in.answer["resp"] = _Resp(503, {"error": {"message": SECRET}})
    elif case == "unexpected":
        sign_in.answer["resp"] = _Resp(200, {"access_token": SECRET})
    elif case == "no-encryption":
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
    elif case == "api-key":
        pasted = "sk-proj-" + SECRET
    r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": pasted})
    assert r.status_code >= 400, r.text
    assert SECRET not in r.text and q["state"] not in r.text
    assert sign_in.registered == []


class TestAnApiKeyInTheCodeField:
    """A value that is plainly an API-key attempt gets the API-key format
    message, not "that is not the whole code", and spends nothing."""

    @pytest.mark.parametrize("pasted", ["sk-ant-api03-" + "k" * 40, "sk-proj-" + "k" * 40, "ant-api03-" + "k" * 40])
    def test_it_is_told_what_an_api_key_looks_like(self, sign_in, pasted):
        client = _client()
        _start(client)
        r = client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": pasted})
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "api_key_not_code"
        assert "sk-ant-api" in detail["message"] and "not the whole code" not in detail["message"]
        assert sign_in.calls == [] and sign_in.registered == [] and len(sign_in.store.data) == 1


class TestTheAuditRow:
    def test_a_subscription_registered_here_is_audited_by_name_and_actor_only(self, sign_in, monkeypatch):
        rows = []

        async def log(**kw):
            rows.append(kw)

        monkeypatch.setattr(subs_router.platform_audit_service, "log", log)
        client = _client()
        _, q = _start(client)
        assert client.post("/api/subscriptions/claude-sign-in/exchange",
                           json={"code": f"{CODE}#{q['state']}"}).status_code == 200
        mine = [r for r in rows if r.get("event_action") == "subscription_signed_in"]
        assert len(mine) == 1, rows
        row = mine[0]
        assert row["event_type"] == subs_router.AuditEventType.CREDENTIALS
        assert row["actor_user"].id == 1
        assert row["target_type"] == "subscription" and row["target_id"] == "primary"
        assert row["details"] == {"name": "primary"}
        assert TOKEN not in repr(row) and CODE not in repr(row) and q["state"] not in repr(row)

    def test_a_refused_exchange_writes_no_registration_row(self, sign_in, monkeypatch):
        rows = []

        async def log(**kw):
            rows.append(kw)

        monkeypatch.setattr(subs_router.platform_audit_service, "log", log)
        sign_in.answer["resp"] = _Resp(400, {})
        client = _client()
        _, q = _start(client)
        client.post("/api/subscriptions/claude-sign-in/exchange", json={"code": f"{CODE}#{q['state']}"})
        assert [r for r in rows if r.get("event_action") == "subscription_signed_in"] == []


def test_the_provider_timeout_is_well_under_the_assistants_wait():
    """The assistant's credential field reaches this route on the loopback and
    gives up after its own timeout (enterprise `assistant/credential.py`,
    30 s), answering "may or may not have been stored". The code is spent the
    moment the exchange starts, so the provider call plus the registration
    must finish well inside that wait: the provider gets a third of it at most."""
    svc = _live("services.claude_sign_in_service")
    assert svc.TIMEOUT_SECONDS <= 10.0
    try:
        from enterprise.backend.assistant import credential  # noqa: PLC0415
    except ImportError:
        return                                    # the private module is not mounted here
    assert svc.TIMEOUT_SECONDS * 3 <= credential.TIMEOUT_SECONDS


def test_the_provider_call_uses_that_timeout(monkeypatch):
    svc = _live("services.claude_sign_in_service")
    import httpx
    seen = {}

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp(200, {})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    import asyncio
    asyncio.run(svc._post_token("https://example.invalid/token", {}))
    assert seen["timeout"] == svc.TIMEOUT_SECONDS


def test_the_routes_are_on_the_router_main_includes():
    """main.py includes `routers.subscriptions.router`; the two routes ride on it."""
    paths = {(r.path, tuple(sorted(r.methods))) for r in subs_router.router.routes}
    assert ("/api/subscriptions/claude-sign-in", ("POST",)) in paths
    assert ("/api/subscriptions/claude-sign-in/exchange", ("POST",)) in paths
    import ast
    from pathlib import Path
    main = Path(subs_router.__file__).resolve().parents[1] / "main.py"
    calls = [n for n in ast.walk(ast.parse(main.read_text())) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "include_router"
             and n.args and getattr(n.args[0], "id", None) == "subscriptions_router"]
    assert calls, "main.py no longer includes routers.subscriptions.router"

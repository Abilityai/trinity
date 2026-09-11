"""ent#582 — configure Claude, email (Resend) and Gemini keys from the first-run flow.

Pins what the first-run credential steps rely on:

* the new key endpoints are admin-only, validate with named errors, and persist
  through the ent#435 encrypted path (no cleartext row, ever);
* the runtime resolves Settings first, then env — for the Gemini key, the
  Resend key, the email provider and the sender address;
* the Anthropic key tab refuses an `sk-ant-oat` token with the spec's copy;
* `claude_auth_configured` is true with a subscription and no API key;
* the install's FIRST Claude credential reaches the agents created without one,
  and only those.

DB-level unit tests against the conftest's throwaway SQLite — no network (every
provider call is a fake), no Docker, no running backend.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import types

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.settings as sr  # noqa: E402
import routers.subscriptions as subs_router  # noqa: E402
from database import db  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from services import platform_keys_service as pks  # noqa: E402
from services import subscription_service  # noqa: E402
from services.secret_settings import encrypted_key_for, looks_like_envelope  # noqa: E402
from services.settings_service import settings_service  # noqa: E402

pytestmark = pytest.mark.unit


def _live(name):
    """The module the code under test resolves at CALL time. Harnesses elsewhere
    in the suite re-import `database` / `services.*` (pytest-randomly orders
    them before or after this file), so a module-level binding here can be a
    stale object that the function no longer reads."""
    return importlib.import_module(name)

_KEYS = ("resend_api_key", "google_api_key", "anthropic_api_key")
_OAT_COPY = (
    "That key was rejected. API keys start with sk-ant-api — this one starts "
    "with sk-ant-oat, which is a subscription token. Paste it on the "
    "Subscription token tab instead."
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Shared per-process SQLite: purge in a finalizer so a failure can't leak."""
    def purge():
        for key in _KEYS:
            db.delete_setting(key)
            db.delete_setting(encrypted_key_for(key))
        db.delete_setting("email_from_address")
    for env in ("RESEND_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(_live("config"), "GEMINI_API_KEY", "")
    monkeypatch.setattr(_live("config"), "EMAIL_PROVIDER", "console")
    monkeypatch.setattr(_live("config"), "SMTP_FROM", "noreply@env.example.com")
    purge()
    yield
    purge()


def _user(role):
    return types.SimpleNamespace(
        id=1, username=role, role=role, email=f"{role}@example.com",
        agent_name=None, connector_agent=None, mcp_scope=None,
    )


def _client(role="admin"):
    app = FastAPI()
    app.include_router(sr.router)
    app.include_router(subs_router.router)
    app.dependency_overrides[get_current_user] = lambda: _user(role)
    return TestClient(app, raise_server_exceptions=True)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.content = b"{}" if body is not None else b""

    def json(self):
        return self._body


def _fake_httpx(monkeypatch, resp, calls=None):
    """Replace platform_keys_service's httpx.AsyncClient with one canned answer."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if calls is not None:
                calls.append((url, kw))
            return resp

    monkeypatch.setattr(pks.httpx, "AsyncClient", _Client)


# ---------------------------------------------------------------------------
# Admin-only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("put", "/api/settings/api-keys/resend", {"api_key": "re_x"}),
    ("delete", "/api/settings/api-keys/resend", None),
    ("post", "/api/settings/api-keys/resend/test", {"api_key": "re_x"}),
    ("put", "/api/settings/api-keys/gemini", {"api_key": "AIzaX"}),
    ("delete", "/api/settings/api-keys/gemini", None),
    ("post", "/api/settings/api-keys/gemini/test", {"api_key": "AIzaX"}),
    ("post", "/api/subscriptions/test", {"token": "sk-ant-oat01-x"}),
])
def test_new_endpoints_are_admin_only(method, path, body):
    client = _client("user")
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_agent_scoped_key_is_refused_even_on_an_admin_owner():
    """assert_admin rejects agent principals itself (ent#293) — the grant gate."""
    app = FastAPI()
    app.include_router(sr.router)
    agent = _user("admin")
    agent.agent_name, agent.mcp_scope = "some-agent", "agent"
    app.dependency_overrides[get_current_user] = lambda: agent
    r = TestClient(app).put("/api/settings/api-keys/gemini", json={"api_key": "AIzaX"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Validation — named, actionable
# ---------------------------------------------------------------------------

def test_oat_token_in_the_api_key_tab_gets_the_spec_copy():
    client = _client()
    r = client.put("/api/settings/api-keys/anthropic", json={"api_key": "sk-ant-oat01-abc"})
    assert r.status_code == 400
    assert r.json()["detail"] == _OAT_COPY
    t = client.post("/api/settings/api-keys/anthropic/test", json={"api_key": "sk-ant-oat01-abc"})
    assert t.json() == {"valid": False, "error": _OAT_COPY}
    assert db.get_setting_value(encrypted_key_for("anthropic_api_key"), None) is None


@pytest.mark.parametrize("path,body,needle", [
    ("/api/settings/api-keys/resend", {"api_key": "sk-nope"}, "start with re_"),
    ("/api/settings/api-keys/resend", {"api_key": "re_ok", "from_address": "not an address"},
     "noreply@your-domain.com"),
    ("/api/settings/api-keys/gemini", {"api_key": "sk-nope"}, "start with AIza"),
])
def test_bad_input_is_a_named_400_and_stores_nothing(path, body, needle):
    r = _client().put(path, json=body)
    assert r.status_code == 400
    assert needle in r.json()["detail"]
    assert db.get_setting_value(encrypted_key_for("resend_api_key"), None) is None
    assert db.get_setting_value(encrypted_key_for("google_api_key"), None) is None


def test_subscription_token_format_errors_name_the_other_tab():
    client = _client()
    r = client.post("/api/subscriptions/test", json={"token": "sk-ant-api03-abc"})
    assert r.json()["valid"] is False and "API key tab" in r.json()["error"]
    r = client.post("/api/subscriptions/test", json={"token": "hello"})
    assert r.json()["valid"] is False and "claude setup-token" in r.json()["error"]


@pytest.mark.parametrize("status,valid,needle", [
    ("ok", True, None),
    ("rate_limited", True, "usage limit"),
    ("invalid_token", False, "rejected"),
    ("error", False, "Couldn't reach Anthropic"),
])
def test_subscription_token_live_check_maps_every_probe_status(monkeypatch, status, valid, needle):
    import services.subscription_headroom_service as hs

    async def fake_check(token):
        assert token == "sk-ant-oat01-abc"
        return status

    monkeypatch.setattr(hs, "check_token", fake_check)
    body = _client().post("/api/subscriptions/test", json={"token": " sk-ant-oat01-abc "}).json()
    assert body["valid"] is valid
    if needle:
        assert needle in (body.get("error") or body.get("warning"))


# ---------------------------------------------------------------------------
# Encrypted at rest + runtime resolution order
# ---------------------------------------------------------------------------

def test_resend_key_is_encrypted_at_rest_and_selects_resend():
    client = _client()
    r = client.put("/api/settings/api-keys/resend",
                   json={"api_key": "re_test_key_1", "from_address": "Trinity <codes@acme.test>"})
    assert r.status_code == 200 and r.json()["masked"] == "...ey_1"
    assert db.get_setting_value("resend_api_key", None) is None
    assert looks_like_envelope(db.get_setting_value(encrypted_key_for("resend_api_key"), None))
    assert settings_service.get_resend_api_key() == "re_test_key_1"
    assert settings_service.get_email_provider() == "resend"  # env says console
    assert settings_service.get_email_from_address() == "Trinity <codes@acme.test>"

    status = client.get("/api/settings/api-keys").json()["resend"]
    assert status["configured"] and status["source"] == "settings" and status["provider"] == "resend"

    assert client.delete("/api/settings/api-keys/resend").json()["deleted"] is True
    assert settings_service.get_email_provider() == "console"
    assert settings_service.get_email_from_address() == "noreply@env.example.com"


def test_resend_resolution_is_settings_then_env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_from_env")
    assert settings_service.get_resend_api_key() == "re_from_env"
    # An env key alone keeps the env provider — only a key saved in Settings flips it.
    assert settings_service.get_email_provider() == "console"
    settings_service.set_secret_setting("resend_api_key", "re_from_settings")
    assert settings_service.get_resend_api_key() == "re_from_settings"


def test_gemini_key_is_encrypted_at_rest_and_resolved_settings_then_env(monkeypatch):
    monkeypatch.setattr(_live("config"), "GEMINI_API_KEY", "AIza-from-env")
    assert settings_service.get_gemini_api_key() == "AIza-from-env"

    r = _client().put("/api/settings/api-keys/gemini", json={"api_key": "AIza-from-settings"})
    assert r.status_code == 200
    assert db.get_setting_value("google_api_key", None) is None
    assert looks_like_envelope(db.get_setting_value(encrypted_key_for("google_api_key"), None))
    assert settings_service.get_gemini_api_key() == "AIza-from-settings"

    r = _client().delete("/api/settings/api-keys/gemini")
    assert r.json() == {"success": True, "deleted": True, "fallback_configured": True}
    assert settings_service.get_gemini_api_key() == "AIza-from-env"


def test_a_saved_gemini_key_lights_the_voice_flags_without_a_restart(monkeypatch):
    monkeypatch.setattr(_live("config"), "VOICE_ENABLED", True)
    client = _client()
    assert client.get("/api/settings/feature-flags").json()["voice_available"] is False
    client.put("/api/settings/api-keys/gemini", json={"api_key": "AIza-live"})
    assert client.get("/api/settings/feature-flags").json()["voice_available"] is True


def test_email_is_sent_with_the_stored_key_and_sender(monkeypatch):
    import httpx
    from services.email_service import EmailService

    settings_service.set_secret_setting("resend_api_key", "re_stored")
    settings_service.set_email_from_address("codes@acme.test")
    sent = {}

    class _Post:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update(url=url, json=json, headers=headers)
            return _Resp(200, {})

    monkeypatch.setattr(httpx, "AsyncClient", _Post)
    assert asyncio.run(EmailService().send_email("to@acme.test", "s", "b")) is True
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["headers"]["Authorization"] == "Bearer re_stored"
    assert sent["json"]["from"] == "codes@acme.test"


# ---------------------------------------------------------------------------
# Live checks (fake transport)
# ---------------------------------------------------------------------------

def _domains(*pairs):
    return {"data": [{"name": n, "status": s} for n, s in pairs]}


def test_resend_check_passes_a_verified_sender(monkeypatch):
    calls = []
    _fake_httpx(monkeypatch, _Resp(200, _domains(("acme.test", "verified"))), calls)
    out = asyncio.run(pks.check_resend_key("re_k", "noreply@acme.test"))
    assert out == {"valid": True, "verified_domains": ["acme.test"]}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer re_k"


def test_resend_check_refuses_an_unverified_sender_and_names_the_verified_ones(monkeypatch):
    _fake_httpx(monkeypatch, _Resp(200, _domains(("acme.test", "verified"), ("new.test", "pending"))))
    out = asyncio.run(pks.check_resend_key("re_k", "noreply@new.test"))
    assert out["valid"] is False
    assert "new.test" in out["error"] and "noreply@acme.test" in out["error"]


def test_resend_check_accepts_a_sending_only_key_with_a_warning(monkeypatch):
    _fake_httpx(monkeypatch, _Resp(401, {"name": "restricted_api_key"}))
    out = asyncio.run(pks.check_resend_key("re_k", "noreply@acme.test"))
    assert out["valid"] is True and out["sending_only"] and "acme.test" in out["warning"]


def test_resend_check_rejects_a_bad_key(monkeypatch):
    _fake_httpx(monkeypatch, _Resp(400, {"name": "validation_error"}))
    out = asyncio.run(pks.check_resend_key("re_bad", "noreply@acme.test"))
    assert out["valid"] is False and "resend.com/api-keys" in out["error"]


def test_resend_test_endpoint_checks_the_sender_in_force_when_none_is_given(monkeypatch):
    seen = {}

    async def fake_check(key, from_address):
        seen.update(key=key, from_address=from_address)
        return {"valid": True}

    monkeypatch.setattr(sr.platform_keys_service, "check_resend_key", fake_check)
    _client().post("/api/settings/api-keys/resend/test", json={"api_key": "re_k"})
    assert seen == {"key": "re_k", "from_address": "noreply@env.example.com"}


@pytest.mark.parametrize("status,valid,needle", [
    (200, True, None),
    (429, True, None),
    (400, False, "aistudio.google.com/apikey"),
    (403, False, "Generative Language API"),
])
def test_gemini_check_maps_provider_answers(monkeypatch, status, valid, needle):
    calls = []
    _fake_httpx(monkeypatch, _Resp(status, {}), calls)
    out = asyncio.run(pks.check_gemini_key("AIza-k"))
    assert out["valid"] is valid
    if needle:
        assert needle in out["error"]
    # The key rides a header, never the query string (access logs).
    assert calls[0][1]["headers"] == {"x-goog-api-key": "AIza-k"}
    assert "key" not in calls[0][1].get("params", {})


# ---------------------------------------------------------------------------
# Claude — claude_auth_configured + the first credential reaching the fleet
# ---------------------------------------------------------------------------

def _admin_user_id():
    from db_models import UserCreate
    user = db.get_user_by_username("ent582-admin")
    if not user:
        db.create_user(UserCreate(username="ent582-admin", role="admin", email="ent582@example.com"))
        user = db.get_user_by_username("ent582-admin")
    return user["id"]


def test_claude_auth_configured_is_true_with_a_subscription_and_no_api_key():
    assert settings_service.get_anthropic_api_key() == ""
    client = _client()
    assert client.get("/api/settings/feature-flags").json()["claude_auth_configured"] is False
    sub = db.create_subscription(name="ent582-sub", token="sk-ant-oat01-test", owner_id=_admin_user_id())
    try:
        assert subscription_service.is_claude_auth_configured() is True
        assert client.get("/api/settings/feature-flags").json()["claude_auth_configured"] is True
    finally:
        db.delete_subscription(sub.id)


# The fleet the first credential meets. The DB query is exercised for real
# against the conftest SQLite; names are prefixed because that DB is shared.
_P = "ent582-f-"


@pytest.fixture
def agent_rows():
    """Real agent_ownership + schedule_executions rows, removed afterwards."""
    from sqlalchemy import text
    live_db = _live("database").db
    engine = _live("db.engine").get_engine()
    _admin_user_id()
    owner = live_db.get_user_by_username("ent582-admin")
    sub = live_db.create_subscription(name="ent582-fleet-sub", token="sk-ant-oat01-fleet", owner_id=owner["id"])

    def agent(name, **kw):
        live_db.register_agent_owner(_P + name, owner["username"], **kw)
        return _P + name

    def execution(name, status):
        ex = live_db.create_task_execution(_P + name, "hello")
        live_db.update_execution_status(ex.id, status)

    for n in ("fresh", "stopped", "busy", "worked", "has-sub", "own-key", "gone"):
        agent(n)
    agent("ghost", is_ephemeral=True)
    execution("worked", "success")          # authenticated some other way
    execution("busy", "failed")             # a failure proves nothing
    live_db.create_task_execution(_P + "busy", "mid-run")   # stays `running`
    live_db.assign_subscription_to_agent(_P + "has-sub", sub.id)
    live_db.set_use_platform_api_key(_P + "own-key", False)
    live_db.delete_agent_ownership(_P + "gone")             # soft-deleted
    yield live_db
    live_db.delete_subscription(sub.id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM schedule_executions WHERE agent_name LIKE :p"), {"p": _P + "%"})
        conn.execute(text("DELETE FROM agent_ownership WHERE agent_name LIKE :p"), {"p": _P + "%"})


def test_candidates_come_from_db_rows_and_skip_agents_that_ever_succeeded(agent_rows):
    """(a) + (b): rows, not containers; a success, a subscription, an own key,
    a ghost or a soft-delete each take an agent out."""
    mine = [n for n in agent_rows.list_agents_awaiting_first_credential() if n.startswith(_P)]
    assert mine == [_P + "busy", _P + "fresh", _P + "stopped"]


def test_running_execution_is_seen_by_the_db(agent_rows):
    assert agent_rows.agent_has_running_execution(_P + "busy") is True
    assert agent_rows.agent_has_running_execution(_P + "worked") is False
    assert agent_rows.agent_has_running_execution(_P + "fresh") is False


@pytest.fixture
def fleet(monkeypatch):
    """The service over a fixed candidate list, with Docker and restarts faked."""
    live_db = _live("database").db
    ss = _live("services.subscription_service")
    ds = _live("services.docker_service")
    state = {"assigned": [], "restarted": [], "ss": ss}
    monkeypatch.setattr(live_db, "list_agents_awaiting_first_credential",
                        lambda: ["seeded", "stopped", "codex", "creating"])
    monkeypatch.setattr(ds, "agent_container_runtimes",
                        lambda: {"seeded": "claude-code", "codex": "codex"})  # stopped: no label
    monkeypatch.setattr(ds, "agent_container_states",
                        lambda: {"seeded": "running", "stopped": "stopped", "codex": "running"})

    def no_containers_list():
        raise AssertionError("list_all_agents_fast swallows Docker errors — must not be the source")

    monkeypatch.setattr(ds, "list_all_agents_fast", no_containers_list)
    monkeypatch.setattr(live_db, "assign_subscription_to_agent",
                        lambda n, s: state["assigned"].append((n, s)))

    async def fake_restart(names):
        state["restarted"].extend(names)

    monkeypatch.setattr(ss, "_restart_connected_agents", fake_restart)
    return state


def _run_connect(fleet, subscription_id=None):
    async def go():
        out = fleet["ss"].connect_agents_to_first_credential(subscription_id)
        await asyncio.sleep(0)  # let the background restart task run
        return out
    return asyncio.run(go())


def test_first_subscription_connects_claude_rows_and_restarts_only_running_containers(fleet):
    """(a) + (d): `creating` has a row but no container yet — assigned, not restarted."""
    assert _run_connect(fleet, "sub-1") == 3
    assert fleet["assigned"] == [("seeded", "sub-1"), ("stopped", "sub-1"), ("creating", "sub-1")]
    assert fleet["restarted"] == ["seeded"]


def test_first_api_key_restarts_the_same_agents_without_assigning(fleet):
    assert _run_connect(fleet, None) == 3
    assert fleet["assigned"] == []
    assert fleet["restarted"] == ["seeded"]


def test_docker_unreadable_still_connects_in_the_db_but_restarts_nothing(fleet, monkeypatch):
    ds = _live("services.docker_service")
    monkeypatch.setattr(ds, "agent_container_runtimes", lambda: None)
    monkeypatch.setattr(ds, "agent_container_states", lambda: None)
    assert _run_connect(fleet, "sub-1") == 4  # runtime unknown → claude-code (ent#403)
    assert fleet["restarted"] == []


def test_restart_skips_a_running_execution_and_an_already_current_env(monkeypatch):
    """(c): checked per agent at restart time, under the switch lock."""
    live_db = _live("database").db
    ss = _live("services.subscription_service")
    sas = _live("services.subscription_auto_switch")
    sas._reset_locks_for_test()
    restarted = []

    async def fake_restart_agent(name):
        restarted.append(name)
        return "success"

    monkeypatch.setattr(sas, "_restart_agent", fake_restart_agent)
    monkeypatch.setattr(live_db, "agent_has_running_execution", lambda n: n == "busy")
    monkeypatch.setattr(ss, "_auth_env_is_current", lambda n: n == "current")
    asyncio.run(ss._restart_connected_agents(["busy", "current", "idle"]))
    assert restarted == ["idle"]


def test_api_key_save_returns_an_int_count_only_for_the_first_credential(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "connect_agents_to_first_credential", lambda: calls.append(1) or 2)
    client = _client()

    monkeypatch.setattr(sr, "is_claude_auth_configured", lambda: False)
    r = client.put("/api/settings/api-keys/anthropic", json={"api_key": "sk-ant-api03-first"})
    assert r.status_code == 200 and r.json()["connected_agents"] == 2

    monkeypatch.setattr(sr, "is_claude_auth_configured", lambda: True)
    r = client.put("/api/settings/api-keys/anthropic", json={"api_key": "sk-ant-api03-second"})
    assert r.json()["connected_agents"] == 0
    assert calls == [1]


@pytest.mark.parametrize("first,expected", [(True, 3), (False, 0)])
def test_subscription_registration_returns_connected_agents_through_the_response_model(monkeypatch, first, expected):
    """(d): `response_model` would silently strip a field the model lacks."""
    connected = []
    ss = _live("services.subscription_service")  # the router imports it at call time
    monkeypatch.setattr(ss, "connect_agents_to_first_credential",
                        lambda sid: connected.append(sid) or 3)
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: not first)
    uid = _admin_user_id()
    monkeypatch.setattr(subs_router.db, "get_user_by_username", lambda u: {"id": uid})
    r = _client().post("/api/subscriptions", json={"name": "ent582-first", "token": "sk-ant-oat01-x"})
    try:
        assert r.status_code == 200
        body = r.json()
        assert body["connected_agents"] == expected and isinstance(body["connected_agents"], int)
        assert body["name"] == "ent582-first" and body["id"]
        assert connected == ([body["id"]] if first else [])
    finally:
        db.delete_subscription(r.json()["id"])


# ---------------------------------------------------------------------------
# Agents whose create straddles the save — the post-seed re-run
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_connect(monkeypatch):
    ss = _live("services.subscription_service")
    calls = []
    monkeypatch.setattr(ss, "connect_agents_to_first_credential", lambda sid=None: calls.append(sid) or 0)
    monkeypatch.setattr(ss, "select_subscription_for_new_agent", lambda: types.SimpleNamespace(id="sub-9"))
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: True)
    return ss, calls


def test_a_seed_pass_that_created_agents_reruns_the_connect(seed_connect):
    seed = _live("services.system_seed_service")
    _, calls = seed_connect
    seed._connect_seeded_agents({"action": "none"}, {"action": "created"})
    assert calls == ["sub-9"]


def test_a_seed_pass_that_created_nothing_or_has_no_credential_does_not(seed_connect, monkeypatch):
    seed = _live("services.system_seed_service")
    ss, calls = seed_connect
    seed._connect_seeded_agents({"action": "none"}, {"action": "skipped"})
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: False)
    seed._connect_seeded_agents({"action": "created"}, None)
    assert calls == []


def test_the_rerun_uses_the_platform_key_when_there_is_no_subscription(seed_connect, monkeypatch):
    seed = _live("services.system_seed_service")
    ss, calls = seed_connect
    monkeypatch.setattr(ss, "select_subscription_for_new_agent", lambda: None)
    seed._connect_seeded_agents({"action": "created"}, {"action": "none"})
    assert calls == [None]


# ---------------------------------------------------------------------------
# Gemini-runtime agents get the saved key at create
# ---------------------------------------------------------------------------

def test_gemini_runtime_agent_gets_the_saved_key_then_the_env(monkeypatch):
    from services.agent_service import crud
    monkeypatch.setenv("OTEL_ENABLED", "0")
    cfg = types.SimpleNamespace(runtime="gemini-cli")

    env = {}
    crud._apply_gemini_and_otel_env(cfg, env)
    assert "GEMINI_API_KEY" not in env

    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-env-only")
    monkeypatch.setattr(_live("config"), "GEMINI_API_KEY", "AIza-env-only")  # config coalesces at import
    env = {}
    crud._apply_gemini_and_otel_env(cfg, env)
    assert env["GEMINI_API_KEY"] == "AIza-env-only"

    assert _client().put("/api/settings/api-keys/gemini", json={"api_key": "AIza-saved"}).status_code == 200
    env = {}
    crud._apply_gemini_and_otel_env(cfg, env)
    assert env["GEMINI_API_KEY"] == "AIza-saved"

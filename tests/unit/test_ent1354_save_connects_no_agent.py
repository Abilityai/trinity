"""trinity-enterprise#1354 — a save made through the assistant's Claude card switches no agent.

The card saves through the same two routes as the Settings page:
`PUT /api/settings/api-keys/anthropic` (an API key) and `POST /api/subscriptions`
(a subscription token). On an instance that held no Claude credential, those
routes connect the agents waiting for one (ent#582), and the subscription route
also runs the #2572 sweep for credential-less agents. Both move an existing
agent onto the credential, and restart it, without the person being asked.

The ruling (2026-11-24, under the operator's "confirm with user, and do that if
confirmed"): through the card, no existing agent is switched without a yes. The
card's save passes `connect_agents=false`; then neither the connect nor the
sweep runs, and `connected_agents` is 0. Without the flag the Settings page
keeps its own behaviour, which this file also pins.

Drives the real routers (the shipped `routers.settings` package and
`routers.subscriptions`) in a FastAPI app over the conftest's throwaway SQLite;
only the connect and the sweep are recorded instead of run.
"""
from __future__ import annotations

import importlib
import os
import types

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

pytestmark = pytest.mark.unit


def _live(name):
    # Other harnesses re-import these modules; resolve the one the routes read now.
    return importlib.import_module(name)


def _admin():
    return types.SimpleNamespace(id=1, username="ent1354-admin", role="admin", email="ent1354@example.com",
                                 agent_name=None, connector_agent=None, mcp_scope=None)


@pytest.fixture()
def client(monkeypatch):
    sr = _live("routers.settings")
    subs = _live("routers.subscriptions")
    deps = _live("dependencies")
    db = _live("database").db
    from db_models import UserCreate

    if not db.get_user_by_username("ent1354-admin"):
        db.create_user(UserCreate(username="ent1354-admin", role="admin", email="ent1354@example.com"))
    uid = db.get_user_by_username("ent1354-admin")["id"]
    monkeypatch.setattr(subs.db, "get_user_by_username", lambda u: {"id": uid})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls = []
    ss = _live("services.subscription_service")
    # The instance holds no Claude credential: both saves are its first.
    monkeypatch.setattr(ss, "is_claude_auth_configured", lambda: False)
    monkeypatch.setattr(sr.credentials, "is_claude_auth_configured", lambda: False)
    monkeypatch.setattr(ss, "connect_agents_to_first_credential", lambda sid=None: calls.append(("connect", sid)) or 2)
    monkeypatch.setattr(sr.credentials, "connect_agents_to_first_credential",
                        lambda: calls.append(("connect", None)) or 2)

    async def adopt(**kw):
        calls.append(("adopt", None))
        return {"waiting": "primary"}

    monkeypatch.setattr(ss, "adopt_for_credentialless_agents", adopt)

    app = FastAPI()
    app.include_router(sr.router)
    app.include_router(subs.router)
    # The gate each router imported (another harness may have re-imported `dependencies`).
    for gate in {deps.get_current_user, subs.get_current_user, sr.credentials.get_current_user}:
        app.dependency_overrides[gate] = _admin
    made = []
    with TestClient(app) as c:
        yield types.SimpleNamespace(http=c, calls=calls, made=made)
    for sid in made:
        db.delete_subscription(sid)
    db.delete_setting("anthropic_api_key")
    db.delete_setting("anthropic_api_key_encrypted")


def _register(client, query=""):
    r = client.http.post(f"/api/subscriptions{query}", json={"name": "ent1354-card", "token": "sk-ant-oat01-x"})
    assert r.status_code == 200, r.text
    client.made.append(r.json()["id"])
    return r.json()


def test_the_cards_subscription_save_connects_and_adopts_no_agent(client):
    body = _register(client, "?connect_agents=false")
    assert body["connected_agents"] == 0
    assert client.calls == []


def test_the_cards_api_key_save_connects_no_agent(client):
    r = client.http.put("/api/settings/api-keys/anthropic?connect_agents=false", json={"api_key": "sk-ant-api03-card"})
    assert r.status_code == 200, r.text
    assert r.json()["connected_agents"] == 0
    assert client.calls == []


def test_the_settings_page_still_connects_the_waiting_agents(client):
    """No flag: the Settings page's first credential reaches the waiting agents, as before."""
    body = _register(client)
    assert body["connected_agents"] == 2
    assert [c[0] for c in client.calls] == ["connect", "adopt"]
    client.calls.clear()
    r = client.http.put("/api/settings/api-keys/anthropic", json={"api_key": "sk-ant-api03-settings"})
    assert r.json()["connected_agents"] == 2 and client.calls == [("connect", None)]

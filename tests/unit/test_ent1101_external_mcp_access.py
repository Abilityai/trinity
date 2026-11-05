"""External MCP access switch — backend half (abilityai/trinity-enterprise#1101).

The instance-wide `mcp_external_access` setting decides whether an off-box
client may reach `/mcp`. The backend is the sole resolver (Settings row →
`MCP_EXTERNAL_ACCESS` env opt-in → default OFF); the MCP server reads the
verdict from `GET /api/internal/mcp-external-access`.

Covered here: resolver precedence and source, the Settings routes mounted on
the real settings router (any-auth read, admin write, agent-key refusal, the
generic catch-all refusing the key), the internal route behind the shared
secret, the feature-flags field, the advertised default URL, and
`ConnectorStatus.external_access`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.settings as rs  # noqa: E402  the composed router
from database import db  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from routers.settings import flags, mcp_url  # noqa: E402
import routers.connector as connector  # noqa: E402
import routers.internal as internal  # noqa: E402
from services import settings_service as settings_mod  # noqa: E402
from services.platform_audit_service import platform_audit_service  # noqa: E402

pytestmark = pytest.mark.unit

KEY = "mcp_external_access"


@pytest.fixture()
def store(monkeypatch):
    """An in-memory system_settings table behind the shared `db` object."""
    rows: dict = {}
    monkeypatch.setattr(db, "get_setting_value", lambda key, default=None: rows.get(key, default))

    def _set(key, value, *a, **kw):
        rows[key] = value

    def _delete(key, *a, **kw):
        return rows.pop(key, None) is not None

    monkeypatch.setattr(db, "set_setting", _set)
    monkeypatch.setattr(db, "delete_setting", _delete)
    monkeypatch.delenv("MCP_EXTERNAL_ACCESS", raising=False)
    monkeypatch.delenv("MCP_BIND_ADDRESS", raising=False)
    return rows


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class TestResolver:
    def _resolve(self):
        return settings_mod.is_mcp_external_access_enabled()

    def test_default_is_off(self, store):
        assert self._resolve() == (False, "default")

    @pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
    def test_env_opt_in_turns_it_on(self, store, monkeypatch, value):
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", value)
        assert self._resolve() == (True, "environment")

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
    def test_env_is_opt_in_only(self, store, monkeypatch, value):
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", value)
        assert self._resolve() == (False, "default")

    def test_row_on_wins_over_absent_env(self, store):
        store[KEY] = "true"
        assert self._resolve() == (True, "settings")

    def test_row_off_wins_over_env_on(self, store, monkeypatch):
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", "true")
        store[KEY] = "false"
        assert self._resolve() == (False, "settings")

    def test_row_on_wins_over_env_off(self, store, monkeypatch):
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", "false")
        store[KEY] = "true"
        assert self._resolve() == (True, "settings")

    def test_settings_read_failure_falls_back_to_env(self, store, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "get_setting_value", _boom)
        assert self._resolve() == (False, "default")
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", "true")
        assert self._resolve() == (True, "environment")

    def test_env_value_is_reported_raw(self, store, monkeypatch):
        assert settings_mod.get_mcp_external_access_env_value() is None
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", "true")
        assert settings_mod.get_mcp_external_access_env_value() == "true"


# ---------------------------------------------------------------------------
# Settings routes — mounted on the REAL composed settings router
# ---------------------------------------------------------------------------

def _user(**kw):
    from models import User
    base = dict(id=1, username="admin", role="admin")
    base.update(kw)
    return User(**base)


@pytest.fixture()
def client_as(store, monkeypatch):
    audits: list = []

    async def _log(**kw):
        audits.append(kw)

    monkeypatch.setattr(platform_audit_service, "log", _log)

    def _make(user):
        app = FastAPI()
        app.include_router(rs.router)
        app.dependency_overrides[get_current_user] = lambda: user
        c = TestClient(app)
        c.audits = audits
        return c

    return _make


class TestSettingsRoutes:
    PATH = "/api/settings/mcp-external-access"

    def test_any_authenticated_user_reads_the_state(self, client_as, store):
        r = client_as(_user(role="user", username="u")).get(self.PATH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is False
        assert body["source"] == "default"
        assert body["env_value"] is None
        assert body["advertised_url"].endswith("/mcp")

    def test_payload_advertises_a_non_loopback_bind_consistently(self, client_as, store, monkeypatch):
        monkeypatch.setenv("MCP_BIND_ADDRESS", "203.0.113.7")
        body = client_as(_user(role="user", username="u")).get(self.PATH).json()
        assert body["raw_port_bind_address"] == "203.0.113.7"
        assert body["advertised_url"] == "http://203.0.113.7:8080/mcp"
        assert "raw_port_bind_address" in body

    def test_read_reports_env_value_and_source(self, client_as, store, monkeypatch):
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", "true")
        body = client_as(_user()).get(self.PATH).json()
        assert (body["enabled"], body["source"], body["env_value"]) == (True, "environment", "true")

    def test_admin_turns_it_on_and_off(self, client_as, store):
        c = client_as(_user())
        r = c.put(self.PATH, json={"enabled": True})
        assert r.status_code == 200, r.text
        assert store[KEY] == "true"
        assert (r.json()["enabled"], r.json()["source"]) == (True, "settings")
        r = c.put(self.PATH, json={"enabled": False})
        assert store[KEY] == "false"
        assert r.json()["enabled"] is False
        assert [a["details"]["setting"] for a in c.audits] == [KEY, KEY]

    def test_put_requires_a_boolean(self, client_as, store):
        r = client_as(_user()).put(self.PATH, json={"enabled": "sometimes"})
        assert r.status_code == 422
        assert KEY not in store

    def test_delete_clears_the_row(self, client_as, store, monkeypatch):
        store[KEY] = "false"
        monkeypatch.setenv("MCP_EXTERNAL_ACCESS", "true")
        r = client_as(_user()).delete(self.PATH)
        assert r.status_code == 200, r.text
        assert KEY not in store
        assert (r.json()["enabled"], r.json()["source"]) == (True, "environment")

    @pytest.mark.parametrize("verb", ["put", "delete"])
    def test_non_admin_is_refused(self, client_as, store, verb):
        c = client_as(_user(role="creator", username="c"))
        r = getattr(c, verb)(self.PATH, **({"json": {"enabled": True}} if verb == "put" else {}))
        assert r.status_code == 403
        assert KEY not in store

    @pytest.mark.parametrize("verb", ["put", "delete"])
    def test_admin_owned_agent_key_is_refused(self, client_as, store, verb):
        store[KEY] = "false"
        c = client_as(_user(agent_name="helper", mcp_scope="agent"))
        r = getattr(c, verb)(self.PATH, **({"json": {"enabled": True}} if verb == "put" else {}))
        assert r.status_code == 403
        assert store[KEY] == "false"

    def test_generic_put_refuses_the_key(self, client_as, store):
        r = client_as(_user()).put(f"/api/settings/{KEY}", json={"value": "true"})
        assert r.status_code == 422
        assert "mcp-external-access" in r.json()["detail"]
        assert KEY not in store

    def test_feature_flags_carry_the_switch(self, client_as, store, monkeypatch):
        monkeypatch.setattr(flags, "get_gemini_api_key", lambda: "", raising=False)
        c = client_as(_user(role="user", username="u"))
        r = c.get("/api/settings/feature-flags")
        assert r.status_code == 200, r.text
        assert r.json()["mcp_external_access_enabled"] is False
        store[KEY] = "true"
        assert c.get("/api/settings/feature-flags").json()["mcp_external_access_enabled"] is True


# ---------------------------------------------------------------------------
# Internal route — the MCP server's read, behind the shared secret
# ---------------------------------------------------------------------------

class TestInternalRoute:
    @pytest.fixture()
    def client(self, store, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_SECRET", "unit-secret")
        app = FastAPI()
        app.include_router(internal.router)
        return TestClient(app)

    def test_without_the_secret_is_refused(self, client):
        assert client.get("/api/internal/mcp-external-access").status_code == 403
        r = client.get("/api/internal/mcp-external-access", headers={"X-Internal-Secret": "wrong"})
        assert r.status_code == 403

    def test_with_the_secret_returns_the_verdict(self, client, store):
        h = {"X-Internal-Secret": "unit-secret"}
        assert client.get("/api/internal/mcp-external-access", headers=h).json() == {
            "enabled": False, "source": "default"}
        store[KEY] = "true"
        assert client.get("/api/internal/mcp-external-access", headers=h).json() == {
            "enabled": True, "source": "settings"}


# ---------------------------------------------------------------------------
# Advertised default URL
# ---------------------------------------------------------------------------

def _request(url="http://trinity.test/api/x", **headers):
    scheme, rest = url.split("://", 1)
    netloc = rest.split("/", 1)[0]
    return SimpleNamespace(
        url=SimpleNamespace(scheme=scheme, netloc=netloc),
        headers={"host": netloc,
                 **{k.replace("_", "-").lower(): v for k, v in headers.items()}},
    )


class TestDefaultMcpUrl:
    @pytest.mark.parametrize("bind", [None, "127.0.0.1"])
    @pytest.mark.parametrize("proto, expected", [
        ("https", "https://trinity.test/mcp"),
        # Through nginx over plain http: nginx serves /mcp on the same origin.
        ("http", "http://trinity.test/mcp"),
    ])
    def test_forwarded_request_advertises_the_same_origin(self, monkeypatch, bind, proto, expected):
        if bind:
            monkeypatch.setenv("MCP_BIND_ADDRESS", bind)
        else:
            monkeypatch.delenv("MCP_BIND_ADDRESS", raising=False)
        assert mcp_url._get_default_mcp_url(_request(x_forwarded_proto=proto)) == expected

    @pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1", " 127.0.0.1 "])
    def test_loopback_bound_raw_port_is_advertised_as_host_only(self, monkeypatch, bind):
        monkeypatch.setenv("MCP_BIND_ADDRESS", bind)
        assert mcp_url._get_default_mcp_url(_request()) == "http://127.0.0.1:8080/mcp"

    @pytest.mark.parametrize("bind, expected", [
        # A LAN / VPN address: the raw port answers there, so that is the URL.
        ("203.0.113.7", "http://203.0.113.7:8080/mcp"),
        ("trinity-vpn.example.com", "http://trinity-vpn.example.com:8080/mcp"),
        # An IPv6 literal needs brackets to be a URL at all.
        ("2001:db8::7", "http://[2001:db8::7]:8080/mcp"),
    ])
    def test_specific_non_loopback_bind_is_advertised_as_itself(self, monkeypatch, bind, expected):
        monkeypatch.setenv("MCP_BIND_ADDRESS", bind)
        assert mcp_url._get_default_mcp_url(_request()) == expected

    @pytest.mark.parametrize("bind", [None, "", "0.0.0.0", "::"])
    def test_unset_bind_keeps_todays_raw_port_url(self, monkeypatch, bind):
        if bind is None:
            monkeypatch.delenv("MCP_BIND_ADDRESS", raising=False)
        else:
            monkeypatch.setenv("MCP_BIND_ADDRESS", bind)
        assert mcp_url._get_default_mcp_url(_request()) == "http://trinity.test:8080/mcp"


# ---------------------------------------------------------------------------
# ConnectorStatus.external_access
# ---------------------------------------------------------------------------

class TestConnectorStatus:
    @pytest.mark.parametrize("row, expected", [
        (None, {"enabled": False, "source": "default"}),
        ("true", {"enabled": True, "source": "settings"}),
    ])
    def test_status_carries_the_switch(self, store, monkeypatch, row, expected):
        if row is not None:
            store[KEY] = row
        monkeypatch.setattr(connector.db, "get_connector_config", lambda name: None, raising=False)
        monkeypatch.setattr(connector.db, "get_connector_key_prefix", lambda name: None, raising=False)
        status = connector._status("alpha", _request(x_forwarded_proto="https"))
        assert status.external_access.model_dump() == expected

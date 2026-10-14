"""AAuth graduation, PR1 — the flag split and the admin settings surface
(abilityai/trinity-enterprise#862).

Two switches now:

* ``aauth_enabled`` (``AAUTH_ENABLED``) — the graduated agent identity, Flows
  1–4: the well-known documents, outbound signing, inbound verification and the
  card / 401 advertisement. ``config.active_issuer()`` is its single reader.
* ``aauth_prototype_enabled`` (``AAUTH_PROTOTYPE_ENABLED``) — narrowed to the
  person-token and resource-calling prototypes (Flows 5–6), which require BOTH
  switches, gated once at ``aauth_person.require_live``.

The settings route is a grant surface (turning it on publishes this instance's
key and accepts signed callers from other instances), so its write is admin AND
interactive: an MCP key of any scope — agent, user, system — is refused.

Sync throughout (`tests/unit/pytest.ini` overrides `asyncio_mode = auto`).
"""
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import database
import routers.settings as rs
from dependencies import get_current_user
from services.aauth import config

ISSUER = "https://trinity-a.example.com"


class _Store:
    """`system_settings` in memory, installed on the shared `db` facade."""

    def __init__(self):
        self.rows = {}

    def get_setting_value(self, key, default=None):
        return self.rows.get(key, default)

    def set_setting(self, key, value):
        self.rows[key] = value

    def delete_setting(self, key):
        return self.rows.pop(key, None) is not None


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    for name in ("get_setting_value", "set_setting", "delete_setting"):
        monkeypatch.setattr(database.db, name, getattr(s, name))
    for var in ("AAUTH_ENABLED", "AAUTH_PROTOTYPE_ENABLED", "AAUTH_ISSUER",
                "AAUTH_PERSON_SERVER", "AAUTH_RESOURCE_ALLOWLIST"):
        monkeypatch.delenv(var, raising=False)
    return s


# ---------------------------------------------------------------------------
# Flag split (N3)
# ---------------------------------------------------------------------------

def test_aauth_is_off_by_default(store):
    assert config.is_enabled() is False
    assert config.active_issuer() is None


def test_the_prototype_flag_alone_no_longer_turns_on_flows_1_to_4(store):
    store.rows.update({"aauth_prototype_enabled": "true", "aauth_issuer": ISSUER})
    assert config.active_issuer() is None
    assert config.prototype_active_issuer() is None


def test_the_graduated_flag_turns_on_flows_1_to_4_only(store):
    store.rows.update({"aauth_enabled": "true", "aauth_issuer": ISSUER})
    assert config.active_issuer() == ISSUER
    assert config.prototype_active_issuer() is None


def test_flows_5_and_6_need_both_switches(store):
    store.rows.update({
        "aauth_enabled": "true", "aauth_prototype_enabled": "true", "aauth_issuer": ISSUER,
    })
    assert config.prototype_active_issuer() == ISSUER


def test_the_env_var_turns_it_on_and_a_stored_row_overrides_it(store, monkeypatch):
    monkeypatch.setenv("AAUTH_ENABLED", "true")
    monkeypatch.setenv("AAUTH_ISSUER", ISSUER)
    assert config.active_issuer() == ISSUER
    store.rows["aauth_enabled"] = "false"  # the admin's kill switch beats env
    assert config.active_issuer() is None


@pytest.mark.parametrize("graduated,prototype,live", [
    (False, False, False), (True, False, False), (False, True, False), (True, True, True),
])
def test_require_live_gates_the_prototype_routes_on_both_flags(store, graduated, prototype, live):
    """Every Flow 5/6 route depends on `require_live`; it is the one gate."""
    from routers import aauth_person

    store.rows["aauth_issuer"] = ISSUER
    store.rows["aauth_enabled"] = "true" if graduated else "false"
    store.rows["aauth_prototype_enabled"] = "true" if prototype else "false"
    if live:
        aauth_person.require_live()
    else:
        with pytest.raises(HTTPException) as exc:
            aauth_person.require_live()
        assert exc.value.status_code == 404


def test_the_resource_call_route_takes_the_same_gate():
    from routers import aauth_person, aauth_resource

    route = next(r for r in aauth_resource.router.routes if r.path.endswith("/aauth/resource-call"))
    assert aauth_person.require_live in [d.call for d in route.dependant.dependencies]


# ---------------------------------------------------------------------------
# GET/PUT /api/settings/aauth
# ---------------------------------------------------------------------------

def _principal(role="admin", agent_name=None, connector_agent=None, mcp_scope=None):
    return types.SimpleNamespace(
        id=1, username="admin", email="admin@example.com", role=role,
        agent_name=agent_name, connector_agent=connector_agent, mcp_scope=mcp_scope,
        vouched_source_agent=None,
    )


@pytest.fixture
def api(store, monkeypatch):
    import routers.settings.aauth as rs_aauth
    from services import a2a_outbound, a2a_outbound_service

    app = FastAPI()
    app.include_router(rs.router)
    who = {"user": _principal()}
    app.dependency_overrides[get_current_user] = lambda: who["user"]

    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(rs_aauth.platform_audit_service, "log", audit)
    endpoints = [
        {"id": "1", "name": "peer-a", "url": "https://a.example.com/a2a/x",
         "has_credentials": False, "auth_scheme": "aauth"},
        {"id": "2", "name": "vendor", "url": "https://v.example.com/a2a",
         "has_credentials": True, "auth_scheme": "bearer"},
    ]
    monkeypatch.setattr(a2a_outbound, "list_oss_endpoints", lambda: endpoints)
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: True)

    e = types.SimpleNamespace()
    e.client = TestClient(app, raise_server_exceptions=False)
    e.store = store
    e.audit = audit
    e.as_ = lambda p: who.__setitem__("user", p)
    return e


def test_get_reports_state_sources_and_readiness(api, monkeypatch):
    monkeypatch.setenv("AAUTH_ISSUER", ISSUER)
    api.store.rows["aauth_enabled"] = "true"

    body = api.client.get("/api/settings/aauth").json()

    assert body["enabled"] is True and body["enabled_source"] == "settings"
    assert body["issuer"] == ISSUER and body["issuer_source"] == "env"
    assert body["issuer_valid"] is True
    assert body["live"] is True
    assert body["identity_example"] == "aauth:<agent-name>@trinity-a.example.com"
    assert body["prototype_enabled"] is False
    assert body["readiness"] == {
        "issuer_valid": True,
        "outbound_enabled": True,
        "endpoints": 2,
        "aauth_endpoints": 1,
    }


def test_get_is_honest_about_an_inert_switch(api):
    """On without a valid issuer is OFF in effect — the panel must say so."""
    api.store.rows.update({"aauth_enabled": "true", "aauth_issuer": "http://nope"})

    body = api.client.get("/api/settings/aauth").json()

    assert body["enabled"] is True
    assert body["issuer_valid"] is False
    assert body["live"] is False


def test_put_writes_only_what_it_was_sent(api):
    resp = api.client.put("/api/settings/aauth", json={"enabled": True})

    assert resp.status_code == 200, resp.text
    assert api.store.rows == {"aauth_enabled": "true"}
    assert resp.json()["enabled"] is True


def test_put_sets_a_valid_issuer_and_audits_the_change(api):
    resp = api.client.put("/api/settings/aauth", json={"issuer": ISSUER})

    assert resp.status_code == 200, resp.text
    assert api.store.rows == {"aauth_issuer": ISSUER}
    api.audit.assert_awaited_once()
    kwargs = api.audit.await_args.kwargs
    assert kwargs["event_action"] == "aauth_config_change"
    assert kwargs["details"]["changes"] == {"issuer": {"from": None, "to": ISSUER}}


@pytest.mark.parametrize("bad", [
    "http://trinity-a.example.com",          # not https
    "https://trinity-a.example.com:8443",   # port
    "https://trinity-a.example.com/x",      # path
    "https://Trinity-A.example.com",        # not lowercase — never repaired
    "https://10.0.0.5",                     # IP literal
])
def test_put_refuses_an_invalid_issuer_by_name(api, bad):
    resp = api.client.put("/api/settings/aauth", json={"issuer": bad})

    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_issuer"
    assert api.store.rows == {}


def test_put_with_an_empty_issuer_clears_the_stored_one(api):
    api.store.rows["aauth_issuer"] = ISSUER
    resp = api.client.put("/api/settings/aauth", json={"issuer": ""})

    assert resp.status_code == 200, resp.text
    assert "aauth_issuer" not in api.store.rows


def test_put_toggles_the_prototype_switch(api):
    resp = api.client.put("/api/settings/aauth", json={"prototype_enabled": True})

    assert resp.status_code == 200, resp.text
    assert api.store.rows == {"aauth_prototype_enabled": "true"}


def test_put_sets_and_clears_the_person_server(api):
    resp = api.client.put("/api/settings/aauth", json={"person_server": "https://person.example.com"})
    assert resp.status_code == 200, resp.text
    assert api.store.rows == {"aauth_person_server": "https://person.example.com"}
    assert resp.json()["person_server"] == "https://person.example.com"
    assert resp.json()["person_server_source"] == "settings"

    resp = api.client.put("/api/settings/aauth", json={"person_server": ""})
    assert resp.status_code == 200, resp.text
    assert api.store.rows == {}


def test_put_refuses_an_invalid_person_server_by_name(api):
    resp = api.client.put("/api/settings/aauth", json={"person_server": "http://person.example.com"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_person_server"
    assert api.store.rows == {}


def test_put_sets_a_resource_allowlist_that_parses(api):
    raw = "https://senzing.example.com, https://whoami.example.com"
    resp = api.client.put("/api/settings/aauth", json={"resource_allowlist": raw})
    assert resp.status_code == 200, resp.text
    assert api.store.rows == {"aauth_resource_allowlist": raw}
    body = resp.json()
    assert body["resource_allowlist"] == raw
    assert body["resource_allowlist_count"] == 2
    assert body["resource_allowlist_source"] == "settings"


@pytest.mark.parametrize("raw", [
    "https://ok.example.com, http://bad.example.com",   # one bad entry empties the list
    "[not json",
    "https://ok.example.com:8443",
])
def test_put_refuses_a_resource_allowlist_the_sink_would_read_as_nothing(api, raw):
    """`parse_allowlist` fails closed to () — storing such a value would silently
    allow nothing while the row looks configured."""
    resp = api.client.put("/api/settings/aauth", json={"resource_allowlist": raw})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_resource_allowlist"
    assert api.store.rows == {}


def test_put_with_an_empty_resource_allowlist_clears_it(api):
    api.store.rows["aauth_resource_allowlist"] = "https://ok.example.com"
    resp = api.client.put("/api/settings/aauth", json={"resource_allowlist": ""})
    assert resp.status_code == 200, resp.text
    assert api.store.rows == {}


def test_get_reports_the_prototype_knobs_with_their_sources(api, monkeypatch):
    monkeypatch.setenv("AAUTH_PERSON_SERVER", "https://person.example.com")
    body = api.client.get("/api/settings/aauth").json()
    assert body["person_server"] == "https://person.example.com"
    assert body["person_server_source"] == "env"
    assert body["resource_allowlist"] == "" and body["resource_allowlist_source"] == "default"
    assert body["resource_allowlist_count"] == 0


def test_put_with_nothing_to_change_is_refused(api):
    assert api.client.put("/api/settings/aauth", json={}).status_code == 400


def test_put_refuses_a_number_for_a_boolean(api):
    """StrictBool: `1` is not `true` (lax ints would store it)."""
    assert api.client.put("/api/settings/aauth", json={"enabled": 1}).status_code == 422
    assert api.store.rows == {}


@pytest.mark.parametrize("method", ["get", "put"])
def test_a_non_admin_is_refused(api, method):
    api.as_(_principal(role="user"))
    kw = {"json": {"enabled": True}} if method == "put" else {}
    assert getattr(api.client, method)("/api/settings/aauth", **kw).status_code == 403


@pytest.mark.parametrize("principal", [
    _principal(agent_name="scout", mcp_scope="agent"),   # admin-owned agent key
    _principal(mcp_scope="user"),                        # admin's own user-scoped key
    _principal(mcp_scope="system"),                      # trinity-system
])
def test_no_mcp_key_of_any_scope_may_change_it(api, principal):
    api.as_(principal)

    resp = api.client.put("/api/settings/aauth", json={"enabled": True})

    assert resp.status_code == 403
    assert api.store.rows == {}


# ---------------------------------------------------------------------------
# The generic catch-all is not a way around the dedicated route
# ---------------------------------------------------------------------------

# cso-diff-2026-10-13-ent862 F1: the two Flow 5–6 keys decide where a signed
# agent request may go and which person server mediates binding — the same
# grant class as the issuer, so the same door closes on them.
AAUTH_ROUTE_KEYS = [
    "aauth_enabled", "aauth_issuer", "aauth_prototype_enabled",
    "aauth_person_server", "aauth_resource_allowlist",
]


@pytest.mark.parametrize("key", AAUTH_ROUTE_KEYS)
def test_the_generic_put_refuses_the_aauth_switches(api, key):
    resp = api.client.put(f"/api/settings/{key}", json={"value": "true"})

    assert resp.status_code == 422
    assert "/api/settings/aauth" in resp.text
    assert api.store.rows == {}


@pytest.mark.parametrize("key", AAUTH_ROUTE_KEYS)
def test_the_generic_delete_refuses_the_aauth_switches(api, key):
    api.store.rows[key] = "true"

    resp = api.client.delete(f"/api/settings/{key}")

    assert resp.status_code == 422
    assert api.store.rows == {key: "true"}


# ---------------------------------------------------------------------------
# Feature flags and deployment wiring
# ---------------------------------------------------------------------------

def test_feature_flags_reports_whether_aauth_is_live(store, monkeypatch):
    import asyncio

    from routers.settings import flags

    monkeypatch.setattr(config, "active_issuer", lambda: ISSUER)
    assert asyncio.run(flags.get_public_feature_flags(current_user=None))["aauth_available"] is True
    monkeypatch.setattr(config, "active_issuer", lambda: None)
    assert asyncio.run(flags.get_public_feature_flags(current_user=None))["aauth_available"] is False


@pytest.mark.parametrize("compose_file,expected", [
    ("docker-compose.yml", "AAUTH_ENABLED=${AAUTH_ENABLED:-}"),
    ("docker-compose.prod.yml", "AAUTH_ENABLED=${AAUTH_ENABLED:-}"),
    ("docker-compose.hosted.yml", "AAUTH_ENABLED=${AAUTH_ENABLED:-}"),
    # The two-instance harness turns it on outright, as it does the prototype flag.
    ("scripts/dev/aauth_prototype/compose.b.yml", "AAUTH_ENABLED=true"),
])
def test_every_backend_compose_file_forwards_the_flag_with_no_default(compose_file, expected):
    """A flag absent from `environment:` is un-switchable (learnings 2026-07-30),
    and a compose default would make "env" the reported source on every install
    (learnings 2026-09-29). The live consumer of these files is compose itself."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    doc = yaml.safe_load((root / compose_file).read_text())
    forwarded = []
    for svc in doc.get("services", {}).values():
        env = svc.get("environment") or []
        items = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
        forwarded += [i for i in items if str(i).startswith("AAUTH_ENABLED=")]
    assert forwarded == [expected]

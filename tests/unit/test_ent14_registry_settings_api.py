"""`/api/settings/template-registry` — the admin surface (ent#14).

The security-critical assertions are the PRINCIPAL MATRIX and the catch-all
blocklist, not the happy path.

`assert_admin` answers *what role*, never *is this a human*: `get_current_user`
resolves an agent-scoped MCP key to its owner CARRYING THE OWNER'S ROLE, so on a
default admin-owned install any agent's injected `TRINITY_MCP_API_KEY` satisfies
a bare admin gate (trinity-ops-agent#232, Invariant #8). The consequence here is
total — an agent could repoint the platform's template registry at a URL it
controls and every operator browsing templates would see its catalog.

Sync throughout (`tests/unit/pytest.ini` overrides `asyncio_mode = auto`).
"""
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.settings as rs  # the composed router
# #1028: the template-registry handlers live in the package's `templates`
# module; collaborator patches must land there (a patch on the package
# would raise — deliberately, so it cannot silently test nothing).
import routers.settings.templates as rs_templates
from dependencies import get_current_user

DEFAULT_URL = "https://raw.githubusercontent.com/Abilityai/trinity-templates/main/registry.yaml"


def _principal(role="admin", agent_name=None, connector_agent=None):
    return types.SimpleNamespace(
        id=1, username="admin", email="admin@example.com", role=role,
        agent_name=agent_name, connector_agent=connector_agent, mcp_scope=None,
    )


class _Settings:
    """Records writes so the tests can assert on intent, not on SQLite."""

    def __init__(self):
        self.rows = {}
        self.generation = 0
        self.lkg = {"anything": True}
        self.hard_disabled = False

    # --- the accessors the router uses -------------------------------------
    def get_setting(self, key, default=None):
        return self.rows.get(key, default)

    def get_template_registry_url(self):
        return self.rows.get("template_registry_url") or DEFAULT_URL

    def is_template_registry_enabled(self):
        if self.hard_disabled:
            return False
        raw = self.rows.get("template_registry_enabled")
        return True if raw is None else str(raw).lower() in ("1", "true", "yes", "on")

    def is_template_registry_hard_disabled(self):
        return self.hard_disabled

    def get_github_templates(self):
        return None

    def set_template_registry_config(self, *, url=None, enabled=None):
        if url is not None:
            self.rows["template_registry_url"] = url
        if enabled is not None:
            self.rows["template_registry_enabled"] = "true" if enabled else "false"
        self.generation += 1

    def delete_template_registry_config(self):
        removed = bool(
            self.rows.pop("template_registry_url", None) is not None
            or self.rows.pop("template_registry_enabled", None) is not None
        )
        self.lkg = None
        self.generation += 1
        return removed


@pytest.fixture
def env(monkeypatch):
    app = FastAPI()
    app.include_router(rs.router)
    principal = {"user": _principal()}
    app.dependency_overrides[get_current_user] = lambda: principal["user"]

    settings = _Settings()
    monkeypatch.setattr(rs_templates, "settings_service", settings)

    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(rs_templates.platform_audit_service, "log", audit)

    import services.template_registry_service as trs
    monkeypatch.setattr(
        trs, "get_registry_status",
        lambda: {
            "last_fetch_at": "2026-08-04T10:00:00Z", "last_status": "ok",
            "last_error_code": None, "template_count": 3, "stale": False, "errors": [],
        },
    )
    monkeypatch.setattr(trs, "invalidate_registry_cache", lambda: None)
    monkeypatch.setattr(
        "utils.url_validation.validate_template_registry_url", lambda u: u
    )

    class Env:
        pass

    e = Env()
    e.client = TestClient(app, raise_server_exceptions=False)
    e.settings = settings
    e.audit = audit
    e.as_ = lambda p: principal.__setitem__("user", p)
    yield e


# ---------------------------------------------------------------------------
# Principal matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_a_non_admin_is_refused(env, method):
    env.as_(_principal(role="user"))
    body = {"json": {"enabled": True}} if method == "put" else {}
    resp = getattr(env.client, method)("/api/settings/template-registry", **body)
    assert resp.status_code == 403


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_a_connector_key_is_refused(env, method):
    env.as_(_principal(connector_agent="some-agent"))
    body = {"json": {"enabled": True}} if method == "put" else {}
    resp = getattr(env.client, method)("/api/settings/template-registry", **body)
    assert resp.status_code == 403


def test_an_ADMIN_OWNED_AGENT_KEY_cannot_repoint_the_registry(env):
    """The whole reason `reject_agent_principal` is on the writes. This
    principal passes `assert_admin` — it carries its owner's admin role — and
    must still be refused."""
    env.as_(_principal(role="admin", agent_name="scout"))

    resp = env.client.put(
        "/api/settings/template-registry",
        json={"url": "https://evil.example.com/registry.yaml"},
    )
    assert resp.status_code == 403
    assert "human-only" in resp.json()["detail"]
    assert "template_registry_url" not in env.settings.rows


def test_an_admin_owned_agent_key_cannot_reset_the_registry(env):
    env.settings.rows["template_registry_url"] = "https://ours.example.com/r.yaml"
    env.as_(_principal(role="admin", agent_name="scout"))
    resp = env.client.delete("/api/settings/template-registry")
    assert resp.status_code == 403
    assert env.settings.rows["template_registry_url"] == "https://ours.example.com/r.yaml"


def test_an_agent_key_cannot_READ_either(env):
    """The READ is admin-gated too, and since #1890 `require_admin` itself
    rejects an agent principal — so the whole surface, not just the writes, is
    human-only.

    This assertion used to be `200`, on the reasoning that a read is harmless
    and the human check belongs on the writes. #1890 overruled that at the GATE
    rather than per-endpoint (third recurrence of the class: trinity-ops-agent
    #232, #1644, #1816), and it is the stronger call here: this GET returns the
    registry URL plus a live `status` block, which on a private/per-customer
    catalog is exactly the pointer an agent should not be able to enumerate.
    The explicit `reject_agent_principal` on PUT/DELETE is now belt-and-braces
    over the same gate, and is kept deliberately — it states the intent locally
    and survives any future relaxation of `require_admin`."""
    env.as_(_principal(role="admin", agent_name="scout"))
    assert env.client.get("/api/settings/template-registry").status_code == 403


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_get_reports_the_default_source(env):
    body = env.client.get("/api/settings/template-registry").json()
    assert body["source"] == "default"
    assert body["url"] == ""
    assert body["default_url"] == DEFAULT_URL
    assert body["effective_url"] == DEFAULT_URL
    assert body["enabled"] is True
    assert body["hard_disabled"] is False


def test_get_reports_an_override(env):
    env.settings.rows["template_registry_url"] = "https://ours.example.com/r.yaml"
    body = env.client.get("/api/settings/template-registry").json()
    assert body["source"] == "settings"
    assert body["effective_url"] == "https://ours.example.com/r.yaml"


def test_get_surfaces_the_status_block(env):
    """Fail-open makes every registry failure invisible in the catalog by
    design, so this is the ONLY place an operator can see a broken registry."""
    status = env.client.get("/api/settings/template-registry").json()["status"]
    assert set(status) >= {
        "last_fetch_at", "last_status", "last_error_code",
        "template_count", "stale", "errors",
    }


def test_get_reports_when_an_admin_list_suppresses_the_registry(env, monkeypatch):
    monkeypatch.setattr(env.settings, "get_github_templates", lambda: [{"github_repo": "a/b"}])
    assert env.client.get("/api/settings/template-registry").json()[
        "suppressed_by_github_templates"
    ] is True


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------

def test_put_stores_a_valid_url_and_audits(env):
    resp = env.client.put(
        "/api/settings/template-registry",
        json={"url": "https://ours.example.com/registry.yaml"},
    )
    assert resp.status_code == 200
    assert env.settings.rows["template_registry_url"] == "https://ours.example.com/registry.yaml"
    env.audit.assert_awaited()
    action = env.audit.await_args.kwargs["event_action"]
    assert action == "template_registry_config_change"
    assert env.audit.await_args.kwargs["details"]["url"]["new"].endswith("registry.yaml")


def test_put_bumps_the_generation_so_the_OTHER_worker_invalidates(env):
    before = env.settings.generation
    env.client.put("/api/settings/template-registry", json={"enabled": False})
    assert env.settings.generation > before


def test_put_rejects_an_invalid_url_with_the_validator_message(env, monkeypatch):
    def refuse(url):
        raise ValueError("Template registry URL must use HTTPS")

    monkeypatch.setattr("utils.url_validation.validate_template_registry_url", refuse)
    resp = env.client.put(
        "/api/settings/template-registry", json={"url": "http://evil.example.com/r.yaml"}
    )
    assert resp.status_code == 400
    assert "HTTPS" in resp.json()["detail"]
    assert "template_registry_url" not in env.settings.rows


def test_put_with_no_fields_is_a_400(env):
    assert env.client.put("/api/settings/template-registry", json={}).status_code == 400


def test_put_with_a_blank_url_points_at_DELETE(env):
    resp = env.client.put("/api/settings/template-registry", json={"url": "   "})
    assert resp.status_code == 400
    assert "DELETE" in resp.json()["detail"]


def test_put_is_partial_so_the_toggle_does_not_clear_the_url(env):
    env.settings.rows["template_registry_url"] = "https://ours.example.com/r.yaml"
    env.client.put("/api/settings/template-registry", json={"enabled": False})
    assert env.settings.rows["template_registry_url"] == "https://ours.example.com/r.yaml"
    assert env.settings.rows["template_registry_enabled"] == "false"


def test_the_config_hard_switch_cannot_be_overridden_by_an_admin(env):
    env.settings.hard_disabled = True
    resp = env.client.put("/api/settings/template-registry", json={"enabled": True})
    assert resp.status_code == 409
    assert "TEMPLATE_REGISTRY_ENABLED" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_reverts_to_the_config_default_and_audits(env):
    env.settings.rows["template_registry_url"] = "https://ours.example.com/r.yaml"
    resp = env.client.delete("/api/settings/template-registry")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert "template_registry_url" not in env.settings.rows
    assert env.audit.await_args.kwargs["event_action"] == "template_registry_config_reset"


def test_delete_drops_the_durable_last_known_good(env):
    """It was captured under the overridden URL; serving it after a reset would
    attribute one registry's catalog to another."""
    env.settings.rows["template_registry_url"] = "https://ours.example.com/r.yaml"
    env.client.delete("/api/settings/template-registry")
    assert env.settings.lkg is None


def test_delete_audits_even_when_nothing_was_stored(env):
    """Resetting an already-default registry is still an administrative act;
    its absence from the log would be indistinguishable from never having been
    attempted (the #1966 lesson, one route over)."""
    resp = env.client.delete("/api/settings/template-registry")
    assert resp.status_code == 200
    env.audit.assert_awaited()


# ---------------------------------------------------------------------------
# The catch-all blocklist — validate at the boundary AND at the sink (#1525)
# ---------------------------------------------------------------------------

REGISTRY_KEYS = [
    "template_registry_url",
    "template_registry_enabled",
    "template_registry_generation",
    "template_registry_lkg",
]


@pytest.mark.parametrize("key", REGISTRY_KEYS)
def test_the_generic_PUT_refuses_every_registry_key(env, key):
    """`PUT /api/settings/{key}` takes an unvalidated `Dict[str, str]` — without
    this the whole SSRF gate is one request away from being bypassed."""
    resp = env.client.put(f"/api/settings/{key}", json={"value": "http://evil/x"})
    assert resp.status_code == 422
    assert "/api/settings/template-registry" in resp.json()["detail"]


@pytest.mark.parametrize("key", REGISTRY_KEYS)
def test_the_generic_DELETE_refuses_every_registry_key(env, key):
    """Blocked here as well as on PUT, unlike the #1644 retention acks: deleting
    an ack re-arms a guard and fails safe, whereas deleting
    `template_registry_enabled` reverts it to its default of ON — re-enabling
    egress an operator deliberately switched off, through a route with no human
    gate."""
    resp = env.client.delete(f"/api/settings/{key}")
    assert resp.status_code == 422
    assert "/api/settings/template-registry" in resp.json()["detail"]


def test_an_unrelated_key_still_flows_through_the_catch_all(env, monkeypatch):
    """The blocklist must be a blocklist, not a wholesale closure of the route."""
    monkeypatch.setattr(rs_templates.db, "delete_setting", lambda key: True)
    assert env.client.delete("/api/settings/some_unrelated_key").status_code == 200


def test_the_registry_routes_are_registered_before_the_catch_all(env):
    """Invariant #4. If `/{key}` were registered first, `template-registry`
    would be swallowed as a setting NAME and every test above would be testing
    the wrong handler."""
    paths = [r.path for r in rs.router.routes]
    registry = [i for i, p in enumerate(paths) if "template-registry" in p]
    catchall = [i for i, p in enumerate(paths) if p.endswith("/{key}")]
    assert registry and catchall
    assert max(registry) < min(catchall)

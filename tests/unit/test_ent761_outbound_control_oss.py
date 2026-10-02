"""ent#761 — outbound A2A endpoint control is OSS-core, and says so.

The MCP control tools (`register_a2a_endpoint` / `list_a2a_endpoints` /
`remove_a2a_endpoint`) were repointed off the entitled per-agent routes onto the
three OSS settings routes, because the OSS store is the only one the call path
resolves against. This file holds the backend half of that claim:

* the settings package's `# mcp:` header now names the covering tool module, so
  `/validate-architecture` reads "exposed, deliberately" rather than "excluded,
  deliberately" for the three outbound routes (the rest of the package stays a
  human-only grant surface);
* the three handlers carry no entitlement gate — asserted by walking the AST
  rather than by reading the docs, because "OSS-core" is a property of the code;
* `PUT /api/settings/a2a-endpoints` reports the outbound switch state, so a
  caller that has just registered an endpoint learns in the same response
  whether it is callable (honest status — the switch defaults OFF);
* the write tier is admin **and** human-only, which is the grant-vs-use line:
  the registry is platform-wide, so a write grants every agent on the instance a
  credentialed egress target.

**Own FastAPI app on purpose.** `test_736_a2a_outbound_call.py::client` mounts
only `routers/a2a.py`, its principal is a `role="user"` stand-in with no
`mcp_scope`, and it registers a stub resolver provider — all three are wrong for
this file, which needs the settings router, a real admin principal and **no**
provider registered (that is the point of the AC1 case: the OSS store answers
with nothing registered).

Sync throughout with explicit `asyncio` via `TestClient`: `tests/unit/pytest.ini`
overrides `pyproject.toml`, so `asyncio_mode = auto` does not apply here.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import dependencies as deps  # noqa: E402
import models  # noqa: E402
import routers.settings as settings_pkg  # noqa: E402
import routers.settings.integrations as integrations  # noqa: E402
from services import a2a_outbound  # noqa: E402

_SETTINGS_PKG_INIT = Path(_BACKEND) / "routers" / "settings" / "__init__.py"
_INTEGRATIONS = Path(_BACKEND) / "routers" / "settings" / "integrations.py"

# The three handlers the ruling covers. Named by function, not by line, so the
# pins survive edits elsewhere in a 700-line module.
_OUTBOUND_HANDLERS = {
    "list_a2a_outbound_endpoints",
    "upsert_a2a_outbound_endpoint",
    "remove_a2a_outbound_endpoint",
}

# Reuse the real envelope round-trip over an in-memory settings row rather than a
# second copy of it; importing the fixture is what makes it one definition.
# Sibling import — the unit dir is not implicitly importable.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_736_a2a_outbound_edges import oss_store  # noqa: E402,F401


def _principal(**over) -> models.User:
    """A `models.User`, never a `SimpleNamespace`.

    The admin gate's scope allowlist fails CLOSED on a principal that does not
    declare `mcp_scope` (#2323), so a stand-in that is merely user-shaped is
    rejected for the wrong reason and would make every 403 below meaningless.
    """
    base = dict(
        id=1, username="admin", email="admin@example.com",
        role="admin", agent_name=None, mcp_scope=None,
    )
    base.update(over)
    return models.User(**base)


@pytest.fixture()
def app_client(monkeypatch):
    """The settings router under a TestClient, admin principal, audit stubbed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class _Audit:
        entries: list = []

        async def log(self, **kwargs):
            _Audit.entries.append(kwargs)
            return None

    _Audit.entries = []
    monkeypatch.setattr(integrations, "platform_audit_service", _Audit())

    app = FastAPI()
    app.include_router(settings_pkg.router)
    holder = {"user": _principal()}
    app.dependency_overrides[deps.get_current_user] = lambda: holder["user"]

    import types as _types
    return _types.SimpleNamespace(
        http=TestClient(app), holder=holder, audit=_Audit,
    )


# ---------------------------------------------------------------------------
# S7 — the `# mcp:` header is a pointer now, not an exclusion
# ---------------------------------------------------------------------------

def test_settings_package_header_names_the_covering_tool_module():
    """Invariant #13's discoverability contract.

    The header is how `/validate-architecture` tells "unexposed on purpose" from
    "forgotten". With the three outbound routes now driven by MCP tools, a flat
    `# mcp: none` is simply false — and false in the direction that hides a live
    external surface from the only audit that looks for it.
    """
    first = _SETTINGS_PKG_INIT.read_text().splitlines()[0]

    assert first.startswith("# mcp:"), (
        "line 1 of routers/settings/__init__.py must stay the `# mcp:` header — "
        "the existing pins match it with startswith()"
    )
    assert "a2a.ts" in first, (
        "the header must name the covering MCP tool module for the three "
        "outbound A2A endpoint routes"
    )
    assert "a2a-endpoints" in first, (
        "the header must name WHICH routes are exposed; the rest of this "
        "package is a human-only grant surface"
    )
    assert "none" not in first.split("—")[0], (
        "`# mcp: none` claims the whole package is unexposed, which is no "
        "longer true"
    )


# ---------------------------------------------------------------------------
# The handlers carry no entitlement gate (asserted over the AST, not the prose)
# ---------------------------------------------------------------------------

def _handler_nodes() -> dict:
    tree = ast.parse(_INTEGRATIONS.read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _OUTBOUND_HANDLERS
    }


def test_all_three_outbound_handlers_are_present():
    found = _handler_nodes()
    assert set(found) == _OUTBOUND_HANDLERS, (
        f"missing outbound A2A handler(s): {_OUTBOUND_HANDLERS - set(found)} — "
        "if one was renamed, rename it here too; the pins below are by name"
    )


@pytest.mark.parametrize("handler", sorted(_OUTBOUND_HANDLERS))
def test_outbound_handler_carries_no_entitlement_gate(handler):
    """The ruling, as a property of the code.

    An entitlement decorator or a service call anywhere in one of these three
    handlers would re-gate outbound control, which is exactly what ent#761
    removed. Named identifiers rather than a substring search over the file, so
    an unrelated mention in a docstring cannot make this pass or fail.
    """
    node = _handler_nodes()[handler]
    names = {
        n.id for n in ast.walk(node) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }
    for decorator in node.decorator_list:
        names |= {n.id for n in ast.walk(decorator) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(decorator) if isinstance(n, ast.Attribute)}

    forbidden = {"requires_entitlement", "entitlement_service", "require_entitlement"}
    assert not (names & forbidden), (
        f"{handler} references {names & forbidden}: outbound A2A endpoint "
        "control is OSS-core on every build (ent#761)"
    )


def test_integrations_module_never_imports_the_entitlement_service():
    """Module scope, not just the three handlers — an import here would mean the
    seam moved and somebody is about to gate one of them."""
    tree = ast.parse(_INTEGRATIONS.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {a.name for a in node.names}
    assert not {n for n in imported if "entitlement" in (n or "")}, (
        "routers/settings/integrations.py must not import the entitlement seam"
    )


@pytest.mark.parametrize("handler", sorted(_OUTBOUND_HANDLERS))
def test_outbound_handler_is_admin_and_human_only(handler):
    """Taste (b): the write tier is admin + human-only, unchanged.

    The registry is platform-wide, so registering an endpoint grants every agent
    on the instance a credentialed server-side egress target — a grant, not a
    use (Invariant #8). `assert_admin` rejects agent principals itself since
    #1890; the explicit `reject_agent_principal` beside it is belt-and-braces
    and pinned here so a cleanup pass does not read it as dead code.
    """
    node = _handler_nodes()[handler]
    called = {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "assert_admin" in called, f"{handler} lost its admin gate"
    assert "reject_agent_principal" in called, f"{handler} lost its human-only gate"


# ---------------------------------------------------------------------------
# AC1, at the unit level: register → list → resolve → remove, no provider
# ---------------------------------------------------------------------------

def test_put_response_reports_the_outbound_switch_state(app_client, oss_store):
    """Decision #17 — honest status in the same response as the write.

    The switch defaults OFF (§32.5 FR-11, deliberately unchanged), so an
    operator who registers an endpoint on a fresh install has a working-looking
    registration and a dead call path. The write answers the question it
    provokes; the MCP tool turns `enabled: false` into the one admin step that
    fixes it.
    """
    r = app_client.http.put(
        "/api/settings/a2a-endpoints",
        json={"name": "partner", "url": "https://peer.example.com/a2a"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert "enabled" in body, (
        "PUT /api/settings/a2a-endpoints must report the outbound switch state "
        "(decision #17) — the MCP tool surfaces it as `outbound_enabled`"
    )
    assert isinstance(body["enabled"], bool), body["enabled"]
    # The GET has reported it since #736; the two reads must not disagree.
    assert app_client.http.get("/api/settings/a2a-endpoints").json()["enabled"] == body["enabled"]


def test_put_reports_the_switch_as_on_when_an_admin_has_flipped_it(
    app_client, oss_store, monkeypatch
):
    """Both sides of the switch, so the field is proven to be read rather than
    hardcoded to the default."""
    from services import a2a_outbound_service

    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: True)
    r = app_client.http.put(
        "/api/settings/a2a-endpoints",
        json={"name": "partner", "url": "https://peer.example.com/a2a"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True


def test_register_list_resolve_remove_with_no_provider_registered(app_client, oss_store):
    """AC1's mechanism: the store the MCP tools write is the store the call path
    resolves, on a build with no resolver provider registered at all."""
    r = app_client.http.put(
        "/api/settings/a2a-endpoints",
        json={"name": "partner", "url": "https://peer.example.com/a2a"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["endpoint"]["name"] == "partner"

    listed = app_client.http.get("/api/settings/a2a-endpoints")
    assert listed.status_code == 200
    assert [e["name"] for e in listed.json()["endpoints"]] == ["partner"]

    # Platform scope: every agent on the instance resolves the same row.
    first = a2a_outbound.resolve_endpoint("agent-one", "partner")
    second = a2a_outbound.resolve_endpoint("agent-two", "partner")
    assert first is not None and second is not None
    assert (first.id, first.url) == (second.id, second.url)

    gone = app_client.http.delete("/api/settings/a2a-endpoints/partner")
    assert gone.status_code == 200, gone.text
    assert gone.json() == {"success": True, "removed": "partner"}
    assert a2a_outbound.resolve_endpoint("agent-one", "partner") is None


def test_remove_unknown_ref_is_a_named_404(app_client, oss_store):
    r = app_client.http.delete("/api/settings/a2a-endpoints/nope")
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


# ---------------------------------------------------------------------------
# E8 — the 403 detail texts the TypeScript mapper regexes
# ---------------------------------------------------------------------------

_HUMAN_ONLY = "This operation is human-only; agent-scoped keys cannot perform it"


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/settings/a2a-endpoints"),
    ("GET", "/api/settings/a2a-endpoints"),
    ("DELETE", "/api/settings/a2a-endpoints/partner"),
])
def test_agent_scoped_key_is_refused_with_the_mapped_detail(app_client, oss_store, method, path):
    """The MCP layer distinguishes `human_only` from `not_authorized` by matching
    this sentence, so it is a contract between the two surfaces, not prose."""
    app_client.holder["user"] = _principal(
        role="admin", agent_name="bot", mcp_scope="agent"
    )
    r = app_client.http.request(
        method, path,
        json={"name": "partner", "url": "https://peer.example.com/a2a"} if method == "PUT" else None,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _HUMAN_ONLY


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/settings/a2a-endpoints"),
    ("GET", "/api/settings/a2a-endpoints"),
    ("DELETE", "/api/settings/a2a-endpoints/partner"),
])
def test_non_admin_human_is_refused(app_client, oss_store, method, path):
    app_client.holder["user"] = _principal(role="user", username="bob")
    r = app_client.http.request(
        method, path,
        json={"name": "partner", "url": "https://peer.example.com/a2a"} if method == "PUT" else None,
    )
    assert r.status_code == 403, r.text

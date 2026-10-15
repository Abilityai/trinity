"""Controller-scoped MCP key (abilityai/trinity-enterprise#693).

A remote controller — fleet-facing automation that provisions and operates an
instance from outside it — had two wrong choices: an `agent` key (refused at
admin gates, bound to one container) or a `user` key (IS the person: admitted at
every admin gate on an admin owner, and every write recorded as that human).

`controller` is the fourth machine scope. This suite pins its three properties
through the REAL gates — the real `get_current_user` with a key row minted into
the real schema (`tests/db_harness.py`) and presented as a bearer, the real
`dependencies` gate functions, the real audit writer and execution writer:

1. reach: creator-tier, not admin-blanket (admin routes opt in per route);
2. attributable: audit and execution rows name the controller, not the owner;
3. refused on grant surfaces, with the named code `controller_grant_refused`.

Related: docs/memory/requirements/security.md §20.11,
docs/memory/architecture/security.md §6.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from db_harness import (  # noqa: E402,F401
    count as _count,
    db_backend,
    run as _hrun,
)

import dependencies  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "ctl-owner"      # the dedicated service account (admin)
CREATOR = "ctl-creator"  # a creator-role service account
PLAIN = "ctl-plain"      # an ordinary operator account

GRANT_CODE = "controller_grant_refused"
ADMIN_CODE = "controller_route_not_admitted"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@pytest.fixture
def accounts(db_backend, monkeypatch):
    # JWT revocation reads Redis; there is none here.
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(UserCreate(username=OWNER, role="admin", email="ctl-owner@example.com"))
    db.create_user(UserCreate(username=CREATOR, role="creator", email="ctl-creator@example.com"))
    db.create_user(UserCreate(username=PLAIN, role="operator", email="ctl-plain@example.com"))
    return db_backend


def _mint(owner: str = OWNER, scope: str = "controller", name: str = "remote-controller"):
    key = db.create_mcp_api_key(owner, McpApiKeyCreate(name=name, scope=scope))
    assert key is not None, f"minting a {scope!r} key returned None"
    return key


def _request(method: str, path: str) -> Request:
    return Request({
        "type": "http", "method": method, "path": path, "root_path": "",
        "path_params": {}, "query_string": b"", "scheme": "http",
        "server": ("backend", 8000), "headers": [(b"host", b"backend:8000")],
    })


def _resolve(token: str, method: str = "GET", path: str = "/api/agents") -> User:
    return asyncio.run(dependencies.get_current_user(_request(method, path), token=token))


def _refused(fn, *args, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    return exc.value


def _principal(**kw) -> User:
    """A REAL `models.User`, never a stand-in (the #2323 lesson)."""
    fields = {"id": 1, "username": OWNER, "email": "ctl-owner@example.com",
              "role": "admin", "mcp_scope": "controller",
              "mcp_key_id": "key-ctl-1", "mcp_key_name": "remote-controller"}
    fields.update(kw)
    return User(**fields)


# --------------------------------------------------------------------------- #
# The principal
# --------------------------------------------------------------------------- #

class TestPrincipal:
    def test_a_controller_key_resolves_as_a_controller_not_as_its_owner(self, accounts):
        key = _mint()
        user = _resolve(key.api_key)
        assert user.mcp_scope == "controller"
        assert user.username == OWNER and user.role == "admin"
        # Distinguishable from the owner's own key at every gate: the key identity
        # rides on the principal, and no agent / connector identity is set.
        assert user.mcp_key_id == key.id
        assert user.mcp_key_name == "remote-controller"
        assert user.agent_name is None and user.connector_agent is None

    def test_the_row_carries_no_agent_name(self, accounts):
        key = _mint()
        assert db.validate_mcp_api_key(key.api_key)["agent_name"] is None

    def test_suspending_the_owner_kills_the_key_at_the_entry_point(self, accounts):
        key = _mint()
        _hrun("UPDATE users SET suspended_at = '2026-10-15T00:00:00Z' WHERE username = :u", u=OWNER)
        exc = _refused(_resolve, key.api_key)
        assert exc.status_code == 401

    def test_it_is_not_a_person_and_not_interactive(self):
        p = _principal()
        assert dependencies.is_person_principal(p) is False
        assert dependencies.is_interactive_principal(p) is False
        assert dependencies.acting_agent_name(p) is None


# --------------------------------------------------------------------------- #
# Reach — creator-tier, not admin-blanket
# --------------------------------------------------------------------------- #

class TestReach:
    def test_creator_gate_admits_it(self):
        gate = dependencies.require_role("creator")
        assert gate(_principal()).mcp_scope == "controller"
        assert gate(_principal(role="creator")).mcp_scope == "controller"

    def test_creator_gate_follows_the_owner_role_down(self):
        """Demotion removes the reach: a controller is a narrowing of its owner."""
        exc = _refused(dependencies.require_role("creator"), _principal(role="operator"))
        assert exc.status_code == 403

    def test_admin_gates_refuse_it_by_default_with_a_named_code(self):
        for gate in (dependencies.require_admin,
                     lambda u: dependencies.assert_admin(u)):
            exc = _refused(gate, _principal())
            assert exc.status_code == 403
            assert isinstance(exc.detail, dict) and exc.detail["code"] == ADMIN_CODE

    def test_an_admin_route_admits_it_only_by_opting_in(self):
        dependencies.assert_admin(_principal(), allow_scopes=frozenset({"controller"}))
        gate = dependencies.require_admin_allowing("controller")
        assert gate(_principal()).mcp_scope == "controller"
        # The ops opt-in does not admit a controller, and vice versa.
        exc = _refused(dependencies.assert_admin, _principal(), allow_scopes=frozenset({"ops"}))
        assert exc.detail["code"] == ADMIN_CODE
        with pytest.raises(HTTPException):
            dependencies.assert_admin(_principal(mcp_scope="ops"),
                                      allow_scopes=frozenset({"controller"}))

    def test_an_opted_in_admin_route_still_needs_an_admin_owner(self):
        exc = _refused(dependencies.assert_admin, _principal(role="creator"),
                       allow_scopes=frozenset({"controller"}))
        assert exc.status_code == 403

    def test_it_passes_the_agent_capability_gates(self):
        for capability in ("skills.manage", "schedules.manage", "agents.manage",
                           "instructions.manage", "projects.manage"):
            assert dependencies.capability_refusal(_principal(), capability) is None

    def test_the_capability_fence_lets_a_deploy_through(self):
        fence = dependencies.capability_fence("agents.manage")
        req = _request("POST", "/api/systems/deploy")
        assert asyncio.run(fence(req, current_user=_principal())) is None

    def test_the_human_only_primitives_still_refuse_it(self):
        assert _refused(dependencies.assert_person, _principal()).status_code == 403
        assert _refused(dependencies.require_interactive, _principal()).status_code == 403
        assert dependencies.scope_may_open_event_stream("controller") is False


# --------------------------------------------------------------------------- #
# Refused on grant surfaces — at the auth entry point, with a named code
# --------------------------------------------------------------------------- #

REFUSED = [
    # key minting
    ("POST", "/api/mcp/keys", "key_minting"),
    ("GET", "/api/mcp/keys", "key_minting"),
    ("POST", "/api/mcp/keys/ensure-default", "key_minting"),
    ("POST", "/api/mcp/keys/k1/revoke", "key_minting"),
    ("DELETE", "/api/mcp/keys/k1", "key_minting"),
    ("GET", "/api/agents/a1/mcp-key", "key_minting"),
    ("POST", "/api/agents/a1/mcp-key/regenerate", "key_minting"),
    ("POST", "/api/agents/a1/mcp-key/verify", "key_minting"),
    # role / permission changes
    ("PUT", "/api/agents/a1/permissions", "permissions"),
    ("POST", "/api/agents/a1/permissions/a2", "permissions"),
    ("DELETE", "/api/agents/a1/permissions/a2", "permissions"),
    ("POST", "/api/agents/a1/share", "permissions"),
    ("DELETE", "/api/agents/a1/share/someone@example.com", "permissions"),
    ("PUT", "/api/agents/a1/capability-grants/skills.manage", "permissions"),
    ("PUT", "/api/users/bob/role", "permissions"),
    # credential values
    ("POST", "/api/agents/a1/credentials/inject", "credential_values"),
    ("POST", "/api/agents/a1/credentials/export", "credential_values"),
    ("POST", "/api/agents/a1/credentials/import", "credential_values"),
    ("GET", "/api/credentials/encryption-key", "credential_values"),
    ("PUT", "/api/agents/a1/github-pat", "credential_values"),
    ("DELETE", "/api/agents/a1/github-pat", "credential_values"),
    ("PUT", "/api/users/me/github-pat", "credential_values"),
    ("DELETE", "/api/users/me/github-pat", "credential_values"),
    ("POST", "/api/subscriptions", "credential_values"),
    ("POST", "/api/nevermined/agents/a1/config", "credential_values"),
    ("POST", "/api/agents/a1/schedules/s1/webhook/secret", "credential_values"),
    ("DELETE", "/api/agents/a1/schedules/s1/webhook/secret", "credential_values"),
    ("DELETE", "/api/nevermined/agents/a1/config", "credential_values"),
    # who may sign in, and who outside the platform may read a canvas
    ("POST", "/api/settings/email-whitelist", "permissions"),
    ("DELETE", "/api/settings/email-whitelist/someone@example.com", "permissions"),
    ("POST", "/api/agents/a1/canvas/c1/share", "permissions"),
    # whole-system teardown
    ("DELETE", "/api/enterprise/system-teardown/sys1", "system_teardown"),
    # volume-destroying delete (the agent's workspace is purged after retention)
    ("DELETE", "/api/agents/a1", "destructive_delete"),
]

ADMITTED = [
    # the creator-tier reach a provisioning controller needs
    ("POST", "/api/agents"),
    ("POST", "/api/systems/deploy"),
    ("POST", "/api/systems/sys1/restart"),
    ("PUT", "/api/agents/a1/skills"),
    ("POST", "/api/agents/a1/schedules"),
    # naming a credential is not reading one
    ("GET", "/api/agents/a1/credentials/status"),
    ("GET", "/api/agents/a1/credential-requirements"),
    ("GET", "/api/agents/a1/github-pat"),
    # reading permissions is not changing them
    ("GET", "/api/agents/a1/permissions"),
    ("GET", "/api/agents/a1/capability-grants"),
    ("GET", "/api/agents/a1/shares"),
    ("GET", "/api/subscriptions"),
    ("GET", "/api/nevermined/agents/a1/config"),
    # the fence is anchored: the agent's own sub-resources are not the agent
    ("DELETE", "/api/agents/a1/skills/s1"),
    ("DELETE", "/api/agents/a1/files"),
    ("GET", "/api/agents/a1"),
    ("POST", "/api/agents/a1/schedules/s1/webhook"),
]


class TestGrantSurfaceFence:
    @pytest.mark.parametrize("method,path,surface", REFUSED)
    def test_a_controller_key_is_refused_with_a_named_code(self, accounts, method, path, surface):
        key = _mint()
        exc = _refused(_resolve, key.api_key, method, path)
        assert exc.status_code == 403
        assert exc.detail["code"] == GRANT_CODE
        assert exc.detail["surface"] == surface
        assert exc.detail["message"]

    @pytest.mark.parametrize("method,path", ADMITTED)
    def test_a_controller_key_passes_the_fence_on_use_surfaces(self, accounts, method, path):
        key = _mint()
        assert _resolve(key.api_key, method, path).mcp_scope == "controller"

    @pytest.mark.parametrize("method,path,surface", REFUSED[:3] + REFUSED[8:10] + REFUSED[15:17])
    def test_the_fence_touches_no_other_scope(self, accounts, method, path, surface):
        """Zero-config: the owner's own user key is unaffected by the fence."""
        key = _mint(scope="user", name="own key")
        assert _resolve(key.api_key, method, path).mcp_scope == "user"

    def test_the_fence_reads_the_routed_path_not_the_host(self):
        """#3102 — a Host smuggling an admitted path must not unlock a refused one."""
        req = Request({
            "type": "http", "method": "POST", "path": "/api/agents/a1/credentials/export",
            "root_path": "", "path_params": {}, "query_string": b"", "scheme": "http",
            "server": ("backend", 8000),
            "headers": [(b"host", b"backend/api/agents/a1/credentials/status?")],
        })
        exc = _refused(dependencies._enforce_controller_key_fence, req)
        assert exc.detail["code"] == GRANT_CODE

    def test_every_entry_is_anchored(self):
        for methods, pattern, surface in dependencies._CONTROLLER_REFUSED_ROUTES:
            assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$"), pattern.pattern
            assert methods and surface in {"key_minting", "permissions", "credential_values",
                                           "system_teardown", "destructive_delete"}


_FENCED_ROUTERS = (
    "routers.mcp_keys", "routers.agent_mcp_key", "routers.agent_files",
    "routers.sharing", "routers.agent_config", "routers.users",
    "routers.credentials", "routers.git", "routers.subscriptions",
    "routers.nevermined", "routers.agents", "routers.schedules",
    "routers.canvas", "routers.settings",
)


def _live_routes():
    out = set()
    for name in _FENCED_ROUTERS:
        mod = __import__(name, fromlist=["router"])
        for r in mod.router.routes:
            path = getattr(r, "path", None)
            for m in (getattr(r, "methods", None) or set()):
                out.add((m, re.sub(r"\{[^}]+\}", "x", path)))
    assert out, "no routes resolved — the guard would pass vacuously"
    return out


def test_every_fence_entry_names_a_real_route():
    """A stale entry fails OPEN for a deny-fence (the moved route is no longer
    refused), so every (method, pattern) pair must match a live route. The one
    exception is the whole-system teardown route, which ships in the private
    enterprise module and is not importable here."""
    live = _live_routes()
    for methods, pattern, surface in dependencies._CONTROLLER_REFUSED_ROUTES:
        if pattern.pattern.startswith("^/api/enterprise/"):
            continue
        for method in methods:
            assert any(m == method and pattern.fullmatch(p) for m, p in live), (
                f"controller fence entry matches no live route: {method} {pattern.pattern}"
            )


# --------------------------------------------------------------------------- #
# Minting — a grant: admin AND a signed-in session
# --------------------------------------------------------------------------- #

@pytest.fixture
def mint_client(accounts):
    import routers.mcp_keys as keys_router
    app = FastAPI()
    app.include_router(keys_router.router)
    return TestClient(app)


def _jwt(username: str) -> dict:
    return {"Authorization": f"Bearer {dependencies.create_access_token({'sub': username})}"}


class TestMinting:
    def test_an_admin_session_mints_a_controller_key(self, mint_client):
        res = mint_client.post("/api/mcp/keys", json={"name": "rc", "scope": "controller"},
                               headers=_jwt(OWNER))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["scope"] == "controller" and body["agent_name"] is None
        assert db.validate_mcp_api_key(body["api_key"])["scope"] == "controller"

    def test_a_non_admin_session_cannot_mint_one(self, mint_client):
        before = _count("mcp_api_keys")
        res = mint_client.post("/api/mcp/keys", json={"name": "rc", "scope": "controller"},
                               headers=_jwt(PLAIN))
        assert res.status_code == 403, res.text
        assert _count("mcp_api_keys") == before

    @pytest.mark.parametrize("scope", ["user", "controller"])
    def test_no_key_can_mint_one(self, mint_client, scope):
        presenter = _mint(scope=scope, name="presenter")
        before = _count("mcp_api_keys")
        res = mint_client.post("/api/mcp/keys", json={"name": "rc", "scope": "controller"},
                               headers={"Authorization": f"Bearer {presenter.api_key}"})
        assert res.status_code == 403, res.text
        assert _count("mcp_api_keys") == before


# --------------------------------------------------------------------------- #
# Attribution — audit rows
# --------------------------------------------------------------------------- #

def _audit_rows():
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT actor_type, actor_id, actor_email, mcp_key_id, mcp_key_name, mcp_scope "
            "FROM audit_log ORDER BY id")).mappings().all()]


class TestAuditAttribution:
    def test_a_controller_principal_is_recorded_as_the_controller(self, accounts):
        from services.platform_audit_service import AuditEventType, platform_audit_service
        asyncio.run(platform_audit_service.log(
            AuditEventType.AGENT_LIFECYCLE, "create", "api", actor_user=_principal()))
        (row,) = _audit_rows()
        assert row["actor_type"] == "controller"
        assert row["actor_id"] == "key-ctl-1"
        assert row["mcp_scope"] == "controller"
        assert row["mcp_key_name"] == "remote-controller"
        # The accountable owner still rides on the row, so it joins back.
        assert row["actor_email"] == "ctl-owner@example.com"

    def test_a_keyed_row_without_actor_user_is_recorded_as_the_controller_too(self, accounts):
        from services.platform_audit_service import AuditEventType, platform_audit_service
        asyncio.run(platform_audit_service.log(
            AuditEventType.AUTHORIZATION, "file_read", "api",
            actor_email="ctl-owner@example.com", mcp_key_id="key-ctl-1",
            mcp_key_name="remote-controller", mcp_scope="controller"))
        (row,) = _audit_rows()
        assert (row["actor_type"], row["actor_id"]) == ("controller", "key-ctl-1")

    def test_the_owners_own_key_is_still_recorded_as_the_user(self, accounts):
        """Zero-config: `actor_type` stays `user` for every pre-existing scope."""
        from services.platform_audit_service import AuditEventType, platform_audit_service
        asyncio.run(platform_audit_service.log(
            AuditEventType.AGENT_LIFECYCLE, "create", "api",
            actor_user=_principal(mcp_scope="user")))
        (row,) = _audit_rows()
        assert (row["actor_type"], row["actor_id"]) == ("user", "1")


# --------------------------------------------------------------------------- #
# Attribution — execution rows
# --------------------------------------------------------------------------- #

class TestExecutionAttribution:
    def _create(self, key_id):
        from db.write_params import TaskExecutionFields
        return db.create_task_execution(
            agent_name="a1", message="go", triggered_by="mcp",
            fields=TaskExecutionFields(source_user_id=1, source_user_email="ctl-owner@example.com",
                                       source_mcp_key_id=key_id, source_mcp_key_name="rc"))

    def test_a_controller_started_execution_records_the_controller(self, accounts):
        key = _mint()
        ex = self._create(key.id)
        stored = db.get_execution(ex.id)
        assert stored.source_mcp_scope == "controller"
        from models import ExecutionResponse
        assert ExecutionResponse(**stored.model_dump()).source_mcp_scope == "controller"

    def test_it_survives_the_key_being_deleted(self, accounts):
        key = _mint()
        ex = self._create(key.id)
        _hrun("DELETE FROM mcp_api_keys WHERE id = :i", i=key.id)
        assert db.get_execution(ex.id).source_mcp_scope == "controller"

    def test_other_origins_are_recorded_honestly(self, accounts):
        user_key = _mint(scope="user", name="own key")
        assert db.get_execution(self._create(user_key.id).id).source_mcp_scope == "user"
        assert db.get_execution(self._create(None).id).source_mcp_scope is None

    def test_a_manual_schedule_trigger_records_it_too(self, accounts):
        from db.write_params import ExecutionSource
        key = _mint()
        ex = db.create_schedule_execution(
            schedule_id="s1", agent_name="a1", message="go", triggered_by="manual",
            source=ExecutionSource(source_mcp_key_id=key.id))
        assert db.get_execution(ex.id).source_mcp_scope == "controller"


# --------------------------------------------------------------------------- #
# The backstop guard: a new grant-shaped route must choose a side
# --------------------------------------------------------------------------- #

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALL = _WRITES | {"GET"}

# Route shapes that GRANT authority, mint or rotate a key, or read / set a
# credential value. Deliberately broad: a false positive costs one reviewed
# exemption below, a false negative is a controller reaching a grant.
GRANT_SHAPED = (
    (_ALL, re.compile(r"/mcp/keys(/|$)|/mcp-key(/|$)|encryption-key")),
    (_WRITES, re.compile(r"/permissions(/|$)|/share(/|$)|/capability-grants(/|$)"
                         r"|/role$|/roles(/|$)|whitelist")),
    (_WRITES, re.compile(r"/credentials/|github-pat|api-key|/secret$"
                         r"|/nevermined/agents/[^/]+/config$|^/api/subscriptions$")),
)

# Classes the controller is refused by on its own: it is not a person, not an
# interactive session, and not in ADMIN_GATE_SCOPES.
REFUSING_CLASSES = frozenset({"interactive", "person", "admin_tier"})

# Reviewed: grant-shaped by name, but not a grant a controller must be refused.
# Empty today; an entry is `"METHOD /path"` with the reason beside it.
GRANT_SHAPED_EXEMPT: dict = {}


def _fenced(method: str, path: str) -> bool:
    concrete = re.sub(r"\{[^}]+\}", "x", path)
    return any(method in m and p.fullmatch(concrete)
               for m, p, _ in dependencies._CONTROLLER_REFUSED_ROUTES)


def test_every_grant_shaped_route_refuses_a_controller():
    """The denylist's backstop (abilityai/trinity-enterprise#693).

    The controller fence names grant surfaces; it cannot name a route added
    tomorrow. This walks the LIVE route table (the #2996 census subprocess —
    fails, never skips) and requires every grant-shaped route to refuse a
    controller one of two ways: a human-only / admin gate the controller is not a
    member of, or an entry in the controller fence. A new
    `POST /api/agents/{name}/credentials/rotate` gated only by ownership fails
    here until someone picks a side."""
    census = rc.walk().routes
    rows = rc.runtime_routes()
    checked, offenders = 0, []
    for r in rows:
        if rc.is_enterprise_module(r["module"]):
            continue
        key = rc.runtime_key(r["module"], r["qualname"])
        cls = census[key].code_class if key in census else None
        for method in r["methods"]:
            if not any(method in ms and p.search(r["path"]) for ms, p in GRANT_SHAPED):
                continue
            checked += 1
            if f"{method} {r['path']}" in GRANT_SHAPED_EXEMPT:
                continue
            if cls in REFUSING_CLASSES or _fenced(method, r["path"]):
                continue
            offenders.append(f"{method} {r['path']}  ({key}, class={cls})")
    assert checked > 20, f"only {checked} grant-shaped routes found — the guard would be vacuous"
    assert not offenders, (
        "grant-shaped routes a controller key can reach — gate them with "
        "require_person / require_interactive / require_admin, or add a "
        "dependencies._CONTROLLER_REFUSED_ROUTES entry:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_bites_on_an_unfenced_grant_route():
    """Self-test: the same predicate flags a planted ownership-only grant route."""
    path = "/api/agents/{agent_name}/credentials/rotate"
    assert any("POST" in ms and p.search(path) for ms, p in GRANT_SHAPED)
    assert not _fenced("POST", path) and None not in REFUSING_CLASSES


# --------------------------------------------------------------------------- #
# Attribution — skill-assignment history
# --------------------------------------------------------------------------- #

class TestAssignmentAttribution:
    def test_the_author_of_a_controller_assignment_is_the_controller(self):
        author = dependencies.assignment_author(_principal())
        assert author == "controller:key-ctl-1:remote-controller"
        scope, key_id, name = author.split(":", 2)
        assert (scope, key_id, name) == ("controller", "key-ctl-1", "remote-controller")

    def test_every_other_principal_is_still_recorded_by_username(self):
        for scope in (None, "user", "agent", "system", "ops"):
            assert dependencies.assignment_author(_principal(mcp_scope=scope)) == OWNER

    def test_assigning_a_skill_with_a_controller_key_records_the_controller(self, accounts, monkeypatch):
        import routers.skills as skills_router
        db.register_agent_owner("a1", OWNER)
        monkeypatch.setattr(skills_router.skill_service, "get_skill", lambda n: {"name": n})

        async def _none(*a, **k):
            return None
        monkeypatch.setattr(skills_router, "_sync_gates", _none)
        monkeypatch.setattr(skills_router, "_deliver_assigned_skills", _none)
        monkeypatch.setattr(skills_router, "broadcast_skills_changed", _none)
        app = FastAPI()
        app.include_router(skills_router.router)
        key = _mint()
        res = TestClient(app).post("/api/agents/a1/skills/s1",
                                   headers={"Authorization": f"Bearer {key.api_key}"})
        assert res.status_code == 200, res.text
        (row,) = db.get_agent_skills("a1")
        assert row.assigned_by == f"controller:{key.id}:remote-controller"
        assert row.assigned_by_agent is None

    def test_a_replace_with_a_controller_key_records_the_controller(self, accounts):
        db.set_agent_skills(agent_name="a1", skill_names=["s1", "s2"],
                            assigned_by=dependencies.assignment_author(_principal()))
        assert {r.assigned_by for r in db.get_agent_skills("a1")} == {
            "controller:key-ctl-1:remote-controller"}

    def test_no_assignment_writer_in_the_skills_router_passes_a_bare_username(self):
        """Pin on top of the route test above: every assignment write in
        `routers/skills.py` takes its author from `assignment_author`, so a
        set assignment or a bulk replace cannot record the owner instead."""
        src = (_BACKEND / "routers" / "skills.py").read_text()
        writers = re.findall(
            r"(db\.assign_skill\(|db\.set_agent_skills\(|skill_set_service\.(?:assign|replace|unassign),)"
            r"[^)]*?\)", src, re.S)
        assert len(writers) >= 5
        assert "current_user.username, acting_agent_name" not in src
        assert "assigned_by=current_user.username" not in src


# --------------------------------------------------------------------------- #
# Migrations — both tracks
# --------------------------------------------------------------------------- #

class TestMigrations:
    def test_the_sqlite_migration_adds_the_column_idempotently(self, tmp_path):
        import sqlite3
        from db import migrations
        conn = sqlite3.connect(tmp_path / "old.db")
        cur = conn.cursor()
        cur.execute("CREATE TABLE schedule_executions (id TEXT PRIMARY KEY, source_mcp_key_id TEXT)")
        conn.commit()
        migrations._migrate_execution_source_mcp_scope(cur, conn)
        migrations._migrate_execution_source_mcp_scope(cur, conn)
        cols = {r[1] for r in cur.execute("PRAGMA table_info(schedule_executions)")}
        assert "source_mcp_scope" in cols
        assert ("execution_source_mcp_scope", migrations._migrate_execution_source_mcp_scope) \
            in migrations.MIGRATIONS

    def test_the_alembic_revision_follows_the_single_head(self):
        versions = _BACKEND / "migrations" / "versions"
        (path,) = list(versions.glob("*_execution_source_mcp_scope.py"))
        src = path.read_text()
        assert "schedule_executions" in src and "source_mcp_scope" in src
        down = re.search(r'^down_revision\s*=\s*"([^"]+)"', src, re.M).group(1)
        others = [p for p in versions.glob("*.py") if p != path]
        assert any(f'revision = "{down}"' in p.read_text() for p in others)
        # Nothing else claims the same parent — one head.
        assert not any(f'down_revision = "{down}"' in p.read_text() for p in others)


# --------------------------------------------------------------------------- #
# Zero-config
# --------------------------------------------------------------------------- #

def test_no_existing_allowlist_widened():
    assert dependencies.ADMIN_GATE_SCOPES == frozenset({None, "user", "system"})
    assert dependencies.PERSON_SCOPES == frozenset({None, "user"})
    assert "controller" not in dependencies.WS_EVENT_STREAM_SCOPES
    assert dependencies.CONTROLLER_SCOPE == "controller"

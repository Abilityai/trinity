"""Create-mode deploy writes the platform-wide `trinity_prompt` for an admin only (#4025).

`POST /api/systems/deploy` admits `require_role("creator")` plus
`capability_fence("agents.manage")`. A manifest's top-level `prompt:` replaces
the instructions every agent on the instance receives at its next start, and
the settings route (`PUT /api/settings/{key}`) reserves that key for a caller
who passes `assert_admin`. Deploy used to write it for anyone the route
admitted, so a creator, or an agent key holding `agents.manage`, could change
every agent's instructions through a side door.

Everything here runs through the real routes and their real gates: the systems
router (`require_role` + `capability_fence`) and the settings router
(`assert_admin`), each mounted on a bare FastAPI app. The seams are the
authenticated principal (a dependency override) and the two Docker-bound
`routers.agents` facade functions create mode calls, as in the create-mode
golden (`test_ent695_create_mode_golden.py`).
"""

from __future__ import annotations

import sys

import db_harness
import dependencies
import pytest
import routers.agents as agents_router
import routers.settings as settings_router
from database import db
from db_harness import seed_agent, seed_user
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import User
from routers import systems
from services import system_service

pytestmark = pytest.mark.unit

sqlite_only_backend = db_harness.sqlite_only_backend

# Non-ambient values: neither is a default anything could produce by accident.
ORIGINAL_PROMPT = "ORIGINAL-4025 prompt the instance already had"
MANIFEST_PROMPT = "MANIFEST-4025 prompt a deploy tries to set"
WARNING_CODE = "prompt_requires_admin:"

MANIFEST = f"""
name: p4025
prompt: {MANIFEST_PROMPT}
agents:
  lead:
    template: local:default
"""

ADMIN_ID, CREATOR_ID = 1, 2
ORCH = "orch-4025"  # the agent whose key acts below; owned by the admin

ADMIN_JWT = User(id=ADMIN_ID, username="admin", email=None, role="admin")
CREATOR_JWT = User(id=CREATOR_ID, username="maker", email=None, role="creator")
# An agent key resolves to its owner CARRYING THE OWNER'S ROLE. Owned by an
# admin is the worst case: `require_role("creator")` reads `admin` and passes.
AGENT_KEY = User(id=ADMIN_ID, username="admin", email=None, role="admin",
                 mcp_scope="agent", agent_name=ORCH)
ADMIN_USER_KEY = User(id=ADMIN_ID, username="admin", email=None, role="admin",
                      mcp_scope="user")
ADMIN_SYSTEM_KEY = User(id=ADMIN_ID, username="admin", email=None, role="admin",
                        mcp_scope="system")
CONNECTOR_KEY = User(id=ADMIN_ID, username="admin", email=None, role="admin",
                     mcp_scope="connector", connector_agent=ORCH)
PORTAL_DELEGATE_KEY = User(id=ADMIN_ID, username="admin", email=None, role="admin",
                           mcp_scope="portal_delegate", portal_delegate=True)


class _Seams:
    """`create_agent_internal` / `start_agent_internal` without Docker: create
    registers the ownership row the real create leaves behind."""

    async def create(self, config, current_user, request, skip_name_sanitization=False, **kw):
        if not db.register_agent_owner(config.name, current_user.username):
            raise AssertionError(f"harness: could not register {config.name}")
        return {"status": "created"}

    async def start(self, agent_name):
        return {"status": "started"}


@pytest.fixture
def install(sqlite_only_backend, monkeypatch):
    # Own the lazily imported modules so a sibling's sys.modules stub cannot
    # reach create mode's call-time imports.
    monkeypatch.setitem(sys.modules, "routers.agents", agents_router)
    monkeypatch.setitem(sys.modules, "services.system_service", system_service)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seams = _Seams()
    monkeypatch.setattr(agents_router, "create_agent_internal", seams.create)
    monkeypatch.setattr(agents_router, "start_agent_internal", seams.start)
    seed_user(user_id=ADMIN_ID, username="admin", role="admin")
    seed_user(user_id=CREATOR_ID, username="maker", role="creator")
    seed_agent(ORCH, owner_id=ADMIN_ID)
    db.set_setting("trinity_prompt", ORIGINAL_PROMPT)
    return monkeypatch


def _client(router, principal: User) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # Both routers close over the one `dependencies.get_current_user`.
    assert systems.get_current_user is dependencies.get_current_user
    app.dependency_overrides[dependencies.get_current_user] = lambda: principal
    return TestClient(app, raise_server_exceptions=False)


def _deploy(principal: User, *, dry_run: bool = False):
    return _client(systems.router, principal).post(
        "/api/systems/deploy", json={"manifest": MANIFEST, "dry_run": dry_run})


def _stored_prompt() -> str:
    return db.get_setting("trinity_prompt").value


def _prompt_warnings(body: dict) -> list[str]:
    return [w for w in body["warnings"] if w.startswith(WARNING_CODE)]


def _grant_agents_manage() -> None:
    assert db.grant_agent_capability(ORCH, "agents.manage", "admin")


# --- the side door is closed ---------------------------------------------------


@pytest.mark.parametrize("principal", [CREATOR_JWT, AGENT_KEY], ids=["creator_jwt", "agent_key"])
def test_a_non_admin_deploys_the_fleet_but_not_the_prompt(install, principal):
    if principal is AGENT_KEY:
        _grant_agents_manage()

    resp = _deploy(principal)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deployed"
    assert body["agents_created"] == ["p4025-lead"]
    assert body["prompt_updated"] is False
    assert len(_prompt_warnings(body)) == 1, body["warnings"]
    assert _stored_prompt() == ORIGINAL_PROMPT


def test_the_agent_key_reaches_deploy_only_through_its_grant(install):
    """Control for the agent-key case: without `agents.manage` the fence refuses
    it, so the grant is what opens the route, not some other door."""
    resp = _deploy(AGENT_KEY)

    assert resp.status_code == 403, resp.text
    assert _stored_prompt() == ORIGINAL_PROMPT
    assert not db.get_agent_owner("p4025-lead")


@pytest.mark.parametrize("principal", [ADMIN_JWT, ADMIN_USER_KEY],
                         ids=["admin_jwt", "admin_user_key"])
def test_an_admin_deploy_still_writes_the_prompt(install, principal):
    resp = _deploy(principal)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prompt_updated"] is True
    assert _prompt_warnings(body) == []
    assert _stored_prompt() == MANIFEST_PROMPT


# --- AC4: the preview predicts the write -----------------------------------------


def test_a_non_admin_preview_says_the_prompt_will_not_apply(install):
    resp = _deploy(CREATOR_JWT, dry_run=True)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "valid"
    assert body["prompt_updated"] is False
    assert len(_prompt_warnings(body)) == 1, body["warnings"]
    assert _stored_prompt() == ORIGINAL_PROMPT


def test_an_admin_preview_still_flags_the_prompt(install):
    resp = _deploy(ADMIN_JWT, dry_run=True)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prompt_updated"] is True
    assert _prompt_warnings(body) == []
    assert _stored_prompt() == ORIGINAL_PROMPT


def test_a_manifest_without_a_prompt_names_no_prompt_warning(install):
    no_prompt = MANIFEST.replace(f"prompt: {MANIFEST_PROMPT}\n", "")
    resp = _client(systems.router, CREATOR_JWT).post(
        "/api/systems/deploy", json={"manifest": no_prompt})

    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt_updated"] is False
    assert _prompt_warnings(resp.json()) == []
    assert _stored_prompt() == ORIGINAL_PROMPT


# --- parity with the settings route's admin gate ----------------------------------


PARITY = [
    ("creator_jwt", CREATOR_JWT),
    ("admin_jwt", ADMIN_JWT),
    ("admin_user_key", ADMIN_USER_KEY),
    ("admin_system_key", ADMIN_SYSTEM_KEY),
    ("admin_owned_agent_key_with_grant", AGENT_KEY),
    ("admin_owned_connector_key", CONNECTOR_KEY),
    ("admin_owned_portal_delegate_key", PORTAL_DELEGATE_KEY),
]


@pytest.mark.parametrize("principal", [p for _, p in PARITY], ids=[i for i, _ in PARITY])
def test_deploy_writes_the_prompt_exactly_when_the_settings_route_would(install, principal):
    """Deploy may write `trinity_prompt` for exactly the callers
    `PUT /api/settings/trinity_prompt` accepts. A caller the deploy route itself
    refuses agrees by writing nothing."""
    _grant_agents_manage()

    deploy = _deploy(principal)
    deploy_wrote = _stored_prompt() == MANIFEST_PROMPT
    db.set_setting("trinity_prompt", ORIGINAL_PROMPT)

    put = _client(settings_router.router, principal).put(
        "/api/settings/trinity_prompt", json={"value": MANIFEST_PROMPT})
    assert put.status_code in (200, 403), put.text
    settings_writes = put.status_code == 200

    assert deploy_wrote is settings_writes, (deploy.status_code, deploy.text, put.status_code)
    if deploy.status_code == 200:
        body = deploy.json()
        assert body["prompt_updated"] is deploy_wrote
        assert len(_prompt_warnings(body)) == (0 if deploy_wrote else 1), body["warnings"]

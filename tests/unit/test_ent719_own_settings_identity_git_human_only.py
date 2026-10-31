"""trinity-enterprise#719 — an agent's own settings, identity and git are set by a person.

These owner-tier routes change what an agent may do, how it is named, or which
credential it holds. Each carries a census-recognised human-only gate in its
route DECORATOR, so the gate runs before the path's ownership lookup and its
refusal says nothing about whether the agent exists (Invariant #8, #186):

* PERSON (`Depends(require_person)`: a signed-in session or the person's own
  `user`-scoped key) — the settings and identity routes; the per-agent GitHub
  PAT set, which binds a token the caller supplies; the PAT clear, a revoke; the
  two git switches on unattended behaviour; and initialize, which takes no
  credential from the caller (it seeds the token already configured for the
  agent);
* INTERACTIVE (`Depends(require_interactive)`: a signed-in session only) — the
  Brain Orb voice token, which the platform mints and hands back, and the
  repository binding, which can store the caller's SAVED GitHub token (one the
  request does not carry, and which only a session can set) as the agent's
  per-agent PAT, and can create a repository on the caller's GitHub account.

The PAT set is PERSON by the 2026-10-28 ruling on trinity-enterprise#719: the
public abilities toolkit's `/trinity:onboard` binds a per-agent token with
`set_agent_github_pat` on the person's own user-scoped key ("Option 2: tier-1
per-agent token"), and that flow must keep working. Every agent key and the
system key stay refused.

Matrix M, through the real routers mounted in `main.py`'s order:

* real keys seeded in the real schema and presented as bearers: the agent's own
  key; the key of an agent whose manage reach includes the target (it spawned
  the target with that key); the system key; the owner's `user` key. The owner
  is an admin, as on a default install, so every key carries `role=admin`;
* stated principals (`get_current_user` overridden, every field set, never a
  `MagicMock`) for the kinds the real path fences earlier or cannot mint:
  connector, ops, portal_delegate, a scope nobody has shipped, a `user` scope
  carrying an agent identity, a loopback JWT carrying a vouched source agent,
  and an object with no `mcp_scope` at all;
* a real, signed loopback JWT with no source agent, through the real entry
  fence (the route primitive is the second layer, not the first);
* the people the rule keeps: the owner's session on every route, the owner's
  `user` key on every PERSON route, a person the agent is shared with on the
  voice token.

Every refusal asserts the stored value (or the stubbed side effect) did not
move; every admission asserts it did. For every principal the route's rule
refuses (the real loopback JWT is refused earlier, by the entry fence), the
refusal is the same for an agent that does not exist, and it is still a 403 for
a body that fails validation on the routes that validate one (FastAPI parses
JSON before dependencies, so only a body that is not JSON at all gets 422
first). The stated principals (the no-scope object among them), and the real
keys on a fresh install, are also refused before any agent lookup.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.agent_brain_orb as _ORB  # noqa: E402
import routers.agent_rename as _RENAME  # noqa: E402
import routers.agents as _AGENTS  # noqa: E402
import routers.git as _GIT  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

ADMIN = "s5-admin"  # owns every agent here: a default admin-owned install
SHARED = "s5-shared"  # a person the agent is shared with
AGENT = "s5-agent"
PARENT = "s5-parent"  # spawned AGENT with the key it holds now
EDGE = "s5-edge"  # holds an `agent_permissions` edge to AGENT
SYSTEM = "s5-system"
GHOST = "s5-no-such-agent"
NEW_NAME = "s5-renamed"

HUMAN_ONLY_DETAIL = dependencies.HUMAN_ONLY_DETAIL
INTERACTIVE_DETAIL = (
    "This operation requires an interactive session; MCP API keys cannot perform it"
)
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

PERSON, INTERACTIVE = "person", "interactive"


@dataclass
class _Principal:
    """Every field `models.User` carries, stated — never a MagicMock."""

    id: int
    username: str
    email: Optional[str]
    role: str
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False
    mcp_scope: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    vouched_source_agent: Optional[str] = None
    is_event_loopback: bool = False


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute: both rules must fail
    closed on it (a sentinel, never `getattr(..., None)`)."""

    id = 1
    username = ADMIN
    email = "s5-admin@example.com"
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


# --------------------------------------------------------------------------- #
# The world: a fresh schema, an admin-owned agent, and the real routers
# --------------------------------------------------------------------------- #


def _app() -> FastAPI:
    app = FastAPI()
    for router in _routers():
        app.include_router(router)
    return app


def _routers():
    """`main.py` registers these in this order (Invariant #4)."""
    return [_AGENTS.router, _ORB.router, _RENAME.router, _GIT.router]


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    db.create_user(
        UserCreate(username=ADMIN, role="admin", email="s5-admin@example.com")
    )
    db.create_user(
        UserCreate(username=SHARED, role="operator", email="s5-shared@example.com")
    )
    db.register_agent_owner(PARENT, ADMIN)
    parent_key = db.create_agent_mcp_api_key(PARENT, ADMIN)
    db.register_agent_owner(
        AGENT, ADMIN, spawned_by_agent=PARENT, spawned_by_key_id=parent_key.id
    )
    db.register_agent_owner(EDGE, ADMIN)
    db.add_agent_permission(EDGE, AGENT, created_by=ADMIN)
    db.register_agent_owner(SYSTEM, ADMIN, is_system=True)
    db.share_agent(AGENT, ADMIN, "s5-shared@example.com")
    admin_id = db.get_user_by_username(ADMIN)["id"]

    app = _app()
    client = TestClient(app)
    keys = {
        "agent-own-key": lambda: _bearer(
            db.create_agent_mcp_api_key(AGENT, ADMIN).api_key
        ),
        "agent-sibling-key": lambda: _bearer(parent_key.api_key),
        "agent-edge-key": lambda: _bearer(
            db.create_agent_mcp_api_key(EDGE, ADMIN).api_key
        ),
        "system-key": _system_key,
        "owner-user-key": lambda: _bearer(
            db.create_mcp_api_key(ADMIN, McpApiKeyCreate(name="s5 owner key")).api_key
        ),
    }

    def as_(principal):
        app.dependency_overrides[dependencies.get_current_user] = lambda: principal

    def stated(**fields):
        base = {
            "id": admin_id,
            "username": ADMIN,
            "email": "s5-admin@example.com",
            "role": "admin",
        }
        base.update(fields)
        return _Principal(**base)

    yield SimpleNamespace(
        client=client,
        app=app,
        as_=as_,
        stated=stated,
        keys=keys,
        monkeypatch=monkeypatch,
        parent_key=parent_key,
    )
    app.dependency_overrides.clear()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str) -> dict:
    return _bearer(dependencies.create_access_token({"sub": username}))


def _system_key() -> dict:
    """Created the way the platform creates it: an agent key under the admin,
    then its scope column set to `system` (`system_agent_service._set_system_scope`)."""
    key = db.create_agent_mcp_api_key(SYSTEM, ADMIN)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _bearer(key.api_key)


# --------------------------------------------------------------------------- #
# The routes: how to seed a non-ambient state, the call, and how to read it back
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Spec:
    family: str
    rule: str
    method: str
    suffix: str
    arm: Callable[[Any], Callable[[], Any]]  # seeds + stubs; returns observe()
    after: Any  # what observe() returns once a person's call landed
    json: Optional[dict] = None
    content: Optional[bytes] = None
    bad_json: Optional[dict] = None  # well-formed JSON the route's model rejects

    def url(self, agent: str = AGENT) -> str:
        return f"/api/agents/{agent}/{self.suffix}"


def _call(world, spec: Spec, *, headers=None, agent: str = AGENT, json=None):
    kwargs = {"headers": headers or {}}
    if json is not None:
        kwargs["json"] = json
    elif spec.json is not None:
        kwargs["json"] = spec.json
    elif spec.content is not None:
        kwargs["content"] = spec.content
        kwargs["headers"] = {**kwargs["headers"], "content-type": "application/json"}
    return world.client.request(spec.method, spec.url(agent), **kwargs)


async def _noop(*_a, **_k):
    return None


# ---- S5a: the agent's own settings and identity --------------------------- #


def _arm_circuit_breaker(w):
    db.set_circuit_breaker_enabled(AGENT, False)
    return lambda: db.get_circuit_breaker_enabled(AGENT)


def _arm_label(w):
    db.set_display_label(AGENT, "Day shift")
    return lambda: db.get_display_label(AGENT)


def _arm_mcp_exposed(w):
    db.set_mcp_exposed(AGENT, False)
    return lambda: db.get_mcp_exposed(AGENT)


def _arm_operator_resume(w):
    db.set_operator_resume_enabled(AGENT, False)
    return lambda: db.get_operator_resume_enabled(AGENT)


def _arm_voice_replies(w):
    db.set_tts_config(AGENT, False, None)

    def observe():
        cfg = db.get_tts_config(AGENT)
        return cfg["enabled"], cfg["voice_id"]

    return observe


def _arm_rename(w):
    """The container layer is stubbed; the database rename is the real one."""
    stopped = SimpleNamespace(status="exited", name=f"agent-{AGENT}")
    w.monkeypatch.setattr(
        _RENAME, "get_agent_container", lambda name: stopped if name == AGENT else None
    )
    w.monkeypatch.setattr(_RENAME, "container_stop", _noop)
    w.monkeypatch.setattr(_RENAME, "container_rename", _noop)
    import services.agent_runtime_state as runtime_state

    w.monkeypatch.setattr(runtime_state, "clear_agent_runtime_state", _noop)
    return lambda: (
        db.get_agent_owner(AGENT) is not None,
        db.get_agent_owner(NEW_NAME) is not None,
    )


def _arm_postprocess(w):
    """The agent-server hop is stubbed and recorded."""
    w.monkeypatch.setattr(_ORB, "is_brain_orb_write_enabled", lambda: True)
    forwarded = []

    async def _agent_request(agent_name, method, path, *, content=None, timeout):
        forwarded.append((agent_name, method, path))
        return SimpleNamespace(
            status_code=200, content=b'{"enabled": true, "prompt": "Summarise"}'
        )

    w.monkeypatch.setattr(_ORB, "_agent_request", _agent_request)
    return lambda: len(forwarded)


def _arm_voice_token(w):
    """The flags are on and the Gemini mint is stubbed and recorded."""
    w.monkeypatch.setattr(_ORB, "is_brain_orb_enabled", lambda: True)
    w.monkeypatch.setattr(_ORB, "is_brain_orb_voice_enabled", lambda: True)
    w.monkeypatch.setattr(_ORB, "is_brain_orb_write_enabled", lambda: False)
    w.monkeypatch.setattr(_ORB, "get_gemini_api_key", lambda: "s5-gemini-placeholder")
    w.monkeypatch.setattr(_ORB.rate_limiter, "enforce", lambda *a, **k: None)
    minted = []

    async def _mint(agent_name, **_kw):
        minted.append(agent_name)
        return {"ephemeral_token": "auth_tokens/s5-test", "tools": []}

    w.monkeypatch.setattr(_ORB.brain_orb_voice_service, "mint_voice_token", _mint)
    return lambda: len(minted)


S5A = {
    "circuit-breaker": Spec(
        "S5a",
        PERSON,
        "PUT",
        "circuit-breaker",
        _arm_circuit_breaker,
        True,
        json={"enabled": True},
        bad_json={},
    ),
    "label": Spec(
        "S5a",
        PERSON,
        "PUT",
        "label",
        _arm_label,
        "Night shift",
        json={"label": "Night shift"},
        bad_json={"display_label": "Night shift"},
    ),
    "mcp-exposed": Spec(
        "S5a",
        PERSON,
        "PUT",
        "mcp-exposed",
        _arm_mcp_exposed,
        True,
        json={"enabled": True},
        bad_json={},
    ),
    "operator-resume": Spec(
        "S5a",
        PERSON,
        "PUT",
        "operator-resume",
        _arm_operator_resume,
        True,
        json={"enabled": True},
        bad_json={},
    ),
    "voice-replies": Spec(
        "S5a",
        PERSON,
        "PUT",
        "voice-replies",
        _arm_voice_replies,
        (True, "voice-s5"),
        json={"enabled": True, "voice_id": "voice-s5"},
        bad_json={"channels": {"carrier-pigeon": True}},
    ),
    "rename": Spec(
        "S5a",
        PERSON,
        "PUT",
        "rename",
        _arm_rename,
        (False, True),
        json={"new_name": NEW_NAME},
        bad_json={},
    ),
    "brain-orb-postprocess": Spec(
        "S5a",
        PERSON,
        "PUT",
        "brain-orb/postprocess",
        _arm_postprocess,
        1,
        content=b'{"enabled": true, "prompt": "Summarise"}',
    ),
    "brain-orb-voice-token": Spec(
        "S5a", INTERACTIVE, "POST", "brain-orb/voice-token", _arm_voice_token, 1
    ),
}


# ---- S5b: the agent's git ---------------------------------------------------- #

DEST = "example-org/s5-brain"


def _git_configured():
    db.create_git_config(
        AGENT, "example-org/s5-agent", "trinity/s5-agent/inst-s5", "inst-s5"
    )


class _FakeGitHub:
    """The GitHub API client, answering as for a valid token and an existing repo."""

    def __init__(self, pat):
        self.pat = pat

    async def validate_token(self):
        return True, "s5-octo"

    async def check_repo_exists(self, owner, name):
        return SimpleNamespace(exists=True)

    async def create_repository(self, **_kw):  # pragma: no cover - create_repo is False
        raise AssertionError("the call asked for an existing repository")


def _stub_github(w):
    import services.github_service as github_service

    w.monkeypatch.setattr(github_service, "GitHubService", _FakeGitHub)


def _arm_pat_set(w):
    """GitHub validation and the live push into the container are stubbed; the
    encrypted store is the real one."""
    _git_configured()
    _stub_github(w)
    import services.github_pat_propagation_service as propagation

    async def _propagate(agent_name, pat):
        return {"reason": "agent_not_running"}

    w.monkeypatch.setattr(propagation, "propagate_pat_to_single_agent", _propagate)
    return lambda: db.has_agent_github_pat(AGENT)


def _arm_pat_clear(w):
    _git_configured()
    assert db.set_agent_github_pat(AGENT, "s5-seeded-token")
    return lambda: db.has_agent_github_pat(AGENT)


def _arm_auto_sync(w):
    _git_configured()
    db.set_git_auto_sync_enabled(AGENT, False)
    return lambda: bool(db.get_git_config(AGENT).auto_sync_enabled)


def _arm_freeze(w):
    _git_configured()
    db.set_freeze_schedules_if_sync_failing(AGENT, False)
    return lambda: bool(db.get_git_config(AGENT).freeze_schedules_if_sync_failing)


def _arm_initialize(w):
    """A running container, a resolvable PAT and an existing repository; the
    container-side git work is stubbed and recorded. No git config exists, as on
    a fresh install."""
    import services.docker_service as docker_service

    running = SimpleNamespace(status="running")
    w.monkeypatch.setattr(
        docker_service,
        "get_agent_container",
        lambda name: running if name == AGENT else None,
    )
    w.monkeypatch.setattr(
        _GIT, "get_github_pat_for_agent", lambda name: "s5-placeholder-token"
    )
    _stub_github(w)
    initialised = []

    async def _reserve(*, agent_name, github_repo):
        return "inst-s5", "trinity/s5-agent/inst-s5"

    async def _initialize(**kw):
        initialised.append(kw["agent_name"])
        return SimpleNamespace(
            success=True, error=None, working_branch=kw["working_branch"]
        )

    w.monkeypatch.setattr(
        _GIT.git_service, "reserve_and_generate_instance_id", _reserve
    )
    w.monkeypatch.setattr(_GIT.git_service, "initialize_git_in_container", _initialize)
    return lambda: len(initialised)


class _FakeRedis:
    """The bind lock's SETNX double."""

    def __init__(self):
        self.store = {}

    def ping(self):
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


def _arm_bind(w):
    """The lock store and the binding service are stubbed; the service call is
    recorded."""
    import routers.auth as auth_router
    import services.agent_service.repo_binding as repo_binding

    w.monkeypatch.setattr(auth_router, "get_redis_client", lambda: _FakeRedis())
    bound = []

    async def _bind(**kw):
        bound.append(kw["agent_name"])
        return SimpleNamespace(
            github_repo=DEST,
            previous_repo="example-org/s5-agent",
            default_branch="main",
            private=True,
            created_repo=True,
            reused_existing=False,
            recreated=True,
            audit={"github_repo": DEST},
        )

    w.monkeypatch.setattr(repo_binding, "bind_agent_to_own_repo", _bind)
    return lambda: len(bound)


S5B = {
    "github-pat-set": Spec(
        "S5b",
        PERSON,
        "PUT",
        "github-pat",
        _arm_pat_set,
        True,
        json={"pat": "s5-placeholder-token"},
        bad_json={},
    ),
    "github-pat-clear": Spec(
        "S5b", PERSON, "DELETE", "github-pat", _arm_pat_clear, False
    ),
    "git-auto-sync": Spec(
        "S5b",
        PERSON,
        "PUT",
        "git/auto-sync",
        _arm_auto_sync,
        True,
        json={"enabled": True},
        bad_json={},
    ),
    "git-freeze-schedules": Spec(
        "S5b",
        PERSON,
        "PUT",
        "git/freeze-schedules-if-failing",
        _arm_freeze,
        True,
        json={"enabled": True},
        bad_json={},
    ),
    # Takes no credential from the caller: it seeds the token already configured
    # for the agent (its per-agent PAT or the platform PAT), so it is PERSON.
    "git-initialize": Spec(
        "S5b",
        PERSON,
        "POST",
        "git/initialize",
        _arm_initialize,
        1,
        json={
            "repo_owner": "example-org",
            "repo_name": "s5-agent",
            "create_repo": False,
        },
        bad_json={"repo_name": "s5-agent"},
    ),
    # Can bind the caller's SAVED GitHub token (session-only to set, ent#711) as
    # the agent's per-agent PAT (`repo_binding.py`), a token the request does not
    # carry, and can create a repository on the caller's account: INTERACTIVE. The
    # PAT set binds only the token in its body, so it is PERSON (ent#719 ruling).
    "git-bind-to-own-repo": Spec(
        "S5b",
        INTERACTIVE,
        "POST",
        "git/bind-to-own-repo",
        _arm_bind,
        1,
        json={
            "destination_repo": DEST,
            "github_pat": "s5-placeholder-token",
            "private": True,
        },
        bad_json={"destination_repo": "not a repo"},
    ),
}

SPECS: dict = {**S5A, **S5B}


def _ids(rule=None, with_bad_body=False):
    return sorted(
        k
        for k, s in SPECS.items()
        if (rule is None or s.rule == rule)
        and (not with_bad_body or s.bad_json is not None)
    )


def _armed(world, name):
    spec = SPECS[name]
    observe = spec.arm(world)
    before = observe()
    assert before != spec.after, f"{name}: the seed must differ from what a person sets"
    return spec, observe, before


def _assert_refused(spec: Spec, res, *, key_authenticated: bool):
    """The rule's own refusal: PERSON answers `HUMAN_ONLY_DETAIL` to every
    principal; INTERACTIVE answers its key refusal to every key-authenticated
    caller and `HUMAN_ONLY_DETAIL` to a loopback JWT."""
    assert res.status_code == 403, res.text
    detail = res.json()["detail"]
    if spec.rule == INTERACTIVE and key_authenticated:
        assert detail == INTERACTIVE_DETAIL, detail
    else:
        assert detail == HUMAN_ONLY_DETAIL, detail


# Stated principals the ROUTE refuses: the real path fences ops / connector /
# portal_delegate earlier, and the rest cannot be minted as a key row.
STATED = {
    "connector-key": dict(mcp_scope="connector", connector_agent=AGENT),
    "ops-key": dict(mcp_scope="ops"),
    "portal-delegate-key": dict(mcp_scope="portal_delegate", portal_delegate=True),
    "unknown-future-scope": dict(mcp_scope="a-scope-no-one-has-shipped"),
    "user-scope-carrying-an-agent": dict(mcp_scope="user", agent_name=AGENT),
    "vouched-loopback-jwt": dict(
        mcp_scope=None, is_event_loopback=True, vouched_source_agent=EDGE
    ),
}
REAL_MACHINE_KEYS = ["agent-own-key", "agent-sibling-key", "system-key"]
_AGENT_LOOKUPS = (
    "get_agent_owner",
    "can_user_share_agent",
    "can_user_access_agent",
    "can_user_rename_agent",
    "get_git_config",
)


NO_SCOPE = "no-mcp-scope"


def _refused(with_bad_body=False):
    """Every (route, principal) pair the rule refuses: the real machine keys,
    the stated principals and the no-scope principal on every route; the edge
    key on the voice token, the one route whose reach admits it; and the owner's
    `user` key on the INTERACTIVE routes."""
    rows = []
    for name in _ids(with_bad_body=with_bad_body):
        rows += [(name, k) for k in REAL_MACHINE_KEYS]
        if name == "brain-orb-voice-token":
            rows.append((name, "agent-edge-key"))
        if SPECS[name].rule == INTERACTIVE:
            rows.append((name, "owner-user-key"))
        rows += [(name, p) for p in sorted(STATED)]
        rows.append((name, NO_SCOPE))
    return rows


def _present(world, principal) -> tuple:
    """Headers for a real key, or the override for a stated principal, and
    whether the caller is key-authenticated (an INTERACTIVE route answers a
    loopback JWT with the PERSON detail)."""
    if principal in world.keys:
        return world.keys[principal](), True
    if principal == NO_SCOPE:
        world.as_(_NoScopeAttribute())
        return {}, True
    world.as_(world.stated(**STATED[principal]))
    return {}, principal != "vouched-loopback-jwt"


def _count_agent_lookups(monkeypatch) -> list:
    """Record every agent lookup the access dependencies and handlers make."""
    seen: list = []
    for name in _AGENT_LOOKUPS:
        real = getattr(db, name)

        def counting(*a, _real=real, _name=name, **k):
            seen.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(db, name, counting)
    return seen


# --------------------------------------------------------------------------- #
# Matrix M
# --------------------------------------------------------------------------- #


def test_the_sibling_keys_are_inside_the_targets_reach(world):
    """Precondition: a sibling refusal below is the gate's, not the reach's."""
    spawner = world.stated(mcp_scope="agent", agent_name=PARENT)
    edge = world.stated(mcp_scope="agent", agent_name=EDGE)
    assert dependencies.agent_may_reach(spawner, AGENT, manage=True)
    assert dependencies.agent_may_reach(edge, AGENT)
    assert not dependencies.agent_may_reach(edge, AGENT, manage=True)


@pytest.mark.parametrize("key", REAL_MACHINE_KEYS)
@pytest.mark.parametrize("name", _ids())
def test_a_machine_key_is_refused_and_nothing_moves(world, name, key):
    spec, observe, before = _armed(world, name)
    res = _call(world, spec, headers=world.keys[key]())
    _assert_refused(spec, res, key_authenticated=True)
    assert observe() == before


@pytest.mark.parametrize("name", _ids(INTERACTIVE))
def test_the_owner_user_key_is_refused_where_a_credential_is_minted_or_bound(
    world, name
):
    spec, observe, before = _armed(world, name)
    res = _call(world, spec, headers=world.keys["owner-user-key"]())
    _assert_refused(spec, res, key_authenticated=True)
    assert observe() == before


def test_an_edge_sibling_is_refused_the_voice_token(world):
    """The voice token is an access-tier route, so an edge holder is inside its
    reach; the mint is still not a key's to make."""
    spec, observe, before = _armed(world, "brain-orb-voice-token")
    res = _call(
        world, spec, headers=_bearer(db.create_agent_mcp_api_key(EDGE, ADMIN).api_key)
    )
    _assert_refused(spec, res, key_authenticated=True)
    assert observe() == before


@pytest.mark.parametrize("principal", sorted(STATED))
@pytest.mark.parametrize("name", _ids())
def test_a_stated_machine_principal_is_refused_before_any_agent_lookup(
    world, name, principal
):
    spec, observe, before = _armed(world, name)
    world.as_(world.stated(**STATED[principal]))
    lookups = _count_agent_lookups(world.monkeypatch)
    res = _call(world, spec)
    _assert_refused(spec, res, key_authenticated=principal != "vouched-loopback-jwt")
    assert lookups == [], f"the route looked the agent up before refusing: {lookups}"
    assert observe() == before


@pytest.mark.parametrize("name", _ids())
def test_a_principal_without_mcp_scope_fails_closed(world, name):
    spec, observe, before = _armed(world, name)
    world.as_(_NoScopeAttribute())
    lookups = _count_agent_lookups(world.monkeypatch)
    res = _call(world, spec)
    _assert_refused(spec, res, key_authenticated=True)
    assert lookups == [], f"the route looked the agent up before refusing: {lookups}"
    assert observe() == before


@pytest.mark.parametrize("name", _ids())
def test_a_real_unvouched_loopback_jwt_is_refused_by_the_entry_fence(world, name):
    """A signed loopback JWT with no source agent, through real authentication.
    The route primitive is the second layer; this pins the first on each route."""
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    spec, observe, before = _armed(world, name)
    token = jwt.encode(
        {
            "sub": ADMIN,
            "scope": dependencies.EVENT_LOOPBACK_SCOPE,
            "exp": int(time.time()) + 300,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    res = _call(world, spec, headers=_bearer(token))
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert observe() == before


@pytest.mark.parametrize("name,principal", _refused())
def test_the_refusal_is_the_same_for_an_agent_that_does_not_exist(
    world, name, principal
):
    spec = SPECS[name]
    spec.arm(world)
    headers, key_authenticated = _present(world, principal)
    on_real = _call(world, spec, headers=headers)
    on_ghost = _call(world, spec, headers=headers, agent=GHOST)
    assert (on_ghost.status_code, on_ghost.json()) == (
        on_real.status_code,
        on_real.json(),
    )
    _assert_refused(spec, on_ghost, key_authenticated=key_authenticated)


@pytest.mark.parametrize("name,principal", _refused(with_bad_body=True))
def test_a_body_that_fails_validation_is_still_refused(world, name, principal):
    spec, observe, before = _armed(world, name)
    headers, key_authenticated = _present(world, principal)
    res = _call(world, spec, headers=headers, json=spec.bad_json)
    _assert_refused(spec, res, key_authenticated=key_authenticated)
    assert observe() == before


@pytest.mark.parametrize("name", _ids(with_bad_body=True))
def test_the_invalid_body_is_really_invalid(world, name):
    """Control for the test above: the same body is a 422 for a person."""
    spec, observe, before = _armed(world, name)
    res = _call(world, spec, headers=_jwt(ADMIN), json=spec.bad_json)
    assert res.status_code == 422, res.text
    assert observe() == before


# ---- the people the rule keeps ---------------------------------------------- #


@pytest.mark.parametrize("name", _ids())
def test_the_owner_session_changes_it(world, name):
    spec, observe, _before = _armed(world, name)
    res = _call(world, spec, headers=_jwt(ADMIN))
    assert res.status_code == 200, res.text
    assert observe() == spec.after


@pytest.mark.parametrize("name", _ids(PERSON))
def test_the_owner_user_key_changes_it(world, name):
    spec, observe, _before = _armed(world, name)
    res = _call(world, spec, headers=world.keys["owner-user-key"]())
    assert res.status_code == 200, res.text
    assert observe() == spec.after


def test_a_person_the_agent_is_shared_with_still_mints_a_voice_token(world):
    spec, observe, _before = _armed(world, "brain-orb-voice-token")
    res = _call(world, spec, headers=_jwt(SHARED))
    assert res.status_code == 200, res.text
    assert observe() == 1


# ---- the uses the rule leaves agent-callable --------------------------------- #


def test_send_voice_reply_still_admits_the_agents_own_key(world):
    """`send_voice_reply` is the USE of the voice-replies grant (SELF)."""
    res = world.client.post(
        f"/api/agents/{AGENT}/voice-reply",
        json={"text": "Hello", "execution_id": "s5-no-such-execution"},
        headers=world.keys["agent-own-key"](),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"delivered": False, "reason": "execution_not_found"}


def test_reading_auto_sync_still_admits_the_agents_own_key(world):
    """The agent-server auto-sync loop reads this flag with the agent's own key."""
    _git_configured()
    res = world.client.get(
        f"/api/agents/{AGENT}/git/auto-sync", headers=world.keys["agent-own-key"]()
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"agent_name": AGENT, "auto_sync_enabled": False}


def test_the_user_key_is_refused_the_voice_token(world):
    """Named for the ruling: a key may not mint a credential."""
    spec, observe, before = _armed(world, "brain-orb-voice-token")
    res = _call(world, spec, headers=world.keys["owner-user-key"]())
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == INTERACTIVE_DETAIL
    assert observe() == before


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"destination_repo": DEST, "github_pat": "s5-placeholder-token"}, id="supplied-token"),
        pytest.param({"destination_repo": DEST}, id="saved-token"),
    ],
)
def test_the_user_key_cannot_bind_a_token_through_the_repository_binding(world, body):
    """The route is INTERACTIVE as a whole: it can persist the caller's saved
    token, which the request does not carry, and it can create a repository on the
    caller's account. A key is refused whether or not the body carries a token,
    and no token is stored."""
    spec, observe, before = _armed(world, "git-bind-to-own-repo")
    res = _call(world, spec, headers=world.keys["owner-user-key"](), json=body)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == INTERACTIVE_DETAIL
    assert observe() == before
    assert not db.has_agent_github_pat(AGENT)


def test_the_user_key_sets_the_pat_as_onboarding_does(world):
    """`/trinity:onboard` "Option 2: tier-1 per-agent token":
    `set_agent_github_pat(agent_name, pat)` on the person's own user-scoped key.
    It is admitted and the token is stored (trinity-enterprise#719 ruling,
    2026-10-28)."""
    spec, observe, _before = _armed(world, "github-pat-set")
    res = _call(world, spec, headers=world.keys["owner-user-key"]())
    assert res.status_code == 200, res.text
    assert observe() is True


def test_the_user_key_still_clears_the_pat(world):
    """A revoke is PERSON, so the person's own key stays a kill switch."""
    spec, observe, _before = _armed(world, "github-pat-clear")
    res = _call(world, spec, headers=world.keys["owner-user-key"]())
    assert res.status_code == 200, res.text
    assert observe() is False


# --------------------------------------------------------------------------- #
# Other shape: a fresh install, git never configured
# --------------------------------------------------------------------------- #


class TestFreshInstallGitNotConfigured:
    """No `agent_git_config` row exists. A person gets the handlers' own answers
    (404 `Git not configured` on the toggles; initialize goes ahead); every
    machine principal is refused before any agent or git-config lookup."""

    @pytest.fixture(autouse=True)
    def _no_git_config(self, world):
        assert db.get_git_config(AGENT) is None

    @pytest.mark.parametrize(
        "suffix", ["git/auto-sync", "git/freeze-schedules-if-failing"]
    )
    def test_a_person_is_told_git_is_not_configured(self, world, suffix):
        res = world.client.put(
            f"/api/agents/{AGENT}/{suffix}", json={"enabled": True}, headers=_jwt(ADMIN)
        )
        assert res.status_code == 404, res.text
        assert res.json()["detail"] == "Git not configured"

    def test_a_person_initializes_git_sync(self, world):
        spec = SPECS["git-initialize"]
        observe = spec.arm(world)
        res = _call(world, spec, headers=_jwt(ADMIN))
        assert res.status_code == 200, res.text
        assert res.json()["success"] is True
        assert observe() == 1

    @pytest.mark.parametrize("key", REAL_MACHINE_KEYS)
    @pytest.mark.parametrize(
        "name", ["git-auto-sync", "git-freeze-schedules", "git-initialize"]
    )
    def test_a_machine_key_is_refused_before_any_lookup(self, world, name, key):
        spec = SPECS[name]
        observe = spec.arm(world) if name == "git-initialize" else (lambda: None)
        headers = world.keys[
            key
        ]()  # minted before counting: auth is not an agent lookup
        lookups = _count_agent_lookups(world.monkeypatch)
        res = _call(world, spec, headers=headers)
        _assert_refused(spec, res, key_authenticated=True)
        assert lookups == [], f"looked up before refusing: {lookups}"
        assert observe() in (None, 0)
        assert db.get_git_config(AGENT) is None


# --------------------------------------------------------------------------- #
# The gate is in the decorator, and the census sees it
# --------------------------------------------------------------------------- #

# By name, not identity: another suite may have re-imported `dependencies`.
_GATES = {PERSON: "require_person", INTERACTIVE: "require_interactive"}


def _registered(spec: Spec):
    path = f"/api/agents/{{agent_name}}/{spec.suffix}"
    for router in _routers():
        for route in router.routes:
            if getattr(route, "path", None) == path and spec.method in getattr(
                route, "methods", ()
            ):
                return route
    raise AssertionError(f"{spec.method} {path} is not registered")


@pytest.mark.parametrize("name", _ids())
def test_the_gate_is_a_decorator_dependency(name):
    """Decorator dependencies run before the signature's ownership dependency,
    which is what keeps the refusal ahead of the agent lookup."""
    spec = SPECS[name]
    route = _registered(spec)
    gates = [
        (d.dependency.__module__, d.dependency.__qualname__) for d in route.dependencies
    ]
    assert gates == [("dependencies", _GATES[spec.rule])]


def test_the_census_classifies_every_route_by_its_rule():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _route_census

    census = _route_census.walk()
    baseline = _route_census.load_baseline()
    for name, spec in SPECS.items():
        route = _registered(spec)
        key = f"{_route_key(route)}"
        assert census.routes[key].code_class == spec.rule, (name, key)
        assert key not in baseline, f"{key} is gated and must leave the baseline"


def _route_key(route) -> str:
    module = route.endpoint.__module__.replace(".", "/") + ".py"
    return f"{module}::{route.endpoint.__qualname__}"

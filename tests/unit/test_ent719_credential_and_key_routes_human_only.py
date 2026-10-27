"""trinity-enterprise#719 — credential, paid-account and key routes are human-only.

The rules these routes follow (#2996; Invariant #8, grant vs use):

* **INTERACTIVE** (a signed-in session only) for a route that binds, mints or
  restores a credential;
* **PERSON** (a signed-in session, or the person's own `user` key) for a route
  that revokes or backs up a credential, names an agent's credentials, or changes
  what outside callers may reach. A revoke stays open to the `user` key, so a
  person's own key is still a kill switch.

By family:

* agent credentials — inject and import are INTERACTIVE; export, env-drift and
  requirements are PERSON;
* paid accounts bound to one agent — subscription assign and Nevermined configure
  are INTERACTIVE; subscription clear and Nevermined delete and toggle are PERSON;
* the connector and Trinity keys — the connector key mint is INTERACTIVE;
  connector config, the connector key revoke, and MCP key revoke and delete are
  PERSON.

Both rules are allowlists over `mcp_scope`, attached in the route decorator so
they run before the path's ownership lookup. A refused caller therefore gets the
same 403 whether or not the addressed agent (or key) exists, and nothing is read
or written.

Driven through the real routers (`credentials`, `subscriptions`, `nevermined`,
`connector`, `mcp_keys`) over a real temp schema (`db_harness`): the ownership and
reach predicates and every row the routes write are the real ones. Only the
agent's container, its internal HTTP server, the credential archive service, the
requirements probe and the audit sink are stood in for. Principals are resolved
`models.User` objects with every scope field set (a stand-in without
`mcp_scope` is its own row, and must fail closed).

Two enforcement layers, each tested where it lives:

* the route gate — `get_current_user` overridden by a resolved principal: the
  agent's own key, an agent sibling inside its manage reach, the `system` key, a
  `user` key, `connector`, `ops`, `portal_delegate`, an unknown scope, a principal
  with no `mcp_scope`, and a loopback JWT carrying a vouched source agent;
* the `get_current_user` entry fence — a real, signed loopback JWT with no
  source agent, through real authentication.

The other shape is the feature off. With no platform encryption key, a person's
export or import gets today's error and every machine principal still gets the
403 first.
With no Nevermined SDK, the same holds for configure and toggle.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import importlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)
for _p in (str(_UNIT), str(_UNIT.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import _route_census as census  # noqa: E402
from db_harness import db_backend, scalar, seed_agent, seed_user  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.connector as r_connector  # noqa: E402
import routers.credentials as r_credentials  # noqa: E402
import routers.mcp_keys as r_mcp_keys  # noqa: E402
import routers.nevermined as r_nevermined  # noqa: E402
import routers.subscriptions as r_subscriptions  # noqa: E402
from models import User  # noqa: E402

HUMAN_ONLY_DETAIL = dependencies.HUMAN_ONLY_DETAIL
INTERACTIVE_DETAIL = (
    "This operation requires an interactive session; MCP API keys cannot perform it"
)
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

OWNER = "s6-owner"
TARGET = "s6-target"  # the agent every agent-scoped path names
PARENT = "s6-parent"  # spawned TARGET with the key it holds now: TARGET is in its manage reach
MISSING = "s6-missing"  # no such agent
MISSING_KEY = "key-that-does-not-exist"
SUBSCRIPTION = "s6-subscription"


# --------------------------------------------------------------------------- #
# Principals
# --------------------------------------------------------------------------- #


def _owner(**kw) -> User:
    """The owning person — an admin, the default install and the worst case:
    every owner predicate answers yes for the role alone."""
    base = dict(id=1, username=OWNER, email="owner@example.com", role="admin")
    base.update(kw)
    return User(**base)


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute at all: the allowlists
    must fail closed on it, never read the absent attribute as the JWT value."""

    id = 1
    username = OWNER
    email = "owner@example.com"
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


MACHINE: Dict[str, Callable[[], Any]] = {
    "own_agent_key": lambda: _owner(
        mcp_scope="agent", agent_name=TARGET, mcp_key_id="key-target"
    ),
    "sibling_agent_key": lambda: _owner(
        mcp_scope="agent", agent_name=PARENT, mcp_key_id="key-parent"
    ),
    "system_key": lambda: _owner(mcp_scope="system"),
    "connector_key": lambda: _owner(mcp_scope="connector", connector_agent=TARGET),
    "ops_key": lambda: _owner(mcp_scope="ops"),
    "portal_delegate_key": lambda: _owner(
        mcp_scope="portal_delegate", portal_delegate=True
    ),
    "unknown_scope": lambda: _owner(mcp_scope="a-scope-no-one-has-shipped"),
    "no_scope_attribute": lambda: _NoScopeAttribute(),
    "vouched_loopback": lambda: _owner(
        mcp_scope=None, is_event_loopback=True, vouched_source_agent=PARENT
    ),
}
def USER_KEY(role: str = "admin") -> User:
    """The person's own `user`-scoped key."""
    return _owner(mcp_scope="user", role=role)


def PERSON(role: str = "admin") -> User:
    """A signed-in session."""
    return _owner(mcp_scope=None, role=role)


# The owner roles the pass paths run for: an admin (the default install) and a
# non-admin owner, who passes only through the real ownership predicates.
OWNER_ROLES = ["admin", "creator"]


def _expected_refusal(rule: str, principal_id: str):
    """The detail each rule answers a refused principal with.

    PERSON answers every refused principal with `HUMAN_ONLY_DETAIL`. INTERACTIVE
    answers a key with `reject_non_interactive_principal`'s sentence, and a
    loopback JWT (which carries no key scope) with `HUMAN_ONLY_DETAIL`."""
    if rule == "person" or principal_id == "vouched_loopback":
        return HUMAN_ONLY_DETAIL
    return INTERACTIVE_DETAIL


def _expect(actual, expected):
    assert actual == expected, (actual, expected)


# --------------------------------------------------------------------------- #
# The agent's runtime, as these routes reach it
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _AgentServer:
    """The agent's internal HTTP server (`agent_httpx_client`). Records calls."""

    def __init__(self):
        self.calls = []

    def client(self, agent_name: str):
        server = self

        class _Ctx:
            async def __aenter__(self):
                return server

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url))
        return _Resp(200, {"files_written": sorted((json or {}).get("files", {}))})

    async def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return _Resp(200, {"env_drift": [{"key": "EXAMPLE_KEY", "state": "file_only"}]})


_REPORT = {
    "agent_name": TARGET,
    "state": "ok",
    "requirements_source": "live_workspace",
    "status_source": "live",
    "degraded_reason": None,
    "requirements": [],
    "summary": {
        "total": 0,
        "set": 0,
        "missing": 0,
        "unknown": 0,
        "blocking": 0,
        "platform_injected_excluded": 0,
        "advisory": 0,
    },
    "errors": [],
}


@pytest.fixture
def owner_role():
    """The owning person's role; the pass-path tests parametrize it."""
    return "admin"


@pytest.fixture
def env(db_backend, monkeypatch, owner_role):  # noqa: F811 — the db_harness fixture, imported above
    """The five real routers on one app, over a fresh schema with one admin
    owner, the target agent, and the parent agent that spawned it."""
    from database import db

    seed_user(1, OWNER, role=owner_role)
    for name in (TARGET, PARENT):
        seed_agent(name, owner_id=1)

    # Spawn provenance (ent#629 manage reach): PARENT spawned TARGET with the key
    # it holds now. Stubbed exactly as test_ent629_agent_reach stubs it.
    class _ParentKey:
        id = "key-parent"

    monkeypatch.setattr(
        dependencies.db,
        "get_agent_ephemeral_info",
        lambda agent: (
            {"spawned_by_agent": PARENT, "spawned_by_key_id": "key-parent"}
            if agent == TARGET
            else None
        ),
    )
    monkeypatch.setattr(
        dependencies.db,
        "get_agent_mcp_api_key",
        lambda agent: _ParentKey() if agent == PARENT else None,
    )

    # Count the lookups each route makes of what it addresses: the ownership
    # reads the path dependencies and inline owner checks make, and, on the two
    # MCP key routes, the key call that looks the key up and acts on it. So
    # "refused before any lookup" is observed, not assumed; the person's pass
    # path asserts the count moves, so no route is out of the counter's sight.
    lookups = {"n": 0}
    for name in (
        "get_agent_owner",
        "can_user_share_agent",
        "can_user_access_agent",
        "revoke_mcp_api_key",
        "delete_mcp_api_key",
    ):
        real = getattr(db, name)

        def counted(*a, _real=real, **k):
            lookups["n"] += 1
            return _real(*a, **k)

        monkeypatch.setattr(db, name, counted)

    # The archive is encrypted with the platform key; the subscription token and
    # the Nevermined key are stored under it too.
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

    # The agent container and its internal server.
    agent = _AgentServer()
    monkeypatch.setattr(r_credentials, "get_agent_container", lambda name: MagicMock())
    monkeypatch.setattr(
        r_credentials,
        "get_agent_status_from_container",
        lambda c: MagicMock(status="running"),
    )
    monkeypatch.setattr(r_credentials, "agent_httpx_client", agent.client)
    import services.docker_service as docker_service

    # The subscription routes restart a RUNNING agent; none is running here.
    monkeypatch.setattr(docker_service, "get_agent_container", lambda name: None)
    # `nevermined._agent_exists` asks Docker first, then the ownership row.
    monkeypatch.setattr(docker_service, "get_agent_by_name", lambda name: None)

    # The credential archive service, resolved by a function-local import at
    # request time (sibling files may swap the sys.modules entry at collection).
    archive = MagicMock()
    archive.export_to_agent = AsyncMock(return_value=(".credentials.enc", 1))
    archive.import_to_agent = AsyncMock(
        return_value={".env": "EXAMPLE_KEY=placeholder"}
    )
    ce = importlib.import_module("services.credential_encryption")
    # The DB layer resolves the same accessor lazily and caches what it gets on
    # its ops objects, which outlive this test: pin a real keyed service there
    # first (restored on teardown), so the stub below never reaches storage.
    for ops in (db._subscription_ops, db._nevermined_ops):
        monkeypatch.setattr(
            ops, "_encryption_service", ce.CredentialEncryptionService(key="ab" * 32)
        )
    monkeypatch.setattr(ce, "get_credential_encryption_service", lambda: archive)

    requirements = AsyncMock(return_value=dict(_REPORT))
    monkeypatch.setattr(
        r_credentials.credential_requirements_service, "get_report", requirements
    )
    invalidated = []
    monkeypatch.setattr(
        r_credentials.credential_requirements_service,
        "invalidate_report_cache",
        lambda name: invalidated.append(name),
    )
    monkeypatch.setattr(r_credentials, "enforce_rate_limit", lambda *a, **k: None)

    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(r_credentials.platform_audit_service, "log", audit)

    from services import subscription_auto_switch

    subscription_auto_switch._reset_locks_for_test()
    monkeypatch.setattr(r_nevermined, "NEVERMINED_AVAILABLE", True)

    app = FastAPI()
    for module in (
        r_credentials,
        r_subscriptions,
        r_nevermined,
        r_connector,
        r_mcp_keys,
    ):
        app.include_router(module.router)
    holder = {}
    app.dependency_overrides[dependencies.get_current_user] = lambda: holder[
        "principal"
    ]
    state = dict(
        db=db,
        app=app,
        client=TestClient(app),
        holder=holder,
        lookups=lookups,
        agent=agent,
        archive=archive,
        requirements=requirements,
        invalidated=invalidated,
        audit=audit,
        ce=ce,
        owner_role=owner_role,
    )
    yield type("Env", (), state)
    app.dependency_overrides.clear()
    subscription_auto_switch._reset_locks_for_test()


# --------------------------------------------------------------------------- #
# The routes, by family
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Route:
    key: str  # the census key
    rule: str  # "person" | "interactive"
    method: str
    path: str  # "{target}" is the agent (or key id) the request names
    request: dict  # a well-formed request
    invalid: Optional[dict] = None  # a request a person is answered 422 for
    seed: Callable = lambda env: TARGET  # sets the state up; returns the target
    effect: Callable = lambda env, target: None  # a snapshot of what the route changes
    done: Callable = lambda env, target, after, response: None  # the person's result
    missing: str = MISSING  # a target that does not exist


# Agent credentials: what the routes touch is the agent's runtime.
def _calls(env, target):
    return (
        tuple(env.agent.calls),
        env.archive.export_to_agent.await_count,
        env.archive.import_to_agent.await_count,
        env.requirements.await_count,
        tuple(env.invalidated),
        env.audit.await_count,
    )


S6A_ROUTES = [
    Route(
        "routers/credentials.py::inject_credentials",
        "interactive",
        "POST",
        "/api/agents/{target}/credentials/inject",
        {"json": {"files": {".env": "EXAMPLE_KEY=placeholder"}}},
        invalid={"json": {"files": "not-a-map"}},
        effect=_calls,
    ),
    Route(
        "routers/credentials.py::export_credentials",
        "person",
        "POST",
        "/api/agents/{target}/credentials/export",
        {},
        effect=_calls,
    ),
    Route(
        "routers/credentials.py::import_credentials",
        "interactive",
        "POST",
        "/api/agents/{target}/credentials/import",
        {},
        effect=_calls,
    ),
    Route(
        "routers/credentials.py::get_agent_env_drift",
        "person",
        "GET",
        "/api/agents/{target}/credentials/env-drift",
        {},
        effect=_calls,
    ),
    Route(
        "routers/credentials.py::get_agent_credential_requirements",
        "person",
        "GET",
        "/api/agents/{target}/credential-requirements",
        {},
        effect=_calls,
    ),
]


# Paid accounts bound to one agent: the rows they write.
def _seed_subscription(env, assign=False):
    sub = env.db.create_subscription(SUBSCRIPTION, "placeholder-token", 1, "max")
    if assign:
        env.db.assign_subscription_to_agent(TARGET, sub.id)
    return TARGET


def _subscription_of(env, target):
    return env.db.get_agent_subscription_id(target)


def _nevermined_row(env, target):
    cfg = env.db.get_nevermined_config(target)
    return None if cfg is None else (cfg.nvm_agent_id, bool(cfg.enabled))


def _seed_nevermined(env):
    env.db.create_or_update_nevermined_config(
        TARGET,
        "sandbox:placeholder",
        "sandbox",
        "did:example:agent",
        "did:example:plan",
        1,
    )
    return TARGET


NEVERMINED_BODY = {
    "nvm_api_key": "sandbox:placeholder",
    "nvm_environment": "sandbox",
    "nvm_agent_id": "did:example:agent",
    "nvm_plan_id": "did:example:plan",
    "credits_per_request": 2,
}


def _assigned(env, target, after, response):
    assert after == env.db.get_subscription_by_name(SUBSCRIPTION).id


def _is_none(env, target, after, response):
    assert after is None


S6B_ROUTES = [
    Route(
        "routers/subscriptions.py::assign_subscription_to_agent",
        "interactive",
        "PUT",
        "/api/subscriptions/agents/{target}",
        {"params": {"subscription_name": SUBSCRIPTION}},
        invalid={},
        seed=lambda env: _seed_subscription(env),
        effect=_subscription_of,
        done=_assigned,
    ),
    Route(
        "routers/subscriptions.py::clear_agent_subscription",
        "person",
        "DELETE",
        "/api/subscriptions/agents/{target}",
        {},
        seed=lambda env: _seed_subscription(env, assign=True),
        effect=_subscription_of,
        done=_is_none,
    ),
    Route(
        "routers/nevermined.py::configure_nevermined",
        "interactive",
        "POST",
        "/api/nevermined/agents/{target}/config",
        {"json": NEVERMINED_BODY},
        invalid={"json": {"nvm_environment": "sandbox"}},
        effect=_nevermined_row,
        done=lambda env, t, after, r: _expect(after, ("did:example:agent", False)),
    ),
    Route(
        "routers/nevermined.py::delete_nevermined_config",
        "person",
        "DELETE",
        "/api/nevermined/agents/{target}/config",
        {},
        seed=_seed_nevermined,
        effect=_nevermined_row,
        done=_is_none,
    ),
    Route(
        "routers/nevermined.py::toggle_nevermined",
        "person",
        "PUT",
        "/api/nevermined/agents/{target}/config/toggle",
        {"params": {"enabled": "true"}},
        invalid={"params": {"enabled": "not-a-boolean"}},
        seed=_seed_nevermined,
        effect=_nevermined_row,
        done=lambda env, t, after, r: _expect(after, ("did:example:agent", True)),
    ),
]


# The connector and Trinity keys: the rows they write.
def _connector_state(env, target):
    cfg = env.db.get_connector_config(target)
    return (
        None if cfg is None else (bool(cfg["enabled"]), cfg["exposed_playbooks"]),
        env.db.get_connector_key_prefix(target),
    )


def _seed_connector_key(env):
    env.db.regenerate_connector_key(TARGET, 1)
    return TARGET


def _seed_mcp_key(env):
    from db_models import McpApiKeyCreate

    return env.db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="s6-person-key")).id


def _mcp_key_row(env, key_id):
    return (
        scalar("SELECT COUNT(*) FROM mcp_api_keys WHERE id = :i", i=key_id),
        scalar("SELECT is_active FROM mcp_api_keys WHERE id = :i", i=key_id),
    )


def _minted(env, target, after, response):
    assert after[1] is not None and response.json()["key_prefix"] == after[1]


S6C_ROUTES = [
    Route(
        "routers/connector.py::update_connector",
        "person",
        "PUT",
        "/api/agents/{target}/connector",
        {"json": {"enabled": True, "exposed_playbooks": ["daily-brief"]}},
        invalid={"json": {"exposed_playbooks": "not-a-list"}},
        effect=_connector_state,
        done=lambda env, t, after, r: _expect(after[0], (True, ["daily-brief"])),
    ),
    Route(
        "routers/connector.py::regenerate_connector_key",
        "interactive",
        "POST",
        "/api/agents/{target}/connector/key",
        {},
        effect=_connector_state,
        done=_minted,
    ),
    Route(
        "routers/connector.py::revoke_connector_key",
        "person",
        "DELETE",
        "/api/agents/{target}/connector/key",
        {},
        seed=_seed_connector_key,
        effect=_connector_state,
        done=lambda env, t, after, r: _expect(after[1], None),
    ),
    Route(
        "routers/mcp_keys.py::revoke_mcp_api_key_endpoint",
        "person",
        "POST",
        "/api/mcp/keys/{target}/revoke",
        {},
        seed=_seed_mcp_key,
        effect=_mcp_key_row,
        done=lambda env, t, after, r: _expect(after, (1, 0)),
        missing=MISSING_KEY,
    ),
    Route(
        "routers/mcp_keys.py::delete_mcp_api_key_endpoint",
        "person",
        "DELETE",
        "/api/mcp/keys/{target}",
        {},
        seed=_seed_mcp_key,
        effect=_mcp_key_row,
        done=lambda env, t, after, r: _expect(after[0], 0),
        missing=MISSING_KEY,
    ),
]

ROUTES = S6A_ROUTES + S6B_ROUTES + S6C_ROUTES
_IDS = [r.key.split("::")[1] for r in ROUTES]


def _send(
    env, route: Route, target: str, principal=None, request: Optional[dict] = None
):
    if principal is not None:
        env.holder["principal"] = principal
    return env.client.request(
        route.method,
        route.path.format(target=target),
        **(route.request if request is None else request),
    )


# --------------------------------------------------------------------------- #
# The census sees each route under its rule
# --------------------------------------------------------------------------- #

_CENSUS = census.walk()


def test_each_route_appears_once_under_one_rule():
    assert len({r.key for r in ROUTES}) == len(ROUTES)
    assert {r.rule for r in ROUTES} <= {"person", "interactive"}


@pytest.mark.parametrize("route", ROUTES, ids=_IDS)
def test_the_census_recognises_the_gate_and_the_baseline_has_let_go(route):
    classes, failures = census.classify(_CENSUS, census.load_baseline())
    assert classes[route.key] == route.rule
    assert route.key not in census.load_baseline()
    assert not [f for f in failures if route.key in f[1]]


def test_the_sibling_is_inside_the_targets_manage_reach(env):
    """The sibling row is a real sibling: without the gate, the owner fence
    would let it through to TARGET (ent#629 manage reach, by spawn)."""
    sibling = MACHINE["sibling_agent_key"]()
    assert dependencies.agent_may_reach(sibling, TARGET, manage=True) is True
    assert (
        dependencies.agent_may_reach(MACHINE["own_agent_key"](), TARGET, manage=True)
        is True
    )


# --------------------------------------------------------------------------- #
# Matrix M: every refused principal, on an existing and a missing target, with
# a well-formed and a schema-invalid request — the same 403, nothing touched
# --------------------------------------------------------------------------- #


def _refused(rule: str):
    ids = list(MACHINE)
    if rule == "interactive":
        ids.append("user_key")
    return ids


def _principal(principal_id: str):
    return USER_KEY() if principal_id == "user_key" else MACHINE[principal_id]()


@pytest.mark.parametrize("route", ROUTES, ids=_IDS)
def test_every_refused_principal_gets_the_same_403_and_nothing_changes(env, route):
    target = route.seed(env)
    before = route.effect(env, target)
    variants = [("existing", target, None), ("missing", route.missing, None)]
    if route.invalid is not None:
        variants.append(("schema-invalid", target, route.invalid))

    wrong = []
    for principal_id in _refused(route.rule):
        expected = _expected_refusal(route.rule, principal_id)
        for variant, name, request in variants:
            r = _send(env, route, name, _principal(principal_id), request)
            detail = (
                r.json().get("detail")
                if r.headers.get("content-type", "").startswith("application/json")
                else None
            )
            if r.status_code != 403 or detail != expected:
                wrong.append((principal_id, variant, r.status_code, detail))

    assert wrong == [], f"refused principals answered otherwise: {wrong}"
    assert route.effect(env, target) == before, "a refused request changed state"
    assert env.lookups["n"] == 0, "the route looked the target up before refusing"


# --------------------------------------------------------------------------- #
# The real entry fence: an unvouched loopback JWT, no override
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ROUTES, ids=_IDS)
def test_an_unvouched_loopback_jwt_is_refused_by_the_entry_fence(
    env, route, monkeypatch
):
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    deps = dependencies.get_current_user.__globals__
    monkeypatch.setattr(
        deps["db"],
        "get_user_by_username",
        lambda *_a, **_k: {
            "id": 1,
            "username": OWNER,
            "email": "owner@example.com",
            "role": "admin",
            "suspended_at": None,
        },
    )
    monkeypatch.setitem(deps, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {
            "sub": OWNER,
            "scope": deps["EVENT_LOOPBACK_SCOPE"],
            "exp": int(time.time()) + 300,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    target = route.seed(env)
    before = route.effect(env, target)
    env.app.dependency_overrides.clear()  # real authentication

    r = env.client.request(
        route.method,
        route.path.format(target=target),
        headers={"Authorization": f"Bearer {token}"},
        **route.request,
    )

    assert r.status_code == 403, r.text
    assert r.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert route.effect(env, target) == before


# --------------------------------------------------------------------------- #
# The principals the rule keeps
# --------------------------------------------------------------------------- #


def _assert_person_succeeded(env, route: Route, target: str, before, r):
    assert r.status_code == 200, r.text
    assert env.lookups["n"] > 0, "the lookup counter does not see this route"
    after = route.effect(env, target)
    assert after != before, "the person's request changed nothing"
    route.done(env, target, after, r)


@pytest.mark.parametrize("owner_role", OWNER_ROLES)
@pytest.mark.parametrize("route", ROUTES, ids=_IDS)
def test_a_signed_in_person_succeeds(env, route):
    target = route.seed(env)
    before = route.effect(env, target)
    r = _send(env, route, target, PERSON(env.owner_role))
    _assert_person_succeeded(env, route, target, before, r)


@pytest.mark.parametrize("owner_role", OWNER_ROLES)
@pytest.mark.parametrize("route", ROUTES, ids=_IDS)
def test_the_persons_user_key(env, route):
    """PERSON admits the person's own `user` key (a revoke stays a kill switch);
    INTERACTIVE refuses it — a key does not bind or mint a credential."""
    target = route.seed(env)
    before = route.effect(env, target)
    r = _send(env, route, target, USER_KEY(env.owner_role))
    if route.rule == "person":
        _assert_person_succeeded(env, route, target, before, r)
    else:
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == INTERACTIVE_DETAIL
        assert route.effect(env, target) == before
        assert env.lookups["n"] == 0


@pytest.mark.parametrize(
    "route",
    [r for r in ROUTES if r.invalid is not None],
    ids=[i for r, i in zip(ROUTES, _IDS) if r.invalid is not None],
)
def test_a_person_still_gets_the_validation_answer(env, route):
    """The schema-invalid request in the matrix is a real one: a person gets 422."""
    target = route.seed(env)
    r = _send(env, route, target, PERSON(), route.invalid)
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# Other shape: the feature off
# --------------------------------------------------------------------------- #


def _archive_without_a_key(env, monkeypatch):
    """The real archive service with the platform key unset: it reads the agent's
    files (stood in) and fails where it encrypts or decrypts, as it does today."""
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY_SECONDARY", raising=False)
    service = env.ce.CredentialEncryptionService()
    service.list_agent_credential_files = AsyncMock(return_value=[".env"])
    service.read_agent_credential_files_split = AsyncMock(
        return_value=({".env": "EXAMPLE_KEY=placeholder"}, {})
    )
    service.read_agent_credential_files = AsyncMock(
        # A well-formed archive written by an instance that has a key.
        return_value={
            ".credentials.enc": env.ce.CredentialEncryptionService(
                key="cd" * 32
            ).encrypt_files({".env": "EXAMPLE_KEY=placeholder"})
        }
    )
    service.write_agent_credential_files = AsyncMock(return_value=None)
    monkeypatch.setattr(env.ce, "get_credential_encryption_service", lambda: service)
    return service


_KEYLESS = [
    r
    for r in ROUTES
    if r.key.split("::")[1] in ("export_credentials", "import_credentials")
]
_NO_SDK = [
    r
    for r in ROUTES
    if r.key.split("::")[1] in ("configure_nevermined", "toggle_nevermined")
]


@pytest.mark.parametrize(
    "route", _KEYLESS, ids=[r.key.split("::")[1] for r in _KEYLESS]
)
def test_no_encryption_key_the_person_gets_todays_error_and_machines_403_first(
    env, route, monkeypatch
):
    service = _archive_without_a_key(env, monkeypatch)

    r = _send(env, route, TARGET, PERSON())
    assert r.status_code == 400, r.text
    assert "Encryption key not configured" in r.json()["detail"]

    service.write_agent_credential_files.reset_mock()
    service.read_agent_credential_files.reset_mock()
    service.read_agent_credential_files_split.reset_mock()
    for principal_id in _refused(route.rule):
        r = _send(env, route, TARGET, _principal(principal_id))
        assert r.status_code == 403, (principal_id, r.text)
        assert r.json()["detail"] == _expected_refusal(route.rule, principal_id)
    assert service.read_agent_credential_files.await_count == 0
    assert service.read_agent_credential_files_split.await_count == 0
    assert service.write_agent_credential_files.await_count == 0


@pytest.mark.parametrize("route", _NO_SDK, ids=[r.key.split("::")[1] for r in _NO_SDK])
def test_no_nevermined_sdk_the_person_gets_todays_error_and_machines_403_first(
    env, route, monkeypatch
):
    monkeypatch.setattr(r_nevermined, "NEVERMINED_AVAILABLE", False)
    target = route.seed(env)
    before = route.effect(env, target)

    r = _send(env, route, target, PERSON())
    assert r.status_code == 501, r.text
    assert "payments-py not installed" in r.json()["detail"]

    for principal_id in _refused(route.rule):
        r = _send(env, route, target, _principal(principal_id))
        assert r.status_code == 403, (principal_id, r.text)
        assert r.json()["detail"] == _expected_refusal(route.rule, principal_id)
    assert route.effect(env, target) == before

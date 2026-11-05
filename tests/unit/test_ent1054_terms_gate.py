"""The terms-acceptance gate on the Workspace-only principal (abilityai/trinity-enterprise#1054).

`services/terms_gate.py` is an OSS seam, shaped like `mfa_gate`: an enterprise
module registers a provider; `resolve_platform_user_unfloored` consults it ONCE,
after every scope fence and the suspension check, so the gate covers every door
a Workspace-only person has — the Workspace (`get_portal_principal`), the rooms
principal, the Workspace voice start, and every `@workspace_route` operator
route reached through `get_current_user` (the `/ws` ticket mint included).

The rules the tests pin:

* no provider → the instance's own decision applies (trinity-enterprise#1107:
  the terms are an OSS setting with a shipped default, gating every person);
* operator and above are gated on their VISITS too (ent#1107) — a provider
  fault falls back to the instance decision for them, so a broken optional
  module cannot lock an admin out, while their machine credentials (a
  user-scoped key) are never gated;
* a Workspace-only person is refused with 403 `{"code": "terms_required", ...}`
  on every door while the provider says so, and the provider's own reason
  (`invite_expired`, `invite_revoked`) is passed through;
* a provider that raises fails CLOSED for a Workspace-only person (the gated
  principals are exactly the invitees), unlike `mfa_gate`'s fail-open;
* a route marked `@terms_route` is exempt — sign-out, the viewer's own identity
  read, and the acceptance routes themselves must answer before acceptance;
* an agent's key is untouched: the gate is for persons.

Every request here is a REAL Starlette request routed by FastAPI, or built from
an ASGI scope. Modules are imported inside each test: the unit conftest evicts
`dependencies` between tests.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"


@pytest.fixture(autouse=True)
def _clean_terms_state():
    """trinity-enterprise#1107: the instance decision reads real rows, so start
    and end every test with no terms configured and no acceptance."""
    from database import db
    from db.engine import get_engine

    def reset():
        for key in ("terms_required", "terms_version", "terms_text", "terms_title"):
            try:
                db.delete_setting(key)
            except Exception:  # noqa: BLE001
                pass
        with get_engine().begin() as conn:
            conn.exec_driver_sql("DELETE FROM terms_acceptances")

    reset()
    yield
    reset()


def _switch_off():
    from database import db
    db.set_setting("terms_required", "false")


def _row(role, username="member"):
    return {
        "id": 7, "username": username, "email": f"{username}@example.com",
        "role": role, "suspended_at": None,
    }


@pytest.fixture
def jwt_as(monkeypatch):
    """A real platform JWT whose `sub` resolves to a user holding `role`."""
    import database
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    def make(role, username="member"):
        monkeypatch.setattr(
            database.db, "get_user_by_username", lambda name, *_a, **_k: _row(role, name)
        )
        return jwt.encode(
            {"sub": username, "exp": int(time.time()) + 300}, SECRET_KEY, algorithm=ALGORITHM
        )

    return make


@pytest.fixture
def agent_key_as(monkeypatch):
    """An agent-scoped MCP key for `agent`, owned by a user holding `owner_role`."""
    import database

    def make(owner_role, agent="atlas", scope="agent"):
        monkeypatch.setattr(database.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": scope, "agent_name": agent if scope == "agent" else None,
            "key_id": "k1", "key_name": scope,
            "user_id": "owner", "user_email": "owner@example.com",
        })
        monkeypatch.setattr(
            database.db, "get_user_by_email", lambda *_a, **_k: _row(owner_role, "owner")
        )
        monkeypatch.setattr(
            database.db, "get_agent_ephemeral_info", lambda *_a, **_k: {"is_ephemeral": False}
        )
        return "trinity_mcp_fake_agent_key"

    return make


class _Provider:
    """A recording provider: `answer` is the decision, or an exception to raise."""

    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def decision(self, user):
        self.seen.append((user.username, user.role))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def provider():
    """Register a provider for the test and always clear it afterwards."""
    from services import terms_gate

    made = []

    def install(answer):
        p = _Provider(answer)
        terms_gate.register_provider(p)
        made.append(p)
        return p

    yield install
    terms_gate.clear_provider()


REQUIRED = {"required": True, "version": "2026-11", "reason": "terms_required"}
NOT_REQUIRED = {"required": False, "version": "2026-11", "reason": "terms_required"}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _plain_request(method="GET", path="/api/anything"):
    """An ASGI-scope request that matched NO route (no `scope["route"]`)."""
    return Request({
        "type": "http", "method": method, "path": path, "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "headers": [(b"host", b"backend:8000")],
    })


def _probe_app():
    """A marked Workspace route, a plain operator route, and a terms route —
    all through the REAL `get_current_user`."""
    from fastapi import APIRouter, Depends, FastAPI
    import dependencies as deps

    router = APIRouter(prefix="/api/probe")

    @router.get("/workspace")
    @deps.workspace_route("probe: the Workspace calls it")
    async def workspace(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    @router.get("/terms")
    @deps.workspace_route("probe: the terms screen calls it")
    @deps.terms_route("probe: it must answer before acceptance")
    async def terms(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    @router.get("/plain")
    async def plain(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# No provider: the OSS path is untouched
# --------------------------------------------------------------------------- #

class TestWithoutAProvider:
    def test_a_user_is_gated_by_the_instance_decision(self, jwt_as):
        """trinity-enterprise#1107: no provider is not "no gate" any more — the
        shipped default terms are outstanding until accepted."""
        from services import terms_gate

        terms_gate.clear_provider()
        assert terms_gate.get_provider() is None
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "terms_required"

    def test_a_user_passes_a_workspace_route_once_the_switch_is_off(self, jwt_as):
        from services import terms_gate

        terms_gate.clear_provider()
        _switch_off()
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 200, r.text
        assert r.json() == {"who": "member"}

    def test_the_floor_still_refuses_a_user_off_its_routes(self, jwt_as):
        _switch_off()
        r = _probe_app().get("/api/probe/plain", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "workspace_only"


# --------------------------------------------------------------------------- #
# The refusal, on every door
# --------------------------------------------------------------------------- #

class TestTheRefusalOnEveryDoor:
    def test_a_workspace_route_refuses_a_user_until_accepted(self, jwt_as, provider):
        p = provider(REQUIRED)
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "terms_required"
        assert r.json()["detail"]["version"] == "2026-11"
        assert p.seen == [("member", "user")]

    def test_the_same_route_opens_once_the_provider_is_satisfied(self, jwt_as, provider):
        provider(NOT_REQUIRED)
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 200, r.text

    def test_the_portal_door_refuses_a_user_until_accepted(self, monkeypatch, jwt_as, provider):
        import client_portal.db as portal_db
        from client_portal.portal_auth import get_portal_principal
        from fastapi import HTTPException
        from starlette.responses import Response

        provider(REQUIRED)
        monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(get_portal_principal(_plain_request(), Response(), token=jwt_as("user")))
        assert refused.value.status_code == 403
        assert refused.value.detail["code"] == "terms_required"

    def test_the_rooms_door_refuses_a_user_until_accepted(self, jwt_as, provider):
        """The rooms principal falls back to the portal path when the platform
        resolver raises — the gate's 403 must not be swallowed into that fallback
        and re-raised as a 401 or a portal principal."""
        from fastapi import HTTPException
        from shared_sessions.router import get_room_principal
        from starlette.responses import Response

        provider(REQUIRED)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(get_room_principal(_plain_request(), Response(), token=jwt_as("user")))
        assert refused.value.status_code == 403
        assert refused.value.detail["code"] == "terms_required"

    def test_the_providers_own_reason_is_passed_through(self, jwt_as, provider):
        provider({"required": True, "version": "2026-11", "reason": "invite_expired"})
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "invite_expired"

    def test_an_unknown_reason_collapses_to_terms_required(self, jwt_as, provider):
        """The refusal codes are a closed set the SPA switches on."""
        provider({"required": True, "version": "v", "reason": "something_else"})
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.json()["detail"]["code"] == "terms_required"

    def test_a_role_outside_the_ladder_is_gated_too(self, jwt_as, provider):
        provider(REQUIRED)
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("viewer")))
        assert r.status_code == 403
        # The floor refuses a role outside the ladder on the Workspace route? No:
        # the marked route admits it, so the terms gate is what answers here.
        assert r.json()["detail"]["code"] == "terms_required"

    def test_a_users_own_user_scoped_key_is_gated(self, agent_key_as, provider):
        """A person-scoped MCP key is the person; it does not walk around the gate."""
        from fastapi import HTTPException
        import dependencies as deps

        p = provider(REQUIRED)
        token = agent_key_as("user", scope="user")
        with pytest.raises(HTTPException) as refused:
            asyncio.run(deps.resolve_platform_user_unfloored(_plain_request(), token))
        assert refused.value.status_code == 403
        assert refused.value.detail["code"] == "terms_required"
        assert p.seen == [("owner", "user")]


# --------------------------------------------------------------------------- #
# Who is never gated
# --------------------------------------------------------------------------- #

class TestWhoIsGatedAndWhoIsNot:
    @pytest.mark.parametrize("role", ["operator", "creator", "admin"])
    def test_operator_and_above_are_gated_on_their_visits(self, jwt_as, provider, role):
        """trinity-enterprise#1107: every person's first visit, the admin's included."""
        p = provider(REQUIRED)
        r = _probe_app().get("/api/probe/plain", headers=_bearer(jwt_as(role)))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "terms_required"
        assert p.seen == [("member", role)]

    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_a_raising_provider_falls_back_to_the_instance_decision_for_an_operator(
        self, jwt_as, provider, role
    ):
        """A broken optional module must not lock an administrator out: the
        door layer is ignored for them and the instance decision stands —
        the shipped default is outstanding (accept it), or the switch is off."""
        from services import terms_defaults

        provider(RuntimeError("the provider is broken"))
        r = _probe_app().get("/api/probe/plain", headers=_bearer(jwt_as(role)))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "terms_required"
        assert r.json()["detail"]["version"] == terms_defaults.DEFAULT_TERMS_VERSION
        _switch_off()
        r = _probe_app().get("/api/probe/plain", headers=_bearer(jwt_as(role)))
        assert r.status_code == 200, r.text

    def test_an_operators_user_scoped_key_is_not_gated(self, agent_key_as, provider):
        """A machine credential of operator-and-above is for automation, not a
        visit: an ops key keeps working while the admin has not signed in."""
        import dependencies as deps

        p = provider(REQUIRED)
        user = asyncio.run(
            deps.resolve_platform_user_unfloored(_plain_request(), agent_key_as("admin", scope="user"))
        )
        assert user.role == "admin" and user.mcp_scope == "user"
        assert p.seen == []

    def test_an_agent_key_of_a_workspace_only_owner_is_untouched(self, agent_key_as, provider):
        """The agent's own runtime (heartbeat, reports, asks) keeps working while
        its owner has not accepted: the gate is for persons."""
        import dependencies as deps

        p = provider(REQUIRED)
        user = asyncio.run(
            deps.resolve_platform_user_unfloored(_plain_request(), agent_key_as("user"))
        )
        assert user.agent_name == "atlas"
        assert p.seen == []


# --------------------------------------------------------------------------- #
# Fail closed for the gated principals
# --------------------------------------------------------------------------- #

class TestFailClosed:
    def test_a_raising_provider_refuses_a_user(self, jwt_as, provider, caplog):
        provider(RuntimeError("the provider is broken"))
        with caplog.at_level("ERROR"):
            r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "terms_required"
        assert r.json()["detail"]["version"] is None
        assert any("terms_gate" in rec.message or "terms_gate" in rec.name for rec in caplog.records)

    def test_a_provider_answering_nothing_refuses_a_user(self, jwt_as, provider):
        """`None` is not an answer: a provider that cannot decide has not admitted."""
        provider(None)
        r = _probe_app().get("/api/probe/workspace", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "terms_required"


# --------------------------------------------------------------------------- #
# Exempt routes
# --------------------------------------------------------------------------- #

class TestExemptRoutes:
    def test_a_terms_route_answers_before_acceptance(self, jwt_as, provider):
        p = provider(REQUIRED)
        r = _probe_app().get("/api/probe/terms", headers=_bearer(jwt_as("user")))
        assert r.status_code == 200, r.text
        assert p.seen == []

    def test_the_marker_needs_a_reason(self):
        import dependencies as deps

        with pytest.raises(ValueError):
            deps.terms_route("")

    def test_the_marker_leaves_the_function_itself(self):
        import dependencies as deps

        async def endpoint():
            return None

        marked = deps.terms_route("why")(endpoint)
        assert marked is endpoint
        assert getattr(endpoint, deps.TERMS_ROUTE_ATTR) == "why"

    def test_sign_out_and_the_identity_read_are_marked(self):
        """`POST /api/auth/logout` and `GET /api/users/me` must answer a person who
        has not accepted: the SPA reads the role to land them on the terms screen,
        and they must be able to leave. Shape check — main.py is not importable in
        the unit island; the marker itself is executed in the probe above."""
        expected = {
            ("main.py", "get_current_user_info"),
            ("routers/auth.py", "logout"),
        }
        for rel, fn_name in expected:
            tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
            fn = next(
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
            )
            marks = [
                d for d in fn.decorator_list
                if isinstance(d, ast.Call) and (
                    getattr(d.func, "id", None) == "terms_route"
                    or getattr(d.func, "attr", None) == "terms_route"
                )
            ]
            assert len(marks) == 1, f"{rel}::{fn_name} must carry exactly one @terms_route"
            route_idx = next(
                i for i, d in enumerate(fn.decorator_list)
                if isinstance(d, ast.Call) and getattr(d.func, "attr", None) in
                {"get", "post", "put", "patch", "delete", "api_route"}
            )
            assert fn.decorator_list.index(marks[0]) > route_idx, (
                f"{rel}::{fn_name}: the mark must sit BELOW the route decorator"
            )

    def test_token_validation_never_reaches_the_resolver(self):
        """`GET /api/auth/validate` reads the token itself (nginx auth_request) and
        never resolves a principal, so it is exempt by construction, not by mark."""
        tree = ast.parse((BACKEND / "routers/auth.py").read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "validate_token"
        )
        names = {
            getattr(n, "id", None) or getattr(n, "attr", None)
            for n in ast.walk(fn) if isinstance(n, (ast.Name, ast.Attribute))
        }
        assert "get_current_user" not in names
        assert "resolve_platform_user_unfloored" not in names


# --------------------------------------------------------------------------- #
# The consult is one call, in the shared resolver
# --------------------------------------------------------------------------- #

class TestOneConsult:
    def test_the_resolver_consults_the_gate_exactly_once_and_last(self):
        """Every door shares `resolve_platform_user_unfloored`; the gate is one
        call there, after the credential is fully resolved, so no door and no
        scope fence can be skipped by a second code path."""
        tree = ast.parse((BACKEND / "dependencies.py").read_text(encoding="utf-8"))
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "enforce"
                            and getattr(sub.func.value, "id", None) == "terms_gate"):
                        calls.append(node.name)
        assert calls == ["resolve_platform_user_unfloored"]

    def test_the_gate_runs_after_the_suspension_check(self, monkeypatch, provider):
        """A suspended person is refused with the resolver's own 401 before the
        provider is ever asked — suspension stays the stronger refusal."""
        import database
        from config import ALGORITHM, SECRET_KEY
        from fastapi import HTTPException
        from jose import jwt
        import dependencies as deps

        p = provider(REQUIRED)
        row = dict(_row("user"), suspended_at="2026-11-02T00:00:00Z")
        monkeypatch.setattr(database.db, "get_user_by_username", lambda *_a, **_k: row)
        token = jwt.encode({"sub": "member", "exp": int(time.time()) + 300}, SECRET_KEY,
                           algorithm=ALGORITHM)
        with pytest.raises(HTTPException) as refused:
            asyncio.run(deps.resolve_platform_user_unfloored(_plain_request(), token))
        assert refused.value.status_code == 401
        assert p.seen == []

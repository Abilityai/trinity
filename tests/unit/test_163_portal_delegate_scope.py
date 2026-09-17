"""`portal_delegate` key scope — OSS containment primitive (ent#163).

OSS owns the scope, the mint, and the fence; the entitled module owns the
endpoint that decides whether a given email may be delegated. This suite pins
the OSS half, because that half is what makes the capability safe to hand to a
third party at all.

The threat it answers: a delegate key resolves to the KEY OWNER, exactly like
every other MCP key. Unfenced it would simply BE an admin's key — the delegation
feature would ship a fleet-wide credential. The fence confines it to one route.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _deps():
    try:
        import dependencies
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return dependencies


def test_the_fence_is_a_single_route_not_a_prefix():
    """A prefix would grow silently: every future portal endpoint would inherit
    delegate access the day it is added. The minted session — not this key —
    drives the portal surface, so one exchange route is all it ever needs."""
    d = _deps()
    assert d.PORTAL_DELEGATE_SCOPE == "portal_delegate"
    assert d.PORTAL_DELEGATE_ALLOWED_ROUTES == {
        ("POST", "/api/enterprise/client-portal/auth/exchange")
    }
    assert len(d.PORTAL_DELEGATE_ALLOWED_ROUTES) == 1


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/agents"),                                    # the fleet
        ("GET", "/api/users"),                                     # other humans
        ("POST", "/api/agents/atlas/chat"),                        # chat as the owner
        ("GET", "/api/enterprise/client-portal/my-agents"),        # the portal itself
        ("GET", "/api/enterprise/client-portal/briefings"),        # #2163 — fenced by construction
        ("GET", "/api/enterprise/client-portal/agents/a/history"), # someone's history
        ("POST", "/api/mcp/keys"),                                 # minting more keys
        ("GET", "/api/enterprise/client-portal/auth/exchange"),    # right path, wrong method
    ],
)
def test_everything_except_the_exchange_route_is_out_of_reach(method, path):
    d = _deps()
    assert (method, path) not in d.PORTAL_DELEGATE_ALLOWED_ROUTES


def test_the_exchange_route_itself_is_reachable():
    d = _deps()
    assert ("POST", "/api/enterprise/client-portal/auth/exchange") in d.PORTAL_DELEGATE_ALLOWED_ROUTES


def test_user_model_defaults_to_not_delegated():
    """A consumer must branch on the flag, and absence must mean 'no'. If the
    default were True, any principal built without the field would silently gain
    the capability."""
    from models import User

    assert User(id=1, username="u").portal_delegate is False


def test_minting_the_scope_is_admin_only_and_human_only():
    """It reads other people's conversations. An ordinary user must not be able
    to self-issue one, and neither may an AGENT — an agent-scoped key resolves
    to its owner carrying the owner's role, so on a default admin-owned install
    a bare role check passes (trinity-ops-agent#232)."""
    from fastapi import HTTPException

    d = _deps()
    non_admin = SimpleNamespace(id=2, username="bob", role="user",
                                agent_name=None, connector_agent=None, mcp_scope=None)
    with pytest.raises(HTTPException) as exc:
        d.assert_admin(non_admin)
    assert exc.value.status_code == 403

    agent_principal = SimpleNamespace(id=1, username="admin", role="admin",
                                      agent_name="atlas", connector_agent=None, mcp_scope=None)
    with pytest.raises(HTTPException) as exc:
        d.reject_agent_principal(agent_principal)
    assert exc.value.status_code == 403


def test_db_layer_refuses_scopes_bound_to_an_agent():
    """Validate at the boundary AND at the sink. `agent`/`connector`/`system`
    keys carry an agent binding and are minted by their own code paths; letting
    this endpoint mint one would forge an agent principal with no agent."""
    try:
        from db.mcp_keys import McpKeyOperations
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    creatable = McpKeyOperations._USER_CREATABLE_SCOPES
    assert "user" in creatable and "portal_delegate" in creatable
    for forbidden in ("agent", "connector", "system"):
        assert forbidden not in creatable


# ---------------------------------------------------------------------------
# Behavioural: the fence must actually fire, not merely be declared
# ---------------------------------------------------------------------------

def _request(method: str, path: str):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(request_id="r1"),
        headers={},
    )


@pytest.fixture()
def delegate_key(monkeypatch):
    """A valid `portal_delegate` key owned by an admin — the worst case: if the
    fence leaks, the caller IS an admin principal."""
    d = _deps()
    monkeypatch.setattr(
        d.db, "validate_mcp_api_key",
        lambda token, **kw: {
            "user_email": "admin@example.com", "user_id": "admin",
            "scope": "portal_delegate", "agent_name": None,
        })
    monkeypatch.setattr(
        d.db, "get_user_by_email",
        lambda email: {"id": 1, "username": "admin", "email": email,
                       "role": "admin", "suspended_at": None})
    return d


@pytest.mark.asyncio
async def test_delegate_key_is_accepted_on_the_exchange_route(delegate_key):
    d = delegate_key
    user = await d.get_current_user(
        _request("POST", "/api/enterprise/client-portal/auth/exchange"), "trinity_mcp_x")
    assert user.portal_delegate is True
    # It still resolves to the OWNER — which is exactly why the endpoint must
    # branch on the flag rather than on this user.
    assert user.username == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/agents"),
        ("GET", "/api/users"),
        ("POST", "/api/mcp/keys"),
        ("GET", "/api/enterprise/client-portal/my-agents"),
        ("GET", "/api/enterprise/client-portal/auth/exchange"),  # wrong method
    ],
)
async def test_delegate_key_is_refused_everywhere_else(delegate_key, method, path):
    """The regression that matters: without this the feature ships an admin
    credential to a third party."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await delegate_key.get_current_user(_request(method, path), "trinity_mcp_x")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_ordinary_user_key_is_untouched_by_the_fence(monkeypatch):
    """The fence keys off scope, so a normal key must be unaffected on the very
    paths the delegate key is refused."""
    d = _deps()
    monkeypatch.setattr(
        d.db, "validate_mcp_api_key",
        lambda token, **kw: {
            "user_email": "bob@example.com", "user_id": "bob",
            "scope": "user", "agent_name": None,
        })
    monkeypatch.setattr(
        d.db, "get_user_by_email",
        lambda email: {"id": 2, "username": "bob", "email": email,
                       "role": "user", "suspended_at": None})

    user = await d.get_current_user(_request("GET", "/api/agents"), "trinity_mcp_y")
    assert user.username == "bob"
    assert user.portal_delegate is False

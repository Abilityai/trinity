"""An admin gate is never agent-callable (ent#293).

An agent-scoped MCP key resolves to its owner **carrying the owner's role**, so
on a default admin-owned install every agent's injected `TRINITY_MCP_API_KEY`
satisfied every `assert_admin` / `require_admin` gate. The concrete chain: a
prompt-injected agent repoints `skills_library_url` at an attacker-controlled
repo through the generic `PUT /api/settings/{key}`, and the scheduled sync then
writes attacker-authored `SKILL.md` files into every running agent — instructions
Claude executes, persistent across restarts. One compromised agent becomes all of
them.

This is the THIRD occurrence of the class (trinity-ops-agent#232 → #1644 → #1816),
each previously closed by bolting `reject_agent_principal` onto one more
endpoint. So the fix moves into the gate, and these tests pin the gate rather
than any single route — a fourth endpoint added tomorrow inherits the protection
without anyone remembering to ask for it.

**No MagicMock anywhere below, deliberately.** A bare `MagicMock` auto-creates a
truthy `.agent_name`, so it reads as an agent key and passes these tests for the
wrong reason — the trap #1816 recorded. Every principal here is an explicit
object with explicit fields.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@dataclass
class _Principal:
    """An explicit stand-in for `models.User` — every field stated."""
    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False


def _deps():
    try:
        import dependencies
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return dependencies


# The exact principal the exploit uses: an agent's own key on an admin-owned
# install. It carries role="admin" because that is precisely the bug.
AGENT_KEY = _Principal(username="admin", role="admin", agent_name="prospector")
HUMAN_ADMIN = _Principal(username="admin", role="admin", agent_name=None)
SYSTEM_KEY = _Principal(username="admin", role="admin", agent_name=None)
CONNECTOR_KEY = _Principal(role="admin", connector_agent="atlas")
NON_ADMIN = _Principal(username="bob", role="user", agent_name=None)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_assert_admin_rejects_an_agent_scoped_key():
    from fastapi import HTTPException

    d = _deps()
    with pytest.raises(HTTPException) as exc:
        d.assert_admin(AGENT_KEY)
    assert exc.value.status_code == 403


def test_require_admin_rejects_an_agent_scoped_key():
    """The `Depends` form must match the imperative one — half a fix is how the
    class survived three rounds."""
    from fastapi import HTTPException

    d = _deps()
    with pytest.raises(HTTPException) as exc:
        d.require_admin(AGENT_KEY)
    assert exc.value.status_code == 403


def test_the_agent_principal_carries_the_owners_admin_role():
    """Pins WHY the gate was wrong, so nobody 'simplifies' the fix away by
    reasoning that an agent key could not have been an admin in the first
    place."""
    assert AGENT_KEY.role == "admin"
    assert AGENT_KEY.agent_name == "prospector"


# ---------------------------------------------------------------------------
# Non-regression — the fix must not lock out legitimate callers
# ---------------------------------------------------------------------------

def test_a_human_admin_still_passes_both_gates():
    d = _deps()
    d.assert_admin(HUMAN_ADMIN)
    assert d.require_admin(HUMAN_ADMIN) is HUMAN_ADMIN


def test_a_system_scoped_key_still_passes():
    """`trinity-system` must keep working. Safe by construction:
    `User.agent_name` is populated only for `scope == "agent"`, so a
    system-scoped key has `agent_name=None` and never trips the agent guard."""
    d = _deps()
    d.assert_admin(SYSTEM_KEY)
    assert d.require_admin(SYSTEM_KEY) is SYSTEM_KEY


def test_a_connector_key_is_still_rejected():
    from fastapi import HTTPException

    d = _deps()
    for gate in (d.assert_admin, d.require_admin):
        with pytest.raises(HTTPException) as exc:
            gate(CONNECTOR_KEY)
        assert exc.value.status_code == 403


def test_a_non_admin_human_is_still_rejected():
    from fastapi import HTTPException

    d = _deps()
    with pytest.raises(HTTPException) as exc:
        d.assert_admin(NON_ADMIN)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# The exploit, at the endpoint the chain actually used
# ---------------------------------------------------------------------------

def test_an_agent_key_cannot_repoint_the_skills_library(monkeypatch):
    """AC #1, driven through the real handler.

    `PUT /api/settings/{key}` is the generic setter the chain used, and the
    SEC-179 guard constrains the HOST to github.com but not WHICH repository —
    so any public repo was accepted. The gate is what has to stop this.
    """
    import asyncio
    from fastapi import HTTPException

    try:
        from routers import settings as settings_router
        from db_models import SystemSettingUpdate
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    class _Req:
        client = type("c", (), {"host": "127.0.0.1"})()
        url = type("u", (), {"path": "/api/settings/skills_library_url"})()
        state = type("s", (), {"request_id": "r1"})()
        headers: dict = {}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            settings_router.update_setting(
                key="skills_library_url",
                body=SystemSettingUpdate(value="https://github.com/attacker/evil-skills"),
                request=_Req(),
                current_user=AGENT_KEY,
            )
        )
    assert exc.value.status_code == 403


def test_the_same_write_still_works_for_a_human_admin(monkeypatch):
    """The fix must close the hole without breaking the feature: an operator
    must still be able to point the fleet at a skills library."""
    import asyncio

    try:
        from routers import settings as settings_router
        from db_models import SystemSettingUpdate
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    written = {}
    monkeypatch.setattr(
        settings_router.db, "set_setting",
        lambda key, value, *a, **k: written.update({key: value}) or True,
        raising=False,
    )
    monkeypatch.setattr(
        settings_router.db, "get_setting",
        lambda *a, **k: {"key": "skills_library_url",
                         "value": "https://github.com/acme/skills",
                         "updated_at": "2026-07-30T00:00:00Z"},
        raising=False,
    )

    class _Req:
        client = type("c", (), {"host": "127.0.0.1"})()
        url = type("u", (), {"path": "/api/settings/skills_library_url"})()
        state = type("s", (), {"request_id": "r1"})()
        headers: dict = {}

    try:
        asyncio.run(
            settings_router.update_setting(
                key="skills_library_url",
                body=SystemSettingUpdate(value="https://github.com/acme/skills"),
                request=_Req(),
                current_user=HUMAN_ADMIN,
            )
        )
    except Exception as e:  # pragma: no cover - surfaces a real regression
        pytest.fail(f"a human admin must still be able to set the library: {e!r}")


# ---------------------------------------------------------------------------
# Surface coherence — raised in review of this PR
# ---------------------------------------------------------------------------
#
# Closing the admin gate to agent keys had fallout the first audit missed: six
# MCP tools call admin-gated routes. Four of those 403s are INTENDED and are the
# grant-vs-use line working —
#
#   get_credential_encryption_key  -> pulling the platform master key
#   get_agent_ssh_access           -> already documents itself as admin-only
#   register_subscription          -> granting a platform credential
#   delete_subscription            -> revoking one
#
# — but two were reads, not grants, and left a half-working workflow, which is
# worse than a cleanly closed door. These pin the resolution so the next change
# to the gate cannot silently re-break them.

def _dependency_names(fn) -> set:
    """Names of the FastAPI dependencies a handler actually resolves.

    Reads the resolved `Depends(...)` objects rather than grepping the source —
    a substring search matches the word inside a docstring explaining the very
    change, which is how the first version of these tests failed.
    """
    import inspect

    names = set()
    for param in inspect.signature(fn).parameters.values():
        dep = getattr(param.default, "dependency", None)
        if dep is not None:
            names.add(getattr(dep, "__name__", str(dep)))
        # Annotated[...] form: Depends lives in the metadata
        for meta in getattr(param.annotation, "__metadata__", ()):  # noqa: B007
            dep = getattr(meta, "dependency", None)
            if dep is not None:
                names.add(getattr(dep, "__name__", str(dep)))
    return names


def _calls_in_body(fn, name: str) -> bool:
    """True if the function BODY calls `name` — AST, so comments and docstrings
    cannot produce a false positive."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(
        isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == name
        for n in ast.walk(tree)
    )


def test_triggering_a_health_check_is_not_admin_gated():
    """An ops agent could READ health (`GET /status`, `GET /agents/{name}` — both
    non-admin) but not REFRESH it. Probing an agent you can already see is a use,
    not a grant, and the access control it relies on must remain."""
    try:
        from routers import monitoring
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    assert "require_admin" not in _dependency_names(monitoring.trigger_health_check)
    assert not _calls_in_body(monitoring.trigger_health_check, "assert_admin")

    # The per-agent access gate stays. Asserted from the SOURCE annotation, not
    # the runtime-resolved dependency: sibling monitoring tests override that
    # dependency with a lambda, so a runtime check here passes alone and fails
    # in a full run purely on collection order.
    import ast
    import inspect
    import textwrap

    fn_ast = ast.parse(
        textwrap.dedent(inspect.getsource(monitoring.trigger_health_check))
    ).body[0]
    annotations = {
        ast.unparse(a.annotation)
        for a in fn_ast.args.args
        if a.annotation is not None
    }
    assert "AuthorizedAgentByName" in annotations


def test_listing_subscriptions_is_not_admin_gated():
    """`assign_subscription` / `clear_agent_subscription` / `get_agent_auth` all
    kept working under owner gates, so an agent could ASSIGN a subscription it
    could no longer ENUMERATE. The read is restored; assignment stays owner-gated.
    """
    try:
        from routers import subscriptions
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    assert not _calls_in_body(subscriptions.list_subscriptions, "assert_admin")
    # The write path must NOT have been widened along with the read.
    assert _calls_in_body(subscriptions.assign_subscription_to_agent, "assert_agent_owner")


def test_the_restored_read_is_scoped_not_ungated(monkeypatch):
    """Un-gating this read outright was the first attempt and it traded one
    disclosure for another: the payload carries `owner_email` and every agent
    name, so any `role=user` account would enumerate the whole fleet. Each
    caller sees only their own — and an agent key is NOT the fleet-wide admin
    view even though it carries the owner's admin role, which is the entire
    point of ent#293."""
    import asyncio

    try:
        from routers import subscriptions
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    seen = []
    monkeypatch.setattr(
        subscriptions.db, "list_subscriptions_with_agents",
        lambda owner_id=None: seen.append(owner_id) or [],
        raising=False,
    )

    asyncio.run(subscriptions.list_subscriptions(current_user=HUMAN_ADMIN))
    asyncio.run(subscriptions.list_subscriptions(current_user=NON_ADMIN))
    asyncio.run(subscriptions.list_subscriptions(current_user=AGENT_KEY))

    fleet_wide, non_admin, agent_key = seen
    assert fleet_wide is None                 # a human admin still sees the fleet
    assert non_admin == NON_ADMIN.id          # everyone else is scoped
    assert agent_key == AGENT_KEY.id          # NOT the fleet view, despite role="admin"


def test_the_granting_endpoints_stay_admin_only():
    """The four intended 403s. If any of these loses its admin gate, the fix has
    been widened past its own rationale."""
    try:
        from routers import subscriptions, credentials, agent_ssh
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    assert _calls_in_body(subscriptions.register_subscription, "assert_admin")
    assert _calls_in_body(subscriptions.delete_subscription, "assert_admin")
    assert "require_admin" in _dependency_names(credentials.get_encryption_key)
    assert "require_admin" in _dependency_names(agent_ssh.create_ssh_access)

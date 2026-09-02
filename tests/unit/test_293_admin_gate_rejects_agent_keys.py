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
    # #2323 — this dataclass was docstringed "every field stated" while missing
    # the field #1854 added to `models.User`. That is why the guard could not
    # express the failure it exists to catch: `mcp_scope` is the ONLY thing that
    # distinguishes a `system`/`portal_delegate`/`ops` key from a browser
    # session, and all three set neither `agent_name` nor `connector_agent`, so
    # the two named rejections are no-ops for every one of them.
    # `None` = the JWT branch, matching `get_current_user`.
    mcp_scope: Optional[str] = None


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
        # #1028: the generic /{key} handlers live in the package's `generic` module.
        from routers.settings import generic as settings_router
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
    """The gate must discriminate by PRINCIPAL, not blanket-block the route.

    ent#346 changed which key this can be demonstrated on. This test used to
    write `skills_library_url`; that key is now refused for EVERYONE — human
    admins included — because `_adopt_legacy_clone` turns it into a
    `skill_sources` row, so writing it *is* the source-grant action ent#237
    gates behind its own route. See the companion assertion below.

    The invariant ent#293 exists to protect is unchanged and still worth
    holding: an agent principal is refused while a human admin is not. Proven
    here on a key that is genuinely writable, which is what makes the 403 above
    evidence about the PRINCIPAL rather than about the key.
    """
    import asyncio

    try:
        # #1028: the generic /{key} handlers live in the package's `generic` module.
        from routers.settings import generic as settings_router
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
        lambda *a, **k: {"key": "public_chat_url",
                         "value": "https://chat.example.com",
                         "updated_at": "2026-07-30T00:00:00Z"},
        raising=False,
    )

    class _Req:
        client = type("c", (), {"host": "127.0.0.1"})()
        url = type("u", (), {"path": "/api/settings/public_chat_url"})()
        state = type("s", (), {"request_id": "r1"})()
        headers: dict = {}

    asyncio.run(
        settings_router.update_setting(
            key="public_chat_url",
            body=SystemSettingUpdate(value="https://chat.example.com"),
            request=_Req(),
            current_user=HUMAN_ADMIN,
        )
    )
    assert written.get("public_chat_url") == "https://chat.example.com", (
        "a human admin must still be able to write an ordinary setting — if this "
        "fails, the agent gate has become a blanket block"
    )


def test_the_skills_library_key_is_refused_for_humans_too(monkeypatch):
    """ent#346: the key is a SOURCE GRANT, so the principal is not the question.

    ent#293 stopped an agent-scoped key writing it. ent#346 goes further: nobody
    writes it here, because `_adopt_legacy_clone` converts it into a
    `skill_sources` row at CUSTOM priority with no validation, which is the
    grant action `/skills/sources` gates. A human admin gets a 422 pointing at
    the supported route rather than a silent success that registers a fleet-wide
    skills source through the back door.
    """
    import asyncio
    from fastapi import HTTPException

    try:
        # #1028: the generic /{key} handlers live in the package's `generic` module.
        from routers.settings import generic as settings_router
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
                body=SystemSettingUpdate(value="https://github.com/acme/skills"),
                request=_Req(),
                current_user=HUMAN_ADMIN,
            )
        )
    assert exc.value.status_code == 422
    assert "POST /api/skills/sources" in exc.value.detail, (
        "the refusal must name the supported route, or an operator has no way "
        "forward and routes around the control"
    )

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


# ---------------------------------------------------------------------------
# The third spelling — found reviewing this PR
# ---------------------------------------------------------------------------

def test_no_route_uses_require_role_admin_as_an_admin_gate():
    """`require_role("admin")` is a THIRD spelling of an admin gate, and the one
    this PR's first pass missed.

    `require_role` rejects connector principals but NOT agent ones, and that is
    deliberate — agent-spawned agent creation runs through
    `require_role("creator")` (ent#69 Part 2), so a blanket rejection there would
    break ghost spawning. The consequence is that closing `require_admin` and
    `assert_admin` left `require_role("admin")` open, and its one call site was
    `POST /api/agents/{name}/circuit-breaker/reset` — a route ent#297 names in
    its blast radius. An agent able to re-close its own dispatch breaker on
    demand defeats the control that exists to contain it.

    `require_admin` is equivalent for the admin case (`admin` is last in
    ROLE_HIERARCHY, so ">= admin" is "== admin") AND rejects agent principals,
    so it is the only correct spelling. Pinned as a source scan because the
    failure mode is a NEW route choosing the wrong helper, which no behavioural
    test of existing routes can see.
    """
    import ast
    from pathlib import Path

    routers = Path(__file__).resolve().parents[2] / "src" / "backend" / "routers"
    offenders = []
    for path in sorted(routers.rglob("*.py")):
        # AST, not a regex over lines: the comment ON the fixed call site
        # explains what not to write and therefore CONTAINS the offending
        # string. A text scan flags its own documentation — which is exactly
        # how the first version of this test failed, and how the sibling
        # `_calls_in_body` helper above came to exist.
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:            # newer syntax than this interpreter
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "require_role"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "admin"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "use `require_admin` (rejects agent principals) rather than "
        f'`require_role("admin")` (does not): {offenders}'
    )


# ---------------------------------------------------------------------------
# #2323 — the gate is an ALLOWLIST, not a list of two named enemies
# ---------------------------------------------------------------------------
#
# ent#293 closed the gate against `agent` and `connector` BY NAME. `scope` is a
# free-text column with no CHECK constraint, so that is a denylist over an open
# space: any scope setting neither `agent_name` nor `connector_agent` walks both
# rejections and inherits the owner's admin role across ~163 admin-gated sites.
# `models.User.mcp_scope`'s docstring predicted exactly this. These tests pin the
# inversion so scope #7 is protected the day it is invented.

OPS_KEY = _Principal(username="admin", role="admin", mcp_scope="ops")
PORTAL_DELEGATE_KEY = _Principal(username="admin", role="admin", mcp_scope="portal_delegate")
FUTURE_KEY = _Principal(username="admin", role="admin", mcp_scope="scope_from_2027")


@pytest.mark.parametrize("principal", [OPS_KEY, PORTAL_DELEGATE_KEY, FUTURE_KEY])
def test_admin_gates_reject_every_scope_outside_the_allowlist(principal):
    """Including a scope that does not exist yet — that is the whole point."""
    from fastapi import HTTPException

    d = _deps()
    for gate in (d.assert_admin, d.require_admin):
        with pytest.raises(HTTPException) as exc:
            gate(principal)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("scope", [None, "user", "system"])
def test_admin_gates_still_admit_the_scopes_that_passed_before(scope):
    """The inversion must be zero-behaviour-change for everything legitimate.
    A guard that only proves rejection would pass if the gate rejected
    everyone — including the browser session and `trinity-system`."""
    d = _deps()
    principal = _Principal(username="admin", role="admin", mcp_scope=scope)
    d.assert_admin(principal)
    assert d.require_admin(principal) is principal


def test_admin_gate_scopes_is_a_closed_literal():
    """Widening the set must be a deliberate, reviewed edit — never a drive-by
    addition inside an unrelated PR."""
    d = _deps()
    assert d.ADMIN_GATE_SCOPES == frozenset({None, "user", "system"})


def test_a_route_may_opt_a_bounded_scope_in_but_only_that_one():
    """#2323's per-route grant. This is what makes an ops key authorized because
    of WHAT IT IS rather than who owns it — and it must not become a skeleton
    key for every other unlisted scope."""
    from fastapi import HTTPException

    d = _deps()
    d.assert_admin(OPS_KEY, allow_scopes={"ops"})
    with pytest.raises(HTTPException):
        d.assert_admin(FUTURE_KEY, allow_scopes={"ops"})


def test_opting_a_scope_in_does_not_bypass_the_role_check():
    """`allow_scopes` widens WHICH principals may satisfy an admin gate, never
    WHAT counts as admin. An ops key on a non-admin owner still gets nothing."""
    from fastapi import HTTPException

    d = _deps()
    with pytest.raises(HTTPException) as exc:
        d.assert_admin(_Principal(username="bob", role="user", mcp_scope="ops"),
                       allow_scopes={"ops"})
    assert exc.value.status_code == 403


def test_named_rejections_still_fire_ahead_of_the_allowlist():
    """An agent key must keep its OWN 403 message. If the allowlist started
    answering first, ent#293's diagnostic would silently regress into a generic
    scope error."""
    from fastapi import HTTPException

    d = _deps()
    agent_with_scope = _Principal(username="admin", role="admin",
                                  agent_name="prospector", mcp_scope="agent")
    with pytest.raises(HTTPException) as exc:
        d.assert_admin(agent_with_scope)
    assert "human-only" in exc.value.detail

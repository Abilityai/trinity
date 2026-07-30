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

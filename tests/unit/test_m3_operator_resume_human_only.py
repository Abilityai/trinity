"""PUT /api/agents/{name}/operator-resume is human-only (0.9.5 work order M3).

An agent-scoped MCP key resolves to its OWNER on REST (`dependencies.get_current_user`),
so `OwnedAgentByName` alone is satisfied by the agent's own injected key — the agent
could switch on its own paid wake-ups (ent#329: "answers to this agent may now spend
money, and the bill lands on the owner"). Same class as the ent#223 consent toggle and
the ent#109 bind endpoint: the endpoint that GRANTS a spend is human-only; the flag's
*use* (the resume dispatch itself) is untouched.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

_backend = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit


def _router():
    try:
        from routers import agents as agents_router
    except Exception as e:  # pragma: no cover — venv-less checkout
        pytest.skip(f"backend venv required: {e}")
    return agents_router


class TestOperatorResumeToggleIsHumanOnly:
    def test_the_toggle_carries_the_guard(self):
        src = inspect.getsource(_router().set_operator_resume_endpoint)
        assert "reject_agent_principal(current_user)" in src, (
            "PUT /operator-resume lost its human-only guard — an agent-scoped key "
            "(which resolves to the owner) could switch on its own paid wake-ups"
        )

    def test_an_agent_principal_gets_403_before_any_write(self, monkeypatch):
        """Runtime proof, ordering included: the reject is the FIRST statement, so
        making the DB write and the audit write loud proves nothing past the gate
        runs for an agent key."""
        from fastapi import HTTPException
        from models import User, OperatorResumeUpdate

        agents_router = _router()

        def _boom(*a, **k):
            raise AssertionError("reached a write past the human-only gate")

        monkeypatch.setattr(agents_router.db, "set_operator_resume_enabled", _boom, raising=False)
        monkeypatch.setattr(agents_router.platform_audit_service, "log", _boom, raising=False)

        agent_key = User(id=1, username="owner", role="admin", agent_name="sidekick")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(agents_router.set_operator_resume_endpoint(
                agent_name="sidekick",
                body=OperatorResumeUpdate(enabled=True),
                current_user=agent_key,
            ))
        assert ei.value.status_code == 403
        assert ei.value.detail == (
            "This operation is human-only; agent-scoped keys cannot perform it"
        )

    def test_a_human_owner_still_flips_it(self, monkeypatch):
        """The guard narrows the principal, not the feature: a JWT user (no
        agent_name) reaches the write exactly as before."""
        from models import User, OperatorResumeUpdate

        agents_router = _router()
        writes: list = []
        monkeypatch.setattr(agents_router.db, "set_operator_resume_enabled",
                            lambda name, enabled: writes.append((name, enabled)) or True, raising=False)

        async def _log(**kw):
            writes.append(("audit", kw["event_action"]))
        monkeypatch.setattr(agents_router.platform_audit_service, "log", _log, raising=False)

        human = User(id=1, username="owner", role="user")
        out = asyncio.run(agents_router.set_operator_resume_endpoint(
            agent_name="sidekick", body=OperatorResumeUpdate(enabled=True), current_user=human))
        assert out == {"agent_name": "sidekick", "enabled": True}
        assert writes == [("sidekick", True), ("audit", "operator_resume_config")]

    def test_the_read_stays_agent_reachable(self):
        """GET is a *use* (an agent may read whether its answers resume it); only
        the grant is fenced. Guard against an over-eager copy of the reject."""
        src = inspect.getsource(_router().get_operator_resume_endpoint)
        assert "reject_agent_principal" not in src

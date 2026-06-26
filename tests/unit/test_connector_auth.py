"""
Connector-scope auth boundary (OSS edition-agnostic primitive, ent#46).

The connector *feature* (config, key minting, snippets, UI) is an entitled
module in the private repo. What ships in OSS core is the security enforcement:
core recognizes a `scope='connector'` MCP key as a consumption-only principal
and fences it to its bound agent, refusing owner and role-gated operations —
the same core-primitive + enterprise-knob shape as `users.suspended_at` (#995).

These tests pin that enforcement. Pure (no DB).
"""
import sys
from pathlib import Path

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


def _user(connector_agent=None, role="creator"):
    from models import User
    return User(id=1, username="owner", role=role, connector_agent=connector_agent)


class TestConnectorScope:
    def test_non_connector_is_noop(self):
        from dependencies import _enforce_connector_scope
        # Ordinary principal: never fenced, even on an owner op.
        _enforce_connector_scope(_user(), "agent-1", owner_op=True)

    def test_connector_blocked_on_owner_op(self):
        from fastapi import HTTPException
        from dependencies import _enforce_connector_scope
        with pytest.raises(HTTPException) as exc:
            _enforce_connector_scope(_user(connector_agent="agent-1"), "agent-1", owner_op=True)
        assert exc.value.status_code == 403

    def test_connector_fenced_to_bound_agent(self):
        from fastapi import HTTPException
        from dependencies import _enforce_connector_scope
        # A different agent is refused...
        with pytest.raises(HTTPException) as exc:
            _enforce_connector_scope(_user(connector_agent="agent-1"), "agent-2", owner_op=False)
        assert exc.value.status_code == 403
        # ...the bound agent is allowed.
        _enforce_connector_scope(_user(connector_agent="agent-1"), "agent-1", owner_op=False)


class TestRoleGate:
    def test_connector_rejected_from_role_gate(self):
        from fastapi import HTTPException
        from dependencies import _reject_connector_principal
        # Even resolving to an admin owner, a connector key can't role-gate.
        with pytest.raises(HTTPException) as exc:
            _reject_connector_principal(_user(connector_agent="agent-1", role="admin"))
        assert exc.value.status_code == 403

    def test_ordinary_principal_passes(self):
        from dependencies import _reject_connector_principal
        _reject_connector_principal(_user())


class TestUserModel:
    def test_connector_agent_defaults_none(self):
        from models import User
        u = User(id=1, username="owner", role="user")
        assert u.connector_agent is None

"""
Connector-scope auth boundary (OSS edition-agnostic primitive, ent#46).

The connector *feature* (config, key minting, snippets, UI) is an entitled
module in the private repo. What ships in OSS core is the security enforcement:
core recognizes a `scope='connector'` MCP key as a consumption-only principal
and fences it to its bound agent, refusing owner and role-gated operations —
the same core-primitive + enterprise-knob shape as `users.suspended_at` (#995).

These tests pin that enforcement.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_user, seed_agent  # noqa: E402,F401

pytestmark = pytest.mark.unit


def _user(connector_agent=None, role="creator"):
    from models import User
    return User(id=1, username="owner", role=role, connector_agent=connector_agent)


def _fake_request(method, path):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


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


class TestCentralGuard:
    """get_current_user is the single auth entry point — a connector key must be
    contained there to the exact routes its MCP tools call, so the ~dozens of
    endpoints with inline access checks can't be reached by a leaked snippet."""

    @pytest.fixture
    def patched(self, monkeypatch):
        import dependencies as deps
        monkeypatch.setattr(deps.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": "connector", "agent_name": "agent-1",
            "user_id": "owner", "user_email": "owner@example.com",
        })
        monkeypatch.setattr(deps.db, "get_user_by_email", lambda *_a, **_k: {
            "id": 1, "username": "owner", "email": "owner@example.com", "role": "creator",
        })
        return deps

    def _call(self, deps, request):
        # A non-JWT token falls through to the MCP-key path.
        return asyncio.run(deps.get_current_user(request, token="trinity_mcp_fake"))

    def test_allows_bound_agent_chat(self, patched):
        u = self._call(patched, _fake_request("POST", "/api/agents/agent-1/chat"))
        assert u.connector_agent == "agent-1"

    def test_allows_bound_agent_playbooks(self, patched):
        u = self._call(patched, _fake_request("GET", "/api/agents/agent-1/connector/playbooks"))
        assert u.connector_agent == "agent-1"

    def test_blocks_other_agent_chat(self, patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("POST", "/api/agents/agent-2/chat"))
        assert exc.value.status_code == 403

    def test_blocks_owner_endpoint_on_bound_agent(self, patched):
        from fastapi import HTTPException
        # An inline-checked endpoint on the bound agent (e.g. loops) is refused
        # at the auth layer before any handler runs.
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("POST", "/api/agents/agent-1/loops"))
        assert exc.value.status_code == 403

    def test_blocks_wrong_method(self, patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            self._call(patched, _fake_request("DELETE", "/api/agents/agent-1/chat"))
        assert exc.value.status_code == 403


class TestKeyCleanup:
    """ent#46 re-split fix: connector keys (scope='connector' in the OSS
    mcp_api_keys table) must be swept on agent delete, like scope='agent' keys."""

    def test_connector_key_in_agent_refs(self):
        from db.agent_cleanup import AGENT_REFS
        conn_refs = [r for r in AGENT_REFS
                     if r.table == "mcp_api_keys" and (r.extra_filter or "").find("connector") >= 0]
        assert len(conn_refs) == 1, "expected exactly one scope='connector' mcp_api_keys cleanup ref"

    def test_cascade_delete_removes_connector_key(self, db_backend, monkeypatch):
        # Evict cached db modules so they bind to the harness backend.
        for mod in ("db.connection", "database"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        seed_user(user_id=1, username="owner")
        seed_agent(agent_name="agent-1", owner_id=1)
        from db.engine import get_engine
        from db.tables import mcp_api_keys
        from sqlalchemy import insert, select
        from db.agent_cleanup import cascade_delete
        with get_engine().begin() as conn:
            conn.execute(insert(mcp_api_keys).values(
                id="k1", name="connector-agent-1-key", key_prefix="trinity_mcp_xxxx",
                key_hash="hash-connector", created_at="2026-01-01T00:00:00Z",
                user_id=1, agent_name="agent-1", scope="connector", is_active=1,
            ))
        with get_engine().begin() as conn:
            cascade_delete(conn, "agent-1")
        with get_engine().connect() as conn:
            rows = conn.execute(select(mcp_api_keys.c.id).where(
                mcp_api_keys.c.agent_name == "agent-1")).all()
        assert rows == [], "connector key should be deleted with its agent"


class TestExtraAgentTableSeam:
    """The OSS rename/delete seam for entitled-module agent-scoped tables
    (register_agent_owned_table) — resolved by NAME via raw SQL so a private
    enterprise table can't KeyError the OSS metadata lookup."""

    def test_register_is_idempotent(self):
        from db.agent_cleanup import register_agent_owned_table, EXTRA_AGENT_REFS
        register_agent_owned_table("ent_demo_tbl", "agent_name")
        register_agent_owned_table("ent_demo_tbl", "agent_name")
        assert EXTRA_AGENT_REFS.count(("ent_demo_tbl", "agent_name")) == 1

    def test_cascade_delete_sweeps_registered_table(self, db_backend, monkeypatch):
        for mod in ("db.connection", "database"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        from db.engine import get_engine
        from sqlalchemy import text
        from db.agent_cleanup import register_agent_owned_table, cascade_delete
        with get_engine().begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS ent_demo_tbl (agent_name TEXT)"))
            conn.execute(text("INSERT INTO ent_demo_tbl (agent_name) VALUES ('a1')"))
        register_agent_owned_table("ent_demo_tbl", "agent_name")
        with get_engine().begin() as conn:
            cascade_delete(conn, "a1")
        with get_engine().connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ent_demo_tbl WHERE agent_name='a1'")).scalar()
        assert n == 0

    def test_cascade_delete_skips_absent_registered_table(self, db_backend, monkeypatch):
        # A registered table that doesn't exist (OSS-only build) must be skipped,
        # never raise.
        for mod in ("db.connection", "database"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        from db.engine import get_engine
        from db.agent_cleanup import register_agent_owned_table, cascade_delete
        register_agent_owned_table("ent_absent_tbl", "agent_name")
        with get_engine().begin() as conn:
            cascade_delete(conn, "whatever")  # no raise

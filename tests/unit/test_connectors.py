"""
Per-agent MCP connector tests (ent#46).

Covers the connector config CRUD, the connector-scoped key lifecycle
(mint / regenerate-invalidates-old / revoke), the exposed-playbook resolution
(allow-list ∩ user_invocable), the per-client snippet builder, and the
connector-scope auth boundary that fences a connector key to its bound agent
and refuses owner operations.

DB tests run on the real production schema via db_harness (SQLite always; PG
when TEST_POSTGRES_URL is set). Pure helpers (service + auth boundary) need no
DB.
"""
import sys
from pathlib import Path

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_user, seed_agent  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(db_backend):
    """Fresh full production schema; evict cached db modules so production code
    re-resolves against the active backend."""
    def _evict():
        for mod in ("db.connectors", "db.mcp_keys", "db.users", "database"):
            sys.modules.pop(mod, None)
    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def fresh_db(tmp_db):
    """A facade `db` bound to the active backend, with an owner + agent seeded."""
    seed_user(user_id=1, username="owner", role="creator")
    seed_agent(agent_name="agent-1", owner_id=1)
    from database import db
    return db


# ---------------------------------------------------------------------------
# Connector config CRUD
# ---------------------------------------------------------------------------

class TestConnectorConfig:
    def test_absent_by_default(self, fresh_db):
        assert fresh_db.get_connector_config("agent-1") is None

    def test_enable_and_persist(self, fresh_db):
        cfg = fresh_db.upsert_connector_config("agent-1", enabled=True)
        assert cfg.enabled is True
        assert cfg.exposed_playbooks is None  # None ⇒ all user_invocable
        reread = fresh_db.get_connector_config("agent-1")
        assert reread.enabled is True

    def test_set_allow_list(self, fresh_db):
        fresh_db.upsert_connector_config("agent-1", enabled=True, exposed_playbooks=["cso", "commit"])
        cfg = fresh_db.get_connector_config("agent-1")
        assert cfg.exposed_playbooks == ["cso", "commit"]

    def test_partial_update_keeps_allow_list(self, fresh_db):
        fresh_db.upsert_connector_config("agent-1", enabled=True, exposed_playbooks=["cso"])
        # Toggle enabled only; allow-list must survive (exposed_playbooks=None).
        cfg = fresh_db.upsert_connector_config("agent-1", enabled=False)
        assert cfg.enabled is False
        assert cfg.exposed_playbooks == ["cso"]

    def test_clear_playbooks_resets_to_all(self, fresh_db):
        fresh_db.upsert_connector_config("agent-1", enabled=True, exposed_playbooks=["cso"])
        cfg = fresh_db.upsert_connector_config("agent-1", clear_playbooks=True)
        assert cfg.exposed_playbooks is None

    def test_isolation_between_agents(self, fresh_db):
        seed_agent(agent_name="agent-2", owner_id=1)
        fresh_db.upsert_connector_config("agent-1", enabled=True, exposed_playbooks=["cso"])
        assert fresh_db.get_connector_config("agent-2") is None


# ---------------------------------------------------------------------------
# Connector-scoped key lifecycle
# ---------------------------------------------------------------------------

class TestConnectorKey:
    def test_mint_returns_secret_once(self, fresh_db):
        secret = fresh_db.create_connector_mcp_api_key("agent-1", "owner")
        assert secret is not None
        assert secret.api_key.startswith("trinity_mcp_")
        assert secret.scope == "connector"
        assert secret.agent_name == "agent-1"

    def test_get_returns_prefix_not_secret(self, fresh_db):
        secret = fresh_db.create_connector_mcp_api_key("agent-1", "owner")
        key = fresh_db.get_connector_mcp_api_key("agent-1")
        assert key is not None
        assert key.key_prefix == secret.api_key[:20]
        assert not hasattr(key, "api_key")  # McpApiKey has no secret field

    def test_validates_as_connector_scope(self, fresh_db):
        secret = fresh_db.create_connector_mcp_api_key("agent-1", "owner")
        info = fresh_db.validate_mcp_api_key(secret.api_key)
        assert info is not None
        assert info["scope"] == "connector"
        assert info["agent_name"] == "agent-1"

    def test_regenerate_invalidates_old(self, fresh_db):
        old = fresh_db.create_connector_mcp_api_key("agent-1", "owner")
        new = fresh_db.regenerate_connector_mcp_api_key("agent-1", "owner")
        assert new.api_key != old.api_key
        # Old key no longer validates; new one does.
        assert fresh_db.validate_mcp_api_key(old.api_key) is None
        assert fresh_db.validate_mcp_api_key(new.api_key) is not None

    def test_revoke(self, fresh_db):
        secret = fresh_db.create_connector_mcp_api_key("agent-1", "owner")
        assert fresh_db.delete_connector_mcp_api_key("agent-1") is True
        assert fresh_db.get_connector_mcp_api_key("agent-1") is None
        assert fresh_db.validate_mcp_api_key(secret.api_key) is None

    def test_connector_key_distinct_from_agent_key(self, fresh_db):
        # The auto-minted agent-scoped key and the connector key coexist.
        fresh_db.create_agent_mcp_api_key("agent-1", "owner")
        conn = fresh_db.create_connector_mcp_api_key("agent-1", "owner")
        agent_key = fresh_db.get_agent_mcp_api_key("agent-1")
        conn_key = fresh_db.get_connector_mcp_api_key("agent-1")
        assert agent_key.scope == "agent"
        assert conn_key.scope == "connector"
        # Revoking the connector key leaves the agent key intact.
        fresh_db.delete_connector_mcp_api_key("agent-1")
        assert fresh_db.get_agent_mcp_api_key("agent-1") is not None


# ---------------------------------------------------------------------------
# Exposed-playbook resolution (pure)
# ---------------------------------------------------------------------------

class TestPlaybookResolution:
    def _live(self):
        return [
            {"name": "cso", "description": "audit", "user_invocable": True, "automation": "gated", "argument_hint": "[--x]"},
            {"name": "internal-only", "user_invocable": False},
            {"name": "commit", "user_invocable": True},
        ]

    def test_allow_list_filters(self):
        from services.connector_service import resolve_exposed_playbooks
        out = resolve_exposed_playbooks(self._live(), ["cso"])
        assert [p.name for p in out] == ["cso"]
        assert out[0].argument_hint == "[--x]"
        assert out[0].automation == "gated"

    def test_none_exposes_all_user_invocable(self):
        from services.connector_service import resolve_exposed_playbooks
        out = resolve_exposed_playbooks(self._live(), None)
        assert {p.name for p in out} == {"cso", "commit"}  # internal-only excluded

    def test_user_invocable_false_never_exposed(self):
        from services.connector_service import resolve_exposed_playbooks
        # Even if explicitly allow-listed, a non-user-invocable playbook is hidden.
        out = resolve_exposed_playbooks(self._live(), ["internal-only", "cso"])
        assert [p.name for p in out] == ["cso"]


# ---------------------------------------------------------------------------
# Per-client snippet builder (pure)
# ---------------------------------------------------------------------------

class TestSnippets:
    def test_name_slug(self):
        from services.connector_service import connector_name
        assert connector_name("My Agent!") == "trinity-my-agent"

    def test_snippets_embed_key_and_url(self):
        from services.connector_service import build_snippets
        snips = build_snippets("agent-1", "http://localhost:8080/mcp", "trinity_mcp_SECRET")
        clients = {s.client for s in snips}
        assert {"claude-code", "cursor", "claude-desktop"} <= clients
        for s in snips:
            assert "trinity_mcp_SECRET" in s.content
            assert "http://localhost:8080/mcp" in s.content
        cli = next(s for s in snips if s.client == "claude-code")
        assert cli.content.startswith("claude mcp add --transport http")


# ---------------------------------------------------------------------------
# Connector-scope auth boundary (pure)
# ---------------------------------------------------------------------------

class TestAuthBoundary:
    def _user(self, connector_agent=None, role="creator"):
        from models import User
        return User(id=1, username="owner", role=role, connector_agent=connector_agent)

    def test_non_connector_is_noop(self):
        from dependencies import _enforce_connector_scope
        _enforce_connector_scope(self._user(), "agent-1", owner_op=True)  # no raise

    def test_connector_blocked_on_owner_op(self):
        from fastapi import HTTPException
        from dependencies import _enforce_connector_scope
        with pytest.raises(HTTPException) as exc:
            _enforce_connector_scope(self._user(connector_agent="agent-1"), "agent-1", owner_op=True)
        assert exc.value.status_code == 403

    def test_connector_fenced_to_bound_agent(self):
        from fastapi import HTTPException
        from dependencies import _enforce_connector_scope
        # Read access to a DIFFERENT agent is refused.
        with pytest.raises(HTTPException) as exc:
            _enforce_connector_scope(self._user(connector_agent="agent-1"), "agent-2", owner_op=False)
        assert exc.value.status_code == 403
        # Read access to the bound agent is allowed.
        _enforce_connector_scope(self._user(connector_agent="agent-1"), "agent-1", owner_op=False)

    def test_connector_rejected_from_role_gate(self):
        from fastapi import HTTPException
        from dependencies import _reject_connector_principal
        with pytest.raises(HTTPException) as exc:
            _reject_connector_principal(self._user(connector_agent="agent-1", role="admin"))
        assert exc.value.status_code == 403
        # Ordinary principal passes.
        _reject_connector_principal(self._user())

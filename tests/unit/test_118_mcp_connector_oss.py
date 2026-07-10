"""Tests for the per-agent MCP connector — OSS-core since #118 (was ent#46).

Covers the pure service helpers (snippet builder + playbook resolution), the
connector config CRUD, and the connector-key minting — including the contract
that a key minted here validates through ``validate_mcp_api_key`` as
``scope='connector'``.

Runs against a throwaway sqlite seeded with the OSS ``users`` + ``mcp_api_keys``
+ ``enterprise_connectors`` tables. No entitlement wiring — the feature is OSS-core.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def conn_db(tmp_path, monkeypatch):
    """Fresh sqlite: OSS users + mcp_api_keys + enterprise_connectors tables."""
    db_file = tmp_path / "trinity-connector.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as oss_metadata, users, mcp_api_keys, enterprise_connectors
    oss_metadata.create_all(get_engine(), tables=[users, mcp_api_keys, enterprise_connectors])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="owner", role="creator", email="owner@example.com",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        ))
    yield str(db_file)


def _ops():
    from db.connector import ConnectorOperations
    return ConnectorOperations()


# ---------------------------------------------------------------------------
# Pure service helpers
# ---------------------------------------------------------------------------

class TestService:
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

    def test_none_exposes_all_user_invocable(self):
        from services.connector_service import resolve_exposed_playbooks
        out = resolve_exposed_playbooks(self._live(), None)
        assert {p.name for p in out} == {"cso", "commit"}

    def test_user_invocable_false_never_exposed_even_if_listed(self):
        from services.connector_service import resolve_exposed_playbooks
        out = resolve_exposed_playbooks(self._live(), ["internal-only", "cso"])
        assert [p.name for p in out] == ["cso"]

    def test_snippets_embed_key_and_url(self):
        from services.connector_service import build_snippets, connector_name
        assert connector_name("My Agent!") == "trinity-my-agent"
        snips = build_snippets("agent-1", "http://localhost:8080/mcp", "trinity_mcp_SECRET")
        assert {"claude-code", "cursor", "claude-desktop"} <= {s.client for s in snips}
        for s in snips:
            assert "trinity_mcp_SECRET" in s.content and "http://localhost:8080/mcp" in s.content


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

class TestConfig:
    def test_absent_then_enable(self, conn_db):
        cdb = _ops()
        assert cdb.get_config("agent-1") is None
        cfg = cdb.upsert_config("agent-1", enabled=True)
        assert cfg["enabled"] is True
        assert cfg["exposed_playbooks"] is None
        assert cdb.get_config("agent-1")["enabled"] is True

    def test_allow_list_roundtrip_and_partial_update(self, conn_db):
        cdb = _ops()
        cdb.upsert_config("agent-1", enabled=True, exposed_playbooks=["cso", "commit"])
        assert cdb.get_config("agent-1")["exposed_playbooks"] == ["cso", "commit"]
        cfg = cdb.upsert_config("agent-1", enabled=False)
        assert cfg["enabled"] is False and cfg["exposed_playbooks"] == ["cso", "commit"]
        assert cdb.upsert_config("agent-1", clear_playbooks=True)["exposed_playbooks"] is None

    def test_delete_config(self, conn_db):
        cdb = _ops()
        cdb.upsert_config("agent-1", enabled=True)
        assert cdb.delete_config("agent-1") is True
        assert cdb.get_config("agent-1") is None


# ---------------------------------------------------------------------------
# Connector key (rows in mcp_api_keys, scope='connector')
# ---------------------------------------------------------------------------

class TestKey:
    def test_mint_get_revoke(self, conn_db):
        cdb = _ops()
        secret = cdb.mint_key("agent-1", user_id=1)
        assert secret["api_key"].startswith("trinity_mcp_")
        assert cdb.get_key_prefix("agent-1") == secret["api_key"][:20]
        assert cdb.revoke_key("agent-1") is True
        assert cdb.get_key_prefix("agent-1") is None

    def test_validates_as_connector_scope(self, conn_db):
        cdb = _ops()
        from db.mcp_keys import McpKeyOperations
        ops = McpKeyOperations(None)  # validate doesn't touch user_ops
        secret = cdb.mint_key("agent-1", user_id=1)
        info = ops.validate_mcp_api_key(secret["api_key"])
        assert info is not None
        assert info["scope"] == "connector"
        assert info["agent_name"] == "agent-1"

    def test_regenerate_invalidates_old(self, conn_db):
        cdb = _ops()
        from db.mcp_keys import McpKeyOperations
        ops = McpKeyOperations(None)
        old = cdb.mint_key("agent-1", user_id=1)
        new = cdb.regenerate_key("agent-1", user_id=1)
        assert new["api_key"] != old["api_key"]
        assert ops.validate_mcp_api_key(old["api_key"]) is None
        assert ops.validate_mcp_api_key(new["api_key"]) is not None

    def test_regenerate_keeps_exactly_one_active_key(self, conn_db):
        cdb = _ops()
        from db.engine import get_engine
        from db.tables import mcp_api_keys
        from sqlalchemy import select, func
        cdb.mint_key("agent-1", user_id=1)
        cdb.regenerate_key("agent-1", user_id=1)
        cdb.regenerate_key("agent-1", user_id=1)
        with get_engine().connect() as conn:
            n = conn.execute(
                select(func.count()).select_from(mcp_api_keys).where(
                    mcp_api_keys.c.agent_name == "agent-1",
                    mcp_api_keys.c.scope == "connector",
                )
            ).scalar()
        assert n == 1

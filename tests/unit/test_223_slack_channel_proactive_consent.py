"""Per-channel proactive consent for Slack (ent#223).

In an open Slack workspace users never authenticate, so the per-recipient consent
model (``agent_sharing.allow_proactive``, keyed by verified email) has nobody to
key on. For Slack the consent unit is the CHANNEL BINDING.

The property that actually matters here is the DEFAULT POSTURE:
  * a NEW binding denies proactive posts (consent is explicit), and
  * an EXISTING binding is backfilled to allow, because channel posts had no
    consent gate before this change — defaulting them to deny would silently
    break every working integration.

Module: src/backend/db/slack_channels.py, src/backend/db/migrations.py
Issue:  https://github.com/Abilityai/trinity-enterprise/issues/223
"""

import os
import sqlite3
import sys
from pathlib import Path

# IMPORTANT: set REDIS_URL BEFORE any backend import (Issue #589 hard-fail).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401


@pytest.fixture
def ops(db_backend):
    from db import slack_channels as sc_db
    return sc_db.SlackChannelOperations()


def _bind(ops, channel_id="C1", agent="analytics", team="T1"):
    ops.bind_channel_to_agent(
        team_id=team,
        slack_channel_id=channel_id,
        slack_channel_name="general",
        agent_name=agent,
        created_by="alice",
    )
    return ops.get_channel_agent(team, channel_id)


class TestDefaultPosture:
    def test_new_binding_denies_proactive_by_default(self, ops):
        """Binding an agent to a channel is NOT itself consent to unprompted
        posts — the flag starts off."""
        binding = _bind(ops)
        assert binding is not None
        assert binding["allow_proactive"] is False

    def test_read_exposes_the_flag_on_the_agent_channel_list(self, ops):
        """The router gates on this field from get_channels_for_agent, so it has
        to survive that read path too (not just get_channel_agent)."""
        _bind(ops)
        rows = ops.get_channels_for_agent("analytics")
        assert rows and "allow_proactive" in rows[0]
        assert rows[0]["allow_proactive"] is False


class TestToggle:
    def test_toggle_on_then_off(self, ops):
        _bind(ops)
        assert ops.set_channel_allow_proactive("T1", "C1", True) is True
        assert ops.get_channel_agent("T1", "C1")["allow_proactive"] is True

        assert ops.set_channel_allow_proactive("T1", "C1", False) is True
        assert ops.get_channel_agent("T1", "C1")["allow_proactive"] is False

    def test_toggle_unknown_binding_reports_miss(self, ops):
        """Returns False so the caller raises 404 instead of silently 'succeeding'."""
        assert ops.set_channel_allow_proactive("T1", "C-nope", True) is False

    def test_toggle_is_scoped_to_one_channel(self, ops):
        _bind(ops, channel_id="C1")
        _bind(ops, channel_id="C2", agent="ads")
        ops.set_channel_allow_proactive("T1", "C1", True)
        assert ops.get_channel_agent("T1", "C1")["allow_proactive"] is True
        assert ops.get_channel_agent("T1", "C2")["allow_proactive"] is False


class TestMigrationBackfill:
    """The no-silent-flip guarantee, tested against a REAL pre-migration table."""

    def _legacy_db(self, tmp_path):
        p = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(p))
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE slack_channel_agents (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                slack_channel_id TEXT NOT NULL,
                slack_channel_name TEXT,
                agent_name TEXT NOT NULL,
                is_dm_default INTEGER DEFAULT 0,
                created_by TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(team_id, slack_channel_id)
            )
            """
        )
        cur.execute(
            "INSERT INTO slack_channel_agents "
            "(id, team_id, slack_channel_id, agent_name, created_at) "
            "VALUES ('1','T1','C1','analytics','t')"
        )
        conn.commit()
        return conn, cur

    def test_existing_bindings_are_backfilled_to_allow(self, tmp_path):
        """Existing integrations kept working before the gate existed; the
        migration must not silently switch them off."""
        from db.migrations import _migrate_slack_channel_allow_proactive
        conn, cur = self._legacy_db(tmp_path)
        _migrate_slack_channel_allow_proactive(cur, conn)
        conn.commit()

        cur.execute("SELECT allow_proactive FROM slack_channel_agents WHERE id='1'")
        assert cur.fetchone()[0] == 1, "pre-existing binding must stay allowed"

    def test_rows_created_after_migration_default_to_deny(self, tmp_path):
        """New bindings get the safe default even on a migrated database."""
        from db.migrations import _migrate_slack_channel_allow_proactive
        conn, cur = self._legacy_db(tmp_path)
        _migrate_slack_channel_allow_proactive(cur, conn)
        cur.execute(
            "INSERT INTO slack_channel_agents "
            "(id, team_id, slack_channel_id, agent_name, created_at) "
            "VALUES ('2','T1','C2','ads','t')"
        )
        conn.commit()
        cur.execute("SELECT allow_proactive FROM slack_channel_agents WHERE id='2'")
        assert cur.fetchone()[0] == 0, "a NEW binding must deny by default"

    def test_migration_is_idempotent(self, tmp_path):
        """Re-running must not re-backfill a binding an operator turned OFF."""
        from db.migrations import _migrate_slack_channel_allow_proactive
        conn, cur = self._legacy_db(tmp_path)
        _migrate_slack_channel_allow_proactive(cur, conn)
        cur.execute("UPDATE slack_channel_agents SET allow_proactive = 0 WHERE id='1'")
        conn.commit()

        _migrate_slack_channel_allow_proactive(cur, conn)   # second run
        conn.commit()
        cur.execute("SELECT allow_proactive FROM slack_channel_agents WHERE id='1'")
        assert cur.fetchone()[0] == 0, "an operator's explicit OFF must survive re-runs"


class TestConsentToggleIsHumanOnly:
    def test_toggle_endpoint_rejects_an_agent_principal(self):
        """An agent-scoped key resolves to the OWNER on REST (dependencies.py:423),
        so can_user_share_agent alone would let an agent flip its OWN consent on —
        self-granting the control ent#223 exists to enforce."""
        import inspect
        from routers import slack as slack_router

        src = inspect.getsource(slack_router.set_slack_channel_proactive)
        assert "reject_agent_principal" in src, (
            "the consent toggle lost its human-only guard — an agent could grant "
            "itself proactive-messaging consent"
        )

    def test_toggle_rejects_agent_principal_at_runtime(self, monkeypatch):
        """Runtime proof, not just source-grep (#1710): an agent-scoped principal
        calling the toggle gets 403 + the human-only detail BEFORE the owner check
        or any channel mutation. ``reject_agent_principal`` is the first statement,
        so making every post-gate db call loud proves the ordering — and this is
        what the AST wiring guard (a plain helper call) cannot see. The migration
        onto ``assert_agent_owner`` must NOT drop this line: that helper is
        agent-permissive, so without the standalone reject an agent could
        self-grant proactive consent."""
        import asyncio
        from fastapi import HTTPException
        from routers import slack as slack_router
        from models import User, SlackChannelProactiveRequest

        def _boom(*a, **k):
            raise AssertionError("reached a db call past the human-only gate")

        # The owner check and every mutation sit AFTER the reject — make them loud.
        monkeypatch.setattr(slack_router.db, "can_user_share_agent", _boom, raising=False)
        monkeypatch.setattr(slack_router.db, "get_slack_channels_for_agent", _boom, raising=False)
        monkeypatch.setattr(slack_router.db, "set_slack_channel_allow_proactive", _boom, raising=False)

        agent_key = User(id=1, username="owner", role="user", agent_name="analytics")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(slack_router.set_slack_channel_proactive(
                name="analytics", channel_id="C1",
                request=SlackChannelProactiveRequest(allow_proactive=True),
                current_user=agent_key))
        assert ei.value.status_code == 403
        assert ei.value.detail == (
            "This operation is human-only; agent-scoped keys cannot perform it"
        )

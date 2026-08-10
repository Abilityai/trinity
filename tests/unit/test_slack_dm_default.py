"""
Unit tests for Slack DM-default management (#584).

Covers:
- ``set_dm_default`` — single-tx clear-then-set, exclusivity, idempotency
- ``unbind_agent`` — must NOT auto-promote (router enforces the guard)
- The "blocked unbind" rule itself is a router-layer concern; we exercise
  it via a thin direct-call shim around the router handler.

Run in-process against an ephemeral SQLite database (no backend, no Docker).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402


pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh full schema (db_harness, #300). Runs on
    SQLite and, when TEST_POSTGRES_URL is set, PostgreSQL. Pops cached db
    modules so production code re-resolves against the active engine."""
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db."):
            if modname in ("db.engine", "db.tables", "db.schema"):
                continue
            sys.modules.pop(modname, None)
    return db_backend


@pytest.fixture
def slack_ops(tmp_db):
    """Fresh SlackChannelOperations bound to the tmp DB."""
    from db.slack_channels import SlackChannelOperations
    return SlackChannelOperations()


def _bind(slack_ops, team_id, agent_name, *, is_dm_default=False, channel_id=None):
    """Helper: bind an agent to a workspace channel."""
    return slack_ops.bind_channel_to_agent(
        team_id=team_id,
        slack_channel_id=channel_id or f"C-{agent_name}",
        slack_channel_name=agent_name,
        agent_name=agent_name,
        is_dm_default=is_dm_default,
    )


# ---------------------------------------------------------------------------
# set_dm_default
# ---------------------------------------------------------------------------


class TestSetDmDefault:

    def test_returns_false_when_agent_not_bound(self, slack_ops):
        """No row to flip → setter returns False so the router can 404."""
        assert slack_ops.set_dm_default("T-x", "ghost-agent") is False

    def test_sets_default_on_bound_agent(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha")
        assert slack_ops.set_dm_default("T-1", "alpha") is True
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

    def test_clears_previous_default(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

        slack_ops.set_dm_default("T-1", "beta")
        # Exactly one default after the flip
        assert slack_ops.get_dm_default_agent("T-1") == "beta"
        agents = slack_ops.get_agents_for_workspace("T-1")
        assert sum(1 for a in agents if a["is_dm_default"]) == 1

    def test_idempotent_when_already_default(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        slack_ops.set_dm_default("T-1", "alpha")
        slack_ops.set_dm_default("T-1", "alpha")
        agents = slack_ops.get_agents_for_workspace("T-1")
        assert sum(1 for a in agents if a["is_dm_default"]) == 1
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

    def test_isolated_per_workspace(self, slack_ops):
        """Setting default in workspace A must not touch workspace B."""
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-2", "alpha")  # different workspace, same name
        slack_ops.set_dm_default("T-2", "alpha")

        # Both workspaces have alpha as default — no cross-talk.
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"
        assert slack_ops.get_dm_default_agent("T-2") == "alpha"

    def test_setting_one_does_not_touch_siblings(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")
        _bind(slack_ops, "T-1", "gamma")

        slack_ops.set_dm_default("T-1", "gamma")

        agents = {a["agent_name"]: a["is_dm_default"]
                  for a in slack_ops.get_agents_for_workspace("T-1")}
        assert agents == {"alpha": False, "beta": False, "gamma": True}


# ---------------------------------------------------------------------------
# unbind_agent — pure delete, no auto-promote
# ---------------------------------------------------------------------------


class TestUnbindAgent:

    def test_unbind_non_default_agent(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")

        assert slack_ops.unbind_agent("T-1", "beta") is True
        # Default is unchanged
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

    def test_unbind_default_agent_does_not_promote(self, slack_ops):
        """Per #584 the DB layer is a pure delete — auto-promote was
        rejected in favour of a router-layer guard. This test pins the
        contract so any future drift is loud."""
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        _bind(slack_ops, "T-1", "beta")

        slack_ops.unbind_agent("T-1", "alpha")

        # Beta is NOT promoted automatically.
        assert slack_ops.get_dm_default_agent("T-1") is None
        agents = slack_ops.get_agents_for_workspace("T-1")
        assert len(agents) == 1
        assert agents[0]["agent_name"] == "beta"
        assert agents[0]["is_dm_default"] is False

    def test_unbind_only_agent_clears_workspace(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        assert slack_ops.unbind_agent("T-1", "alpha") is True
        assert slack_ops.get_agents_for_workspace("T-1") == []
        assert slack_ops.get_dm_default_agent("T-1") is None

    def test_unbind_unknown_agent_returns_false(self, slack_ops):
        _bind(slack_ops, "T-1", "alpha", is_dm_default=True)
        assert slack_ops.unbind_agent("T-1", "ghost") is False
        # Default unchanged
        assert slack_ops.get_dm_default_agent("T-1") == "alpha"

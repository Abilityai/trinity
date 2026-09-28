"""trinity-enterprise#527 rider — the readiness stamp on the agents list and the fleet grid.

Operator ruling 2026-09-24 (ent#560's closure): readiness is a role-companion
property, and the owner's stamp (`calibrating | ready`, ent#663) should be
visible on the agents list and the fleet grid, not only on the role card.

What is pinned here:
- ONE batched read for the whole list (the display-label pattern — a per-agent
  read on the fleet's hottest endpoint is an N+1), on a real database.
- Only a STAMP is shown. An agent without one is either not a companion or an
  unstamped one, and telling those apart needs the container's template.yaml,
  which a list must never read — so it carries no readiness at all rather than
  a guessed `calibrating`.
- The list says WHAT and WHEN, never WHO: `changed_by` is an email (or the
  ent#689 rollout sentinel) and the list is visible to every agent viewer; the
  role card, which is owner-scoped, keeps the person.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.fixture
def readiness_ops(db_backend):
    try:
        from db.role_readiness import RoleReadinessOperations
    except ImportError:  # pragma: no cover - env guard
        pytest.skip("backend venv required")
    return RoleReadinessOperations()


class TestBatchedRead:
    def test_returns_only_stamped_agents(self, readiness_ops):
        readiness_ops.set_role_readiness("companion-ready", "ready", "owner@example.com")
        readiness_ops.set_role_readiness("companion-cal", "calibrating", "owner@example.com")
        got = readiness_ops.get_role_readiness_for_agents(
            ["companion-ready", "companion-cal", "plain-agent"])
        assert set(got) == {"companion-ready", "companion-cal"}
        assert got["companion-ready"]["status"] == "ready"
        assert got["companion-cal"]["status"] == "calibrating"
        assert got["companion-ready"]["changed_at"]

    def test_the_rollout_seed_is_named_as_such(self, readiness_ops):
        readiness_ops.set_role_readiness("grandfathered", "ready", "rollout:ent#689")
        assert readiness_ops.get_role_readiness_for_agents(["grandfathered"])["grandfathered"] == {
            "status": "ready",
            "changed_at": readiness_ops.get_role_readiness("grandfathered")["changed_at"],
            "source": "rollout",
        }

    def test_an_owners_flip_is_source_owner(self, readiness_ops):
        readiness_ops.set_role_readiness("a", "ready", "owner@example.com")
        assert readiness_ops.get_role_readiness_for_agents(["a"])["a"]["source"] == "owner"

    def test_never_carries_who(self, readiness_ops):
        readiness_ops.set_role_readiness("a", "ready", "owner@example.com")
        assert "changed_by" not in readiness_ops.get_role_readiness_for_agents(["a"])["a"]

    def test_empty_input_does_not_query(self, readiness_ops, monkeypatch):
        import db.role_readiness as mod
        monkeypatch.setattr(mod, "get_engine", lambda: (_ for _ in ()).throw(AssertionError("queried")))
        assert readiness_ops.get_role_readiness_for_agents([]) == {}

    def test_an_unknown_state_in_the_row_is_not_shown(self, readiness_ops):
        # A row written outside the one writer (which validates) must not reach
        # the UI as a third state it has no badge for.
        _hrun("INSERT INTO agent_role_readiness (agent_name, status, changed_at, changed_by) "
              "VALUES ('odd', 'maybe', '2026-09-28T00:00:00Z', 'x')")
        assert readiness_ops.get_role_readiness_for_agents(["odd"]) == {}


class TestListEndpoint:
    @pytest.mark.asyncio
    async def test_attaches_readiness_from_one_batched_read(self, monkeypatch):
        from unittest.mock import MagicMock
        import importlib
        mod = importlib.import_module("routers.agents")
        import database

        agents = [{"name": "companion"}, {"name": "plain"}]
        monkeypatch.setattr(mod, "get_accessible_agents", lambda user: [dict(a) for a in agents])
        db = MagicMock()
        db.get_tags_for_agents.return_value = {}
        db.get_display_labels_for_agents.return_value = {}
        db.get_role_readiness_for_agents.return_value = {
            "companion": {"status": "calibrating", "changed_at": "2026-09-28T10:00:00Z", "source": "owner"},
        }
        monkeypatch.setattr(database, "db", db)

        out = await mod.list_agents_endpoint(MagicMock(), tags=None, current_user=MagicMock())

        by_name = {a["name"]: a for a in out}
        assert by_name["companion"]["readiness"] == {
            "status": "calibrating", "changed_at": "2026-09-28T10:00:00Z", "source": "owner"}
        # Not a companion (or not stamped): no field value, never a guessed state.
        assert by_name["plain"]["readiness"] is None
        db.get_role_readiness_for_agents.assert_called_once_with(["companion", "plain"])

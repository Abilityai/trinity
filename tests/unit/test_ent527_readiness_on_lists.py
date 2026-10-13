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

from db_harness import db_backend, run as _hrun, seed_user  # noqa: E402,F401

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

    @pytest.mark.asyncio
    async def test_a_failed_readiness_read_lists_without_stamps(self, monkeypatch):
        # The stamp is decoration on the fleet's hottest endpoint: a DB fault in
        # its read degrades every row to "no stamp", never a 500 for the list.
        from unittest.mock import MagicMock
        import importlib
        mod = importlib.import_module("routers.agents")
        import database

        agents = [{"name": "companion", "autonomy_enabled": True}]
        monkeypatch.setattr(mod, "get_accessible_agents", lambda user: [dict(a) for a in agents])
        db = MagicMock()
        db.get_tags_for_agents.return_value = {}
        db.get_display_labels_for_agents.return_value = {}
        db.get_role_readiness_for_agents.side_effect = RuntimeError("database is locked")
        monkeypatch.setattr(database, "db", db)

        out = await mod.list_agents_endpoint(MagicMock(), tags=None, current_user=MagicMock())

        assert [a["name"] for a in out] == ["companion"]
        assert out[0]["readiness"] is None
        assert out[0]["brief_held"] is False


# ---------------------------------------------------------------------------
# brief_held on the list (PR #3038 review, item 1): the calibrating tooltip may
# say "its scheduled brief is paused" only when the role card would — an
# enabled seat-delivery schedule AND autonomy on. One predicate for both.
# ---------------------------------------------------------------------------

def _gate():
    try:
        from services import role_readiness_gate
    except ImportError:  # pragma: no cover - env guard
        pytest.skip("backend venv required")
    return role_readiness_gate


class TestSharedPredicate:
    @pytest.mark.parametrize("enabled,email,seat", [
        (1, "s@example.com", True),
        (True, "s@example.com", True),
        (0, "s@example.com", False),
        (1, None, False),
        (1, "   ", False),
        (None, "s@example.com", False),
    ])
    def test_seat_delivery_schedule(self, enabled, email, seat):
        assert _gate().is_seat_delivery_schedule(enabled, email) is seat

    @pytest.mark.parametrize("status,autonomy,has_seat,held", [
        ("calibrating", True, True, True),
        (None, True, True, True),          # the card's unstamped companion
        ("ready", True, True, False),
        ("calibrating", False, True, False),  # autonomy off: a flip starts nothing
        ("calibrating", True, False, False),  # nothing scheduled to hold
    ])
    def test_brief_is_held(self, status, autonomy, has_seat, held):
        assert _gate().brief_is_held(status, autonomy, has_seat) is held

    def test_the_role_card_uses_the_same_predicates(self):
        from client_portal import role_card
        gate = _gate()
        assert role_card.is_seat_delivery_schedule is gate.is_seat_delivery_schedule
        assert role_card.brief_is_held is gate.brief_is_held


def _seed_schedule(sid, agent, enabled, email, deleted_at=None):
    _hrun(
        "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, "
        "enabled, timezone, owner_id, created_at, updated_at, deliver_to_workspace_email, deleted_at) "
        "VALUES (:id, :a, 'brief', '0 8 * * *', 'brief', :en, 'UTC', 1, "
        "'2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', :em, :dl)",
        id=sid, a=agent, en=enabled, em=email, dl=deleted_at,
    )


class TestBatchedScheduleRead:
    def test_one_read_returns_only_live_rows_with_a_delivery_address(self, db_backend):
        from db.schedules import ScheduleOperations
        ops = ScheduleOperations(None, None)
        seed_user(1)
        _seed_schedule("s1", "held", 1, "seat@example.com")
        _seed_schedule("s2", "disabled", 0, "seat@example.com")
        _seed_schedule("s3", "plain", 1, None)
        _seed_schedule("s4", "gone", 1, "seat@example.com", deleted_at="2026-09-02T00:00:00Z")
        _seed_schedule("s5", "other", 1, "seat@example.com")
        rows = ops.get_workspace_delivery_schedules_for_agents(
            ["held", "disabled", "plain", "gone"])
        got = {(r["agent_name"], bool(r["enabled"])) for r in rows}
        # `other` is not asked for; `plain` has no address; `gone` is soft-deleted.
        assert got == {("held", True), ("disabled", False)}

    def test_empty_input_does_not_query(self, db_backend, monkeypatch):
        from db.schedules import ScheduleOperations
        import db.schedules.crud as crud
        monkeypatch.setattr(crud, "get_engine", lambda: (_ for _ in ()).throw(AssertionError("queried")))
        assert ScheduleOperations(None, None).get_workspace_delivery_schedules_for_agents([]) == []


class TestBriefsHeldForList:
    def _db(self, monkeypatch, rows):
        from unittest.mock import MagicMock
        import database
        db = MagicMock()
        db.get_workspace_delivery_schedules_for_agents.return_value = rows
        monkeypatch.setattr(database, "db", db)
        return db

    def test_only_a_calibrating_stamp_with_autonomy_and_a_seat_schedule_is_held(self, monkeypatch):
        seat = lambda a: {"agent_name": a, "enabled": 1, "deliver_to_workspace_email": "s@example.com"}
        db = self._db(monkeypatch, [seat("held"), seat("auto-off"), seat("ready"),
                                    {"agent_name": "no-seat", "enabled": 0,
                                     "deliver_to_workspace_email": "s@example.com"}])
        agents = [
            {"name": "held", "autonomy_enabled": True},
            {"name": "auto-off", "autonomy_enabled": False},
            {"name": "ready", "autonomy_enabled": True},
            {"name": "no-seat", "autonomy_enabled": True},
            {"name": "unstamped", "autonomy_enabled": True},
        ]
        readiness = {
            "held": {"status": "calibrating"}, "auto-off": {"status": "calibrating"},
            "ready": {"status": "ready"}, "no-seat": {"status": "calibrating"},
        }
        assert _gate().briefs_held_for_list(agents, readiness) == {"held"}
        # One read, and only over the agents that could be held.
        db.get_workspace_delivery_schedules_for_agents.assert_called_once()
        asked = set(db.get_workspace_delivery_schedules_for_agents.call_args.args[0])
        assert asked == {"held", "no-seat"}

    def test_nothing_calibrating_reads_nothing(self, monkeypatch):
        db = self._db(monkeypatch, [])
        assert _gate().briefs_held_for_list(
            [{"name": "a", "autonomy_enabled": True}], {"a": {"status": "ready"}}) == set()
        db.get_workspace_delivery_schedules_for_agents.assert_not_called()

    def test_an_unreadable_schedule_list_claims_no_pause(self, monkeypatch):
        db = self._db(monkeypatch, [])
        db.get_workspace_delivery_schedules_for_agents.side_effect = RuntimeError("db")
        assert _gate().briefs_held_for_list(
            [{"name": "a", "autonomy_enabled": True}], {"a": {"status": "calibrating"}}) == set()


class TestListEndpointBriefHeld:
    @pytest.mark.asyncio
    async def test_attaches_brief_held_next_to_readiness(self, monkeypatch):
        from unittest.mock import MagicMock
        import importlib
        mod = importlib.import_module("routers.agents")
        import database

        agents = [{"name": "held", "autonomy_enabled": True},
                  {"name": "auto-off", "autonomy_enabled": False},
                  {"name": "plain", "autonomy_enabled": True}]
        monkeypatch.setattr(mod, "get_accessible_agents", lambda user: [dict(a) for a in agents])
        db = MagicMock()
        db.get_tags_for_agents.return_value = {}
        db.get_display_labels_for_agents.return_value = {}
        db.get_role_readiness_for_agents.return_value = {
            "held": {"status": "calibrating", "changed_at": "2026-09-28T10:00:00Z", "source": "owner"},
            "auto-off": {"status": "calibrating", "changed_at": "2026-09-28T10:00:00Z", "source": "owner"},
        }
        db.get_workspace_delivery_schedules_for_agents.return_value = [
            {"agent_name": "held", "enabled": 1, "deliver_to_workspace_email": "s@example.com"},
            {"agent_name": "auto-off", "enabled": 1, "deliver_to_workspace_email": "s@example.com"},
        ]
        monkeypatch.setattr(database, "db", db)

        out = await mod.list_agents_endpoint(MagicMock(), tags=None, current_user=MagicMock())

        by_name = {a["name"]: a for a in out}
        assert by_name["held"]["brief_held"] is True
        assert by_name["auto-off"]["brief_held"] is False
        assert by_name["plain"]["brief_held"] is False

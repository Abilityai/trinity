"""#3495 — a Workspace-only account cannot set its own room budget either.

ent#387 made a room's budget an operator control: a workspace (portal) client's
supplied `max_messages` / `max_cost_usd` / `ttl_hours` are ignored and the
operator defaults apply. It drew the line at "platform principal", and since
#3488 that includes the Workspace-only `user` role (ent#837) — so such an
account kept whatever budget it sent: an uncapped cost, 500 messages, or a TTL
of 0 (a room that never expires), while a portal client sending the same values
was cut back.

Pinned here:
  1. a principal below `operator` (the `user` rung, an MCP key owned by one, or a
     role outside the ladder) is treated like a workspace client: its values are
     IGNORED and the operator defaults apply — the ent#387 rule, not a clamp;
  2. `operator` and above may still bound a room explicitly, including TTL 0;
  3. `max_cost_usd` has an upper bound, enforced on room creation AND on the
     operator default (the two ranges mirror each other), and a stored default
     above it is clamped to it — never read as "uncapped".
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from unit.test_ent387_room_budget_defaults import _mk_room, _row, allow_all, rooms_db  # noqa: F401


def _principal(role, **kw):
    from models import User
    return User(id=1, username="alice", role=role, **kw)


class TestWhoMayBoundARoom:
    @pytest.mark.parametrize("principal", [
        pytest.param(lambda: _principal("user"), id="workspace-only user (JWT)"),
        pytest.param(lambda: _principal("user", mcp_scope="user"), id="MCP key of a user"),
        pytest.param(lambda: _principal("not-a-role"), id="role outside the ladder"),
    ])
    def test_below_operator_gets_the_operator_defaults(self, rooms_db, allow_all, principal):
        from shared_sessions import service
        room = _mk_room(principal(), max_messages=500, max_cost_usd=999.0, ttl_hours=0)
        row = _row(room)
        assert row["max_messages"] == service.DEFAULT_MAX_MESSAGES, "the caller's 500 must not survive"
        assert row["max_cost_usd"] is None, "the caller's cost must not survive"
        assert row["expires_at"] is not None, "a TTL of 0 (never expires) must not survive"

    @pytest.mark.parametrize("role", ["operator", "creator", "admin"])
    def test_operator_and_above_may_bound_a_room(self, rooms_db, allow_all, role):
        room = _mk_room(_principal(role), max_messages=10, max_cost_usd=1.5, ttl_hours=0)
        row = _row(room)
        assert row["max_messages"] == 10
        assert float(row["max_cost_usd"]) == 1.5
        assert row["expires_at"] is None, "an operator may still open a room that never expires"

    def test_a_platform_principal_without_a_role_fails_closed(self, rooms_db, allow_all):
        """Belt: a future principal type that carries neither `is_portal` nor a
        role must not inherit the operator's authority by omission."""
        from shared_sessions import service

        class _NoRole:
            id = 1
            username = "alice"
            email = "alice@example.com"
            agent_name = None

        room = _mk_room(_NoRole(), max_messages=500)
        assert _row(room)["max_messages"] == service.DEFAULT_MAX_MESSAGES


class TestCostCeiling:
    def test_room_create_refuses_a_cost_above_the_ceiling(self):
        from shared_sessions import service
        from shared_sessions.models import RoomCreate
        RoomCreate(name="r", agents=["a"], max_cost_usd=service.MAX_COST_USD_CEILING)
        with pytest.raises(ValidationError):
            RoomCreate(name="r", agents=["a"], max_cost_usd=service.MAX_COST_USD_CEILING + 0.01)

    def test_the_operator_default_has_the_same_ceiling(self):
        """The two models' ranges mirror each other: a default must never be a
        value a caller would have been refused."""
        from shared_sessions import service
        from shared_sessions.models import RoomBudgetDefaultsUpdate
        RoomBudgetDefaultsUpdate(max_cost_usd=service.MAX_COST_USD_CEILING)
        with pytest.raises(ValidationError):
            RoomBudgetDefaultsUpdate(max_cost_usd=service.MAX_COST_USD_CEILING + 0.01)

    def test_a_stored_default_above_the_ceiling_is_clamped_not_uncapped(self, rooms_db):
        """A row written before the ceiling existed (or by hand) must tighten to
        the ceiling. Reading it as invalid would fall back to NO cost cap — the
        unsafe direction."""
        from database import db
        from shared_sessions import service
        db.set_setting(service.ROOM_DEFAULT_MAX_COST_KEY, str(service.MAX_COST_USD_CEILING * 10))
        assert service.budget_defaults()["max_cost_usd"] == service.MAX_COST_USD_CEILING

    def test_the_defaults_response_reports_the_ceiling(self, rooms_db):
        """The operator panel reads its limits from the response, as it does for
        messages and TTL — never from a second hard-coded copy."""
        from shared_sessions import router, service
        assert router._budget_defaults_response().max_cost_usd_ceiling == service.MAX_COST_USD_CEILING

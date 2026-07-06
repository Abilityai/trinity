"""
Tests for the #1445 no-orphan schedule/webhook creation gate — DB layer.

The router-level ordering (access-check-first, then the 404 that blocks orphan
creation) is pinned in ``tests/unit/test_929_timeout_validation.py``; the
end-to-end 404s are in ``tests/test_webhook_triggers.py``. This file covers the
two DB-layer primitives those higher-level tests can NOT reach:

1. ``AgentOperations.is_agent_live`` — the new predicate. Its whole point is
   that it checks ONLY ``agent_ownership`` (``deleted_at IS NULL``, no ``users``
   join) so it matches the webhook token-lookup predicate
   (``get_schedule_by_webhook_token``, #1423 INNER JOIN) *exactly*. A live agent
   whose owner-user row is missing must read as live — ``get_agent_owner``
   (INNER-JOINs ``users``) would say the opposite, which is precisely why the
   fix does NOT reuse it.

2. ``ScheduleOperations.create_schedule`` chokepoint — the "no-orphan invariant
   holds for every caller" guard (router, MCP, system-manifest deploy, future).
   The router 404s *before* the DB layer and the router unit tests stub
   ``db.create_schedule``, so this guard is only reached by non-router callers —
   the sibling path an incomplete fix would silently leave open.

Backend-agnostic via ``db_harness`` (#300): runs on SQLite and, when
``TEST_POSTGRES_URL`` is set, PostgreSQL too, against the full production schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import (  # noqa: E402
    db_backend,
    seed_user,
    run as _hrun,
    scalar as _hscalar,
)


_TS = "2026-01-01T00:00:00Z"


@pytest.fixture
def tmp_db(db_backend):
    """Fresh full schema; seeds an admin (id=2) whose ``can_user_access_agent``
    is unconditionally True, so the create_schedule chokepoint's is_agent_live
    branch is the ONLY thing that can block — isolating what's under test."""
    seed_user(2, "admin", "admin")
    return db_backend


@pytest.fixture
def agent_ops(tmp_db):
    try:
        from db.agents import AgentOperations
        from db.users import UserOperations
    except ImportError:
        pytest.skip("backend venv required (no `db.agents` import)")
    return AgentOperations(UserOperations())


@pytest.fixture
def schedule_ops(tmp_db):
    try:
        from db.schedules import ScheduleOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required (no `db.schedules` import)")
    user_ops = UserOperations()
    return ScheduleOperations(user_ops, AgentOperations(user_ops))


def _seed_agent(name: str, owner_id: int = 2, deleted_at: str | None = None):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, :o, :ts, :deleted)",
        n=name, o=owner_id, ts=_TS, deleted=deleted_at,
    )


# -----------------------------------------------------------------------------
# is_agent_live — the token-lookup-matching predicate (#1445)
# -----------------------------------------------------------------------------

class TestIsAgentLive:
    def test_live_agent_is_live(self, tmp_db, agent_ops):
        _seed_agent("alive")
        assert agent_ops.is_agent_live("alive") is True

    def test_soft_deleted_agent_is_not_live(self, tmp_db, agent_ops):
        _seed_agent("ghost", deleted_at=_TS)
        assert agent_ops.is_agent_live("ghost") is False

    def test_nonexistent_agent_is_not_live(self, tmp_db, agent_ops):
        assert agent_ops.is_agent_live("never-created") is False

    def test_live_agent_with_missing_owner_user_still_live(self, tmp_db, agent_ops):
        """The load-bearing invariant (#1445): is_agent_live must NOT join
        ``users``. A live agent whose owner-user row is absent (FKs are off
        platform-wide) is still live for the webhook lookup — so the creation
        gate must agree, or it 404s an agent the trigger path happily serves.

        ``get_agent_owner`` INNER-JOINs ``users`` and returns None for the same
        row; asserting the divergence pins why the fix can't reuse it."""
        _seed_agent("orphan-owner", owner_id=99999)  # no such users row

        assert agent_ops.is_agent_live("orphan-owner") is True, (
            "a live ownership row with a missing owner-user must read as live "
            "(no users join) — matching get_schedule_by_webhook_token"
        )
        assert agent_ops.get_agent_owner("orphan-owner") is None, (
            "sanity: get_agent_owner DOES join users and diverges here — the "
            "exact false-negative is_agent_live exists to avoid"
        )


# -----------------------------------------------------------------------------
# create_schedule chokepoint — no-orphan for every caller (#1445)
# -----------------------------------------------------------------------------

class TestCreateScheduleChokepoint:
    """``ScheduleOperations.create_schedule`` refuses to mint a schedule (→ the
    router maps None to 403/404) on an agent with no live ownership row. The
    router blocks first for HTTP callers, so this DB guard is the safety net for
    MCP / system-manifest-deploy / any future non-router creation path."""

    def _schedule_data(self):
        from db_models import ScheduleCreate
        return ScheduleCreate(
            name="gate", cron_expression="*/5 * * * *", message="hi",
        )

    def test_create_on_missing_agent_returns_none(self, tmp_db, schedule_ops):
        """Admin has access (unconditional) but the agent was never created →
        the is_agent_live guard returns None and NO row is persisted. This is
        the orphan-schedule class the whole fix closes."""
        result = schedule_ops.create_schedule("never-created", "admin", self._schedule_data())
        assert result is None
        assert _hscalar(
            "SELECT COUNT(*) FROM agent_schedules WHERE agent_name = :n",
            n="never-created",
        ) == 0, "no orphan schedule row may be written for a dead agent"

    def test_create_on_soft_deleted_agent_returns_none(self, tmp_db, schedule_ops):
        _seed_agent("was-here", deleted_at=_TS)
        result = schedule_ops.create_schedule("was-here", "admin", self._schedule_data())
        assert result is None
        assert _hscalar(
            "SELECT COUNT(*) FROM agent_schedules WHERE agent_name = :n",
            n="was-here",
        ) == 0

    def test_create_on_live_agent_succeeds(self, tmp_db, schedule_ops):
        """Positive control: the gate must not over-block — a live agent still
        creates normally (guards against a false-positive that would break all
        schedule creation)."""
        _seed_agent("real-agent")
        result = schedule_ops.create_schedule("real-agent", "admin", self._schedule_data())
        assert result is not None
        assert result.agent_name == "real-agent"
        assert _hscalar(
            "SELECT COUNT(*) FROM agent_schedules WHERE agent_name = :n",
            n="real-agent",
        ) == 1

    def test_create_unknown_user_returns_none(self, tmp_db, schedule_ops):
        """Pre-existing pre-gate branch, kept as a guard: an unknown username
        is refused before the agent is ever consulted."""
        _seed_agent("real-agent")
        assert schedule_ops.create_schedule(
            "real-agent", "nobody", self._schedule_data()
        ) is None

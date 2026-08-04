"""
Regression for #1945 — the autonomy toggle must not erase per-schedule intent.

``set_autonomy_status_logic`` used to loop over every schedule on the agent and
write ``set_schedule_enabled(id, enabled)``, unfiltered and in both directions.
That made the agent-level gate and the per-schedule ``enabled`` flag share one
write path, and only one of them survived:

- a schedule the owner deliberately disabled was silently re-armed on the next
  autonomy-on;
- a template-authored ``enabled: false`` was erased the same way;
- autonomy-off was a set-all, not a pause — nothing remembered the prior state.

With a template able to materialize up to 20 declared schedules at creation,
one unrelated toggle could arm all of them at once.

The fix keeps the two concepts separate: the toggle writes ONLY
``agent_ownership.autonomy_enabled``, and the scheduler's cron-fire gate reads
it. Per-schedule ``enabled`` is owner intent and is never touched.

Backend-agnostic via ``db_harness`` (#300): the schedule + autonomy reads and
writes go through the active engine (SQLite, and PostgreSQL when
``TEST_POSTGRES_URL`` is set). Only the access check and the Docker container
lookup are stubbed — neither is under test here.
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
    seed_agent,
    seed_user,
    run as _hrun,
    scalar as _hscalar,
)


# Sibling tests stub `sys.modules["db.<sub>"]` with importlib-loaded modules
# bound to *their* tmp DBs and never restore on teardown. Snapshot + pop any
# stale stubs so this file's imports re-resolve fresh, and restore on teardown
# so we don't pollute siblings either. (Precedent: test_schedule_soft_delete.)
_STUBBED_MODULE_NAMES = [
    "db.schedules",
    "db.agents",
    "db.users",
]

AGENT = "agent-1"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    for name in _STUBBED_MODULE_NAMES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _seed_schedule(sid: str, *, enabled: bool, next_run_at: str | None = None) -> None:
    _hrun(
        "INSERT INTO agent_schedules "
        "(id, agent_name, name, cron_expression, message, enabled, timezone, "
        " owner_id, created_at, updated_at, next_run_at) "
        "VALUES (:id, :a, :nm, '0 0 * * *', 'hi', :en, 'UTC', 1, "
        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', :nr)",
        id=sid, a=AGENT, nm=sid, en=1 if enabled else 0, nr=next_run_at,
    )


def _enabled(sid: str) -> bool:
    return bool(_hscalar("SELECT enabled FROM agent_schedules WHERE id = :s", s=sid))


def _row(sid: str, column: str):
    return _hscalar(f"SELECT {column} FROM agent_schedules WHERE id = :s", s=sid)


@pytest.fixture
def autonomy_env(db_backend, monkeypatch):
    """Live agent owned by ``owner``, with the auth + container checks stubbed.

    Returns the service module so tests call the real code path.
    """
    try:
        from services.agent_service import autonomy
        from models import User
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")

    seed_user(1, "owner", "user")
    seed_agent(AGENT, 1)

    monkeypatch.setattr(autonomy.db, "can_user_share_agent", lambda u, a: True)
    monkeypatch.setattr(autonomy.db, "is_system_agent", lambda a: False)
    monkeypatch.setattr(
        autonomy, "get_agent_container", lambda a: type("C", (), {"status": "running"})()
    )

    return autonomy, User(id=1, username="owner", role="user")


async def _toggle(autonomy_env, enabled: bool) -> dict:
    autonomy, user = autonomy_env
    return await autonomy.set_autonomy_status_logic(AGENT, {"enabled": enabled}, user)


# ---------------------------------------------------------------------------
# AC5 — the scenario named in the issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_schedule_survives_an_autonomy_off_on_cycle(autonomy_env):
    """Disable one of two schedules → autonomy off → on → it is STILL disabled.

    Against the pre-#1945 loop the second toggle re-enabled it.
    """
    _seed_schedule("sched-on", enabled=True)
    _seed_schedule("sched-off", enabled=False)

    await _toggle(autonomy_env, True)
    await _toggle(autonomy_env, False)
    await _toggle(autonomy_env, True)

    assert _enabled("sched-off") is False, (
        "an owner-disabled schedule was force-enabled by an autonomy toggle (#1945)"
    )
    assert _enabled("sched-on") is True


@pytest.mark.asyncio
async def test_autonomy_off_does_not_flatten_enabled_schedules(autonomy_env):
    """AC2 — disabling autonomy pauses, it does not rewrite intent."""
    _seed_schedule("sched-on", enabled=True)
    _seed_schedule("sched-off", enabled=False)

    await _toggle(autonomy_env, False)

    assert _enabled("sched-on") is True
    assert _enabled("sched-off") is False


@pytest.mark.asyncio
async def test_toggle_writes_no_schedule_row_at_all(autonomy_env):
    """Stronger than the flag check: the rows are not written, period.

    ``set_schedule_enabled`` bumps ``updated_at`` (the column the scheduler's
    sync loop diffs on, #420) and rewrites ``next_run_at`` — so an unchanged
    pair proves the fan-out is gone rather than merely idempotent.
    """
    _seed_schedule("sched-on", enabled=True, next_run_at="2026-01-02T00:00:00Z")
    before = (_row("sched-on", "updated_at"), _row("sched-on", "next_run_at"))

    await _toggle(autonomy_env, False)
    await _toggle(autonomy_env, True)

    assert (_row("sched-on", "updated_at"), _row("sched-on", "next_run_at")) == before


# ---------------------------------------------------------------------------
# AC3 — the agent-level gate stays authoritative, and is now the ONLY thing
# stopping a cron fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_toggle_still_moves_the_agent_gate(autonomy_env):
    autonomy, _ = autonomy_env
    _seed_schedule("sched-on", enabled=True)

    await _toggle(autonomy_env, True)
    assert autonomy.db.get_autonomy_enabled(AGENT) is True

    await _toggle(autonomy_env, False)
    assert autonomy.db.get_autonomy_enabled(AGENT) is False


def test_scheduler_still_gates_cron_fires_on_autonomy():
    """The gate carries the whole load now — pin it at the source.

    ``src/scheduler/service.py::_execute_schedule_with_lock`` refuses a
    cron-triggered fire when the agent's autonomy is off. Before #1945 an
    enabled schedule on a paused agent was a rarity (the toggle disabled them
    all); now it is the normal paused state, so losing this check would fire
    every schedule of every paused agent.
    """
    src = (
        Path(__file__).resolve().parents[2] / "src" / "scheduler" / "service.py"
    ).read_text(encoding="utf-8")
    assert 'triggered_by == "schedule" and not self.db.get_autonomy_enabled(' in src


# ---------------------------------------------------------------------------
# Response honesty — the old `schedules_updated` count described a write that
# no longer happens; the replacement must name what the schedules will do.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_response_reports_counts_not_a_bogus_update_tally(autonomy_env):
    _seed_schedule("sched-on", enabled=True)
    _seed_schedule("sched-off", enabled=False)

    result = await _toggle(autonomy_env, True)

    assert result["autonomy_enabled"] is True
    assert result["total_schedules"] == 2
    assert result["enabled_schedules"] == 1
    assert "schedules_updated" not in result
    assert "1 of 2" in result["message"]


@pytest.mark.asyncio
async def test_enabling_autonomy_with_every_schedule_disabled_says_so(autonomy_env):
    """The upgrade case: an agent whose schedules were already flattened to
    disabled by a pre-#1945 autonomy-off no longer silently re-arms — so the
    operator must be told nothing will run."""
    _seed_schedule("sched-off", enabled=False)

    result = await _toggle(autonomy_env, True)

    assert result["enabled_schedules"] == 0
    assert "nothing will run" in result["message"]


@pytest.mark.asyncio
async def test_agent_with_no_schedules_reports_zeroes(autonomy_env):
    result = await _toggle(autonomy_env, True)

    assert result["total_schedules"] == 0
    assert result["enabled_schedules"] == 0
    assert "no schedules" in result["message"].lower()

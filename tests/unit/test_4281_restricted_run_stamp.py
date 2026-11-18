"""A schedule-backed run whose tier is not owner says so on the run (#4281).

A schedule's runs are rated by its creator. When the creator is neither an
admin nor the agent's owner, every run is a shared person's turn: no shell, no
owner-added MCP servers. The run still ended `success` and nothing on the row
said it had been restricted. Now the terminal write records `restricted_turn`
in the existing `turn_integrity` JSON column (#2467's column for "a `success`
that was not clean"): `{tier, kind, cause, schedule_id, creator_id, shell}`.
No status change, no migration.

Driven through the real terminal appliers (`TaskExecutionService.apply_result`
and the pull-mode `apply_task_result`) on the unit conftest's SQLite, with real
users, a real share and a real schedule. Only the side effects that leave the
process (activity, capacity, breaker, events, channel report) are stubbed.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, update

from database import db
from db.engine import get_engine
from db.tables import agent_schedules, schedule_executions, users
from db.write_params import ExecutionSource
from db_models import ScheduleCreate, UserCreate
from models import TaskExecutionStatus
from services import turn_tier_service
from services.execution_envelope import TaskExecutionErrorCode, TerminalEnvelope

pytestmark = pytest.mark.unit


def _person(role, *, name=None):
    username = f"s4281-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(UserCreate(username=username, email=username, role=role, password="x", name=name))
    return SimpleNamespace(username=username, id=db.get_user_by_username(username)["id"])


@pytest.fixture
def world(monkeypatch):
    from services import operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    owner = _person("creator")
    mate = _person("operator", name="Mate Shared")
    admin = _person("admin")
    agent = f"s4281-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(agent, owner.username)
    db.share_agent(agent, owner.username, mate.username)
    return SimpleNamespace(agent=agent, owner=owner, mate=mate, admin=admin)


def schedule_by(world, who, **extra):
    data = ScheduleCreate(name=f"brief-{uuid.uuid4().hex[:4]}", cron_expression="0 9 * * 1",
                          message="Weekly brief", timezone="UTC", **extra)
    return db.create_schedule(world.agent, getattr(world, who).username, data)


def run_of(world, schedule, *, source=None, triggered_by="schedule"):
    return db.create_schedule_execution(schedule.id, world.agent, "Weekly brief",
                                        triggered_by=triggered_by, source=source).id


def stored(eid):
    raw = db.get_execution(eid).turn_integrity
    return json.loads(raw) if raw else None


def apply(world, envelope):
    from services.task_execution_service import TaskExecutionService

    with (
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(release=AsyncMock())),
        patch("services.task_execution_service.activity_service",
              MagicMock(complete_activity=AsyncMock())),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.event_dispatch_service", MagicMock()),
        patch("services.task_execution_service.channel_completion_report", MagicMock()),
        patch("services.task_execution_service._spawn_bg", lambda coro: coro.close()),
    ):
        return asyncio.run(TaskExecutionService().apply_result(world.agent, envelope))


def success(eid, **over):
    base = dict(execution_id=eid, status=TaskExecutionStatus.SUCCESS, response="brief sent",
                metadata={"cost_usd": 0.01, "context_window": 200000},
                execution_log=[{"type": "result", "subtype": "success"}])
    base.update(over)
    return TerminalEnvelope(**base)


def failure(eid, **over):
    base = dict(execution_id=eid, status=TaskExecutionStatus.FAILED,
                error="[turn_refused] This agent runs on gemini-cli, which cannot limit such a turn",
                error_code=TaskExecutionErrorCode.TURN_REFUSED, metadata={})
    base.update(over)
    return TerminalEnvelope(**base)


def kill_log():
    """A #2467 kill tail: a background task killed at CLI exit."""
    return [
        {"type": "system", "subtype": "background_tasks_changed",
         "tasks": [{"task_id": "bg1", "task_type": "local_bash"}]},
        {"type": "system", "subtype": "task_updated", "task_id": "bg1",
         "patch": {"status": "killed", "end_time": 1}},
    ]


def _set(table, row_id, **values):
    with get_engine().begin() as conn:
        conn.execute(update(table).where(table.c.id == row_id).values(**values))


# ---- the stamp ------------------------------------------------------------------------

def test_a_share_holders_schedule_run_is_success_and_says_it_was_restricted(world):
    schedule = schedule_by(world, "mate")
    eid = run_of(world, schedule)

    result = apply(world, success(eid))

    assert result.status == TaskExecutionStatus.SUCCESS
    row = db.get_execution(eid)
    assert row.status == TaskExecutionStatus.SUCCESS
    assert stored(eid) == {"restricted_turn": {
        "tier": "share", "kind": "person", "cause": "creator_not_owner_tier",
        "schedule_id": schedule.id, "creator_id": world.mate.id, "shell": False}}


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_an_owner_tier_creators_run_is_byte_identical_to_before(world, who):
    eid = run_of(world, schedule_by(world, who))
    apply(world, success(eid))
    row = db.get_execution(eid)
    assert row.turn_integrity is None
    assert row.response == "brief sent"


def test_the_2467_keys_and_the_stamp_share_one_object(world):
    eid = run_of(world, schedule_by(world, "mate"))
    apply(world, success(eid, execution_log=kill_log()))
    payload = stored(eid)
    assert payload["background_tasks_killed"][0]["task_id"] == "bg1"
    assert payload["restricted_turn"]["cause"] == "creator_not_owner_tier"
    assert db.get_execution(eid).response.startswith("> ⚠️ Background work lost")


def test_a_refused_gemini_run_is_stamped_on_its_failure_too(world):
    schedule = schedule_by(world, "mate")
    eid = run_of(world, schedule)
    apply(world, failure(eid))
    assert db.get_execution(eid).status == TaskExecutionStatus.FAILED
    assert stored(eid)["restricted_turn"]["schedule_id"] == schedule.id


def test_a_dangling_creator_is_stamped_creator_missing(world):
    schedule = schedule_by(world, "mate")
    _set(agent_schedules, schedule.id, owner_id=987654321)
    eid = run_of(world, schedule)
    apply(world, success(eid))
    stamp = stored(eid)["restricted_turn"]
    assert (stamp["cause"], stamp["creator_id"]) == ("creator_missing", 987654321)


def test_an_agent_whose_owner_row_is_gone_is_stamped_agent_has_no_owner(world):
    schedule = schedule_by(world, "mate")
    with get_engine().begin() as conn:      # FKs are off: the ownership row stays, its user goes
        conn.execute(delete(users).where(users.c.id == world.owner.id))
    eid = run_of(world, schedule)
    apply(world, success(eid))
    assert stored(eid)["restricted_turn"]["cause"] == "agent_has_no_owner"


def test_a_schedule_deleted_mid_run_is_stamped_unreadable_with_no_creator(world):
    schedule = schedule_by(world, "owner")
    eid = run_of(world, schedule)
    assert db.delete_schedule(schedule.id, world.owner.username)
    apply(world, success(eid))
    assert stored(eid)["restricted_turn"] == {
        "tier": "share", "kind": "person", "cause": "schedule_unreadable",
        "schedule_id": schedule.id, "creator_id": None, "shell": False}


def test_a_manual_run_now_carries_its_clicker_and_is_not_stamped(world):
    schedule = schedule_by(world, "mate")
    eid = run_of(world, schedule, triggered_by="manual",
                 source=ExecutionSource(source_user_id=world.owner.id))
    apply(world, success(eid))
    assert db.get_execution(eid).turn_integrity is None


def test_a_rating_that_raises_writes_no_stamp_and_the_run_still_succeeds(world, monkeypatch):
    eid = run_of(world, schedule_by(world, "mate"))

    def broken(*_a, **_k):
        raise RuntimeError("users table unreadable")
    monkeypatch.setattr(turn_tier_service, "_user_tier", broken)

    result = apply(world, success(eid))
    assert result.status == TaskExecutionStatus.SUCCESS
    row = db.get_execution(eid)
    assert (row.status, row.turn_integrity) == (TaskExecutionStatus.SUCCESS, None)


def test_the_stamp_is_kept_when_a_late_success_corrects_the_failure(world):
    """The column's `None` means "keep it" (#2467): the stamp records that an
    attempt ran restricted, and a later write with nothing to add keeps it."""
    schedule = schedule_by(world, "mate")
    eid = run_of(world, schedule)
    apply(world, failure(eid))
    _set(users, world.mate.id, role="admin")          # rated owner from now on
    apply(world, success(eid))
    row = db.get_execution(eid)
    assert row.status == TaskExecutionStatus.SUCCESS
    assert stored(eid)["restricted_turn"]["cause"] == "creator_not_owner_tier"


def test_the_pull_mode_result_writer_stamps_too(world, monkeypatch):
    from services import pull_coordination_service as pcs
    for name in ("event_dispatch_service", "channel_completion_report", "activity_service",
                 "subscription_auto_switch"):
        monkeypatch.setattr(pcs, name, MagicMock())
    schedule = schedule_by(world, "mate")
    eid = run_of(world, schedule)
    _set(schedule_executions, eid, claim_token="tok-4281")

    outcome = pcs.apply_task_result(eid, "tok-4281", status="success", content="brief sent")

    assert outcome.kind == "applied"
    assert stored(eid)["restricted_turn"]["creator_id"] == world.mate.id


def test_the_pull_mode_writer_leaves_an_owner_run_untouched(world, monkeypatch):
    from services import pull_coordination_service as pcs
    for name in ("event_dispatch_service", "channel_completion_report", "activity_service",
                 "subscription_auto_switch"):
        monkeypatch.setattr(pcs, name, MagicMock())
    eid = run_of(world, schedule_by(world, "owner"))
    _set(schedule_executions, eid, claim_token="tok-4281b")
    pcs.apply_task_result(eid, "tok-4281b", status="failed", content="boom")
    assert db.get_execution(eid).turn_integrity is None


# ---- the record's own belt (the #2467 charset rule) -------------------------------------

def test_the_record_carries_tokens_and_ids_only():
    from services import execution_integrity as ei
    record = ei.restricted_turn_record(
        tier="share", kind="person", cause="creator not\nowner", schedule_id="id; drop",
        creator_id=True)
    assert record == {"tier": "share", "kind": "person", "cause": "unknown",
                      "schedule_id": "invalid", "creator_id": None, "shell": False}


def test_merging_keeps_every_2467_key_and_none_is_a_no_op():
    from services import execution_integrity as ei
    assert ei.with_restricted_turn(None, None) is None
    assert ei.with_restricted_turn('{"background_tasks_pending_at_exit": 2}', None) == \
        '{"background_tasks_pending_at_exit": 2}'
    merged = json.loads(ei.with_restricted_turn('{"background_tasks_pending_at_exit": 2}',
                                                {"tier": "share"}))
    assert merged == {"background_tasks_pending_at_exit": 2, "restricted_turn": {"tier": "share"}}


# ---- the stamp agrees with the turn's own rating -------------------------------------------

@pytest.mark.parametrize("shape", ["share", "owner", "admin", "deleted", "manual", "no_schedule"])
def test_the_stamped_rating_is_the_rating_the_turn_gets(world, shape):
    """`run_schedule` restates `turn_actor`'s test for "a schedule-backed run",
    so the record can never describe a run the hook rated differently."""
    who = {"share": "mate", "admin": "admin"}.get(shape, "owner")
    schedule = schedule_by(world, who)
    source = ExecutionSource(source_user_id=world.mate.id) if shape == "manual" else None
    eid = run_of(world, schedule, source=source)
    if shape == "deleted":
        db.delete_schedule(schedule.id, world.owner.username)
    if shape == "no_schedule":
        eid = db.create_task_execution(world.agent, "hi", triggered_by="schedule").id
    execution = db.get_execution(eid)

    rated = turn_tier_service.schedule_backed_run_tier(db, world.agent, execution)
    tier, kind, _ = turn_tier_service.turn_actor(db, world.agent, execution=execution)

    if shape in ("manual", "no_schedule"):
        assert rated is None
    else:
        assert (rated.tier, rated.kind) == (tier, kind)
    assert (rated is not None and rated.tier != "owner") == (shape in ("share", "deleted"))

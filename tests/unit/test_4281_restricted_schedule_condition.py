"""A schedule whose runs are a shared person's is a platform condition (#4281).

The operator hears of it once per agent x creator (kind
`schedule_runs_restricted`, subject `schedule_runs_restricted:<creator id>`),
on the #3246 one-row-per-subject seam, and the row ends when the cause goes
away: the schedule is fixed, deleted, disabled or taken over.

What each test catches:
* per-run or per-schedule alert spam (the #3246 class) and a person-driven
  flood of the owner's queue (the #1677 criterion, review R4): many runs and
  many schedules by one creator are ONE row;
* a stale row: every writer of `enabled` (the routes, the admin pause, resume
  and emergency stop, review R5) decides the row again, and so do dispatch
  (a run whose terminal never reaches `apply_result`, e.g. the watchdog's) and
  the winning terminal write;
* a late run re-filing what a disable cleared (review X7);
* a person's ending being re-filed by the same person's next brief, while a
  new cause still files (`person_ending_is_final`, material key `cause`);
* a creator disclosure to a share holder or a machine key (decision D5).

Real routes, the real `get_current_user`, real JWTs, the real seam and the
unit conftest's SQLite.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from database import db
from db.engine import get_engine
from db.tables import operator_queue, users
from db_models import UserCreate
from dependencies import create_access_token
from models import InternalTaskExecutionRequest, TaskExecutionStatus
from routers import operator_queue as operator_queue_router
from routers import ops as ops_router
from routers import schedules as schedules_router
from services.execution_envelope import TerminalEnvelope

pytestmark = pytest.mark.unit

KIND = "schedule_runs_restricted"
BODY = {"cron_expression": "0 9 * * 1", "message": "Weekly brief", "timezone": "UTC"}


def _person(role, *, name=None):
    username = f"c4281-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(UserCreate(username=username, email=username, role=role, password="x", name=name))
    return SimpleNamespace(username=username, id=db.get_user_by_username(username)["id"],
                           jwt=create_access_token({"sub": username}))


@pytest.fixture
def client(monkeypatch):
    from services import operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    app = FastAPI()
    app.include_router(ops_router.router)
    app.include_router(schedules_router.router)
    app.include_router(operator_queue_router.router)
    return TestClient(app)


@pytest.fixture
def world():
    owner = _person("creator")
    mate = _person("operator", name="Mate Shared")
    other = _person("operator", name="Other Shared")
    admin = _person("admin")
    agent = f"c4281-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(agent, owner.username)
    db.share_agent(agent, owner.username, mate.username)
    db.share_agent(agent, owner.username, other.username)
    return SimpleNamespace(agent=agent, owner=owner, mate=mate, other=other, admin=admin)


def call(client, world, who, method, path="", **kw):
    return client.request(method, f"/api/agents/{world.agent}/schedules{path}",
                          headers={"Authorization": f"Bearer {getattr(world, who).jwt}"}, **kw)


def create(client, world, who, **extra):
    resp = call(client, world, who, "POST", json={**BODY, "name": f"brief-{uuid.uuid4().hex[:4]}", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def alerts(world, status="pending"):
    return [r for r in db.list_operator_queue_items(agent_name=world.agent, status=status)
            if (r.get("subject") or "").startswith(f"{KIND}:")]


def the_alert(world):
    rows = alerts(world)
    assert len(rows) == 1, rows
    return rows[0]


def _set_role(person, role):
    with get_engine().begin() as conn:
        conn.execute(update(users).where(users.c.id == person.id).values(role=role))


def finish(world, schedule_id, status=TaskExecutionStatus.SUCCESS, eid=None):
    """A run of the schedule ends through the shared terminal applier."""
    from services.task_execution_service import TaskExecutionService
    eid = eid or db.create_schedule_execution(schedule_id, world.agent, "Weekly brief").id
    envelope = TerminalEnvelope(execution_id=eid, status=status, response="done",
                                error=None if status == TaskExecutionStatus.SUCCESS else "boom",
                                metadata={"context_window": 200000})
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
        asyncio.run(TaskExecutionService().apply_result(world.agent, envelope))
    return eid


def dispatch(world, eid, monkeypatch):
    """The scheduler hands a run to the backend; the turn itself is not run."""
    from routers import internal

    async def no_turn(*_a, **_k):
        return None

    monkeypatch.setattr("services.cleanup_service.is_startup_recovery_complete", lambda: True)
    monkeypatch.setattr(internal, "_execute_task_internal_background", no_turn)
    monkeypatch.setattr(internal.platform_audit_service, "log", AsyncMock())
    request = InternalTaskExecutionRequest(agent_name=world.agent, message="Weekly brief",
                                           triggered_by="schedule", execution_id=eid,
                                           async_mode=True)

    async def go():
        return await internal.execute_task_internal(request, idempotency_key=None)
    return asyncio.run(go())


def end_as_person(row):
    from services import ask_service
    return ask_service.answer(row, response="acknowledged", response_text=None,
                              actor=ask_service.Actor(email="op@example.com")).rows[0]


# ---- one row per agent x creator --------------------------------------------------------

def test_a_share_holders_schedule_files_one_row_naming_them_and_the_schedule(client, world):
    sid = create(client, world, "mate", name="Morning brief")
    row = the_alert(world)
    assert row["subject"] == f"{KIND}:{world.mate.id}"
    assert row["request_id"].startswith("schedule-restricted-")
    assert row["priority"] == "medium"
    assert "Mate Shared" in row["question"] and "Morning brief" in row["question"]
    ctx = row["context"]
    assert (ctx["cause"], ctx["creator_id"], ctx["schedule_ids"], ctx["schedule_count"]) == (
        "creator_not_owner_tier", world.mate.id, [sid], 1)


def test_two_schedules_by_one_creator_are_one_row_listing_both(client, world):
    first = create(client, world, "mate")
    second = create(client, world, "mate")
    ctx = the_alert(world)["context"]
    assert set(ctx["schedule_ids"]) == {first, second} and ctx["schedule_count"] == 2


def test_two_creators_are_two_rows(client, world):
    create(client, world, "mate")
    create(client, world, "other")
    assert {r["subject"] for r in alerts(world)} == {
        f"{KIND}:{world.mate.id}", f"{KIND}:{world.other.id}"}


def test_fifty_schedules_by_one_creator_are_still_one_bounded_row(client, world):
    for _ in range(50):
        create(client, world, "mate")
    row = the_alert(world)
    assert row["context"]["schedule_count"] == 50
    assert len(row["context"]["schedule_ids"]) == 20
    assert "and 45 more" in row["question"]


def test_three_runs_of_the_schedule_are_one_row(client, world):
    sid = create(client, world, "mate")
    for _ in range(3):
        finish(world, sid)
    assert the_alert(world)["context"]["seen_count"] >= 4      # the create + three runs


@pytest.mark.parametrize("who", ["owner", "admin"])
def test_an_owner_tier_creator_files_nothing(client, world, who):
    sid = create(client, world, who)
    finish(world, sid)
    assert alerts(world) == []


# ---- it clears when the cause goes away ----------------------------------------------------

def test_deleting_the_schedule_ends_the_row_as_the_platform(client, world):
    sid = create(client, world, "mate")
    row = the_alert(world)
    assert call(client, world, "mate", "DELETE", f"/{sid}").status_code == 204
    assert alerts(world) == []
    ended = db.get_operator_queue_item(row["id"])
    assert (ended["status"], ended["disposed_by"]) == ("cancelled", "platform")


def test_deleting_one_of_two_keeps_the_row_for_the_other(client, world):
    first = create(client, world, "mate")
    second = create(client, world, "mate")
    call(client, world, "mate", "DELETE", f"/{first}")
    assert the_alert(world)["context"]["schedule_ids"] == [second]


def test_disabling_ends_the_row_and_enabling_files_it_again(client, world):
    sid = create(client, world, "mate")
    assert call(client, world, "owner", "POST", f"/{sid}/disable").status_code == 200
    assert alerts(world) == []
    assert call(client, world, "owner", "POST", f"/{sid}/enable").status_code == 200
    assert the_alert(world)["context"]["schedule_ids"] == [sid]


def test_an_update_that_disables_ends_the_row(client, world):
    sid = create(client, world, "mate")
    the_alert(world)
    assert call(client, world, "mate", "PUT", f"/{sid}", json={"enabled": False}).status_code == 200
    assert alerts(world) == []


def test_creating_a_disabled_schedule_files_nothing(client, world):
    create(client, world, "mate", enabled=False)
    assert alerts(world) == []


def test_a_creator_promoted_between_runs_is_cleared_by_the_next_run(client, world):
    sid = create(client, world, "mate")
    the_alert(world)
    _set_role(world.mate, "admin")
    finish(world, sid)
    assert alerts(world) == []


def test_a_creator_demoted_after_creation_is_filed_by_the_next_run(client, world):
    _set_role(world.mate, "admin")
    sid = create(client, world, "mate")
    assert alerts(world) == []
    _set_role(world.mate, "operator")
    finish(world, sid, status=TaskExecutionStatus.FAILED)
    assert the_alert(world)["context"]["schedule_ids"] == [sid]


def test_a_run_that_ends_after_a_disable_does_not_file_it_again(client, world):
    sid = create(client, world, "mate")
    eid = db.create_schedule_execution(sid, world.agent, "Weekly brief").id   # running
    call(client, world, "owner", "POST", f"/{sid}/disable")
    assert alerts(world) == []
    finish(world, sid, eid=eid)
    assert alerts(world) == []
    assert '"restricted_turn"' in db.get_execution(eid).turn_integrity   # the run still says so


def test_dispatch_files_the_row_even_when_no_terminal_write_reaches_the_applier(
        client, world, monkeypatch):
    """The watchdog and the scheduler's own failure writers never reach
    `apply_result`, so the dispatch is where a schedule that broke after it was
    created is caught."""
    _set_role(world.mate, "admin")
    sid = create(client, world, "mate")
    _set_role(world.mate, "operator")
    assert alerts(world) == []
    eid = db.create_schedule_execution(sid, world.agent, "Weekly brief").id
    assert dispatch(world, eid, monkeypatch)["status"] == "accepted"
    assert the_alert(world)["context"]["schedule_ids"] == [sid]


def test_a_schedule_unreadable_mid_run_files_no_row(client, world):
    sid = create(client, world, "owner")
    eid = db.create_schedule_execution(sid, world.agent, "Weekly brief").id
    call(client, world, "owner", "DELETE", f"/{sid}")
    finish(world, sid, eid=eid)
    assert alerts(world) == []


# ---- the admin pause, resume and emergency stop (review R5) -----------------------------------

def _ops(client, world, path, **params):
    return client.post(f"/api/ops/{path}", params=params,
                       headers={"Authorization": f"Bearer {world.admin.jwt}"})


def test_the_admin_pause_ends_the_row_and_resume_files_it_again(client, world):
    sid = create(client, world, "mate")
    assert _ops(client, world, "schedules/pause", agent_name=world.agent).status_code == 200
    assert alerts(world) == []
    assert _ops(client, world, "schedules/resume", agent_name=world.agent).status_code == 200
    assert the_alert(world)["context"]["schedule_ids"] == [sid]


def test_the_emergency_stop_ends_the_row(client, world, monkeypatch):
    from services import fleet_ops_service
    monkeypatch.setattr(fleet_ops_service, "list_all_agents_fast", lambda: [])
    create(client, world, "mate")
    the_alert(world)
    assert _ops(client, world, "emergency-stop", system_prefix=world.agent).status_code == 200
    assert alerts(world) == []


# ---- a person's ending ------------------------------------------------------------------------

def _ended_long_ago(row):
    """Past the 7-day snooze window, so only a FINAL ending still silences."""
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == row["id"])
                     .values(disposed_at="2026-01-01T00:00:00.000000Z"))


def test_a_persons_ending_is_final_for_that_creator_and_cause(client, world):
    create(client, world, "mate")
    row = the_alert(world)
    end_as_person(row)
    _ended_long_ago(row)
    create(client, world, "mate")
    assert alerts(world) == []


def test_a_new_cause_files_again_after_a_persons_ending(client, world):
    sid = create(client, world, "mate")
    row = the_alert(world)
    end_as_person(row)
    _ended_long_ago(row)
    with get_engine().begin() as conn:          # the owner's user row goes: a new cause
        conn.execute(delete(users).where(users.c.id == world.owner.id))
    finish(world, sid)
    assert the_alert(world)["context"]["cause"] == "agent_has_no_owner"


# ---- one writer per reading ---------------------------------------------------------------------

def test_concurrent_readings_of_one_creator_are_one_row(client, world):
    from services import schedule_tier_alerts
    sid = create(client, world, "mate", enabled=False)
    db.set_schedule_enabled(sid, True)
    barrier = threading.Barrier(4)

    def read():
        barrier.wait()
        schedule_tier_alerts.sync(world.agent, world.mate.id)
    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(alerts(world)) == 1


# ---- who sees it (decision D5) --------------------------------------------------------------------

def _queue(client, who_jwt, world):
    resp = client.get("/api/operator-queue", params={"agent_name": world.agent, "status": "pending"},
                      headers={"Authorization": f"Bearer {who_jwt}"})
    assert resp.status_code == 200, resp.text
    return [r for r in resp.json()["items"] if (r.get("subject") or "").startswith(f"{KIND}:")]


def test_the_owner_tier_sees_the_row_and_a_share_holder_does_not(client, world):
    create(client, world, "mate")
    assert len(_queue(client, world.owner.jwt, world)) == 1
    assert len(_queue(client, world.admin.jwt, world)) == 1
    assert _queue(client, world.other.jwt, world) == []
    assert _queue(client, world.mate.jwt, world) == []


def test_the_agents_own_key_does_not_read_the_row(client, world):
    create(client, world, "mate")
    assert len(_queue(client, world.owner.jwt, world)) == 1
    key = db.create_agent_mcp_api_key(world.agent, world.owner.username).api_key
    assert _queue(client, key, world) == []


# ---- the registration ------------------------------------------------------------------------------

def test_the_kind_is_registered_reserved_and_about_a_person():
    from services import operator_queue_service as oqs
    from services import platform_alerts as pa
    kind = pa.KINDS[KIND]
    assert (kind.prefix, kind.lifetime, kind.clears) == ("schedule-restricted-", pa.LIFETIME_NET,
                                                         pa.CLEARS_HOOK)
    assert kind.material_keys == ("cause",) and kind.person_ending_is_final
    assert kind.budgeted_type is None        # bounded by the share list, not by a cap
    assert oqs.is_platform_minted({"request_id": "schedule-restricted-7-x"})
    assert "schedule-restricted-" in oqs.OWNER_ONLY_ID_PREFIXES
    assert "schedule-restricted-" in oqs.ABOUT_A_PERSON_ID_PREFIXES

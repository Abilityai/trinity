"""#1968 — a manual trigger must name the execution it started, and admit when
it started nothing.

`_trigger_handler` was fire-and-forget: it spawned the run with
`asyncio.create_task` and responded immediately, *before* the execution record
existed. So it had no id to return, the backend relayed the same id-less five
fields, and the MCP tool interpolated the missing key — telling every agent
`Execution started with ID 'undefined'` on every single trigger. A caller could
not correlate its trigger with a run, poll it, or fetch its result.

The same ordering hid a second thing. The response was emitted before
`_execute_manual_trigger` had even attempted the distributed lock, so a trigger
suppressed because the schedule was already running still answered
`"status": "triggered"`. A suppressed trigger and a real one were byte-identical
to the caller; the only trace was a scheduler-side WARNING.

The fix acquires the lock and creates the row synchronously in the handler, then
hands both to the background task. That makes two facts sayable that previously
did not exist yet at response time: which execution this is, and whether one was
started at all (409 `already_running`).

What is pinned here, beyond the happy path:
  * exactly ONE execution row per trigger — the handler creating one and
    `_execute_schedule_with_lock` creating another would give the caller an id
    naming a row that never runs while a second row does the work;
  * the lock is released exactly once on every path, including the failure ones
    — the handler now holds it across a DB write, which is new;
  * a pre-created row is never left `running` when a gate aborts the run (canary
    E-01 flags exactly that shape);
  * the MCP tool answers 409 with a structured `already_running` rather than
    throwing, and never claims an id it did not receive.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The `src.scheduler` namespace import resolves only with the repo root on
# sys.path — true for a repo-root `pytest` run but NOT in CI, whose rootdir is
# `tests/`. Appended (never inserted at 0) so the repo root cannot shadow the
# conftest-managed `src/backend` entries. Mirrors test_1808.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _main_module():
    import src.scheduler.main as scheduler_main

    return scheduler_main


def _service_module():
    import src.scheduler.service as scheduler_service

    return scheduler_service


def _database_module():
    import src.scheduler.database as scheduler_database

    return scheduler_database


# ---------------------------------------------------------------------------
# Fakes: the handler is aiohttp-shaped, so drive it with the minimum surface.
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, schedule_id: str, body: dict | None = None):
        self.match_info = {"schedule_id": schedule_id}
        self._body = body
        self.content_type = "application/json" if body is not None else "text/plain"
        self.content_length = len(json.dumps(body)) if body is not None else 0

    async def json(self):
        return self._body


class _FakeLock:
    def __init__(self):
        self.release_count = 0

    def release(self):
        self.release_count += 1


class _FakeLockManager:
    def __init__(self, *, grant: bool):
        self.lock = _FakeLock() if grant else None
        self.acquire_count = 0

    def try_acquire_schedule_lock(self, schedule_id):
        self.acquire_count += 1
        return self.lock


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY, agent_name TEXT, name TEXT, cron_expression TEXT,
            message TEXT, enabled INTEGER, timezone TEXT, description TEXT,
            owner_id INTEGER, created_at TEXT, updated_at TEXT,
            last_run_at TEXT, next_run_at TEXT, model TEXT, deleted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY, schedule_id TEXT, agent_name TEXT, status TEXT,
            started_at TEXT, completed_at TEXT, duration_ms INTEGER, message TEXT,
            response TEXT, error TEXT, triggered_by TEXT, model_used TEXT,
            attempt_number INTEGER DEFAULT 1, retry_of_execution_id TEXT,
            source_user_id INTEGER, source_user_email TEXT, source_agent_name TEXT,
            source_mcp_key_id TEXT, source_mcp_key_name TEXT,
            -- Written by update_execution_status. Omitting them made the
            -- abandon path raise OperationalError, which its fail-safe
            -- swallowed, so the row stayed `running` and the test "found" a bug
            -- that was the fixture's. Keep this column set in step with the
            -- UPDATE in db.update_execution_status.
            context_used INTEGER, context_max INTEGER, cost REAL,
            tool_calls TEXT, execution_log TEXT, claude_session_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_schedules VALUES ('sch-1','a1','nightly','0 3 * * *',"
        "'do the thing',1,'UTC',NULL,1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
        "NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()


def _executions(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM schedule_executions")]
    conn.close()
    return rows


def _app(db_path: Path, *, grant_lock: bool = True, ran: list | None = None):
    """A SchedulerApp whose service is real but whose lock manager is a fake,
    with `_execute_schedule_with_lock` stubbed so no agent is contacted."""
    app = _main_module().SchedulerApp()
    service = _service_module().SchedulerService(
        database=_database_module().SchedulerDatabase(str(db_path)),
        lock_manager=_FakeLockManager(grant=grant_lock),
        redis_url="redis://test:test@redis:6379",
    )

    async def _stub(schedule_id, triggered_by="schedule", origin=None, execution=None):
        if ran is not None:
            ran.append(
                {
                    "schedule_id": schedule_id,
                    "triggered_by": triggered_by,
                    "origin": origin,
                    "execution": execution,
                }
            )

    service._execute_schedule_with_lock = _stub
    app.scheduler_service = service
    return app


async def _trigger(app, schedule_id="sch-1", body=None):
    """Call the handler and let the spawned background task finish."""
    response = await app._trigger_handler(_FakeRequest(schedule_id, body))
    # The handler fires the run via create_task; give it a turn to complete so
    # lock-release assertions see the final state.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return response


def _payload(response) -> dict:
    return json.loads(response.body.decode())


# ---------------------------------------------------------------------------
# The headline defect.
# ---------------------------------------------------------------------------


def test_trigger_returns_a_real_execution_id(tmp_path):
    """The bug, directly: the response had no id because it was sent before the
    row existed."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    response = asyncio.run(_trigger(app))
    body = _payload(response)

    assert response.status == 200
    assert body["status"] == "triggered"
    assert body.get("execution_id"), "the response still carries no execution_id"

    rows = _executions(db_path)
    assert len(rows) == 1
    assert body["execution_id"] == rows[0]["id"], (
        "the returned id does not name the row that was created"
    )


def test_the_returned_id_is_usable_before_the_run_finishes(tmp_path):
    """The id must be valid at RESPONSE time, not eventually.

    The whole point is that a caller can poll it. If the row only appeared once
    the background task got around to it, the id would name nothing for a
    while, and a fast poller would 404 — a subtler version of the same bug.
    """
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    async def _drive():
        # Deliberately NOT awaiting the background task before looking.
        response = await app._trigger_handler(_FakeRequest("sch-1"))
        return _payload(response), _executions(db_path)

    body, rows = asyncio.run(_drive())

    assert len(rows) == 1, "the row did not exist by the time the caller was answered"
    assert rows[0]["id"] == body["execution_id"]
    assert rows[0]["status"] == "running"
    assert rows[0]["triggered_by"] == "manual"


def test_exactly_one_execution_row_per_trigger(tmp_path):
    """Handler-creates + service-creates would be two rows for one trigger, and
    the caller's id would name the one that never runs."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    ran = []
    app = _app(db_path, ran=ran)

    asyncio.run(_trigger(app))

    assert len(_executions(db_path)) == 1, "one trigger produced more than one row"
    # And the run was handed the row rather than left to make its own.
    assert ran and ran[0]["execution"] is not None


def test_service_does_not_recreate_a_passed_execution(tmp_path):
    """The other half of the same invariant, at the service boundary."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    db = _database_module().SchedulerDatabase(str(db_path))
    service = _service_module().SchedulerService(
        database=db,
        lock_manager=_FakeLockManager(grant=True),
        redis_url="redis://test:test@redis:6379",
    )

    pre = db.create_execution(
        schedule_id="sch-1", agent_name="a1", message="m", triggered_by="manual"
    )

    dispatched = []

    async def _no_dispatch(schedule, execution, message, triggered_by):
        dispatched.append(execution.id)

    service._dispatch_and_record_outcome = _no_dispatch
    service._publish_event = lambda *a, **k: asyncio.sleep(0)

    asyncio.run(
        service._execute_schedule_with_lock(
            "sch-1", triggered_by="manual", execution=pre
        )
    )

    rows = _executions(db_path)
    assert len(rows) == 1, "the service created a second row for a pre-created run"
    assert dispatched == [pre.id], "the pre-created row was not the one dispatched"


# ---------------------------------------------------------------------------
# Honest suppression.
# ---------------------------------------------------------------------------


def test_lock_denied_returns_409_not_a_false_triggered(tmp_path):
    """A suppressed trigger used to be byte-identical to a real one."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path, grant_lock=False)

    response = asyncio.run(_trigger(app))
    body = _payload(response)

    assert response.status == 409
    assert body["status"] == "already_running"
    assert "execution_id" not in body, (
        "a suppressed trigger must not hand back an id — nothing was started"
    )


def test_lock_denied_creates_no_execution_row(tmp_path):
    """Nothing ran, so nothing may be recorded as running. (Auditing the
    *cron*-side suppression is #1969's job and uses a `skipped` row; this path
    simply must not invent a `running` one.)"""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path, grant_lock=False)

    asyncio.run(_trigger(app))

    assert _executions(db_path) == []


def test_lock_denied_does_not_run_the_schedule(tmp_path):
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    ran = []
    app = _app(db_path, grant_lock=False, ran=ran)

    asyncio.run(_trigger(app))

    assert ran == []


# ---------------------------------------------------------------------------
# Lock hygiene — the handler now holds a lock across a DB write.
# ---------------------------------------------------------------------------


def test_lock_released_exactly_once_on_success(tmp_path):
    """Released twice, a lock re-acquired by the next run in between would be
    freed out from under it."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    asyncio.run(_trigger(app))

    assert app.scheduler_service.lock_manager.lock.release_count == 1


def test_lock_released_when_the_run_raises(tmp_path):
    """A failing run must not strand the lock until its Redis TTL."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    async def _boom(*args, **kwargs):
        raise RuntimeError("agent exploded")

    app.scheduler_service._execute_schedule_with_lock = _boom

    asyncio.run(_trigger(app))

    assert app.scheduler_service.lock_manager.lock.release_count == 1


def test_lock_released_when_the_row_cannot_be_created(tmp_path):
    """The new failure window: the lock is held across `create_execution`. If
    that write fails we must release before returning, or the schedule is
    wedged until the TTL for a run that never started."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    def _fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    app.scheduler_service.db.create_execution = _fail

    response = asyncio.run(_trigger(app))

    assert response.status == 500
    assert app.scheduler_service.lock_manager.lock.release_count == 1


def test_lock_released_when_row_creation_returns_none(tmp_path):
    """Same window, non-raising variant."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)
    app.scheduler_service.db.create_execution = lambda *a, **k: None

    response = asyncio.run(_trigger(app))

    assert response.status == 500
    assert app.scheduler_service.lock_manager.lock.release_count == 1


def test_the_spawned_task_is_strongly_referenced(tmp_path):
    """The event loop keeps only a WEAK reference to a task, so a bare
    `create_task(...)` nobody holds can be collected mid-flight (asyncio says
    so outright).

    That was survivable before this change — a dropped task meant the run
    silently didn't happen. It is not survivable now: the lock and the row are
    created BEFORE the task, so a collected task strands a `running` execution
    whose id the caller already holds and pins the lock until its TTL. Same
    `_inflight` shape as the #1083 result-callback path.
    """
    import asyncio

    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    seen = {}

    async def _drive():
        # Look BEFORE the task has run — that is the window where a weakly
        # referenced task can be collected.
        await app._trigger_handler(_FakeRequest("sch-1"))
        seen["held"] = len(app._inflight_triggers)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        seen["after"] = len(app._inflight_triggers)

    asyncio.run(_drive())

    assert seen["held"] == 1, "the spawned trigger task is not strongly referenced"
    assert seen["after"] == 0, (
        "the done-callback does not discard the task — the set grows without "
        "bound for the life of the process"
    )


def test_no_lock_acquired_for_an_unknown_schedule(tmp_path):
    """The 404 gate must stay ahead of the lock — locking a schedule that does
    not exist would block nothing and leak a key."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    response = asyncio.run(_trigger(app, schedule_id="nope"))

    assert response.status == 404
    assert app.scheduler_service.lock_manager.acquire_count == 0


# ---------------------------------------------------------------------------
# A pre-created row must never be stranded `running`.
# ---------------------------------------------------------------------------


def test_gate_abort_fails_the_precreated_row(tmp_path):
    """The schedule is deleted between the response and the task starting.

    The row already exists and its id is already in the caller's hands. Left
    `running` it never terminates — canary E-01's exact signature, and a task
    the UI shows forever.
    """
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    db = _database_module().SchedulerDatabase(str(db_path))
    service = _service_module().SchedulerService(
        database=db,
        lock_manager=_FakeLockManager(grant=True),
        redis_url="redis://test:test@redis:6379",
    )

    pre = db.create_execution(
        schedule_id="sch-1", agent_name="a1", message="m", triggered_by="manual"
    )

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM agent_schedules WHERE id = 'sch-1'")
    conn.commit()
    conn.close()

    asyncio.run(
        service._execute_schedule_with_lock(
            "sch-1", triggered_by="manual", execution=pre
        )
    )

    rows = _executions(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed", (
        "a pre-created row was left running after the run was abandoned (#1968)"
    )
    assert rows[0]["error"]


def test_abandon_helper_never_raises(tmp_path):
    """It runs on abort paths. A raise there would replace a clean abandon with
    an exception in a background task nobody awaits."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    service = _service_module().SchedulerService(
        database=_database_module().SchedulerDatabase(str(db_path)),
        lock_manager=_FakeLockManager(grant=True),
        redis_url="redis://test:test@redis:6379",
    )

    def _fail(*args, **kwargs):
        raise sqlite3.OperationalError("gone")

    service.db.update_execution_status = _fail

    class _Row:
        id = "exec-x"

    service._abandon_precreated_execution(_Row(), "reason")  # must not raise
    service._abandon_precreated_execution(None, "reason")  # None is a no-op


# ---------------------------------------------------------------------------
# #1970 interaction: attribution must survive the move.
# ---------------------------------------------------------------------------


def test_precreated_row_still_carries_the_caller_identity(tmp_path):
    """`create_execution` moved from the service into the handler. The origin
    columns must move with it, or #1970 silently regresses to all-NULL."""
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    asyncio.run(
        _trigger(
            app,
            body={
                "triggered_by": "manual",
                "source_user_id": 7,
                "source_user_email": "operator@example.com",
                "source_mcp_key_id": "key-abc",
                "source_mcp_key_name": "ops laptop",
            },
        )
    )

    row = _executions(db_path)[0]
    assert row["source_user_id"] == 7
    assert row["source_user_email"] == "operator@example.com"
    assert row["source_mcp_key_id"] == "key-abc"
    assert row["source_mcp_key_name"] == "ops laptop"


def test_webhook_trigger_still_records_its_trigger_type(tmp_path):
    db_path = tmp_path / "t.db"
    _seed_db(db_path)
    app = _app(db_path)

    response = asyncio.run(_trigger(app, body={"triggered_by": "webhook"}))

    assert _payload(response)["triggered_by"] == "webhook"
    assert _executions(db_path)[0]["triggered_by"] == "webhook"


# ---------------------------------------------------------------------------
# Backend + MCP relay.
# ---------------------------------------------------------------------------


def _backend_trigger_endpoint() -> str:
    """The FULL source of `trigger_schedule`, via AST.

    This used to take a fixed 5000-character window from the `async def` marker.
    That is a byte count standing in for a function boundary, and it broke the
    moment the handler grew: #2094 added a four-line comment explaining its
    dependency choice, which pushed the `execution_id` relay from 4835 to 5071
    characters — 71 past the cliff — and every assertion below started failing
    against code that was completely correct.

    A test that fails when a COMMENT is added is measuring the wrong thing. The
    property is "this handler relays execution_id", so read the handler.
    """
    import ast

    path = _REPO / "src" / "backend" / "routers" / "schedules.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "trigger_schedule":
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("trigger_schedule not found in routers/schedules.py")


def test_backend_relays_the_execution_id():
    """The backend passed through five fields and dropped the one the MCP tool
    reads."""
    endpoint = _backend_trigger_endpoint()
    assert re.search(r'"execution_id":\s*result\.get\("execution_id"\)', endpoint), (
        "the backend does not relay execution_id to its caller (#1968)"
    )


def test_backend_maps_409_instead_of_flattening_it_to_500():
    """Without this the honest 409 reaches the caller as 'Failed to trigger
    schedule' — a worse lie than the one being fixed, since it claims failure
    where the schedule is healthily busy."""
    endpoint = _backend_trigger_endpoint()
    assert "409" in endpoint
    assert "HTTP_409_CONFLICT" in endpoint


def test_mcp_tool_handles_409_without_throwing():
    """An agent should get a decision it can act on, not an exception string."""
    tool = (
        _REPO / "src" / "mcp-server" / "src" / "tools" / "schedules.ts"
    ).read_text(encoding="utf-8")
    trigger = tool[tool.index("trigger_agent_schedule"):]
    assert "error.status === 409" in trigger
    assert "already_running" in trigger


def test_mcp_tool_does_not_claim_an_id_it_did_not_get():
    """An older backend still omits the field. Interpolating it anyway is
    literally the reported bug — do not swap one confident lie for another."""
    tool = (
        _REPO / "src" / "mcp-server" / "src" / "tools" / "schedules.ts"
    ).read_text(encoding="utf-8")
    trigger = tool[tool.index("trigger_agent_schedule"):]
    assert "!result.execution_id" in trigger, (
        "the MCP tool interpolates execution_id unguarded — an old backend "
        "still yields 'undefined' (#1968)"
    )


def test_trigger_result_type_admits_the_field_can_be_absent():
    """`execution_id: string` (required) is why the compiler never flagged the
    `undefined` in the first place — the type asserted a guarantee the wire
    never made."""
    types = (_REPO / "src" / "mcp-server" / "src" / "types.ts").read_text(
        encoding="utf-8"
    )
    block = types[types.index("export interface ScheduleTriggerResult"):][:600]
    assert "execution_id?" in block, (
        "ScheduleTriggerResult still declares execution_id as always present"
    )


def test_ui_distinguishes_already_running_from_a_failure():
    """The 409 is not the 'nothing was changed — try again' case: a run IS in
    flight and retrying hits the same lock."""
    panel = (
        _REPO / "src" / "frontend" / "src" / "components" / "SchedulesPanel.vue"
    ).read_text(encoding="utf-8")
    trigger = panel[panel.index("async function triggerSchedule("):][:1600]
    assert "409" in trigger
    assert "already running" in trigger.lower()

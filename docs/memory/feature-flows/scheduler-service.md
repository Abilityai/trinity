# Feature: Dedicated Scheduler Service

> **Status**: Implemented
> **Created**: 2026-01-13
> **Priority**: HIGH
> **Requirement**: Fixes duplicate execution bug in multi-worker deployments

---

## Overview

The Dedicated Scheduler Service is a standalone Python service that executes scheduled agent tasks. It replaces the previous embedded scheduler in the backend to fix the duplicate execution bug that occurred when running multiple uvicorn workers.

**Key Features**:
- **Single-instance design** - Only one scheduler runs, preventing duplicates
- **Distributed locking** - Redis locks ensure exactly-once execution
- **Independent deployment** - Can be scaled/monitored separately from API workers
- **Event publishing** - Redis pub/sub for WebSocket compatibility
- **Health endpoints** - Kubernetes-ready health checks
- **Process schedules** - Cron-triggered process execution alongside agent schedules

---

## User Story

As a **platform administrator**, I want **scheduled tasks to execute exactly once** so that **agents do not receive duplicate commands and billing is accurate**.

---

## Architecture

```
+------------------------------------------------------------------+
|                        Trinity Platform                            |
+------------------------------------------------------------------+
|                                                                    |
|  +------------------+    +------------------+    +--------------+  |
|  |     Backend      |    |    Scheduler     |    |    Agent     |  |
|  |  (API + Task     |    |   (singleton)    |    |  Containers  |  |
|  |   Execution)     |    |   1 replica      |    |              |  |
|  +--------+---------+    +--------+---------+    +------+-------+  |
|           |                       |                     ^          |
|           |   CRUD operations     | POST /api/internal/ |          |
|           |                       | execute-task        |          |
|           v                       v                     |          |
|  +------------------+    +------------------+           |          |
|  |     SQLite       |    |      Redis       |    Backend calls     |
|  |  - Schedules     |<---|  - Locks         |    agent /api/task   |
|  |  - Executions    |    |  - Events        |    via TaskExec-     |
|  +------------------+    |  - Heartbeats    |    utionService      |
|                          +------------------+                      |
+------------------------------------------------------------------+
```

**Data Flow**:
1. Backend writes schedule CRUD to SQLite (`agent_schedules` / `process_schedules` tables)
2. Scheduler reads schedules from SQLite on startup
3. APScheduler triggers jobs at cron times
4. Scheduler acquires Redis lock before execution
5. For **agent schedules**: Scheduler calls backend's `POST /api/internal/execute-task` which routes through `TaskExecutionService` for slot management, activity tracking, agent HTTP call, and credential sanitization
6. For **process schedules**: Scheduler calls backend's `POST /api/processes/{id}/execute` to start a process execution
7. Scheduler publishes events to Redis for WebSocket relay

---

## Entry Points

- **Service Startup**: `python -m scheduler.main`
- **Health Check**: `GET http://localhost:8001/health`
- **Status Endpoint**: `GET http://localhost:8001/status`
- **Manual Trigger**: `POST http://localhost:8001/api/schedules/{schedule_id}/trigger`
- **Docker**: `docker-compose up scheduler`

---

## Source Files

| Category | File | Purpose |
|----------|------|---------|
| Entry | `src/scheduler/main.py` | SchedulerApp, signal handlers, health server, manual trigger endpoint |
| Core | `src/scheduler/service.py` | SchedulerService with APScheduler integration |
| Config | `src/scheduler/config.py` | Environment-based configuration |
| Models | `src/scheduler/models.py` | Schedule, ScheduleExecution, ProcessSchedule, ProcessScheduleExecution |
| Database | `src/scheduler/database.py` | SQLite read/write operations |
| HTTP | `src/scheduler/agent_client.py` | Agent container communication (legacy, unused in main path) |
| Locking | `src/scheduler/locking.py` | Redis distributed locks |
| Docker | `docker/scheduler/Dockerfile` | Container definition |
| Docker | `docker/scheduler/requirements.txt` | Python dependencies |
| Docker | `docker/scheduler/docker-compose.test.yml` | Standalone testing |
| Tests | `tests/scheduler_tests/*.py` | Unit and integration tests |

---

## Configuration

**Location**: `src/scheduler/config.py:13-94`

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_PATH` | `/data/trinity.db` | SQLite database path |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `LOCK_TIMEOUT` | `600` | Lock expiration in seconds (10 min) |
| `LOCK_AUTO_RENEWAL` | `true` | Auto-renew locks during execution |
| `HEALTH_PORT` | `8001` | Health check server port |
| `HEALTH_HOST` | `0.0.0.0` | Health check server bind address |
| `DEFAULT_TIMEZONE` | `UTC` | Default timezone for schedules |
| `SCHEDULE_RELOAD_INTERVAL` | `60` | Seconds between schedule sync checks |
| `AGENT_TIMEOUT` | `900` | Default agent request timeout (15 min) |
| `POLL_INTERVAL` | `10` | Seconds between DB polls for async task completion (SCHED-ASYNC-001) |
| `DISPATCH_TIMEOUT` | `30` | HTTP deadline for the scheduler→backend `POST /api/internal/execute-task` round-trip (dispatch only; the async endpoint returns ~instantly). Reaching it means the backend did not respond — outcome is **UNKNOWN**, not "rejected" (#1022) |
| `PRE_CHECK_TIMEOUT` | `70` | HTTP deadline for the scheduler→backend pre-check call (agent-side hook is 60s; 10s headroom). Fail-open (#1022) |
| `MISFIRE_GRACE_TIME` | `3600` | Seconds after a missed trigger that APScheduler will still execute (Issue #145) |
| `BACKEND_URL` | `http://backend:8000` | Backend API URL for process executions and task delegation |
| `INTERNAL_API_SECRET` | _(empty)_ | Shared secret for backend internal API auth (C-003) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `PUBLISH_EVENTS` | `true` | Enable Redis event publishing |

---

## Flow 1: Service Startup

**Trigger**: Container starts or `python -m scheduler.main`

```
main.py:265                     main.py:51                      service.py:80
asyncio.run(main())   -->       SchedulerApp.start()   -->      SchedulerService.initialize()
                                |                               |
                                v                               v
                                Start health server             Ensure process_schedules table
                                (aiohttp on :8001)              |
                                |                               v
                                v                               Load enabled agent schedules
                                main.py:68                      Detect missed schedules (Issue #145)
                                initialize()                    |
                                |                               v
                                v                               Load enabled process schedules
                                main.py:71                      |
                                fire_missed_schedules()         v
                                |                               Add CronTrigger jobs
                                v                               Add EVENT_JOB_MAX_INSTANCES listener
                                Run heartbeat/sync loop         |
                                (30s heartbeat, 60s sync)       v
                                                                scheduler.start()
```

**Key Code** (`service.py:80-141`):
```python
def initialize(self):
    """Initialize the scheduler and load all enabled schedules."""
    if self._initialized:
        return

    # Ensure process schedules table exists
    self.db.ensure_process_schedules_table()

    # Create scheduler with memory job store
    jobstores = {'default': MemoryJobStore()}
    self.scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=pytz.UTC)

    # Load all enabled agent schedules from database
    schedules = self.db.list_all_enabled_schedules()

    # Detect missed schedules BEFORE _add_job overwrites next_run_at (Issue #145)
    self._missed_schedules = self._get_missed_schedules(schedules)

    for schedule in schedules:
        self._add_job(schedule)
        self._schedule_snapshot[schedule.id] = (schedule.enabled, ...)

    # Load all enabled process schedules from database
    process_schedules = self.db.list_all_enabled_process_schedules()
    for process_schedule in process_schedules:
        self._add_process_job(process_schedule)
        self._process_schedule_snapshot[process_schedule.id] = (...)

    # Add listener for skipped executions (max_instances reached)
    self.scheduler.add_listener(self._on_job_max_instances, EVENT_JOB_MAX_INSTANCES)

    self.scheduler.start()
    self._initialized = True
    self._start_time = datetime.utcnow()
```

**Missed Schedule Recovery** (`service.py:142-194`, `service.py:209-222`):
On startup, `_get_missed_schedules()` detects schedules whose `next_run_at` is in the past but within the `misfire_grace_time` window. After initialization, `fire_missed_schedules()` fires catch-up executions for each.

---

## Flow 2: Agent Schedule Execution (Cron Trigger)

**Trigger**: APScheduler CronTrigger fires at scheduled time

```
APScheduler                     service.py:597                  locking.py:221
CronTrigger fires   -->         _execute_schedule()   -->       try_acquire_schedule_lock()
                                |                               |
                                v                               v (if acquired)
                                Check lock                      DistributedLock.acquire()
                                |                               Redis SET NX EX
                                |                               |
                                v (if locked)                   v
                                Skip execution                  _execute_schedule_with_lock()
                                Log "already running"           |
                                                                v
                                                                database.py:196
                                                                create_execution()
                                                                INSERT schedule_executions
                                                                |
                                                                v
                                                                Publish "started" event
                                                                |
                                                                v
                                                                _call_backend_execute_task()
                                                                POST /api/internal/execute-task
                                                                (backend TaskExecutionService)
                                                                |
                                                                v
                                                                If async accepted:
                                                                  spawn _poll_and_finalize() task
                                                                  return "dispatched" immediately
                                                                  (Issue #132: fire-and-forget)
                                                                |
                                                                v
                                                                Update last_run_at immediately
                                                                (for missed-schedule detection)
                                                                |
                                                                v
                                                                lock.release()
                                                                Job function returns
                                                                |
                                                                v (background)
                                                                _poll_and_finalize() polls DB
                                                                Publish "completed" event
```

**Lock Acquisition** (`service.py:597-613`):
```python
async def _execute_schedule(self, schedule_id: str):
    """
    Execute a scheduled task.
    This is called by APScheduler when a schedule is due.
    Uses distributed locking to prevent duplicate executions.
    """
    lock = self.lock_manager.try_acquire_schedule_lock(schedule_id)
    if not lock:
        logger.info(f"Schedule {schedule_id} already being executed by another instance")
        return

    try:
        await self._execute_schedule_with_lock(schedule_id)
    finally:
        lock.release()
```

**Execution Logic** (`service.py:615-758`):
```python
async def _execute_schedule_with_lock(self, schedule_id: str, triggered_by: str = "schedule"):
    """Execute schedule after acquiring lock."""
    schedule = self.db.get_schedule(schedule_id)
    if not schedule:
        return

    if not schedule.enabled and triggered_by == "schedule":
        return

    # Check if agent has autonomy enabled (only for cron-triggered)
    if triggered_by == "schedule" and not self.db.get_autonomy_enabled(schedule.agent_name):
        return

    # Create execution record
    execution = self.db.create_execution(...)

    # Broadcast execution started
    await self._publish_event({...})

    try:
        # Delegate to backend's TaskExecutionService
        result = await self._call_backend_execute_task(...)

        status = result.get("status", ExecutionStatus.FAILED)
        if status == ExecutionStatus.SUCCESS:
            # Update schedule last run time
            ...
        else:
            # Log error, detect auth failures
            ...

    except Exception as e:
        # SCHED-ASYNC-001: Check DB before overwriting — if backend already
        # finalized, preserve status instead of marking as 'failed'
        current = self.db.get_execution(execution.id)
        if current and current.status != ExecutionStatus.RUNNING:
            pass  # Don't overwrite
        else:
            self.db.update_execution_status(execution_id=execution.id, status=ExecutionStatus.FAILED, ...)
```

**Backend Task Delegation** (`_call_backend_execute_task`):

Uses async fire-and-forget dispatch (SCHED-ASYNC-001):
1. `POST /api/internal/execute-task` with `async_mode=True` and the configured dispatch deadline (`config.dispatch_timeout`, env `DISPATCH_TIMEOUT`, default 30s — was a literal before #1022)
2. If backend accepts (`{"status": "accepted", "async_mode": true}`), poll DB
3. Backward compatible: if backend returns sync result, use directly

A dispatch `httpx.TimeoutException` is re-raised as a **named, non-blank** error (`"dispatch to /api/internal/execute-task timed out after {N}s ({TimeoutType}) — outcome unknown"`) *before* it can reach the generic `except` handler. The outcome is genuinely unknown: the backend spawns the background task before replying (`internal.py`), so a dispatch timeout may mean the task is already running and will surface as an orphan recovered by the cleanup service. This closes the #1022 silent-failure — bare `httpx` timeouts stringify to `''`, which previously persisted as a blank `error`. Defense-in-depth: `_describe_exception()` normalizes any other blank-stringifying exception (falls back to the type name) across every execution, retry, and process-schedule error path.

**DB Polling** (`service.py:835-887`):
```python
async def _poll_execution_completion(self, execution_id, timeout_seconds):
    """Poll DB every config.poll_interval seconds until status != 'running'."""
    deadline = time.monotonic() + float(timeout_seconds) + 60
    while time.monotonic() < deadline:
        await asyncio.sleep(config.poll_interval)
        execution = self.db.get_execution(execution_id)
        if execution and execution.status != ExecutionStatus.RUNNING:
            return {
                "execution_id": execution.id,
                "status": execution.status,
                "response": execution.response,
                ...
            }
    raise Exception(f"Polling deadline exceeded for execution {execution_id}")
```

---

## Flow 3: Process Schedule Execution

**Trigger**: APScheduler CronTrigger fires for a process schedule

```
APScheduler                     service.py:987                  locking.py:221
CronTrigger fires   -->         _execute_process_schedule() --> try_acquire_schedule_lock()
                                |                               key: "process_{schedule_id}"
                                v (if acquired)                 |
                                _execute_process_schedule_with_lock()
                                service.py:1005                 |
                                |                               v
                                v                               database.py:602
                                Get process schedule            create_process_schedule_execution()
                                from DB                         INSERT process_schedule_executions
                                |                               |
                                v                               v
                                Publish "started" event         POST /api/processes/{id}/execute
                                |                               (backend process execution API)
                                v                               |
                                On success:                     v
                                - Update execution status       Update process schedule run times
                                - Publish "completed" event     |
                                                                v
                                On failure:                     lock.release()
                                - Update execution as failed
                                - Publish "completed" with error
```

**Process Execution** (`service.py:987-1145`):
```python
async def _execute_process_schedule(self, schedule_id: str):
    """Execute a scheduled process. Uses distributed locking."""
    lock = self.lock_manager.try_acquire_schedule_lock(f"process_{schedule_id}")
    if not lock:
        return
    try:
        await self._execute_process_schedule_with_lock(schedule_id)
    finally:
        lock.release()

async def _execute_process_schedule_with_lock(self, schedule_id: str):
    """Execute process schedule after acquiring lock."""
    schedule = self.db.get_process_schedule(schedule_id)
    # ... validation ...

    execution = self.db.create_process_schedule_execution(...)
    await self._publish_event({"type": "process_schedule_execution_started", ...})

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{config.backend_url}/api/processes/{schedule.process_id}/execute",
            json={"triggered_by": "schedule", "input_data": {"trigger": {...}}},
            timeout=60.0
        )
        # Handle success/failure, update execution status, publish events
```

**Key Differences from Agent Schedules**:
- Uses `process_schedules` / `process_schedule_executions` tables (separate from agent schedules)
- Calls `POST /api/processes/{id}/execute` instead of `/api/internal/execute-task`
- No autonomy check (process-level, not agent-level)
- No async polling -- uses synchronous HTTP call with 60s timeout
- Lock key prefixed with `process_` to avoid collision with agent schedule locks

**Process Schedule Job Management** (`service.py:934-985`):
- `_get_process_job_id()` (line 934): Returns `process_schedule_{schedule_id}`
- `_add_process_job()` (line 938): Adds CronTrigger job for process schedule
- `_remove_process_job()` (line 975): Removes process schedule job from APScheduler

---

## Flow 4: Distributed Locking

**Purpose**: Prevent duplicate executions across multiple scheduler instances or restarts

**Redis Key Pattern**: `scheduler:lock:schedule:{schedule_id}`

```
locking.py:21                   Redis                           locking.py:129
DistributedLock()   -->         SET key token NX EX 600   -->   _renewal_loop()
|                               |                               |
v                               v (if NX succeeds)              v (every 300s)
acquire() returns True          Lock acquired                   EXPIRE key 600
|                                                               (auto-renewal)
v
_start_renewal()
(background thread)

...execution happens...

locking.py:92
release()           -->         Lua script:
                                if GET key == token
                                  DEL key
                                else
                                  0 (not our lock)
```

**Lock Implementation** (`locking.py:55-90`):
```python
def acquire(self, blocking: bool = False, blocking_timeout: float = None) -> bool:
    import secrets
    self._lock_token = secrets.token_hex(16)

    if blocking:
        end_time = time.time() + (blocking_timeout or self.timeout)
        while time.time() < end_time:
            if self._try_acquire():
                self._start_renewal()
                return True
            time.sleep(0.1)
        return False
    else:
        if self._try_acquire():
            self._start_renewal()
            return True
        return False

def _try_acquire(self) -> bool:
    return self.redis.set(self.name, self._lock_token, nx=True, ex=self.timeout)
```

**Auto-Renewal** (`locking.py:129-154`):
```python
def _renewal_loop(self):
    """Background thread that renews the lock."""
    renewal_interval = self.timeout / 2
    while not self._stop_renewal.wait(renewal_interval):
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = self.redis.eval(script, 1, self.name, self._lock_token, self.timeout)
        if not result:
            break
```

---

## Flow 5: Event Publishing

**Purpose**: Broadcast execution events to Redis for backend WebSocket relay

```
service.py:1166                 Redis                           Backend
_publish_event()    -->         PUBLISH scheduler:events  -->   (subscriber)
|                               {type, agent, ...}              |
v                                                               v
JSON serialize                                                  Relay to WebSocket
redis.publish()                                                 clients
```

**Event Types**:

| Event | Fields | Description |
|-------|--------|-------------|
| `schedule_execution_started` | agent, schedule_id, execution_id, schedule_name, triggered_by | Agent execution begins |
| `schedule_execution_completed` | agent, schedule_id, execution_id, status, error? | Agent execution ends |
| `schedule_execution_skipped` | agent, schedule_id, execution_id, schedule_name, reason | Agent execution skipped (max_instances) |
| `process_schedule_execution_started` | process_id, process_name, schedule_id, trigger_id, execution_id | Process execution begins |
| `process_schedule_execution_completed` | process_id, process_name, schedule_id, execution_id, process_execution_id?, status, error? | Process execution ends |
| `process_schedule_execution_skipped` | process_id, process_name, schedule_id, trigger_id, execution_id, reason | Process execution skipped (max_instances) |

**Publishing Code** (`service.py:1166-1180`):
```python
async def _publish_event(self, event: dict):
    if not config.publish_events:
        return
    try:
        event_json = json.dumps(event)
        self.redis.publish("scheduler:events", event_json)
    except Exception as e:
        logger.error(f"Failed to publish event: {e}")
```

---

## Flow 6: Periodic Schedule Sync

**Purpose**: Detect new/updated/deleted schedules without container restart

**Trigger**: Every 60 seconds (configurable via `SCHEDULE_RELOAD_INTERVAL`)

```
main.py:211                   service.py:345                  service.py:362
_run_until_shutdown() -->     _sync_schedules()      -->      _sync_agent_schedules()
(main loop)                   |                                |
|                             v                                v
v                             _sync_agent_schedules()          Compare DB with _schedule_snapshot
heartbeat (30s)               _sync_process_schedules()        Build current_state from list_all_schedules()
|                             service.py:426                   Detect: new / deleted / updated
v                                                              Update APScheduler jobs
check sync_interval                                            Update snapshot
(>= 60s since last?)
```

**Agent Schedule Sync** (`service.py:362-424`):
```python
async def _sync_agent_schedules(self):
    """Sync agent schedules with database."""
    all_schedules = self.db.list_all_schedules()

    # Build current state map: schedule_id -> (enabled, updated_at_iso)
    current_state = {}
    for schedule in all_schedules:
        current_state[schedule.id] = (schedule.enabled, schedule.updated_at.isoformat())

    # Detect changes
    snapshot_ids = set(self._schedule_snapshot.keys())
    current_ids = set(current_state.keys())

    # New schedules (add jobs)
    for schedule_id in (current_ids - snapshot_ids):
        if schedule.enabled:
            self._add_job(schedule)
        self._schedule_snapshot[schedule_id] = current_state[schedule_id]

    # Deleted schedules (remove jobs)
    for schedule_id in (snapshot_ids - current_ids):
        self._remove_job(schedule_id)
        del self._schedule_snapshot[schedule_id]

    # Updated schedules (enabled/disabled or cron changed)
    for schedule_id in (snapshot_ids & current_ids):
        if old_state != new_state:
            # Re-add job with updated config
```

**Process Schedule Sync** (`service.py:426-483`):

Identical pattern to agent schedule sync but operates on `_process_schedule_snapshot` and uses `_add_process_job()` / `_remove_process_job()`. Reads from `process_schedules` table via `db.list_all_process_schedules()`.

**Invariant — run-time writes must not bump `updated_at`** (Issue #420):

The sync loop compares `(enabled, updated_at)` to detect config changes. `update_schedule_run_times()` and `update_process_schedule_run_times()` therefore write `last_run_at` / `next_run_at` only — they must NOT touch `updated_at`. Bumping it produced a self-triggering loop where each sync tick saw its own previous `_add_job` write, flagged every schedule as "updated", and re-registered all N jobs once per tick. Legitimate config edits still bump `updated_at` via `update_schedule()` / `set_schedule_enabled()` in the backend, so user-initiated changes are still detected.

---

## Flow 7: Manual Trigger (via Dedicated Scheduler)

**Purpose**: Manual schedule triggers are routed through the dedicated scheduler for consistent locking and activity tracking.

**Trigger**: User clicks "Run Now" button in UI or API call

```
Backend API                      Scheduler Service              Backend Internal API
POST /api/agents/{name}/         POST /api/schedules/           POST /api/internal/
    schedules/{id}/trigger       {id}/trigger                   execute-task
|                                |                               |
v                                v                               v
schedules.py:trigger_schedule    main.py:136                     TaskExecutionService
  -> httpx.post to scheduler       _trigger_handler()              execute_task()
                                   -> validate schedule
                                   -> create_task(_execute_manual_trigger)
                                   -> return immediately

                                   main.py:191
                                   _execute_manual_trigger()
                                     -> acquire lock
                                     -> _execute_schedule_with_lock(triggered_by="manual")
                                       -> POST /api/internal/execute-task
```

**Endpoint**: `POST /api/schedules/{schedule_id}/trigger` (scheduler service port 8001)

**Response**:
```json
{
  "status": "triggered",
  "schedule_id": "abc123",
  "schedule_name": "Daily Report",
  "agent_name": "my-agent",
  "message": "Execution started in background"
}
```

---

## Flow 8: Activity Tracking (via TaskExecutionService)

**Purpose**: Create `agent_activities` records for Timeline dashboard visibility

**Trigger**: Both cron-triggered and manual schedule executions

> **Note**: Activity tracking for scheduled executions is handled automatically by `TaskExecutionService` when the scheduler calls `POST /api/internal/execute-task`. The scheduler no longer calls activity tracking endpoints directly.

```
Scheduler Service                     Backend Internal API
_execute_schedule_with_lock()   -->   POST /api/internal/execute-task
|                                     {agent_name, message, ..., async_mode: true}
v                                     |
<-- 200 accepted (immediate) <---     Backend spawns background task:
|                                       TaskExecutionService.execute_task()
v                                         -> activity_service.track_activity(CHAT_START)
_poll_execution_completion()              -> POST to agent /api/task
  polls DB every 10s                      -> activity_service.complete_activity()
  until status != "running"               -> slot_service.release_slot()
|                                         -> update execution status in DB
v
Returns result from DB
```

---

## Flow 9: Skipped Execution Recording (Issue #46)

**Purpose**: Record executions dropped due to APScheduler's max_instances=1 constraint

**Trigger**: APScheduler fires `EVENT_JOB_MAX_INSTANCES` when a job is skipped

```
APScheduler                     service.py:488                  database.py:237
EVENT_JOB_MAX_INSTANCES  -->    _on_job_max_instances()  -->    create_skipped_execution()
|                               |                               or create_skipped_process_
v                               v                               schedule_execution()
Job skipped because             Parse job_id prefix:            |
max_instances=1 reached         "schedule_" -> agent            v
                                "process_schedule_" -> process  INSERT with status='skipped'
                                |                               duration_ms=0
                                v                               error=skip_reason
                                _record_skipped_agent_schedule()
                                service.py:512
                                or _record_skipped_process_schedule()
                                service.py:552
                                |
                                v
                                Publish event
                                "schedule_execution_skipped"
                                or "process_schedule_execution_skipped"
```

**Handler** (`service.py:488-511`):
```python
def _on_job_max_instances(self, event: JobExecutionEvent):
    job_id = event.job_id
    if job_id.startswith("schedule_"):
        schedule_id = job_id[len("schedule_"):]
        self._record_skipped_agent_schedule(schedule_id)
    elif job_id.startswith("process_schedule_"):
        schedule_id = job_id[len("process_schedule_"):]
        self._record_skipped_process_schedule(schedule_id)
```

**WebSocket Event**:
```json
{
  "type": "schedule_execution_skipped",
  "agent": "my-agent",
  "schedule_id": "abc123",
  "execution_id": "exec-456",
  "schedule_name": "Daily Report",
  "reason": "Previous execution still running"
}
```

---

## Flow 10: Automatic Retry (RETRY-001)

**Location**: `src/scheduler/service.py:1074-1132`

Failed scheduled executions can be automatically retried with configurable delay.

### Configuration

| Field | Default | Range | Description |
|-------|---------|-------|-------------|
| `max_retries` | 0 | 0-5 | Max retry attempts (0=disabled; opt-in) |
| `retry_delay_seconds` | 60 | 30-600 | Delay between retries |

Both new and existing schedules default to 0 (opt-in). Flipped from `1` to `0` in
#476 because retries amplified load during multi-hour subscription outages
without typically adding value — scheduled agents catch up on the next cron
tick. Users opt in (1-5) when a missed tick is genuinely costly.

### Retry Flow

```
Execution fails → _maybe_schedule_retry()
    ├─ Check schedule.max_retries > 0
    ├─ Check attempt_number <= max_retries
    ├─ Calculate delay (2x for 429/rate-limit, capped at 300s)
    ├─ Persist: DB schedule_retry(execution_id, retry_scheduled_at)
    └─ Schedule: APScheduler DateTrigger → _execute_retry()

_execute_retry() fires:
    ├─ Clear retry_scheduled_at from original execution
    ├─ Verify schedule still exists and enabled
    ├─ Create new execution record (triggered_by="retry", attempt_number=N+1)
    └─ Call _call_backend_execute_task()
```

### Execution Record Fields

| Field | Purpose |
|-------|---------|
| `attempt_number` | Which attempt (1=first try, 2=first retry) |
| `retry_of_execution_id` | Links to original execution for grouping |
| `retry_scheduled_at` | When retry fires (for restart recovery) |

### Restart Recovery

On startup, `_recover_pending_retries()` queries executions with `status='pending_retry'` and reschedules their APScheduler jobs.

### Statuses

| Status | Meaning |
|--------|---------|
| `pending_retry` | Failed, retry scheduled but not yet fired |
| Retry → `running` | Retry in progress |
| Retry → `success/failed` | Retry outcome |

### Database Operations

| Method | Purpose |
|--------|---------|
| `schedule_retry()` | Mark execution as pending_retry |
| `get_pending_retries()` | List executions awaiting retry |
| `clear_retry_scheduled()` | Clear after retry fires |
| `get_original_execution_id()` | Traverse chain for UI grouping |

---

## Database Operations

**Location**: `src/scheduler/database.py`

### Agent Schedule Read Operations

| Method | Line | SQL | Purpose |
|--------|------|-----|---------|
| `get_schedule(id)` | 114-120 | `SELECT * FROM agent_schedules WHERE id = ?` | Get single schedule |
| `list_all_enabled_schedules()` | 122-130 | `SELECT * FROM agent_schedules WHERE enabled = 1` | Load on startup |
| `list_all_schedules()` | 132-140 | `SELECT * FROM agent_schedules` | Sync detection (all states) |
| `list_agent_schedules(name)` | 142-150 | `SELECT * FROM agent_schedules WHERE agent_name = ?` | Per-agent list |
| `get_autonomy_enabled(name)` | 152-160 | `SELECT autonomy_enabled FROM agent_ownership` | Check autonomy |

### Agent Schedule Write Operations

| Method | Line | SQL | Purpose |
|--------|------|-----|---------|
| `update_schedule_run_times()` | 184-215 | `UPDATE agent_schedules SET last_run_at, next_run_at` (does NOT touch `updated_at` — Issue #420) | Track execution times |

### Agent Execution Operations

| Method | Line | SQL | Purpose |
|--------|------|-----|---------|
| `create_execution()` | 196-235 | `INSERT INTO schedule_executions` | Create execution record |
| `create_skipped_execution()` | 237-297 | `INSERT ... status='skipped'` | Record skipped execution (Issue #46) |
| `update_execution_status()` | 299-351 | `UPDATE schedule_executions SET status, response, ..., claude_session_id` | Complete execution (EXEC-023) |
| `get_execution(id)` | 353-359 | `SELECT * FROM schedule_executions WHERE id = ?` | Get execution |
| `get_recent_executions()` | 361-370 | `SELECT ... ORDER BY started_at DESC LIMIT ?` | List recent |

### Process Schedule Operations

| Method | Line | SQL | Purpose |
|--------|------|-----|---------|
| `ensure_process_schedules_table()` | 376-422 | `CREATE TABLE IF NOT EXISTS process_schedules / process_schedule_executions` | Create tables on startup |
| `get_process_schedule(id)` | 459-465 | `SELECT * FROM process_schedules WHERE id = ?` | Get single process schedule |
| `get_process_schedule_by_trigger()` | 467-476 | `SELECT ... WHERE process_id = ? AND trigger_id = ?` | Look up by process+trigger |
| `list_all_enabled_process_schedules()` | 478-486 | `SELECT * FROM process_schedules WHERE enabled = 1` | Load on startup |
| `list_all_process_schedules()` | 488-496 | `SELECT * FROM process_schedules` | Sync detection |
| `list_process_schedules(process_id)` | 498-506 | `SELECT ... WHERE process_id = ?` | Per-process list |
| `create_process_schedule()` | 508-558 | `INSERT INTO process_schedules` | Create schedule |
| `update_process_schedule_run_times()` | 685-715 | `UPDATE process_schedules SET last_run_at, next_run_at` (does NOT touch `updated_at` — Issue #420) | Track run times |
| `delete_process_schedule()` | 586-592 | `DELETE FROM process_schedules WHERE id = ?` | Delete single |
| `delete_process_schedules_for_process()` | 594-600 | `DELETE ... WHERE process_id = ?` | Delete all for process |
| `create_process_schedule_execution()` | 602-639 | `INSERT INTO process_schedule_executions` | Create execution record |
| `create_skipped_process_schedule_execution()` | 641-702 | `INSERT ... status='skipped'` | Record skipped (Issue #46) |
| `update_process_schedule_execution()` | 704-741 | `UPDATE process_schedule_executions SET status, ...` | Complete execution |

---

## Service Method Reference

**Location**: `src/scheduler/service.py`

| Method | Line | Purpose |
|--------|------|---------|
| `__init__()` | 44-71 | Initialize service with DB, lock manager, Redis, snapshots |
| `initialize()` | 80-141 | Load schedules, detect missed, start APScheduler |
| `_get_missed_schedules()` | 142-194 | Detect schedules missed while container was down (Issue #145) |
| `shutdown()` | 196-207 | Stop APScheduler, close Redis, close lock manager |
| `fire_missed_schedules()` | 209-222 | Execute missed schedules on startup |
| `run_forever()` | 224-248 | Main loop (heartbeat + sync) |
| `_get_job_id()` | 254-256 | Generate APScheduler job ID: `schedule_{id}` |
| `_parse_cron()` | 258-278 | Parse 5-field cron expression |
| `_add_job()` | 280-315 | Add agent schedule as APScheduler CronTrigger job |
| `_remove_job()` | 317-327 | Remove agent schedule job |
| `_get_next_run_time()` | 329-339 | Calculate next run time via croniter |
| `_sync_schedules()` | 345-360 | Top-level sync: calls agent + process sync |
| `_sync_agent_schedules()` | 362-424 | Sync agent schedule snapshot with DB |
| `_sync_process_schedules()` | 426-483 | Sync process schedule snapshot with DB |
| `_on_job_max_instances()` | 488-510 | APScheduler event handler for skipped jobs |
| `_record_skipped_agent_schedule()` | 512-550 | Record skipped agent execution |
| `_record_skipped_process_schedule()` | 552-591 | Record skipped process execution |
| `_execute_schedule()` | 597-613 | Agent execution entry point (lock + delegate) |
| `_execute_schedule_with_lock()` | 615-758 | Agent execution with lock held |
| `_call_backend_execute_task()` | 760-833 | HTTP dispatch to backend + async handoff |
| `_poll_execution_completion()` | 835-887 | Poll DB for execution completion (SCHED-ASYNC-001) |
| `add_schedule()` | 893-896 | Runtime: add new agent schedule |
| `remove_schedule()` | 898-900 | Runtime: remove agent schedule |
| `update_schedule()` | 902-906 | Runtime: update agent schedule |
| `reload_schedules()` | 908-928 | Reload all schedules from DB |
| `_get_process_job_id()` | 934-936 | Generate process job ID: `process_schedule_{id}` |
| `_add_process_job()` | 938-973 | Add process schedule as APScheduler job |
| `_remove_process_job()` | 975-985 | Remove process schedule job |
| `_execute_process_schedule()` | 987-1003 | Process execution entry point (lock + delegate) |
| `_execute_process_schedule_with_lock()` | 1005-1145 | Process execution with lock held |
| `add_process_schedule()` | 1147-1150 | Runtime: add new process schedule |
| `remove_process_schedule()` | 1152-1154 | Runtime: remove process schedule |
| `update_process_schedule()` | 1156-1160 | Runtime: update process schedule |
| `_publish_event()` | 1166-1180 | Publish event to Redis pub/sub |
| `get_status()` | 1186-1212 | Get scheduler status |
| `is_healthy()` | 1214-1220 | Health check |

---

## Health & Status Endpoints

**Location**: `src/scheduler/main.py:76-134`

### GET /health

Returns 200 if scheduler is healthy, 503 otherwise.

```json
{"status": "healthy"}
```

### GET /status

Returns detailed scheduler status.

```json
{
  "running": true,
  "jobs_count": 5,
  "uptime_seconds": 3600.5,
  "last_check": "2026-01-13T10:30:00.000Z",
  "jobs": [
    {
      "id": "schedule_abc123",
      "name": "my-agent:Daily Report",
      "next_run": "2026-01-14T09:00:00+00:00"
    }
  ]
}
```

### GET /

Service information.

```json
{
  "service": "Trinity Scheduler",
  "version": "1.0.0",
  "endpoints": {
    "/health": "Health check",
    "/status": "Detailed status",
    "/api/schedules/{schedule_id}/trigger": "Manual trigger (POST)"
  }
}
```

---

## Docker Deployment

**Dockerfile** (`docker/scheduler/Dockerfile`):
```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY docker/scheduler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/scheduler /app/scheduler

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

ENV DATABASE_PATH=/data/trinity.db
ENV REDIS_URL=redis://redis:6379
ENV HEALTH_PORT=8001
ENV LOG_LEVEL=INFO

CMD ["python", "-m", "scheduler.main"]
```

**Dependencies** (`docker/scheduler/requirements.txt`):
```
APScheduler>=3.10.0,<4.0.0
croniter>=2.0.1
httpx>=0.27.0
aiohttp>=3.9.0
redis>=5.0.0
pytz>=2024.1
pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-cov>=4.1.0
pytest-mock>=3.12.0
typing-extensions>=4.9.0
```

---

## Testing

**Location**: `tests/scheduler_tests/`

### Test Files

| File | Tests | Purpose |
|------|-------|---------|
| `test_config.py` | Configuration loading | Environment variables |
| `test_cron.py` | Cron expression parsing | 5-field format validation |
| `test_database.py` | Database operations | CRUD for schedules/executions |
| `test_locking.py` | Distributed locks | Redis lock acquire/release |
| `test_agent_client.py` | HTTP client | Agent communication |
| `test_service.py` | Scheduler service | Full integration tests |
| `test_async_dispatch.py` | Async dispatch + polling | SCHED-ASYNC-001 + dispatch-timeout error persistence (#1022): `_describe_exception()` branches, descriptive-raise on `ReadTimeout('')`, config-driven deadline, non-empty-error regression for both the main dispatch and the retry path (22 tests) |
| `conftest.py` | Fixtures | Mock database, Redis, models |

### Running Tests

```bash
# Run all scheduler tests
pytest tests/scheduler_tests/ -v

# Run with coverage
pytest tests/scheduler_tests/ --cov=src/scheduler --cov-report=html

# Run standalone test environment
docker compose -f docker/scheduler/docker-compose.test.yml up
```

---

## Error Handling

| Error Case | Handling | Result |
|------------|----------|--------|
| Schedule not found | Log error, return | Execution skipped |
| Schedule disabled | Log info, return | Execution skipped |
| Autonomy disabled | Log info, **advance next_run_at only** (no execution row, no last_run_at — #1472), return (cron only, not manual) | Skipped; projection stays fresh (no "Next: Nd ago") |
| **_add_job fails (bad cron/timezone)** | #1472: classify permanent (`UnknownTimeZoneError`/`ValueError` → snapshot + log once, bounded) vs transient (retry next sync). API pre-validates (`services/schedule_validation.py`) so permanent failures rarely reach here | Registered on retry, or bounded + surfaced (not a silent orphan) |
| Lock not acquired | Log info, return | Execution skipped (another running) |
| **Max instances reached** | Create skipped execution, publish event | **Recorded with status='skipped'** (Issue #46) |
| Agent not reachable | Update status=failed (with overwrite guard), publish event | Error recorded |
| Agent timeout | Update status=failed (with overwrite guard), publish event | Error recorded |
| Auth failure detected | Log auth-specific error, publish event | Error recorded with auth context |
| **TCP disconnect (SCHED-ASYNC-001)** | **Check DB before overwriting -- if backend already finalized, preserve status** | **No false failures** |
| **Dispatch timeout (#1022)** | Re-raised as a named non-blank error *before* the generic handler; outcome UNKNOWN (task may already be running → orphan) | Descriptive `error` persisted (never blank); cleanup service recovers any orphan |
| **Blank-stringifying exception (e.g. httpx timeout)** | `_describe_exception()` falls back to the exception type name | `error` never persisted blank (#1022) |
| **Polling deadline exceeded** | Raise exception, overwrite guard checks DB status | Error recorded (if genuinely stale) |
| Process backend HTTP error | Update process execution as failed, publish event | Error recorded |
| Process backend timeout | Update process execution as failed, publish event | Error recorded |
| Redis publish fails | Log error, continue | Execution still succeeds |

---

## Security Considerations

1. **Database Access**: Read-only access to schedules; write access only to executions
2. **Lock Tokens**: Random 16-byte hex tokens prevent lock hijacking
3. **Agent Communication**: Internal Docker network, no external exposure
4. **Internal API Auth**: Calls to backend `/api/internal/` endpoints include `X-Internal-Secret` header (C-003). Health endpoints remain unauthenticated (internal use only).
5. **Credential Isolation**: Scheduler has no access to agent credentials

---

## Graceful Shutdown

**Signal Handling** (`main.py:251-262`):
```python
def setup_signal_handlers(self, loop: asyncio.AbstractEventLoop):
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._signal_handler(s)))

async def _signal_handler(self, sig):
    await self.shutdown()
```

**Shutdown Sequence** (`main.py:238-249`):
```python
async def shutdown(self):
    self._shutdown_event.set()
    if self.scheduler_service:
        self.scheduler_service.shutdown()
    if self.health_runner:
        await self.health_runner.cleanup()
```

---

## Trinity Connect Integration (Added 2026-02-05)

The dedicated scheduler service publishes events to Redis, which the backend relays to both the main WebSocket and the filtered Trinity Connect WebSocket.

### Events Broadcast to Trinity Connect

Schedule execution events are forwarded to external listeners:

| Event Type | Agent Name Field | Description |
|------------|------------------|-------------|
| `schedule_execution_started` | `agent` | Execution begins |
| `schedule_execution_completed` | `agent` | Execution ends (success or failure) |

### Related Documentation

- **Trinity Connect**: [trinity-connect.md](trinity-connect.md) - Full feature flow for `/ws/events` endpoint

---

## Related Flows

- **Upstream**:
  - [scheduling.md](scheduling.md) - Backend CRUD for schedules (API layer)
  - [autonomy-mode.md](autonomy-mode.md) - Autonomy toggle affects execution
- **Downstream**:
  - [parallel-headless-execution.md](parallel-headless-execution.md) - Agent `/api/task` endpoint
  - [execution-log-viewer.md](execution-log-viewer.md) - Viewing execution transcripts
- **Related**:
  - [execution-queue.md](execution-queue.md) - Queue system (not used by scheduler)
  - [activity-stream.md](activity-stream.md) - Activity tracking (not yet integrated)
  - [trinity-connect.md](trinity-connect.md) - Filtered event broadcast for external listeners

---

## Migration Notes

**Embedded Scheduler Fully Removed (2026-02-11)**:

The embedded scheduler (`src/backend/services/scheduler_service.py`) has been completely removed. The dedicated scheduler is now the single source of truth for all schedule execution.

**Current Architecture**:
1. Backend only manages schedule CRUD in database
2. Dedicated scheduler syncs from database every 60 seconds
3. All triggers (cron and manual) are executed by dedicated scheduler
4. Activity tracking via internal API ensures Timeline visibility

---

## Revision History

| Date | Change |
|------|--------|
| 2026-07-06 | **Stale `next_run_at` repair (#1472)**: fixed the "Next: Nd ago" bug across the projection-advance paths. Autonomy-off skip now advances `next_run_at` only (no row/last_run_at). Every fire outcome (success/dispatched/failure/exception) advances **once at fire time** (`_execute_schedule_with_lock`), replacing the per-branch advances — closes the failure/exception leak and a latent phantom-catch-up. `_add_job` returns a bool and classifies permanent (bad cron/tz — snapshot + log-once, bounded) vs transient (retried next sync) instead of orphaning silently; snapshot-on-success at all sync sites. `_get_missed_schedules` compares in UTC (was stripping tzinfo → non-UTC schedules mis-compared). New API validation `services/schedule_validation.py` matches the scheduler's 5-field/`CronTrigger`/`pytz` parser (was bare `croniter()` — accepted `@daily`/seconds-crons/quartz/bad-tz that then failed registration). UI `SchedulesPanel.vue` renders "Overdue by …"; canary **E-06** detects any residual. |
| 2026-06-14 | **Descriptive error on dispatch timeout (#1022)**: a dispatch `httpx.TimeoutException` is re-raised with a named, non-blank message (`"dispatch ... timed out after {N}s — outcome unknown"`) so the silent blank `error` (bare httpx timeouts `str()` to `''`) no longer lands. New `_describe_exception()` helper normalizes blank exceptions across all execution / retry / process-schedule error paths. Dispatch + pre-check HTTP deadlines lifted from literals to config: `DISPATCH_TIMEOUT` (default 30s) and `PRE_CHECK_TIMEOUT` (default 70s). Outcome of a dispatch timeout is UNKNOWN (backend may have already started the bg task → orphan recovered by cleanup). |
| 2026-05-09 | **MCP update_agent_schedule fixes (aaad4f6, #741/#742)**: (1) `src/mcp-server/src/tools/schedules.ts` — added explicit warning to `enabled` field description in `update_agent_schedule` so AI models do not inadvertently re-enable a disabled schedule when updating unrelated fields (e.g. cron expression). (2) `src/backend/routers/schedules.py` — added `max_retries` and `retry_delay_seconds` to `ScheduleUpdateRequest`; both were missing, so `exclude_unset=True` silently dropped them before they reached `db.update_schedule()`, making retry config uneditable via MCP. |
| 2026-04-23 | **Retry Default Flipped (#476)**: `max_retries` default `1 → 0`. Both new and existing schedules are opt-in now. Scheduled agents typically catch up on next tick; retries amplified load during multi-hour outages. |
| 2026-04-14 | **Automatic Retry (RETRY-001)**: Added Flow 10 documenting configurable retry mechanism for failed executions. New fields: max_retries, retry_delay_seconds, attempt_number, retry_of_execution_id, retry_scheduled_at. New status: pending_retry. |
| 2026-03-26 | **Line number refresh + Process Schedules documentation**: Updated all line numbers to match current code. Added Flow 3 (Process Schedule Execution), process schedule sync documentation, process schedule database operations, and full service method reference table. |
| 2026-03-13 | **Schedule Update Nullable Field Fix**: Changed `schedules.py:270` from `if v is not None` filter to `model_dump(exclude_unset=True)`. |
| 2026-03-11 | **Async Fire-and-Forget with DB Polling (SCHED-ASYNC-001, Issue #101)**: Replaced blocking HTTP call with async dispatch + DB polling. |
| 2026-03-09 | **Unified Execution via TaskExecutionService**: Scheduler now calls `POST /api/internal/execute-task` instead of agent containers directly. |
| 2026-03-02 | **MODEL-001 Model Selection**: `Schedule` has `model` field, passed to execution. |
| 2026-03-01 | **Skipped Execution Recording (Issue #46)**: APScheduler event listener for `EVENT_JOB_MAX_INSTANCES`. |
| 2026-02-21 | **Session ID Capture (EXEC-023)**: `claude_session_id` for "Continue as Chat" support. |
| 2026-02-20 | **Per-Schedule Execution Configuration**: Custom timeout and tool restrictions. |
| 2026-02-11 | **Scheduler Consolidation**: Removed embedded scheduler, manual triggers via dedicated scheduler. |
| 2026-02-05 | **Trinity Connect Integration**: Filtered broadcast for external event listeners. |
| 2026-01-29 | Added periodic schedule sync - new schedules work without restart |
| 2026-01-13 | Initial documentation - standalone scheduler service |

---

**Requirement Reference**: [DEDICATED_SCHEDULER_SERVICE.md](../../requirements/DEDICATED_SCHEDULER_SERVICE.md)

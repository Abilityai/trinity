# Feature: Cleanup Service (CLEANUP-001)

## Overview
Background service that periodically recovers stuck intermediate states. Includes active watchdog reconciliation (Issue #129) that checks agent process registries, recovers orphaned executions, auto-terminates timed-out executions, and releases capacity. Also marks stale executions, activities, and Redis slots as failed. Runs every 5 minutes with an immediate startup sweep.

## User Story
As a platform operator, I want stuck executions and activities to be automatically recovered so that the system does not accumulate phantom "running" states that block capacity and mislead dashboards.

## Entry Points
- **Lifecycle**: `src/backend/main.py:265-269` - Started in `lifespan()` during backend boot
- **API (status)**: `GET /api/monitoring/cleanup-status` - Admin-only status check
- **API (trigger)**: `POST /api/monitoring/cleanup-trigger` - Admin-only manual trigger

## Frontend Layer
No dedicated frontend UI. The cleanup service is a headless backend service. Status and manual triggers are available through the monitoring API endpoints (accessible via API docs or admin tools).

## Backend Layer

### Service: CleanupService
**File**: `src/backend/services/cleanup_service.py`

#### Configuration Constants
```python
CLEANUP_INTERVAL_SECONDS = 300        # 5 minutes
EXECUTION_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
ACTIVITY_STALE_TIMEOUT_MINUTES = 120   # SCHED-ASYNC-001: increased from 30 to support long-running tasks
NO_SESSION_TIMEOUT_SECONDS = 60       # Issue #106: fast-fail executions without Claude session
WATCHDOG_HTTP_TIMEOUT = 5.0           # Issue #129: timeout for agent HTTP calls during reconciliation
ERROR_FETCH_TIMEOUT = 2.0             # Issue #286: timeout for fetching error context from agent
MAX_ERROR_MESSAGE_LENGTH = 2000       # Issue #286: truncate combined error messages
```

#### WebSocket Manager Injection (Issue #129)
Module-level `_ws_manager` set via `set_cleanup_ws_manager(manager)` from `main.py`. Used by watchdog to broadcast recovery events. No-op with debug log if not set.

#### CleanupReport
Dataclass holding results from a single cleanup cycle:
- `orphaned_executions: int` - Executions recovered by watchdog (not found on agent) — Issue #129
- `auto_terminated: int` - Executions terminated by watchdog (exceeded timeout) — Issue #129
- `stale_executions: int` - Executions marked failed (stale timeout)
- `no_session_executions: int` - Executions failed due to no Claude session (Issue #106)
- `orphaned_skipped: int` - Skipped executions finalized (Issue #106)
- `stale_activities: int` - Activities marked failed
- `stale_slots: int` - Redis slots cleaned
- `stale_slot_executions: int` - Execution records failed when their slot was reclaimed (Issue #219)
- `total` property: Sum of all eight fields
- `to_dict()` method: Serializes for API responses

#### CleanupService class (line 48)
Singleton pattern via global `cleanup_service` instance (line 141).

**State fields**:
- `poll_interval: int` - Configurable interval (default 300s)
- `_task: Optional[asyncio.Task]` - Background asyncio task
- `_running: bool` - Service running flag
- `last_run_at: Optional[str]` - ISO timestamp of last run
- `last_report: Optional[CleanupReport]` - Results from last cycle

**Methods**:
- `start()` (line 58): Creates asyncio task for `_cleanup_loop()`, sets `_running = True`
- `stop()` (line 66): Sets `_running = False`, cancels task
- `run_cleanup()` (line 74): Single cleanup cycle (called by loop and manual trigger)
- `_cleanup_loop()` (line 114): Main loop - runs initial sweep, then sleeps `poll_interval` between cycles

### Cleanup Cycle (`run_cleanup`)

Seven sequential operations, each wrapped in individual try/except. Watchdog runs FIRST to release resources before passive cleanup:

0. **Watchdog: reconcile DB vs agent process registries** (Issue #129, #226)
   ```python
   orphaned, terminated, confirmed_running_ids = await self._reconcile_orphaned_executions()
   ```
   Runs first so it can release capacity slots and queue state before the stale cleanup marks executions failed without resource cleanup. Also returns `confirmed_running_ids` (#226) — executions verified as still running on agents within their timeout — so slot cleanup doesn't falsely fail them. See [Watchdog Reconciliation](#watchdog-reconciliation-issue-129) below.

1. **Mark stale executions as failed** (safety net for agent-unreachable cases)
   ```python
   count = db.mark_stale_executions_failed(EXECUTION_STALE_TIMEOUT_MINUTES)
   ```
   Calls `DatabaseManager.mark_stale_executions_failed()` which delegates to `ScheduleOperations.mark_stale_executions_failed()`.

2. **Fast-fail no-session executions** (Issue #106)
   ```python
   count = db.mark_no_session_executions_failed(NO_SESSION_TIMEOUT_SECONDS)
   ```
   Marks `running` executions with `claude_session_id IS NULL` older than 60 seconds as failed. These are silent launch failures where the backend failed to dispatch to the agent. Note: `TaskExecutionService` sets `claude_session_id='dispatched'` before calling the agent (step 3b), so only executions that never reached dispatch are caught here.

3. **Finalize orphaned skipped executions** (lines 98-105, Issue #106)
   ```python
   count = db.finalize_orphaned_skipped_executions()
   ```
   Defensive cleanup for `skipped` executions missing `completed_at`. Sets `completed_at = started_at` and `duration_ms = 0`.

4. **Mark stale activities as failed** (lines 107-114)
   ```python
   count = db.mark_stale_activities_failed(ACTIVITY_STALE_TIMEOUT_MINUTES)
   ```
   Calls `DatabaseManager.mark_stale_activities_failed()` which delegates to `ActivityOperations.mark_stale_activities_failed()`.

5. **Cleanup stale Redis slots and fail execution records** (Issues #219, #226, #61, #378)
   ```python
   slot_service = get_slot_service()
   agent_timeouts = db.get_all_execution_timeouts()  # #226: per-agent TTL
   reclaimed = await slot_service.cleanup_stale_slots(agent_timeouts=agent_timeouts)
   report.stale_slots = sum(len(ids) for ids in reclaimed.values())
   # #378: delegates to _process_stale_slot_reclaims which re-verifies
   # each agent just-in-time before writing FAILED
   await self._process_stale_slot_reclaims(
       reclaimed, confirmed_running_ids, report
   )
   ```
   Calls `SlotService.cleanup_stale_slots()` with per-agent timeouts (#226). The service scans all `agent:slots:*` keys, computes each agent's TTL as `timeout_seconds + 5 min buffer` (or default 20 min if no timeout configured), removes entries older than that TTL, and returns a dict mapping agent names to reclaimed execution IDs. Phase 3 is then implemented by `_process_stale_slot_reclaims()` — see [Phase 3 Slot Reclaim Re-verification](#phase-3-slot-reclaim-re-verification-issue-378) below.

### Watchdog Reconciliation (Issue #129)

Active reconciliation of DB execution state against agent process registries. Replaces the passive "detect-and-report" model with active remediation.

#### `_reconcile_orphaned_executions()` → `tuple[int, int, set]`
1. Query `db.get_running_executions_with_agent_info()` — LEFT JOINs `schedule_executions` with `agent_schedules` and `agent_ownership` for timeout resolution: `COALESCE(schedule.timeout, agent.timeout, 900)`
2. Group executions by `agent_name`
3. Parallel fan-out: `asyncio.gather` queries all agents concurrently via shared `httpx.AsyncClient`
4. Each agent queried via `GET http://agent-{name}:8000/api/executions/running` → set of execution IDs
5. Decision matrix per execution:

| Agent reachable? | In agent's list? | Age > timeout? | Action |
|---|---|---|---|
| No (ConnectError/Timeout) | — | — | **SKIP** (retry next cycle) |
| Yes | No | Age < 60s | **SKIP** (dispatch grace window) |
| Yes | No | Age >= 60s | **ORPHAN RECOVERY** |
| Yes | Yes | No | **CONFIRMED RUNNING** (#226) |
| Yes | Yes | Yes, terminate succeeds | **AUTO-TERMINATE** |
| Yes | Yes | Yes, terminate fails | **SKIP** (defer to 120-min stale cleanup) |

6. **Per-execution error isolation**: each recovery in its own try/except
7. **Systemic failure detection**: warns if >50% of actual recovery attempts fail in a cycle (only counts orphan/terminate attempts, not healthy executions checked)
8. **Concurrency guard**: `asyncio.Lock` prevents overlapping cleanup cycles from background loop + manual trigger
9. **Returns third element** (#226): `confirmed_running` set — execution IDs verified as still running on agent within their timeout. Slot cleanup uses this to avoid falsely failing executions that are legitimately running.

#### `_get_execution_error(client, agent_name, execution_id)` → `Optional[str]` (Issue #286)
Fetches original error context from agent before marking execution failed:
1. `GET http://agent-{name}:8000/api/executions/{id}/last-error` — queries agent's log buffer for error info
2. Returns formatted error string (`[error_type] error_message`) or None if unavailable
3. Sanitizes error message via `sanitize_text()` to remove credential patterns
4. Uses short timeout (`ERROR_FETCH_TIMEOUT = 2.0s`) to avoid blocking cleanup
5. Gracefully handles agent unreachability — returns None on ConnectError/TimeoutException

#### `_recover_execution(execution_id, agent_name, error_msg, action, client=None)` → `bool`
Shared DRY helper for both orphan recovery and auto-terminate:
1. **Issue #286**: If `client` provided, calls `_get_execution_error()` to fetch original error context
2. Combines original error with cleanup reason: `"{original_error}. Cleanup: {cleanup_reason}"`
3. Truncates combined message to `MAX_ERROR_MESSAGE_LENGTH = 2000` to prevent DB bloat
4. `db.mark_execution_failed_by_watchdog()` — conditional UPDATE with `WHERE status='running'` race guard. Returns False if execution already completed (no-op).
5. `slot_service.release_slot()` — idempotent Redis ZREM
6. `queue.force_release_if_matches()` — atomic Lua script: GET running key, compare execution ID, conditional DELETE. Prevents TOCTOU race where a new execution could start between check and release.
7. `_broadcast_watchdog_event()` — WebSocket JSON event with combined error: `{"type": "watchdog_recovery", "agent_name", "execution_id", "action", "reason", "timestamp"}`

#### `_terminate_on_agent(client, agent_name, execution_id)` → `bool`
`POST http://agent-{name}:8000/api/executions/{id}/terminate`. Returns True if HTTP 2xx (agent confirmed termination), False otherwise. Callers only proceed with DB/resource cleanup on success — failed terminations are deferred to the 120-min stale cleanup safety net.

### Phase 3 Slot Reclaim Re-verification (Issue #378)

Before this fix, Phase 3 could mark an execution `FAILED` with "Stale execution — slot TTL expired" while the task was actually still running on the agent (agent had just dropped it from its registry before Phase 0's batch query, so `confirmed_running_ids` missed it). The agent's authoritative `SUCCESS` response then arrived seconds later and overwrote `FAILED` → `SUCCESS`, causing a phantom failure flash in the UI.

#### `_process_stale_slot_reclaims(reclaimed, confirmed_running_ids, report)` → `None`

Replaces the inline Phase 3 loop. Extracted as its own method for direct unit testing (mirrors `_reconcile_orphaned_executions` testability pattern). Key additions over the old inline loop:

1. **Parallel per-agent re-verify fan-out** — one `GET /api/executions/running` call per agent (not per-execution), dispatched concurrently via `asyncio.gather(..., return_exceptions=True)`. Mirrors Phase 0's pattern. Worst-case Phase 3 wall-time goes from O(N_agents × 5s) serial to O(5s) parallel when agents are slow.
2. **Just-in-time re-verify** — the agent is re-queried as close as possible to the `fail_stale_slot_execution` write, minimizing the race window that Phase 0's earlier batch query leaves open.
3. **Per-execution decision matrix**:

| Phase 0 said running? | Re-verify says? | Action |
|---|---|---|
| Yes (`confirmed_running_ids`) | — | **SKIP** (trust Phase 0, save an HTTP call) |
| No | Agent unreachable (None) | **SKIP this cycle** — Phase 1 (120-min stale cleanup) is the backstop |
| No | Agent says still running | **SKIP** — #378 race closed; agent's own SUCCESS write will land correctly |
| No | Agent says not running | **FAIL** — terminate (best-effort, #61) + `fail_stale_slot_execution` with phantom-stale error |

4. **No cross-cycle state** — `slot_service.cleanup_stale_slots` removes reclaimed IDs from Redis permanently (`zremrangebyscore`), so a deferred ID cannot reappear in a later cycle's `reclaimed` dict. Any "retry on next cycle" state machine would be dead code. Transiently-unreachable agents are caught by Phase 0's orphan recovery on subsequent cycles (when the agent becomes reachable again) and by Phase 1's 120-min stale cleanup as a final backstop.

#### Residual-race observability

`db.schedules.update_execution_status` emits a narrowly-scoped `logger.warning` whenever a `SUCCESS` write overwrites a row whose existing error matches the `_STALE_SLOT_ERROR_PATTERN = "Stale execution — slot TTL expired"` marker. Purely observational — update semantics are unchanged (the agent's SUCCESS still wins). The pattern match prevents misattribution of other legitimate FAILED→SUCCESS transitions (Phase 0 auto-terminate, Phase 1 stale cleanup, startup recovery) to #378. Grep with:

```bash
docker logs trinity-backend | grep "residual race condition (#378)"
```

### Startup Loop (`_cleanup_loop`)

```
1. Run immediate startup sweep (run_cleanup)
2. Log startup results
3. While _running:
   a. Sleep poll_interval (300s)
   b. Run cleanup cycle
   c. Handle CancelledError for graceful shutdown
```

### Lifespan Registration

**File**: `src/backend/main.py`

**Import**:
```python
from services.cleanup_service import cleanup_service, set_cleanup_ws_manager
```

**Start** (lines 265-269):
```python
try:
    cleanup_service.start()
    print("Cleanup service started")
except Exception as e:
    print(f"Error starting cleanup service: {e}")
```

**Stop** (lines 300-305):
```python
try:
    cleanup_service.stop()
    print("Cleanup service stopped")
except Exception as e:
    print(f"Error stopping cleanup service: {e}")
```

### API Endpoints

**File**: `src/backend/routers/monitoring.py`

#### GET /api/monitoring/cleanup-status (lines 455-473)
- **Auth**: Admin only (`require_admin`)
- **Response**:
  ```json
  {
    "running": true,
    "interval_seconds": 300,
    "last_run_at": "2026-03-25T10:00:00Z",
    "last_report": {
      "orphaned_executions": 0,
      "auto_terminated": 0,
      "stale_executions": 0,
      "no_session_executions": 0,
      "orphaned_skipped": 0,
      "stale_activities": 0,
      "stale_slots": 0,
      "total": 0
    }
  }
  ```

#### POST /api/monitoring/cleanup-trigger (lines 476-491)
- **Auth**: Admin only (`require_admin`)
- **Behavior**: Runs `cleanup_service.run_cleanup()` synchronously
- **Response**:
  ```json
  {
    "status": "completed",
    "report": {
      "orphaned_executions": 1,
      "auto_terminated": 0,
      "stale_executions": 2,
      "no_session_executions": 1,
      "orphaned_skipped": 0,
      "stale_activities": 1,
      "stale_slots": 0,
      "total": 5
    }
  }
  ```

## Data Layer

### Database Operations

#### mark_stale_executions_failed (ScheduleOperations)
**File**: `src/backend/db/schedules.py:971-1013`

**SQL** (finds stale rows — threshold computed in Python as ISO 8601 to match stored format, Issue #137):
```sql
SELECT id, started_at FROM schedule_executions
WHERE status = 'running'
AND started_at < ?  -- Python: (utcnow - 120 min).strftime('%Y-%m-%dT%H:%M:%S')
```

**SQL** (updates each row):
```sql
UPDATE schedule_executions
SET status = 'failed',
    completed_at = ?,
    duration_ms = ?,
    error = 'Marked as failed by cleanup: exceeded 120-minute timeout'
WHERE id = ?
```

#### mark_execution_dispatched (ScheduleOperations)
**File**: `src/backend/db/schedules.py:570-590`

Called by `TaskExecutionService` (step 3b) before the agent HTTP call. Sets `claude_session_id='dispatched'` so the no-session cleanup only catches executions that never reached dispatch.

**SQL**:
```sql
UPDATE schedule_executions
SET claude_session_id = 'dispatched'
WHERE id = ? AND status = 'running' AND claude_session_id IS NULL
```

#### mark_no_session_executions_failed (ScheduleOperations) — Issue #106
**File**: `src/backend/db/schedules.py:1036-1076`

Only catches executions where `claude_session_id IS NULL` or empty string (never dispatched). Executions that were dispatched have `claude_session_id='dispatched'` and are not affected.

**SQL** (finds no-session rows — threshold computed in Python as ISO 8601, Issue #137):
```sql
SELECT id, started_at FROM schedule_executions
WHERE status = 'running'
AND (claude_session_id IS NULL OR claude_session_id = '')
AND started_at < ?  -- Python: (utcnow - 60 sec).strftime('%Y-%m-%dT%H:%M:%S')
```

**SQL** (updates each row):
```sql
UPDATE schedule_executions
SET status = 'failed',
    completed_at = ?,
    duration_ms = ?,
    error = 'Silent launch failure: no Claude session created within 60 seconds'
WHERE id = ?
```

#### fail_stale_slot_execution (ScheduleOperations) — Issue #219
**File**: `src/backend/db/schedules.py:1103-1144`

Marks a single execution as failed when its Redis slot is reclaimed. Uses a `WHERE status = 'running'` guard to prevent overwriting executions that already completed or failed via another path.

**SQL** (guarded select):
```sql
SELECT started_at FROM schedule_executions WHERE id = ? AND status = 'running'
```

**SQL** (guarded update):
```sql
UPDATE schedule_executions
SET status = 'failed',
    completed_at = ?,
    duration_ms = ?,
    error = ?
WHERE id = ? AND status = 'running'
```

**Delegation chain**:
- `cleanup_service.run_cleanup()` -> `db.fail_stale_slot_execution(execution_id, error)`
- `DatabaseManager.fail_stale_slot_execution()` -> `self._schedule_ops.fail_stale_slot_execution()`

#### finalize_orphaned_skipped_executions (ScheduleOperations) — Issue #106
**File**: `src/backend/db/schedules.py:1146-1170`

**SQL** (single update — now sets terminal status, Issue #137):
```sql
UPDATE schedule_executions
SET status = 'failed',
    completed_at = COALESCE(started_at, ?),
    duration_ms = 0,
    error = 'Finalized by cleanup: skipped execution'
WHERE status = 'skipped'
AND completed_at IS NULL
```

#### mark_stale_activities_failed (ActivityOperations)
**File**: `src/backend/db/activities.py:187-225`

**SQL** (finds stale rows — threshold computed in Python as ISO 8601, Issue #137):
```sql
SELECT id, started_at FROM agent_activities
WHERE activity_state = 'started'
AND started_at < ?  -- Python: (utcnow - timeout_min).strftime('%Y-%m-%dT%H:%M:%S')
```

**SQL** (updates each row):
```sql
UPDATE agent_activities
SET activity_state = 'failed',
    completed_at = ?,
    duration_ms = ?,
    error = 'Marked as failed by cleanup: exceeded 30-minute timeout'
WHERE id = ?
```

**Delegation chain**:
- `cleanup_service.run_cleanup()` -> `db.mark_stale_activities_failed(30)`
- `DatabaseManager.mark_stale_activities_failed()` (line 686-688) -> `self._activity_ops.mark_stale_activities_failed(30)`
- `ActivityOperations.mark_stale_activities_failed()` (line 187)

### Redis Operations

#### cleanup_stale_slots (SlotService)
**File**: `src/backend/services/slot_service.py:259-295`

**Returns**: `Dict[str, List[str]]` — mapping of agent_name to list of reclaimed execution IDs (Issue #219).

**Logic**:
1. Scans all keys matching `agent:slots:*` pattern via `SCAN`
2. For each agent, calls `_cleanup_stale_slots_for_agent()` which returns the reclaimed execution IDs
3. Removes ZSET entries with score (timestamp) older than TTL:
   ```
   ZREMRANGEBYSCORE agent:slots:{name} -inf {cutoff_timestamp}
   ```
4. Deletes corresponding metadata keys: `agent:slot:{name}:{execution_id}`
5. Returns the execution IDs so the caller (cleanup service) can fail corresponding DB records

**TTL** (#226): Per-agent, computed as `execution_timeout_seconds + SLOT_TTL_BUFFER (5 min)`. Falls back to `DEFAULT_SLOT_TTL_SECONDS = 1200` (20 minutes) if no agent timeout is configured. This prevents premature slot reclamation for agents with long-running tasks (e.g., 60-120 min timeouts).

## Side Effects
- **Logging**: Each cleanup cycle logs results at INFO level when resources are cleaned
- **Error Logging**: Individual operation failures logged at ERROR level without stopping other operations
- **WebSocket Broadcasts** (Issue #129): Watchdog recovery events broadcast as `watchdog_recovery` type via `ConnectionManager.broadcast()`
- **Capacity Release** (Issue #129): Watchdog releases Redis capacity slots and execution queue state for recovered executions
- **Agent HTTP Calls** (Issue #129): Watchdog queries agent process registries and may POST terminate commands
- **No Activity Records**: Cleanup itself does not create activity entries (avoids recursion)

## Error Handling

| Error Case | Handling | Impact |
|------------|----------|--------|
| Watchdog agent unreachable | Skipped, retry next cycle | No false positives |
| Watchdog single recovery fails | Per-execution try/except, continues | Other recoveries unaffected |
| Watchdog >50% recoveries fail | WARNING log (systemic failure) | Operator alerted |
| Watchdog terminate fails on agent | Logged, DB/capacity still cleaned | Zombie process may linger |
| Watchdog DB race (already completed) | Returns False, no side effects | Correct behavior |
| Stale execution marking fails | Logged, continues to activities/slots | Partial cleanup |
| Stale activity marking fails | Logged, continues to slots | Partial cleanup |
| Redis slot cleanup fails | Logged, cycle ends | Partial cleanup |
| Entire cleanup cycle crashes | Logged, next cycle still runs | Temporary gap |
| Service start fails | Logged in lifespan, backend starts normally | No auto-cleanup |
| CancelledError in sleep/cleanup | Loop exits gracefully | Normal shutdown |

| API Error Case | HTTP Status | Message |
|----------------|-------------|---------|
| Not admin | 403 | Access forbidden |
| Not authenticated | 401 | Not authenticated |

## Architecture Notes

### Resilience Design
- Each of the seven cleanup steps is independently wrapped in try/except
- Watchdog reconciliation has per-execution error isolation within its step
- One step failing does not prevent the others from running
- The background loop survives individual cycle failures
- Backend startup is not blocked if the cleanup service fails to start

### Timeout Values
Execution and activity timeouts were increased to 120 minutes (SCHED-ASYNC-001) to support long-running scheduled tasks (10-60+ min). Redis slot TTL remains at 30 minutes since slots are released by TaskExecutionService on completion.
- Executions: `EXECUTION_STALE_TIMEOUT_MINUTES = 120`
- Activities: `ACTIVITY_STALE_TIMEOUT_MINUTES = 120`
- Slots: `SLOT_TTL_SECONDS = 1800` (30 minutes)

### No Frontend Dependency
This is a purely backend service. The only "UI" is the two admin API endpoints under `/api/monitoring/` which can be invoked via Swagger UI at `http://localhost:8000/docs`.

## Testing

### Prerequisites
- Backend running (`docker-compose up backend`)
- Redis running (`docker-compose up redis`)
- Admin credentials available

### Test Steps

1. **Verify service starts on boot**
   **Action**: Check backend startup logs
   **Expected**: Log line "Cleanup service started"
   **Verify**: `docker-compose logs backend | grep "Cleanup service started"`

2. **Check cleanup status**
   **Action**: `GET /api/monitoring/cleanup-status` with admin token
   **Expected**: Returns running=true, interval_seconds=300
   **Verify**: `last_run_at` is set (startup sweep ran)

3. **Trigger manual cleanup**
   **Action**: `POST /api/monitoring/cleanup-trigger` with admin token
   **Expected**: Returns status="completed" with report
   **Verify**: All counts are 0 if no stale resources

4. **Verify stale execution cleanup**
   **Action**: Create an execution record with `status='running'` and `started_at` > 30 min ago
   **Expected**: Next cleanup cycle marks it as `status='failed'`
   **Verify**: Check `error` field contains "Marked as failed by cleanup"

5. **Verify stale activity cleanup**
   **Action**: Create an activity with `activity_state='started'` and `started_at` > 30 min ago
   **Expected**: Next cleanup cycle marks it as `activity_state='failed'`
   **Verify**: Check `error` field contains "Marked as failed by cleanup"

## Related Flows
- [parallel-capacity.md](parallel-capacity.md) - Slot service that cleanup calls into
- [task-execution-service.md](task-execution-service.md) - Creates executions that may become stale
- [activity-stream.md](activity-stream.md) - Creates activities that may become stale
- [agent-monitoring.md](agent-monitoring.md) - Monitoring router hosts the cleanup endpoints
- [scheduler-service.md](scheduler-service.md) - Scheduler creates executions that cleanup recovers

## File Summary

| File | Role |
|------|------|
| `src/backend/services/cleanup_service.py` | Service class, watchdog reconciliation, Phase 3 re-verification (Issue #378), and global instance |
| `src/backend/db/schedules.py` | `get_running_executions_with_agent_info()` (Issue #129), `mark_execution_failed_by_watchdog()` (Issue #129), `mark_stale_executions_failed()`, `mark_execution_dispatched()`, `mark_no_session_executions_failed()` (Issue #106), `fail_stale_slot_execution()` (Issue #219), `finalize_orphaned_skipped_executions()` (Issue #106), residual-race observability log in `update_execution_status()` (Issue #378) |
| `src/backend/db/activities.py` | `mark_stale_activities_failed()` |
| `src/backend/database.py` | Delegation methods on DatabaseManager |
| `src/backend/services/slot_service.py` | `cleanup_stale_slots()` Redis cleanup, returns reclaimed IDs (Issue #219), `release_slot()` used by watchdog |
| `src/backend/services/execution_queue.py` | `force_release()` used by watchdog for queue state cleanup |
| `src/backend/main.py` | Import, start in lifespan, stop on shutdown, wire WS manager |
| `src/backend/routers/monitoring.py` | `/cleanup-status` and `/cleanup-trigger` endpoints |
| `docker/base-image/agent_server/routers/chat.py` | `/api/executions/{id}/last-error` endpoint for error context retrieval (Issue #286) |
| `docker/base-image/agent_server/services/process_registry.py` | `get_last_error()` method scans log buffer for errors (Issue #286) |
| `tests/test_cleanup_service.py` | API integration tests for cleanup (Issue #106) |
| `tests/test_watchdog.py` | API integration tests for watchdog fields (Issue #129) |
| `tests/test_watchdog_unit.py` | Unit tests for watchdog reconciliation logic (Issue #129), error context tests (Issue #286), Phase 3 re-verify tests (Issue #378) |
| `tests/unit/test_schedule_status_observability.py` | Residual-race observability log tests (Issue #378) |

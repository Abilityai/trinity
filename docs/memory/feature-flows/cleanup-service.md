# Feature: Cleanup Service (CLEANUP-001)

> **Updated 2026-08-28 (#2433):** Proof-of-life is now **two-sided** — an admitted row is an orphan only when the agent does not know it **and** no live backend dispatcher owns it. The agent-known set (`_extract_agent_known_ids`) gains `pending_ids`; the dispatcher half is read tri-state (`alive` / `absent` / `unknown`) from `agent_call_limiter.inflight_verdicts` with ONE `MGET` per sweep and applied at the periodic watchdog, the Phase-3 slot re-verify and the startup recovery. Withheld rows are counted in `CleanupReport.dispatch_inflight_skipped` (in `to_dict()`, deliberately NOT in `total`); the orphan error string states what was observed (`_orphan_error_message`). The marker `execution:inflight:{execution_id}` is only READ here — the limiter's refresher writes it. See [Watchdog Reconciliation](#watchdog-reconciliation-issue-129), [Phase 3](#phase-3-slot-reclaim-re-verification-issue-378), [Startup Recovery](#startup-recovery-recover_orphaned_executions) and [Redis Operations](#redis-operations).

> **Updated 2026-07-17 (#1449):** `_sweep_retention_772` gained an **unconditional** `backlog_metadata` PII scrub sub-sweep (`db.scrub_terminal_backlog_metadata`) — NULLs the drain-replay blob (`user_message`/`user_email`/`system_prompt`) on authoritative-terminal rows (`success`/`cancelled`/`skipped`). It is a **security invariant, not age-gated** (no ops-config window — a fixed default avoids the #1638 floor-by-seed trap) and runs every cycle even when all #772 windows are `0`. **FAILED is excluded** (resurrectable to SUCCESS via a late token-gated CAS). Count feeds `CleanupReport.backlog_metadata_scrubbed` + the WAL-checkpoint sum; the blob itself is never logged (count-only). See the extended Retention-sweeps step below.

> **Updated 2026-05-17 (#869):** `WATCHDOG_HTTP_TIMEOUT` increased from 5.0 → 15.0 seconds to handle agents under load. `SlotService._cleanup_stale_slots_for_agent` now reads each slot's `timeout_seconds` from its per-slot metadata HASH (stored at acquire time) instead of using the agent-level default for all slots. Per-slot TTL = `stored_timeout + SLOT_TTL_BUFFER`; falls back to the per-agent default when metadata is absent.

> **Updated 2026-05-11 (#686):** The interactive `chat_with_agent()` handler in `routers/chat.py` is now a second producer of the `claude_session_id='dispatched'` sentinel (parallel of #279). The no-session sweep's correctness assumption now extends to interactive `/chat` executions too. See the updated [`mark_execution_dispatched`](#mark_execution_dispatched-scheduleoperations) and [Fast-fail no-session executions](#cleanup-cycle-run_cleanup) sections.

> **Updated 2026-04-26 (#428):** Stale-slot reclaim and watchdog release now go through [`CapacityManager`](capacity-management.md) — `capacity.reclaim_stale(agent_timeouts)` replaces `slot_service.cleanup_stale_slots(...)` and `capacity.release_if_matches(agent, exec_id)` replaces the prior pair of `slot_service.release_slot` + `execution_queue.force_release_if_matches`. Recovery (`_recover_execution`) calls `capacity.release(...)`. `ExecutionQueue` is gone; the TOCTOU-safe match check lives on the new facade.

## Overview
Background service that periodically recovers stuck intermediate states. Includes active watchdog reconciliation (Issue #129) that checks agent process registries, recovers orphaned executions, auto-terminates timed-out executions, and releases capacity. Also marks stale executions, activities, and Redis slots as failed. Runs every 5 minutes with an immediate startup sweep. Since #2433 the watchdog's proof-of-life is **two-sided** — the agent registry (running ∪ recently-completed ∪ pending) OR a live backend dispatcher — so an admitted execution parked in the backend agent-call queue, or accepted-but-not-yet-spawned on the agent, is never falsely orphaned.

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
WATCHDOG_HTTP_TIMEOUT = 15.0          # Timeout for agent HTTP calls during reconciliation (#869: increased from 5s to handle agents under load)
WATCHDOG_MIN_AGE_SECONDS = 60         # Dispatch grace: never orphan-recover a row younger than this
STARTUP_RECOVERY_GRACE_SECONDS = 15   # #748: startup recovery skips rows younger than this (a booting handler may be about to ZADD its slot)
ERROR_FETCH_TIMEOUT = 2.0             # Issue #286: timeout for fetching error context from agent
MAX_ERROR_MESSAGE_LENGTH = 2000       # Issue #286: truncate combined error messages
```

The #2433 in-flight bounds are owned by `services/agent_call_limiter.py`, not by this module: `INFLIGHT_MARKER_TTL_SECONDS = 60`, `INFLIGHT_TICK_SECONDS = 15.0`, `INFLIGHT_MARKER_GRACE_SECONDS = 5.0` (entries younger than this never touch Redis), `DISPATCH_RESTAMP_THRESHOLD_SECONDS = 5.0`, `INFLIGHT_REDIS_RETRY_SECONDS = 30.0` (negative cache after a Redis failure), `INFLIGHT_DEADLINE_SLACK_SECONDS = 60.0`. The watchdog consumes exactly two of its functions: `inflight_verdicts()` and `inflight_max_age_seconds()`.

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
- `ssh_credentials_expired: int` - Expired ephemeral SSH keys removed from agent `authorized_keys` — Issue #1616
- `activities_closed_on_recovery: int` - Dispatch activities closed by a CAS-winning recovery path — Issue #1804 (observability; NOT in `total`)
- `dispatch_inflight_skipped: int` - Rows the periodic watchdog / Phase-3 re-verify would have orphaned but withheld because a live backend dispatcher owns them (`alive`) or the cross-worker marker could not be asked (`unknown`) — Issue #2433. Serialized by `to_dict()`, **deliberately NOT summed into `total`** (a skip is not a recovery)
- `total` property: Sum of the recovery/prune counters (the two observability counters above are excluded)
- `to_dict()` method: Serializes for API responses

> **Note (stale):** this doc enumerates the original cycle; many sweeps have since
> been added (soft-delete purge #834, volume reclaim #1581, operator_queue
> retention #1142, ephemeral GC, lease-reaper #429/#1402, expired-SSH #1616, …) and
> the `CleanupReport` has grown accordingly. The **authoritative, current sweep
> list** is the `cleanup_service` row in the [architecture.md Background Services
> table](../architecture.md). The expired-SSH sweep (`_sweep_expired_ssh_credentials`,
> #1616) calls `SshService.cleanup_expired_credentials()` to remove an expired key's
> line from the container `authorized_keys` sshd reads — TTL was previously enforced
> only on the Redis metadata; see [ssh-access.md](ssh-access.md).

#### CleanupService class (line 48)
Singleton pattern via global `cleanup_service` instance (line 141).

**State fields**:
- `poll_interval: int` - Configurable interval (default 300s)
- `_task: Optional[asyncio.Task]` - Background asyncio task
- `_running: bool` - Service running flag
- `last_run_at: Optional[str]` - ISO timestamp of last run
- `last_report: Optional[CleanupReport]` - Results from last cycle
- `_cycle_count: int` - Cycle counter gating hourly maintenance (#476; every 12th cycle @ 5-min interval)

**Methods**:
- `start()` (line 58): Creates asyncio task for `_cleanup_loop()`, sets `_running = True`
- `stop()` (line 66): Sets `_running = False`, cancels task
- `run_cleanup()` (line 74): Single cleanup cycle (called by loop and manual trigger)
- `_cleanup_loop()` (line 114): Main loop - runs initial sweep, then sleeps `poll_interval` between cycles

### Cleanup Cycle (`run_cleanup`)

Nine sequential operations plus an hourly maintenance gate, each wrapped in individual try/except. Watchdog runs FIRST to release resources before passive cleanup:

0. **Watchdog: reconcile DB vs agent process registries** (Issue #129, #226)
   ```python
   orphaned, terminated, confirmed_running_ids = await self._reconcile_orphaned_executions(report)
   ```
   Runs first so it can release capacity slots and queue state before the stale cleanup marks executions failed without resource cleanup. `report` is passed since #2433 so withheld in-flight rows are counted (`dispatch_inflight_skipped`). Also returns `confirmed_running_ids` (#226) — executions verified as still running on agents within their timeout — so slot cleanup doesn't falsely fail them. See [Watchdog Reconciliation](#watchdog-reconciliation-issue-129) below.

1. **Mark stale executions as failed** (safety net for agent-unreachable cases)
   ```python
   count = db.mark_stale_executions_failed(EXECUTION_STALE_TIMEOUT_MINUTES)
   ```
   Calls `DatabaseManager.mark_stale_executions_failed()` which delegates to `ScheduleOperations.mark_stale_executions_failed()`.

2. **Fast-fail no-session executions** (Issue #106)
   ```python
   count = db.mark_no_session_executions_failed(NO_SESSION_TIMEOUT_SECONDS)
   ```
   Marks `running` executions with `claude_session_id IS NULL` older than 60 seconds as failed. These are silent launch failures where the backend failed to dispatch to the agent. Note: both `TaskExecutionService` (step 3b, for `/task` / public chat / scheduled executions) AND the `chat_with_agent()` handler in `routers/chat.py` (added in #686 as the parallel of #279, for interactive `/chat` executions) set `claude_session_id='dispatched'` before their agent HTTP calls — so only executions that never reached dispatch are caught here. Both `/task` and `/chat` codepaths are protected from this false-fail sweep.

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
   Calls `SlotService.cleanup_stale_slots()` with per-agent timeouts (#226). The service scans all `agent:slots:*` keys and checks each slot individually (#869): it reads the slot's `timeout_seconds` from its metadata HASH (key `agent:slot:{name}:{execution_id}`, stored at acquire time), computes `effective_ttl = stored_timeout + SLOT_TTL_BUFFER (5 min)`, and removes the slot if its score (timestamp) is older than that TTL. Falls back to the per-agent default TTL (`execution_timeout_seconds + SLOT_TTL_BUFFER`, or `DEFAULT_SLOT_TTL_SECONDS = 1200` if no agent timeout is configured) when metadata is absent. This fixes premature reclamation of slots acquired with a schedule-level `timeout_seconds` that exceeds the agent-level default (e.g., a 7200s schedule on an agent with the 3600s default was previously reclaimed at ~3900s). Returns a dict mapping agent names to reclaimed execution IDs. Phase 3 is then implemented by `_process_stale_slot_reclaims()` — see [Phase 3 Slot Reclaim Re-verification](#phase-3-slot-reclaim-re-verification-issue-378) below.

6. **Hourly maintenance: prune rate-limit events** (Issue #476)
   ```python
   if self._cycle_count % 12 == 0:
       pruned = db.cleanup_old_rate_limit_events()
   self._cycle_count += 1
   ```
   Runs on cycle 0 (first sweep after boot) and every 12th cycle thereafter — so roughly hourly at the 5-min cleanup interval. Deletes rows from `subscription_rate_limit_events` with `occurred_at < iso_cutoff(24)`. Wired here after #476 confirmed `cleanup_old_rate_limit_events()` had zero production callers; without this, the SUB-003 rate-limit events table would grow unbounded once the lexicographic-compare fix started letting events age correctly.

7. **Retention sweeps: execution_log + execution rows + health_checks** (Issue #772)
   ```python
   log_days, row_days, hc_days = _read_retention_settings()
   if log_days > 0:
       report.execution_logs_pruned = db.prune_execution_logs(log_days, RETENTION_CHUNK_SIZE_PER_CYCLE)
   if row_days > 0:
       report.execution_rows_pruned = db.prune_execution_rows(row_days, RETENTION_CHUNK_SIZE_PER_CYCLE)
   if hc_days > 0:
       report.health_checks_pruned = db.cleanup_old_health_records(hc_days, RETENTION_CHUNK_SIZE_PER_CYCLE)
   ```
   Three independent sweeps, each gated on its own ops-config retention window (`execution_log_retention_days` default 30, `execution_row_retention_days` default 90, `health_check_retention_days` default 7). `0` disables the corresponding sweep. Per-cycle row budget capped at `RETENTION_CHUNK_SIZE_PER_CYCLE = 5000` so the first post-deploy backfill spreads across multiple ticks rather than holding the write lock end-to-end. All cutoffs use `iso_cutoff()` (Architectural Invariant #16). The two execution sweeps share the partial index `idx_executions_completed_terminal ON schedule_executions(completed_at) WHERE status IN ('success','failed','cancelled','skipped')` (fix: #862 — original #772 used wrong values 'completed'/'terminated' that never matched real rows).

   **`backlog_metadata` PII scrub (#1449)** — a fourth sub-sweep, appended to `_sweep_retention_772` but **NOT gated on any retention window**:
   ```python
   report.backlog_metadata_scrubbed = db.scrub_terminal_backlog_metadata(RETENTION_CHUNK_SIZE_PER_CYCLE)
   ```
   `backlog_service.enqueue` `json.dumps`es the full drain-replay request — `user_message`/`user_email`/`system_prompt` — into `schedule_executions.backlog_metadata` for queued-task reconstruction. Nothing reads it once a row leaves `status='queued'` (the drain claims only queued rows; the #1083/#1081 result callbacks read the POST payload, not the row's metadata; canary E-04/G-04 are queued-scoped), so on a terminal row it is stale PII bounded only by the 90-day row DELETE. `db.scrub_terminal_backlog_metadata` (`db/schedules.py`) NULLs it via a chunked SELECT-ids-then-`UPDATE … SET backlog_metadata=NULL` (each chunk its own txn), scoped to `status IN ('success','cancelled','skipped')` — the `_AUTHORITATIVE_TERMINALS` set. **FAILED is EXCLUDED**: a FAILED row is resurrectable to SUCCESS via a late token-gated CAS (`park_expired_lease` keeps its `claim_token`), so its drain-replay intent must survive; FAILED PII stays bounded by the row DELETE. The scrub runs **unconditionally** (a security invariant, not an operator knob — a fixed default sidesteps the #1638 floor-by-seed trap) and logs **count-only** (the blob carries PII and is never logged).

8. **WAL checkpoint after reclaim** (Issue #772)
   ```python
   # actual sum also includes soft-delete / idempotency / agent_reports /
   # operator_queue counts; backlog_metadata_scrubbed (#1449) is in it too
   if (report.execution_logs_pruned + report.execution_rows_pruned
           + report.backlog_metadata_scrubbed + report.health_checks_pruned + ...) > 0:
       _wal_checkpoint_truncate()  # PRAGMA wal_checkpoint(TRUNCATE)
   ```
   Returns freed pages to the OS so the on-disk size actually shrinks. `backlog_metadata_scrubbed` is folded into the sum so a scrub-only cycle still truncates the WAL (#1449). Cheap — runs only when a retention sweep produced work. Full `VACUUM` is delegated to a separate daily APScheduler job in `services/db_vacuum_service.py` (04:30 UTC, autocommit connection) because VACUUM holds an exclusive lock and is unsuitable for the 5-min cadence.

### Watchdog Reconciliation (Issue #129)

Active reconciliation of DB execution state against agent process registries. Replaces the passive "detect-and-report" model with active remediation.

**Since #2433 proof-of-life is two-sided.** An admitted row (`status='running'`, capacity slot held, `claude_session_id='dispatched'`) is an orphan only when **the agent does not know it AND no live backend dispatcher owns it**. Before, three places an admitted row could wait were invisible to `GET /api/executions/running`: the backend's global agent-call semaphore (`agent_call_limiter`, acquired inside `agent_post_with_retry` *after* the row is `running`), the agent's thread pool / `/api/chat` execution lock (accepted but not yet spawned), and the post-exit drain before `unregister()`. All three matched the old predicate (absent from the agent, ≥60s old) → false FAILED, slot released, turn ran anyway. The agent half is closed by `pending_ids` + the widened `recently_completed_ids`; the backend half by the limiter's in-flight registry + cross-worker marker, read tri-state.

#### Proof-of-life helpers (#2433, module level)

| Helper | Role |
|---|---|
| `_KnownIds(set)` | The agent-known id set, tagged `reports_pending: bool` — whether the agent's image reported `pending_ids`, so the orphan string says "not pending" only when that was actually observed |
| `_extract_agent_known_ids(payload)` | The single parser of `/api/executions/running` for the periodic watchdog AND the startup recovery: `executions[].execution_id` ∪ `recently_completed_ids` (#921) ∪ `pending_ids` (#2433 — ids accepted at `/api/task` / `/api/chat` / the #1083 async spawn but not yet spawned). Each list field is read only when it is a `list`/`tuple`/`set` (a stray string would be iterated as characters); an image without a field degrades to the prior behaviour (no `pending_ids` ⇒ pre-#2433, no `recently_completed_ids` ⇒ pre-#921) |
| `_inflight_verdicts` | Bound once at module level from `agent_call_limiter.inflight_verdicts` (eager import of a stdlib leaf). A call-time lazy import is the learnings-2026-08-12 stub-leak shape — under a leaked `sys.modules` MagicMock every candidate would read as "in flight" and the watchdog would silently skip every row. Tests patch `cleanup_service._inflight_verdicts`, never the package attribute |
| `_inflight_verdict_map(ids)` | `{execution_id: "alive" / "absent" / "unknown"}` for the non-empty string ids, ONE call (one Redis `MGET`) per invocation. Guards stub leaks and fails open: a raise → WARNING + every id `absent` (pre-#2433 behaviour); a non-dict result, or any value outside the three verdicts → `absent` |
| `_inflight_skip(verdict, age_seconds)` | Should orphan recovery be withheld? `alive` → yes; `unknown` → only while `age_seconds < agent_call_limiter.inflight_max_age_seconds()` (queue-wait bound + widest HTTP timeout 7260s + 60s slack = 10920s at the default 3600s queue timeout — an older row cannot be owned by any dispatcher and is orphaned regardless); `absent` → no |
| `_row_age_seconds(execution)` | Age from `started_at`; unparseable → `+inf`, so an `unknown` verdict never withholds recovery on garbage |
| `_orphan_error_message(agent_name, agent_reports_pending)` | The honest string: `Execution not tracked by agent '<name>' (not running, not pending, not recently completed) and no live backend dispatcher (not parked in this worker, no cross-worker marker) — recovered by watchdog` — `not pending` is omitted when the image did not report `pending_ids`. Replaces "Execution completed on agent but status not reported", which asserted a completion for a row the agent never received |

Verdict semantics (`agent_call_limiter.inflight_verdicts`): `alive` = this worker's in-process registry holds the id (exact — `track_inflight_dispatch` registers every outbound call for its whole lifetime: queue wait, connect retries, POST) OR the cross-worker marker `execution:inflight:{execution_id}` exists; `absent` = neither and Redis answered, or no Redis client at all in this process (the in-process registry is then the whole truth); `unknown` = an established client raised or timed out — the breaker client has 1s socket timeouts, so a *slow* Redis is exactly this split state, and it must not read as "no" (#2196's rule: a read that could not be asked ≠ a read that said no). The marker is liveness, not state: a dead worker stops refreshing, the marker lapses within 60s, and the next sweep orphans the row as before (the #408 dead-coroutine class is unchanged). Marker details in [Redis Operations](#redis-operations).

#### `_reconcile_orphaned_executions(report=None)` → `tuple[int, int, set]`
1. Query `db.get_running_executions_with_agent_info()` — LEFT JOINs `schedule_executions` with `agent_schedules` and `agent_ownership` for timeout resolution: `COALESCE(schedule.timeout, agent.timeout, 900)`
2. Group executions by `agent_name`
3. Parallel fan-out: `asyncio.gather` queries all agents concurrently via shared `httpx.AsyncClient`
4. Each agent queried via `GET http://agent-{name}:8000/api/executions/running` → `_extract_agent_known_ids` (running ∪ recently-completed ∪ pending, tagged `reports_pending`); unreachable / non-200 → `None`
5. **Collect first, read once** (#2433): every row absent from its (reachable) agent's known set is collected as a candidate, then `_inflight_verdict_map(candidates)` runs ONCE — one Redis `MGET` per sweep, never per row
6. Decision matrix per execution (age measured from `started_at`, which a park of ≥5s re-stamps at grant — see architecture.md § In-Flight Dispatch Proof-of-Life):

| Agent reachable? | In agent's known set? | Dispatcher verdict | Age | Action |
|---|---|---|---|---|
| No (ConnectError/Timeout/non-200) | — | — | — | **SKIP** (retry next cycle; its rows are not even candidates) |
| Yes | No | — | < 60s (`WATCHDOG_MIN_AGE_SECONDS`) | **SKIP** (dispatch grace window) |
| Yes | No | `alive` | ≥ 60s | **WITHHELD** (#2433) — no terminal write, no slot release, no broadcast; `dispatch_inflight_skipped += 1` |
| Yes | No | `unknown` | ≥ 60s and < `inflight_max_age_seconds()` | **WITHHELD** (#2433) — counted as above; the per-agent line logs at WARNING |
| Yes | No | `unknown` | ≥ `inflight_max_age_seconds()` | **ORPHAN RECOVERY** — no dispatcher can still own it |
| Yes | No | `absent` | ≥ 60s | **ORPHAN RECOVERY** with `_orphan_error_message(...)` |
| Yes | Yes | — | ≤ timeout | **CONFIRMED RUNNING** (#226) |
| Yes | Yes | — | > timeout, terminate succeeds | **AUTO-TERMINATE** |
| Yes | Yes | — | > timeout, terminate fails | **SKIP** (defer to 120-min stale cleanup) |

7. **Per-execution error isolation**: each recovery in its own try/except
8. **Systemic failure detection**: warns if >50% of actual recovery attempts fail in a cycle (only counts orphan/terminate attempts, not healthy executions checked)
9. **Concurrency guard**: `asyncio.Lock` prevents overlapping cleanup cycles from background loop + manual trigger
10. **Aggregated skip log** (#2433): ONE line per agent per cycle, never per row (under a backlog a per-row line would print every parked execution every 5 minutes) — `[Watchdog] Withheld orphan recovery on '<agent>': N execution(s) owned by a live backend dispatcher (parked/in-flight), M with an unreadable cross-worker marker (Redis) (#2433)`, at WARNING when M > 0, else INFO
11. **Returns third element** (#226): `confirmed_running` set — execution IDs verified as still running on agent within their timeout. Slot cleanup uses this to avoid falsely failing executions that are legitimately running. `report` (the cycle's `CleanupReport`, since #2433) receives the withheld count; `None` from a legacy caller just skips the counter.

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

**#2433:** the orphan branch passes `_orphan_error_message(agent_name, agent_reports_pending)` as `error_msg` (the old "Execution completed on agent but status not reported" text is gone). A row withheld by `_inflight_skip` never reaches this helper — no terminal write, no slot release, no `watchdog_recovery` broadcast, no activity close; only the counter and the aggregated log line. The startup path uses the module-level `_recover_execution(execution, agent_name, capacity, stats)` (#1804), not this method.

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
| No | Agent unreachable (None) | **FAIL** — race-guarded `fail_stale_slot_execution` with `"Stale execution — agent '{name}' unresponsive during cleanup re-verify, slot TTL expired (#497)"`. Slot was reclaimed by TTL, so the execution is by construction older than `timeout + buffer`; the race guard (`WHERE status='running'`) preserves any SUCCESS that landed between slot reclaim and this write. (#497) |
| No | Agent says still running | **SKIP** — #378 race closed; agent's own SUCCESS write will land correctly |
| No | Agent does not know it, but the dispatcher verdict is `alive` or `unknown` | **SKIP** (#2433) — one `_inflight_verdict_map([execution_id])` read for the row (rare by construction: the slot had to lapse first); WARNING `Skipping <id> for '<agent>' — slot TTL expired but a backend dispatcher is alive / unverifiable (Redis unreadable) (#2433)`; `dispatch_inflight_skipped += 1`; no terminate, no FAILED write. Any non-`absent` verdict skips — unlike Phase 0, `unknown` carries no age bound here |
| No | Agent does not know it and the dispatcher verdict is `absent` | **FAIL** — terminate (best-effort, #61) + `fail_stale_slot_execution` with phantom-stale error |

4. **No cross-cycle state** — `slot_service.cleanup_stale_slots` removes reclaimed IDs from Redis permanently (`zremrangebyscore`), so a deferred ID cannot reappear in a later cycle's `reclaimed` dict. Any "retry on next cycle" state machine would be dead code. Transiently-unreachable agents now fail immediately (#497) — the prior "wait for Phase 1's 120-min backstop" path produced zombie `running` rows that polluted dashboards under sustained partial-outage.

5. **Residual flicker risk (#497, documented)** — if a force-failed execution's agent later recovers and writes SUCCESS via `update_execution_status`, that path overwrites FAILED per #378's "SUCCESS wins over FAILED" rule. The execution must have run past `timeout + buffer` for its slot to be reclaimed, so this represents a deliverable that exceeded its budget. Follow-up: narrow the SUCCESS-over-FAILED rule to exclude FAILED rows tagged with the cleanup marker if this ever becomes a real operator complaint.

6. **Dispatcher proof-of-life before the FAILED write (#2433)** — the "re-verify confirmed inactive → fail" branch now runs only for an `absent` verdict. The slot itself was already TTL-reclaimed by `cleanup_stale_slots` (nothing here restores it). A parked row should not normally get here — the limiter's refresher renews the slot lease every tick while a call is parked and re-anchors it at grant — so an `alive` row on this branch is a dispatcher mid-call past the slot TTL (an image that does not yet report `pending_ids`, or a lapsed lease); the registry-blind Phase-1 stale sweep (`mark_stale_executions_failed`, `timeout + 300s` from the re-stamped `started_at`) stays the backstop for it.

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

### Startup Recovery (`recover_orphaned_executions`)

Module-level coroutine, awaited once from the `main.py` lifespan on boot (before `mark_startup_recovery_complete()`); it has no `CleanupReport` and returns `{recovered, still_running, skipped_grace, cas_lost, errors, redis_slots_reclaimed, activities_closed}`. Two passes:

1. **SQL → Redis** — `db.get_running_executions()` grouped by agent:
   - Container missing or not `running` → every row outside `STARTUP_RECOVERY_GRACE_SECONDS` (#748) is recovered via the module-level `_recover_execution(execution, agent_name, capacity, stats)` (terminal CAS + `capacity.release` + activity close, #1804); a lost CAS counts as `cas_lost`, not `errors`.
   - Container up → `registry_ids = _extract_agent_known_ids(GET /api/executions/running)` (5s timeout; unreachable → empty set + WARNING). **#2433:** the agent's absent-from-registry, non-grace rows are read with ONE `_inflight_verdict_map` call (one `MGET` per agent); `_inflight_skip(verdict, _row_age_seconds(row))` → counted `still_running` and logged at INFO (`[Recovery] <id> on '<agent>' is owned by a live (or unverifiable) backend dispatcher in another worker — left running (#2433)`); otherwise `_recover_execution`. The worker that just booted has an EMPTY in-process registry, so the cross-worker marker is the only signal that the other uvicorn worker still holds the parked coroutine. After a FULL restart every marker lapses within `INFLIGHT_MARKER_TTL_SECONDS` (60s), so such a row waits at most one periodic sweep instead of being recovered here — the #408 dead-coroutine class is unchanged.
2. **Redis → SQL** (#749) — `_reconcile_orphaned_slots()` ZREMs `agent:slots:*` members whose SQL row is terminal or missing (a backend kill between the slot ZADD and the `finally` ZREM leaks the slot; this pass runs even when SQL has zero running rows).

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
      "dispatch_inflight_skipped": 0,
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
      "dispatch_inflight_skipped": 1,
      "stale_activities": 1,
      "stale_slots": 0,
      "total": 5
    }
  }
  ```
  `dispatch_inflight_skipped` is reported but excluded from `total` (#2433) — the one withheld row above does not change the 5.

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

Called by two codepaths before the agent HTTP call: (1) `TaskExecutionService.execute_task()` step 3b (for `/task`, public chat, and scheduled executions), and (2) `chat_with_agent()` in `src/backend/routers/chat.py:313-321` (for interactive `/chat` executions, added in #686 as the parallel of #279). Sets `claude_session_id='dispatched'` so the no-session cleanup only catches executions that never reached dispatch. On success, `chat_with_agent()` overwrites the sentinel with the real Claude UUID derived from `response_data`/`session_data`/`metadata` via `db.update_execution_status(claude_session_id=real_session_id)` at `routers/chat.py:411-428` (#686 UC1) — for observability and `--resume`-style reattachment.

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
2. For each agent, calls `_cleanup_stale_slots_for_agent()` which iterates all slots via `ZRANGE withscores=True` and returns the reclaimed execution IDs
3. For each slot, reads `timeout_seconds` from its metadata HASH at `agent:slot:{name}:{execution_id}` (stored at acquire time), computes `effective_ttl = stored_timeout + SLOT_TTL_BUFFER`. Falls back to `default_slot_ttl` (per-agent: `execution_timeout_seconds + SLOT_TTL_BUFFER`, or `DEFAULT_SLOT_TTL_SECONDS = 1200` if unconfigured) when metadata is absent (#869)
4. Removes stale entries individually (slot is expired if `score < now - effective_ttl`) via `ZREM`
5. Deletes corresponding metadata keys: `agent:slot:{name}:{execution_id}`
6. Returns the execution IDs so the caller (cleanup service) can fail corresponding DB records

**TTL** (#226, #869): Per-slot, computed from the `timeout_seconds` stored in the slot's metadata HASH at acquire time — this captures the schedule-level timeout actually used (e.g., 7200s), not just the agent-level default (e.g., 3600s). Falls back to the per-agent default (`execution_timeout_seconds + SLOT_TTL_BUFFER`, or `DEFAULT_SLOT_TTL_SECONDS = 1200` if no agent timeout configured) when metadata is absent. Fixes premature reclamation of slots with schedule-level timeouts exceeding the agent-level default.

#### In-flight dispatch markers — READ ONLY here (#2433)
**Key**: `execution:inflight:{execution_id}` — STRING (opaque backend-authored JSON from `_marker_payload`; the watchdog tests only for presence), **TTL 60s** (`INFLIGHT_MARKER_TTL_SECONDS`), refreshed every **15s** (`INFLIGHT_TICK_SECONDS`) by the ONE per-process refresher task in `services/agent_call_limiter.py`, which is the marker's **sole writer and deleter** (an unregister only queues the delete). The cleanup service never writes it. A marker is written for every outbound agent call registered by `track_inflight_dispatch` once the entry is older than `INFLIGHT_MARKER_GRACE_SECONDS` (5s) — a fast acquire never touches Redis — and stops being refreshed past the entry's deadline (`registered_at + queue-wait bound + the call's HTTP timeout + 60s`), so a leaked entry cannot keep a `running` row alive forever. While an entry is `parked` the same tick also renews the capacity-slot lease (`slot_service.renew_slot`).

**Read** (`agent_call_limiter.inflight_verdicts`, via `_inflight_verdict_map`): in-process `_INFLIGHT` first (`alive`, no Redis touched), then ONE `MGET` over the remaining ids in `asyncio.to_thread`, on the fail-open breaker client (`redis_breaker_util.get_breaker_redis`, 1s socket timeouts). A raw value counts as a marker only if `isinstance(raw, (str, bytes))`. No client → `absent`; a raise → `unknown`. The limiter negative-caches a failure for 30s (`INFLIGHT_REDIS_RETRY_SECONDS`) so its 15s refresher stops re-pinging a dead Redis, but this read deliberately **bypasses** that cache (`_get_client(use_negative_cache=False)`): a 5-min sweep landing inside the window asks Redis for real and maps a raise to `unknown` rather than reading "no client" as "no marker" — otherwise the flapping case the tri-state exists for would fail open for exactly one sweep (pinned by `test_watchdog_read_bypasses_the_negative_cache`). Only a process that never obtained a client at all reads `absent`.

**Sibling key**: `execution:cancel:{execution_id}` — the cross-worker cancel flag for a parked dispatch (set by `chat_execution_service.terminate_execution` → `request_cross_worker_cancel`, consumed by the limiter at grant); the cleanup service does not read it. Both keys are execution-keyed and self-expiring, listed as exempt-by-construction in `services/agent_runtime_state.py` (not `agent:*`, so the #1560 name-keyed clear does not apply — clearing either on an agent lifecycle event would strand or un-cancel a call genuinely in flight).

## Side Effects
- **Logging**: Each cleanup cycle logs results at INFO level when resources are cleaned
- **Error Logging**: Individual operation failures logged at ERROR level without stopping other operations
- **WebSocket Broadcasts** (Issue #129): Watchdog recovery events broadcast as `watchdog_recovery` type via `ConnectionManager.broadcast()`
- **Capacity Release** (Issue #129): Watchdog releases Redis capacity slots and execution queue state for recovered executions
- **Agent HTTP Calls** (Issue #129): Watchdog queries agent process registries and may POST terminate commands
- **No Activity Records**: Cleanup itself does not create activity entries (avoids recursion)
- **Withheld recoveries have no side effects** (#2433): a row with an `alive`/`unknown` dispatcher verdict gets no terminal write, no slot release, no `watchdog_recovery` broadcast and no activity close — only `dispatch_inflight_skipped` and one aggregated log line per agent per cycle (WARNING when any verdict was `unknown`)

## Error Handling

| Error Case | Handling | Impact |
|------------|----------|--------|
| Phase 0 watchdog agent unreachable | Skipped, retry next cycle | No false positives |
| Watchdog: row absent from agent, dispatcher verdict `alive` (#2433) | Withheld — no terminal write, no slot release, no broadcast; `dispatch_inflight_skipped += 1`, one aggregated INFO line per agent | No false orphan for a parked / mid-call turn |
| Watchdog: cross-worker marker unreadable — `unknown` (slow/flapping Redis, #2433) | Withheld only while `age < inflight_max_age_seconds()`; aggregated line at WARNING | Never fail-open on a *slow* Redis; a row older than the bound is orphaned regardless |
| No Redis client in this process (#2433) | Reads `absent` — the in-process registry is the whole truth | Same-worker parks stay exact; a genuine cross-worker park may be orphaned (documented residual) |
| `_inflight_verdicts` raises / returns a non-dict / stub leak (#2433) | `_inflight_verdict_map` collapses to `absent` (WARNING on a raise) | Pre-#2433 behaviour; a MagicMock can never read as "everything in flight" |
| Dispatcher worker dies mid-park (#2433) | Marker lapses ≤60s; next sweep orphans with the honest string | The #408 dead-coroutine class is unchanged |
| Phase 3: slot TTL-reclaimed but verdict `alive`/`unknown` (#2433) | FAILED write skipped (WARNING + counter); slot stays reclaimed | Phase-1 stale sweep is the backstop; the refresher renews the lease while parked, so this is the mid-call / old-image case |
| Startup recovery: row owned by the other worker's dispatcher (#2433) | Counted `still_running`, left alone | After a full restart the marker lapses ≤60s; the row waits at most one periodic sweep |
| Phase 3 re-verify agent unreachable | Force-fail via race-guarded writer (#497) | Bounded zombie window; agent recovery + SUCCESS still wins per #378 rule (documented risk) |
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
- The #2433 proof-of-life read degrades in the *safe* direction at every layer: no client ⇒ `absent` (in-process registry still exact), a raise ⇒ `unknown` (withheld only within the dispatcher age bound), a stub/non-dict ⇒ `absent`; a withheld row is never a lost row — the registry-blind Phase-1 stale sweep and the 60s marker TTL bound it

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

6. **Verify a parked dispatch is not orphaned** (#2433)
   **Action**: Set `BACKEND_AGENT_CALL_LIMIT` below the number of concurrent tasks you fire (e.g. 2), start N > limit `/task` calls against one agent, wait > 60s, then `POST /api/monitoring/cleanup-trigger`
   **Expected**: `orphaned_executions` = 0; `dispatch_inflight_skipped` ≥ N − limit; every task ends `success` with its response persisted; the agent's `active_slots` never drops below its actually-running count
   **Verify**: backend log shows one `[Watchdog] Withheld orphan recovery on '<agent>': …` line per cycle (INFO; WARNING only if Redis was unreadable) and no `residual race condition (#378)` lines (a late SUCCESS overwriting a watchdog FAILED)

7. **Verify the honest orphan string** (#2433)
   **Action**: Insert a `running` row (`claude_session_id='dispatched'`, `started_at` > 60s ago) for a running agent that has never seen the id, with no in-flight entry or marker; trigger cleanup
   **Expected**: row → `failed`, `error` = `Execution not tracked by agent '<name>' (not running, not pending, not recently completed) and no live backend dispatcher (not parked in this worker, no cross-worker marker) — recovered by watchdog` (`not pending` absent on an agent image without `pending_ids`)
   **Verify**: the `watchdog_recovery` WS event carries the same `reason`

### Unit Tests (#2433)
- `tests/unit/test_2433_watchdog_inflight.py` — the watchdog half. Imports `services.cleanup_service` inside each test (the `test_watchdog_unit.py` shape — the unit conftest pops `services.*` between collection and test) and patches `cleanup_service._inflight_verdicts` (the module-level binding, never the package attribute). Covers `_extract_agent_known_ids` (∪ `pending_ids`, old-image degrade, non-list fields ignored), `_inflight_verdict_map` (pass-through, stub-leak guard, fail-open on raise), `_inflight_skip` rules, `_orphan_error_message` variants, `CleanupReport` counter in `to_dict` and not in `total`, `_reconcile_orphaned_executions` (alive withholds recovery AND the slot; absent orphans with the honest string; unknown withholds only within the bound; pending-on-agent is proof of life; old-image string does not claim pending was checked), `_process_stale_slot_reclaims` (alive is not stale; absent fails as before), `recover_orphaned_executions` (other worker's live dispatcher → `still_running`; absent → recovered)
- `tests/unit/test_2433_limiter_inflight.py` — the limiter half (fakeredis + injected 10ms tick via `_reset_for_testing` seams). Pins whole-call registration/unregister, `execution_id=None` no-op, one-pipeline refresher writes with the 60s TTL only past the grace, slot renewal only while `parked`, fast acquire never touches Redis, queued deletes flushed even on an empty registry, deadline invisibility + its formula, Redis None / raise never raises (negative-cached, logged once), `inflight_verdicts` alive (in-process) / alive (marker) / absent / unknown (established client raised) / absent (no client) + non-string marker values are not markers, phase parked→calling with `on_granted` only past the re-stamp threshold and a raising `on_granted` never blocking dispatch, cancel-while-parked → `BackendAgentCallCancelled` at grant, cross-worker cancel key honoured, the >5s queue-wait warning on the default branch, docstring default = code default

## Related Flows
- [parallel-capacity.md](parallel-capacity.md) - Slot service that cleanup calls into
- [task-execution-service.md](task-execution-service.md) - Creates executions that may become stale; `agent_post_with_retry` wraps every outbound call in `agent_call_limiter.track_inflight_dispatch`, the producer of the dispatcher proof-of-life the watchdog reads (#2433)
- [capacity-management.md](capacity-management.md) - `CapacityManager` facade and the slot lease the #2433 refresher renews while a call is parked
- [activity-stream.md](activity-stream.md) - Creates activities that may become stale
- [agent-monitoring.md](agent-monitoring.md) - Monitoring router hosts the cleanup endpoints
- [scheduler-service.md](scheduler-service.md) - Scheduler creates executions that cleanup recovers

## File Summary

| File | Role |
|------|------|
| `src/backend/services/cleanup_service.py` | Service class, watchdog reconciliation, Phase 3 re-verification (Issue #378), startup `recover_orphaned_executions`, global instance; #2433 proof-of-life helpers `_KnownIds` / `_extract_agent_known_ids` / `_inflight_verdicts` / `_inflight_verdict_map` / `_inflight_skip` / `_row_age_seconds` / `_orphan_error_message` and `CleanupReport.dispatch_inflight_skipped` |
| `src/backend/services/agent_call_limiter.py` | #2433 in-flight dispatch registry: `track_inflight_dispatch` (whole-call registration), the per-process refresher (sole writer/deleter of `execution:inflight:{execution_id}`, 60s TTL / 15s tick, renews the slot lease while parked), `inflight_verdicts()` (tri-state, one `MGET`) and `inflight_max_age_seconds()` — the only two functions the watchdog calls |
| `src/backend/services/agent_runtime_state.py` | Lists `execution:inflight:*` / `execution:cancel:*` as exempt-by-construction from the #1560 name-keyed clear (execution-keyed, self-expiring) |
| `src/backend/db/schedules.py` | `get_running_executions_with_agent_info()` (Issue #129), `mark_execution_failed_by_watchdog()` (Issue #129), `mark_stale_executions_failed()`, `mark_execution_dispatched()`, `mark_no_session_executions_failed()` (Issue #106), `fail_stale_slot_execution()` (Issue #219), `finalize_orphaned_skipped_executions()` (Issue #106), `scrub_terminal_backlog_metadata()` (Issue #1449), residual-race observability log in `update_execution_status()` (Issue #378) |
| `src/backend/db/activities.py` | `mark_stale_activities_failed()` |
| `src/backend/database.py` | Delegation methods on DatabaseManager |
| `src/backend/services/slot_service.py` | `cleanup_stale_slots()` Redis cleanup, returns reclaimed IDs (Issue #219), `release_slot()` used by watchdog |
| `src/backend/services/execution_queue.py` | `force_release()` used by watchdog for queue state cleanup |
| `src/backend/main.py` | Import, start in lifespan, stop on shutdown, wire WS manager |
| `src/backend/routers/monitoring.py` | `/cleanup-status` and `/cleanup-trigger` endpoints |
| `docker/base-image/agent_server/routers/chat.py` | `/api/executions/{id}/last-error` endpoint for error context retrieval (Issue #286); `/api/executions/running` carries `pending_ids` (#2433) |
| `docker/base-image/agent_server/services/process_registry.py` | `get_last_error()` method scans log buffer for errors (Issue #286); #2433: `register_pending()` / `discard_pending()` / `list_pending_ids()` (accepted-but-not-spawned ids, promoted by `register()`), `list_recently_completed_ids()` widened to exited-but-not-yet-unregistered handles |
| `tests/test_cleanup_service.py` | API integration tests for cleanup (Issue #106) |
| `tests/test_watchdog.py` | API integration tests for watchdog fields (Issue #129) |
| `tests/test_watchdog_unit.py` | Unit tests for watchdog reconciliation logic (Issue #129), error context tests (Issue #286), Phase 3 re-verify tests (Issue #378) |
| `tests/unit/test_schedule_status_observability.py` | Residual-race observability log tests (Issue #378) |
| `tests/unit/test_1449_backlog_metadata_scrub.py` | Real-DB scrub tests: authoritative-terminal NULL-out, FAILED-exclusion, queued/running untouched, chunking/idempotency, canary queued-scope smoke (Issue #1449) |
| `tests/unit/test_cleanup_inner_sweeps.py` | Cleanup-cycle characterization incl. the #1449 sub-sweep (report field, WAL sum, unconditional-run guard) |
| `tests/unit/test_2433_watchdog_inflight.py` | #2433 watchdog half: `_extract_agent_known_ids` ∪ `pending_ids` + non-list guards, `_inflight_verdict_map` stub-leak / fail-open guards, `_inflight_skip` rules, honest-string variants, `CleanupReport` counter in `to_dict` not `total`, reconcile withholds (alive / unknown-within-bound / pending-on-agent) and orphans (absent, old-image string), Phase-3 skip vs fail, startup recovery `still_running` vs recovered |
| `tests/unit/test_2433_limiter_inflight.py` | #2433 limiter half (fakeredis + injected tick): whole-call registration, one-pipeline refresher with the 60s TTL past the grace, slot renewal only while parked, fast acquire never touches Redis, Redis None / raise never raises (negative-cached, logged once), `inflight_verdicts` alive / absent / unknown / no-client + non-string marker guard, deadline invisibility, `on_granted` threshold, cancel at grant + cross-worker cancel key, >5s queue-wait warning on the default branch, docstring default |

# Feature: Task Execution Service (EXEC-024)

> **Updated 2026-06-27 (#792, SUB-003 switch-retry):** `execute_task` now retries the
> triggering execution **once** after a successful SUB-003 subscription auto-switch, so a
> one-shot trigger (manual `…/schedules/{id}/trigger`, webhook, MCP `trigger_agent_schedule`)
> recovers instead of landing FAILED (interactive chat already retried client-side; recurring
> cron recovers next tick). Because `agent_post_with_retry` **returns** the response (never
> `raise_for_status`), a 429/auth is interceptable **pre-raise**, adjacent to the #678 502 block,
> and falls through to the single shared success-parse path by reassigning `response` — no
> duplication. New module helpers: `classify_switch_failure(response)` maps the response to the
> full SUB-003 surface (`429`→rate_limit; `503/401/403/402` or `is_auth_failure` body→auth; else
> None); `_extract_agent_error(response, fallback)` is the shared body→`(msg, metadata)` extractor
> reused by the `except httpx.HTTPError` handler; `_salvage_attempt_cost(metadata)` feeds the #678
> R2 `previous_attempt_cost` rollup. The retry is guarded by a dedicated
> `subscription_switch_attempted` local (NOT `retry_count`, which #678 owns — so the two retry
> reasons never suppress each other) hoisted above the first agent call; the same flag gates the
> `except`-handler switch block so a cascade (retry still failing) does NOT switch a second time.
> The retry **is** the readiness probe — a small `_SWITCH_RETRY_DELAY_S` pre-delay only, no
> circuit-aware `/health` poll (would poison the transport breaker on cold start), no trust in
> `restart_result`'s string status; the retry timeout is capped to the **remaining** original
> budget (`min(remaining, _AUTO_RETRY_MAX_TIMEOUT_S)`). Same `execution_id` ⇒ #1084 `effect_guard`
> dedups wired sinks. Boundaries (follow-ups): the #1083 `DISPATCH_ASYNC` path routes 429s through
> the result-callback (bypasses this sync path); a concurrent switch-lock *loser* (gets `None`)
> doesn't retry. Tests: `tests/unit/test_792_subscription_retry.py`.

> **Updated 2026-05-30 (#526, RELIABILITY-007):** This service is the single
> point where the per-agent **dispatch** circuit breaker records execution
> outcomes (full mechanics in [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md)).
> `execute_task()` reads the combined gate once via `dispatch_breaker_active(agent_name)`
> (global `DISPATCH_BREAKER_ENABLED` master switch AND per-agent
> `circuit_breaker_enabled`; fail-safe False) and threads it through three places:
> (1) the `acquire(..., breaker_enabled=…)` call (Step 2) can now raise
> `CircuitOpen` → a new `except CircuitOpen` arm closes the row
> `FAILED(circuit_open)` with `error_code=CIRCUIT_OPEN` (no agent call, nothing
> enqueued); (2) the **Step 3b** fast-fail now checks BOTH breakers — the transport
> breaker (#631) always, and the dispatch breaker only on the
> `slot_already_held and not dispatch_gate_checked` drain path via a non-probe
> state read (`DispatchBreaker(...).to_dict()["state"] == "open"`), so it never
> double-consumes a half-open probe an upstream `acquire()` gate already admitted;
> (3) outcome recording at the terminals — `_record_dispatch_terminal(agent, enabled, None)`
> at the success terminal (resets the consecutive-failure counter) and the same
> with `error_code=AUTH` at the HTTP-error terminal, **gated on `error_code == AUTH`**
> (D10 — TIMEOUT / AGENT_ERROR never count). On the →open transition the caller
> backgrounds the backlog drain + audit via `_spawn_bg(_fail_backlog_and_audit(agent))`
> (a strong task ref is held so the fire-and-forget drain can't be GC'd mid-flight);
> on →closed it backgrounds a recovery audit. New router param
> `dispatch_gate_checked` (set `True` by the `/task` async + sync routers, which
> already gated at `acquire()`) suppresses the redundant 3b dispatch check. The
> `record_outcome`/drain logic adds no circular import — `dispatch_breaker.py`
> imports neither `capacity` nor `db`; the caller owns the drain. New
> `error_code` value `TaskExecutionErrorCode.CIRCUIT_OPEN`.

> **Updated 2026-05-13 (#678):** Builds on #520 (502 classification) and #531 (drain reordering) by closing the stdout reader-race loophole inside `execute_task`. New gate `_looks_like_reader_race(detail)` at `task_execution_service.py:110-128` matches 502 dict bodies with `num_turns < 5`, `raw_message_count == 0`, `parse_failure_count == 0` and triggers ONE in-line auto-retry reusing the same `execution_id` (around `task_execution_service.py:543`, `:558-602`, `:707`), with both call-side and remote timeout capped at 300s so a long task can't double its wallclock budget; previous-attempt cost is rolled forward via `prev_cost` (`:587`) and accumulated into the salvage write at `:841-847`, and `retry_count=retry_count or None` is persisted at `:707`. The agent server's `_classify_empty_result` (`docker/base-image/agent_server/services/error_classifier.py:341+`) now returns a structured dict body — `{message, metadata, raw_message_count, parse_failure_count, recovery_attempted}` — which the service's HTTPError handler at `task_execution_service.py:768-868` parses via the `partial_metadata` block (`:772`) and salvage cost/context block (`:824-858`), writing `cost=salvage_cost`, `context_used=salvage_context`, `context_max=salvage_context_max` onto the FAILED row at `:856-858` instead of nulling everything. The same salvage shape is mirrored in `routers/chat.py:466-516` (`isinstance(detail, dict)` at `:474`, salvage write at `:514-516`). Shared helper `_compute_context_used(metadata)` at `task_execution_service.py:83-108` is the single context-window math used by both the success path and the salvage path (`routers/chat.py:504`, `task_execution_service.py:832`); migration 59 in `db/schema.py` + `db/migrations.py` adds `schedule_executions.retry_count INTEGER DEFAULT 0`, and `db.get_execution_result()` in `db/schedules.py` surfaces it so the MCP `get_execution_result` tool and executions REST endpoint can display it.

> **Updated 2026-05-11 (#686 UC1):** Interactive `/chat` endpoint now mirrors the service's dispatched-sentinel pattern + real-UUID persistence inline in `routers/chat.py` (parallel of #279). The dispatched-sentinel mechanism is no longer exclusive to `TaskExecutionService.execute_task()`.

> **Updated 2026-04-26 (#428):** Slot acquisition/release now goes through [`CapacityManager`](capacity-management.md) (`acquire(overflow_policy="reject")` + `release()`) rather than calling `SlotService` directly. The `slot_already_held` parameter still applies — routers pre-acquire via `CapacityManager` and pass `slot_already_held=True` so the service's `finally` block remains the single release point.

## Overview
Service that encapsulates the task-execution lifecycle (execution record, slot management, activity tracking, agent HTTP call with retry, credential sanitization, response persistence). Used by most — but not all — execution paths.

## User Story
As the platform, I want task execution paths (authenticated sync tasks, public link chat, scheduled executions) to use a shared orchestration service so that these executions get consistent tracking, slot enforcement, credential sanitization, and dashboard visibility.

## Coverage

> **Important**: Not all execution paths use TaskExecutionService. The table below shows which do and which don't.

| Path | Entry Point | Uses TaskExecutionService? | Notes |
|------|------------|---------------------------|-------|
| Sync parallel task | `POST /api/agents/{name}/task` (sync) | **Yes** | EXEC-024 delegation |
| Async parallel task | `POST /api/agents/{name}/task` (async) | **Yes** | Issue #95: thin wrapper `_run_async_task_with_persistence` in `chat.py`. Router pre-acquires slot (preserves 429-upfront contract) then calls `execute_task(slot_already_held=True, ...)` |
| Public link chat | `POST /api/public/chat/{token}` | **Yes** | Full lifecycle |
| Scheduled execution | `POST /api/internal/execute-task` | **Yes** | Background coroutine wraps service call |
| Interactive chat | `POST /api/agents/{name}/chat` | **No** | Direct agent HTTP call with inline retry in `chat.py`. Calls `db.mark_execution_dispatched()` and persists real `claude_session_id` on success inline (#686) — same protection as the service, just not via `execute_task()`. |
| Process engine | Internal | **No** | Separate `ExecutionEngine` with own status enum |

## Entry Points

This is a **backend service** -- no direct UI entry point. Callers are:

| Caller | File | Endpoint | `triggered_by` |
|--------|------|----------|-----------------|
| Authenticated sync task | `src/backend/routers/chat.py:811` | `POST /api/agents/{name}/task` | `"manual"` or `"agent"` |
| Public link chat | `src/backend/routers/public.py:403` | `POST /api/public/chat/{token}` | `"public"` |
| Dedicated Scheduler | `src/backend/routers/internal.py:188` | `POST /api/internal/execute-task` | `"schedule"` |

## Backend Layer

### Service File

**`src/backend/services/task_execution_service.py`** (431 lines)

#### TaskExecutionResult dataclass (line 42)

```python
@dataclass
class TaskExecutionResult:
    execution_id: str
    status: str                         # TaskExecutionStatus value
    response: str                       # Sanitized response text
    cost: Optional[float] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    session_id: Optional[str] = None    # Claude Code session ID
    execution_log: Optional[str] = None # Sanitized JSON transcript
    raw_response: dict = field(default_factory=dict)
    error: Optional[str] = None
```

Callers inspect `result.status` to decide HTTP response. Status values come from `TaskExecutionStatus` enum (`models.py`). The service never raises for agent-level errors.

#### agent_post_with_retry() (line 61)

Moved from `routers/chat.py`. Module-level async function. Used by:
- `TaskExecutionService.execute_task()` internally (line 249)
- `routers/chat.py` for `/chat` endpoint and `_run_async_task_with_persistence` (the async-mode wrapper introduced by #95; previously named `_execute_task_background`)

```python
async def agent_post_with_retry(
    agent_name: str,
    endpoint: str,
    payload: dict,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    timeout: float = 600.0,
) -> httpx.Response:
```

Exponential backoff: delay = `retry_delay * (2 ** attempt)`. Handles `httpx.ConnectError` for agent servers still booting.

#### terminate_execution_on_agent() (line 120, Issue #61)

When the backend's HTTP client times out waiting for an agent response, this helper kills the orphaned Claude process:

```python
async def terminate_execution_on_agent(
    agent_name: str,
    execution_id: str,
) -> bool:
```

Calls `POST /api/executions/{id}/terminate` on the agent container, which triggers:
1. SIGINT (graceful termination, waits 5s)
2. SIGKILL (force kill if process doesn't respond)

Best-effort: failures are logged but don't raise exceptions. The cleanup service watchdog provides a safety net. Returns `True` for success/already_finished/not_found (404 means process may have finished), `False` for errors.

**Timeout**: 5 seconds (constant `TERMINATE_TIMEOUT`). Short timeout to avoid blocking the failure path.

#### TaskExecutionService.execute_task() (line 209)

Full execution lifecycle in one method:

```
Step  Action                                    Line   Dependency
----  ----------------------------------------  -----  ----------------------------------
1     Create execution record (if not provided)  158    db.create_task_execution()
      [try block starts - #90 fix]              175    Ensures FAILED status on any exception
2     Acquire capacity slot                      178    slot_service.acquire_slot()
3     Track activity start (CHAT_START)          203    activity_service.track_activity()
3b    Mark execution dispatched                  225    db.mark_execution_dispatched()
4     POST to agent /api/task with retry         249    agent_post_with_retry()
5     Sanitize response + execution log          267    sanitize_execution_log(), sanitize_response()
6     Update execution record with result        283    db.update_execution_status()
7     Complete activity                          297    activity_service.complete_activity()
8     Release slot (if acquired, in finally)     412    slot_service.release_slot()
```

> **Step 3b**: Sets `claude_session_id='dispatched'` before the agent HTTP call. This prevents the cleanup service's no-session check from falsely marking long-running executions as "Silent launch failure". Only executions that never reach dispatch (backend crash before step 3b) will be caught by the 60-second no-session cleanup.
>
> **Parallel codepath (#686, 2026-05-11):** The `/chat` endpoint in `src/backend/routers/chat.py` (lines 313-321) now performs the equivalent inline dispatched-mark via `db.mark_execution_dispatched(task_execution_id)` immediately before its agent HTTP call — parallel of #279. On success it also computes the real `claude_session_id` from `response_data.get("session_id")` (falling back to `session_data` then `metadata`) and passes it to `db.update_execution_status()`, replacing the 'dispatched' sentinel with the actual Claude session UUID (#686 UC1 closes the observability gap). Net effect: the no-session sweep protection now applies to interactive chat executions too, even though `/chat` does not go through `TaskExecutionService.execute_task()`.

> **Fix #90**: The try block starts at step 2 (slot acquisition) to ensure any exception updates execution status to FAILED. The `slot_acquired` flag ensures we only release slots that were successfully acquired.

**Signature:**
```python
async def execute_task(
    self,
    agent_name: str,
    message: str,
    triggered_by: str,                      # "manual"|"public"|"schedule"|"agent"|"mcp"
    source_user_id: Optional[int] = None,
    source_user_email: Optional[str] = None,
    source_agent_name: Optional[str] = None,
    source_mcp_key_id: Optional[str] = None,
    source_mcp_key_name: Optional[str] = None,
    model: Optional[str] = None,
    timeout_seconds: Optional[int] = None,  # TIMEOUT-001: None = use agent's config (default 15 min)
    resume_session_id: Optional[str] = None,
    allowed_tools: Optional[list] = None,
    system_prompt: Optional[str] = None,
    execution_id: Optional[str] = None,
    fan_out_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    parent_activity_id: Optional[str] = None,       # Issue #95: CHAT_START parent linkage
    extra_activity_details: Optional[dict] = None,  # Issue #95: merged into CHAT_START details
    slot_already_held: bool = False,                # Issue #95: async path pre-acquires slot upfront
    images: Optional[list] = None,                  # #562: vision content blocks for channel images
) -> TaskExecutionResult:
```

**Issue #95 params** (added 2026-04-13):
- `parent_activity_id`: set by the async `/task` router to the collaboration activity id so the CHAT_START is parented for agent-to-agent call graphs.
- `extra_activity_details`: merged into the CHAT_START `details` dict. The async `/task` router passes `{parallel_mode: True, async_mode: True, model, timeout_seconds}` to keep the frontend Network view filter at `src/frontend/src/stores/network.js:255` working.
- `slot_already_held`: when `True`, the service skips slot acquisition (but still releases in finally). The async `/task` router pre-acquires the slot synchronously so at-capacity returns HTTP 429 upfront, preserving the client contract. Other callers leave this `False` and the service both acquires and releases.

**TIMEOUT-001**: When `timeout_seconds` is `None`, the service reads the agent's configured timeout via `db.get_execution_timeout(agent_name)`. Default agent timeout is 900 seconds (15 minutes).

If `execution_id` is provided, the caller has already created the execution record (e.g. `chat.py` creates it early for async-mode support). Otherwise the service creates one.

#### get_task_execution_service() (line 426)

Global singleton accessor. Lazy-initializes on first call.

### Caller 1: Authenticated Sync Task

**`src/backend/routers/chat.py:653-917`** -- `execute_parallel_task()`

The endpoint handles:
1. Container validation (lines 678-683)
2. Determine `triggered_by` from headers (lines 686-691)
3. Create execution record early (lines 694-705) -- passed to service as `execution_id`
4. Collaboration tracking for agent-to-agent (lines 710-732) -- stays in router
5. **Async mode branch** -- pre-acquires capacity slot, then spawns `_run_async_task_with_persistence()` which delegates to `task_execution_service.execute_task(slot_already_held=True)` (post-#95)
6. **Sync mode branch** (lines 810-827) -- delegates to `task_execution_service.execute_task()`
7. Collaboration activity completion (lines 830-839)
8. Error translation to HTTP exceptions (lines 842-857)
9. Chat session persistence if `save_to_session` (lines 863-912)

```python
# Line 810-827
task_execution_service = get_task_execution_service()
result = await task_execution_service.execute_task(
    agent_name=name,
    message=request.message,
    triggered_by=triggered_by,
    source_user_id=current_user.id,
    source_user_email=current_user.email or current_user.username,
    source_agent_name=x_source_agent,
    ...
    execution_id=execution_id,  # Pre-created
)
```

### Caller 2: Public Link Chat

**`src/backend/routers/public.py:262-460`** -- `public_chat()`

The endpoint handles:
1. Link token validation (lines 277-279)
2. Session identity resolution: email or anonymous (lines 282-309)
3. Rate limiting by IP (lines 312-317)
4. Agent container check (lines 320-325)
5. Public chat session management (lines 330-334)
6. Store user message (lines 337-341)
7. Build context prompt with conversation history (lines 351-355)
8. User memory injection for email sessions (lines 358-362)
9. **Async mode branch** (lines 371-400) -- spawns `_execute_public_chat_background()`, returns immediately
10. **Sync mode: delegate to service** (lines 403-410)
11. Error translation to HTTP exceptions (lines 412-429)
12. Store assistant response in public chat messages (lines 434-439)
13. User memory summarization trigger (lines 442-449)

```python
# Lines 403-410
task_execution_service = get_task_execution_service()
result = await task_execution_service.execute_task(
    agent_name=agent_name,
    message=context_prompt,
    triggered_by="public",
    source_user_email=source_email,    # verified_email or f"anonymous ({client_ip})"
    timeout_seconds=900,
    system_prompt=memory_system_prompt,  # MEM-001: per-user memory
)
```

Key behavioral change: public executions now get full tracking that was previously missing -- execution records, activity stream, slot management, credential sanitization, and Dashboard timeline visibility.

## Data Layer

### Database Operations

| Operation | Method | File | Line |
|-----------|--------|------|------|
| Create execution record | `db.create_task_execution()` | `src/backend/database.py:530` | Delegates to `_schedule_ops` |
| Get max parallel tasks | `db.get_max_parallel_tasks()` | `src/backend/database.py:416` | Delegates to `_agent_ops` |
| Update execution status | `db.update_execution_status()` | `src/backend/database.py:574` | Updates status, response, cost, context, logs |
| Get execution (for cancel check) | `db.get_execution()` | `src/backend/database.py:596` | Checks if status is "cancelled" before overwriting |

### Redis Operations

| Operation | Service | Key Pattern |
|-----------|---------|-------------|
| Acquire slot | `SlotService.acquire_slot()` | `agent:slots:{name}` (ZSET), `agent:slot:{name}:{exec_id}` (HASH) |
| Release slot | `SlotService.release_slot()` | Same keys, ZREM + DELETE |

Slot TTL: Dynamic (agent timeout + 5 min buffer). See parallel-capacity.md for details.

## Side Effects

### Activity Tracking

| Event | Type | When |
|-------|------|------|
| Execution start | `ActivityType.CHAT_START` | After slot acquired (line 203) |
| Execution success | `complete_activity(status="completed")` | After response persisted (line 297) |
| Execution failure | `complete_activity(status="failed")` | On any exception (lines 332, 374, 398) |

### WebSocket Broadcasts

Activity events are broadcast via `ActivityService._broadcast_activity_event()`:

```json
{
  "type": "agent_activity",
  "agent_name": "agent-name",
  "activity_id": "uuid",
  "activity_type": "chat_start",
  "activity_state": "started",
  "action": "Processing: message preview...",
  "timestamp": "2026-03-04T12:00:00",
  "details": {
    "message_preview": "...",
    "execution_id": "exec-uuid",
    "triggered_by": "public"
  }
}
```

### Credential Sanitization

Applied before database persistence (defense-in-depth layer):

| Function | Source | Purpose |
|----------|--------|---------|
| `sanitize_execution_log()` | `src/backend/utils/credential_sanitizer.py:154` | Scrub API keys from JSON execution logs |
| `sanitize_response()` | `src/backend/utils/credential_sanitizer.py:172` | Scrub API keys from agent response text |

Patterns: OpenAI keys (`sk-*`), Anthropic keys (`sk-ant-*`), GitHub tokens (`ghp_*`, `github_pat_*`), AWS keys (`AKIA*`), Bearer tokens, and sensitive env var key-value pairs.

## Error Handling

The service catches all errors and returns `TaskExecutionResult` with `status=TaskExecutionStatus.FAILED`. Callers translate to HTTP.

| Error Case | Service Result | chat.py HTTP | public.py HTTP |
|------------|---------------|--------------|----------------|
| Slot not acquired | `status=FAILED, error="Agent at capacity..."` | 429 | 429 |
| Dispatch breaker open (#526) | `acquire()` raises `CircuitOpen` → `status=FAILED, error="circuit_open: agent unhealthy...", error_code=CIRCUIT_OPEN` (no agent call, nothing enqueued) | 503 + `X-Circuit-Open`/`Retry-After` | 503 |
| Agent timeout (#61) | Terminates agent process, then `status=FAILED, error="timed out...", error_code=TIMEOUT` | 504 | 504 |
| Agent signal-killed (#516) | Agent classifies SIGINT/SIGKILL/SIGTERM exit and returns 504 with "Execution terminated by …" detail; service treats as `status=FAILED, error=detail` (no AUTH code) | 504 | 504 |
| Agent empty result (#520) | Agent returns 502 when `return_code == 0` but `cost_usd` and `duration_ms` are both `None` (final `result` JSON line lost — typically a child subprocess inherited stdout); service treats as `status=FAILED, error=detail` (no AUTH code) | 502 | 502 |
| Agent HTTP error | `status=FAILED, error=detail` | 503 | 502 |
| Auth failure (#285) | `status=FAILED, error=detail, error_code=AUTH` | 503 | 503 |
| Unexpected exception | `status=FAILED, error=str(e)` | 503 | 502 |
| Cancelled execution | Preserved -- does not overwrite `CANCELLED` | N/A | N/A |

Cancel protection (lines 324-325, 366-367, 390-391): Before writing failed status, checks `db.get_execution(execution_id)` -- if status is already `TaskExecutionStatus.CANCELLED` (from user termination), the service does not overwrite it.

### Auth Failure Fast-Fail (Issue #285)

When subscription tokens expire, Claude Code sometimes hangs for up to an hour before failing. This wastes execution slots until the watchdog recovers them. Issue #285 adds real-time auth failure detection:

**Agent Server** (`docker/base-image/agent_server/services/claude_code.py`):

1. **Pattern Matcher** (`_is_auth_failure_message()`, line 675): Detects auth failure patterns in stderr:
   - "Invalid API key", "Authentication failed", "401 Unauthorized", "auth failed"
   - Model-specific errors: "does not have access to model", "exceeds context window"

2. **Real-time Stderr Scan** (line 910): Background thread scans Claude Code stderr during execution. When an auth pattern is detected, sets `auth_failure_event` and captures the reason.

3. **Process Kill** (line 935): Main execution loop checks for auth failure event and kills the Claude Code process immediately instead of waiting for timeout.

4. **HTTP 503 Response**: Auth failures return HTTP 503 (Service Unavailable) so the backend can distinguish from other errors.

**Backend** (`src/backend/services/task_execution_service.py`):

1. **Error Code Detection** (line 415): When agent returns HTTP 503, sets `error_code=TaskExecutionErrorCode.AUTH`

2. **Structured Result**: Returns `TaskExecutionResult` with `error_code=AUTH` so callers can handle auth failures specifically (e.g., prompt user to reconfigure subscription)

**Signal-Kill Pre-Check (Issue #516)**: Two heuristics in the agent's auth-fallback block — string-match on the verbose transcript and "zero tokens processed" — used to fire on every external signal kill (timeout SIGKILL, OOM, parent SIGTERM, operator cancel) and surface a misleading "Subscription token may be expired" 503. Since #61 wired backend-driven `terminate_execution_on_agent()` into the timeout path, signal kills became the *common* case for any timeout. `_classify_signal_exit()` (`claude_code.py:894`) now runs *before* the auth heuristics; signal-killed exits raise HTTP 504 (not 503), so the backend's AUTH classifier at line 542 correctly skips them and they flow through to the generic FAILED path with a clear "killed by SIGKILL/SIGTERM/SIGINT — likely timeout, OOM, or operator cancel" detail. Critical when PR #508 (auth-class auto-switch) lands: prevents a misclassified timeout from triggering an unnecessary subscription rotation.

**Empty-Result Pre-Check (Issue #520)**: Sibling of #516 on the *clean* exit path. When `return_code == 0` but the final `{"type":"result"}` JSON line was dropped before the reader thread captured it (cause: a child subprocess inherited stdout, kept the pipe open past claude exit, the reader thread leaked, the pgroup unwind closed the pipe), `metadata.cost_usd` and `metadata.duration_ms` stay `None`. The success path used to return HTTP 200 with empty diagnostics — agent-server logged "completed successfully" while backend silently reaped the execution as an orphan minutes later. `_classify_empty_result()` (`claude_code.py:935`) now runs *after* the `return_code != 0` block (#516 + auth) and *before* response building. When both `cost_usd` and `duration_ms` are `None`, it raises HTTP 502 with diagnostic context (tools, turns, raw_messages, cause hint). Backend's AUTH classifier only triggers on 503, so 502 falls through to the generic FAILED path — no backend changes needed. The two-field check is conservative: single-field nullability could be a Claude format quirk; both-None is a strong signal that the terminal `result` message never arrived. Pairs with the orchestration plan's #408 dissolution: even if the long-running HTTP transport closes mid-call, agent-side now emits a meaningful FAILED status instead of an empty 200 that the watchdog has to reconcile.

**Result**: Auth failures now fast-fail in seconds instead of hanging for up to an hour.

> **Status Enums (#92)**: Execution statuses use `TaskExecutionStatus` (`running/success/failed/cancelled/skipped`). Activity statuses use `ActivityState` (`started/completed/failed`). Both are defined in `models.py`.

## Execution Lifecycle Diagram

```
Caller (chat.py, public.py, or internal.py)
  |
  v
execute_task()
  |
  +-- 1. db.create_task_execution()
  |      (if execution_id not provided)
  |
  +-- 2. slot_service.acquire_slot()
  |      |
  |      +-- FAIL --> return TaskExecutionResult(status="failed")
  |
  +-- 3. activity_service.track_activity(CHAT_START)
  |
  +-- 3b. db.mark_execution_dispatched()
  |       (sets claude_session_id='dispatched' to prevent false cleanup)
  |
  +-- 4. agent_post_with_retry(agent_name, "/api/task", payload)
  |      |
  |      +-- Retries: 3 attempts, exponential backoff (1s, 2s, 4s)
  |      |
  |      +-- httpx.ConnectError --> retry or fail
  |      +-- httpx.TimeoutException --> terminate_execution_on_agent() --> fail (#61)
  |      +-- httpx.HTTPError --> fail
  |
  +-- 5. sanitize_execution_log() + sanitize_response()
  |
  +-- 6. db.update_execution_status(status="success", ...)
  |
  +-- 7. activity_service.complete_activity(status="completed")
  |
  +-- 8. [FINALLY] slot_service.release_slot() (only if slot_acquired=True)
  |
  v
return TaskExecutionResult(status="success", ...)

Note: The entire flow from step 2 onwards is wrapped in a try block (#90 fix).
Any exception updates execution status to FAILED before releasing the slot.
```

## Agent Payload

The service POSTs to `http://agent-{name}:8000/api/task`:

```json
{
  "message": "task content",
  "model": "sonnet",
  "allowed_tools": ["Read", "Write"],
  "system_prompt": "additional instructions",
  "timeout_seconds": 120,
  "execution_id": "uuid",
  "resume_session_id": "claude-session-id"
}
```

Expected response from agent:

```json
{
  "response": "agent answer text",
  "session_id": "claude-code-session-id",
  "metadata": {
    "input_tokens": 1234,
    "output_tokens": 567,
    "cost_usd": 0.05,
    "context_window": 200000,
    "session_id": "claude-code-session-id"
  },
  "execution_log": [
    {"type": "tool_use", "tool": "Read", ...},
    {"type": "tool_result", ...}
  ]
}
```

## Testing

### Prerequisites
- Services running (`./scripts/deploy/start.sh`)
- At least one agent created and running
- A public link configured for the agent

### Test Steps

1. **Action**: Send a message via public link chat
   **Expected**: Execution appears in agent's Tasks tab with `triggered_by: public`
   **Verify**: `GET /api/agents/{name}/executions` includes an entry with source email or "anonymous (IP)"

2. **Action**: Check Dashboard timeline after public link message
   **Expected**: Execution box appears for the agent on the timeline
   **Verify**: Activity stream includes `CHAT_START` event with `triggered_by: "public"`

3. **Action**: Set agent max_parallel_tasks to 1, run an authenticated task, then immediately send a public link message
   **Expected**: Public link returns 429 "Agent is busy"
   **Verify**: Redis `agent:slots:{name}` ZSET has 1 entry

4. **Action**: Run authenticated sync task via Tasks tab
   **Expected**: Execution completes with same metadata as before (no regression)
   **Verify**: Response includes `task_execution_id`, cost, context usage

5. **Action**: Send a public message that triggers tool use (e.g., file read)
   **Expected**: Execution log in Tasks tab has credentials redacted
   **Verify**: No `sk-*`, `ghp_*`, or Bearer tokens in execution_log column

## Related Flows

- [parallel-headless-execution.md](parallel-headless-execution.md) -- the `/task` endpoint this service backs
- [parallel-capacity.md](parallel-capacity.md) -- slot management consumed by this service
- [public-agent-links.md](public-agent-links.md) -- primary beneficiary of unified tracking
- [activity-stream.md](activity-stream.md) -- activity tracking consumed by this service
- [tasks-tab.md](tasks-tab.md) -- UI that displays execution records
- [dashboard-timeline-view.md](dashboard-timeline-view.md) -- timeline that shows execution events
- [continue-execution-as-chat.md](continue-execution-as-chat.md) -- EXEC-023, resume_session_id support
- [scheduler-service.md](scheduler-service.md) -- dedicated scheduler that calls this service via internal API
- [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md) -- #526 per-agent dispatch breaker; this service is its outcome-recording producer (AUTH-only) and owns the drain-on-open backgrounding

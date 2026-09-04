# Feature: Task Execution Service (EXEC-024)

> **Updated 2026-09-01 (#2467, turn-integrity derivation at terminal write):** `apply_result`'s
> SUCCESS branch now derives turn-integrity flags from the transcript it already holds —
> `services/execution_integrity.py::derive_turn_integrity(exec_log, metadata)` (a pure leaf; the
> hotspot delta is ~8 lines by design) scans the CLI's task lifecycle events for background tasks
> **killed at CLI exit** (`task_updated {"status":"killed"}` / `task_notification
> {"status":"stopped"}`, keyed on the kill events because the ledger drains to `[]` before exit)
> and plucks the #2127 waited-path `background_tasks_pending_at_exit` count from metadata. When
> either is present it writes new nullable `schedule_executions.turn_integrity` (TEXT JSON; the
> `update_execution_status` kwarg is **conditional**, the `retry_count` pattern — an unconditional
> `None` would NULL the column on the FAILED→SUCCESS resurrect CAS) and, for kills only, prepends a
> visible notice to `sanitized_resp` — which then reaches the stored response, the returned result,
> the #1578 event summary and the channel completion report from the one variable. NULL ≡ "no
> evidence", never "verified healthy". Backend-side deliberately (the #1741 no-rebuild precedent);
> canonical description + privacy/validation rules:
> [parallel-headless-execution.md](parallel-headless-execution.md) → *The Kill the Gate Correctly
> Does Not Cover (#2467)*. Prior update follows.
>
> **Updated 2026-08-28 (#2433, in-flight dispatch proof-of-life):** an execution that was
> **admitted** (row `running`, capacity slot held, `claude_session_id='dispatched'`) could park in
> the backend agent-call semaphore (`services/agent_call_limiter.py`, acquired inside
> `agent_post_with_retry` *after* Step 3b) where the cleanup watchdog's proof-of-life
> (`GET agent/api/executions/running`) could not see it — a false `failed` after the 60s grace,
> a **released slot**, and a turn that then ran anyway (billed, overbooked, its late 200 silently
> overwriting the row, #378). Canonical description: architecture.md → *In-Flight Dispatch
> Proof-of-Life (#2433)*. This service's share: (1) `agent_post_with_retry(..., execution_id=None)`
> wraps its **whole** retry loop in `track_inflight_dispatch` — the in-process `_INFLIGHT` registry
> plus the refresher-maintained cross-worker Redis marker `execution:inflight:{execution_id}`
> (60s TTL, 15s tick) — so `inflight_verdicts` reads *alive* for the queue wait, the connect-retry
> sleeps and the POST alike; (2) it passes `on_granted=_on_dispatch_granted`, which fires once at
> semaphore grant when the park reached `DISPATCH_RESTAMP_THRESHOLD_SECONDS` (5s) and re-anchors
> the row at dispatch — `db.restamp_execution_dispatch(execution_id)` (CAS on `RUNNING` + NULL
> lease: `started_at = now`, admission kept in `queued_at`) + `slot_service.renew_slot` via
> `asyncio.to_thread` — because every age check (`mark_stale_executions_failed`, canary E-01, the
> watchdog grace, `duration_ms`) and the slot TTL were measured from admission, so a long park
> spent the run's own budget and `park + run > timeout+300` was bulk-FAILed mid-run; (3) all three
> `/api/task` call sites in `execute_task` (first attempt, #678 reader-race retry, #792
> switch-retry) pass `execution_id`, as does the `/api/chat` site in `chat_execution_service`;
> (4) the `except BackendAgentCallBudgetExhausted` branch writes and returns **CANCELLED** for the
> `BackendAgentCallCancelled` subclass — a cancel that landed while parked is also finalized by
> `chat_execution_service._cancel_inflight_if_parked` (agent-scoped; see
> [chat-turn-cancellation.md](chat-turn-cancellation.md)), and the row reads CANCELLED whichever
> writer wins the CAS; the #1804 lost branch closes the activity in the standing state. No schema change, no flag.
> `BACKEND_AGENT_CALL_LIMIT` / `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S` are now forwarded in prod +
> hosted compose and documented in `.env.example` (they were dev-compose-only). Tests:
> `tests/unit/test_2433_dispatch_wiring.py`, `test_2433_limiter_inflight.py`,
> `test_2433_db_restamp.py`, `test_2433_limiter_packaging.py`.

> **Updated 2026-08-16 (#1853, telemetry+transcript on the FAILED row):** the FAILED branch of
> `apply_result` now **mirrors the SUCCESS branch's telemetry** so an `error_during_execution`
> (502) / timeout (504) row is diagnosable via the same API as a SUCCESS row (the substrate for
> the Aug-14 "system agent identifies errors" initiative; TOWARD the #1401 recovery trace). The
> agent's structured error body now carries the full stream-json transcript
> (`headless_executor._execution_error_502_detail` / the extended `_timeout_504_detail`, each with
> a **UUID-validated** `session_id` fallback — `_valid_session_id`, FI-1: `ctx.claude_session_uuid`
> can be an untrusted `resume_session_id`, a log-forging vector). `_extract_agent_error` returns
> that transcript (now a 3-tuple); the `except httpx.HTTPError` handler threads it + `session_id`
> onto the **existing** `TerminalEnvelope.execution_log`/`.session_id` fields (previously unset on
> failure). In `apply_result`, the FAILED branch reuses the SUCCESS branch's identical
> `sanitize_execution_log` + `extract_tool_calls` (#1741 summary, not a 2nd transcript copy) and
> passes `execution_log`/`tool_calls`/`claude_session_id` into the **existing**
> `db.update_execution_status` call (cost/context already salvaged from `metadata`). **Single
> terminal applier (#1483) preserved** — the columns are added to the SET clause **above** the
> `if won` gate: no new CAS writer, `_write_terminal_and_gate` untouched, and every side-effect
> stays gated on `won` (#1578/#1804). No schema change (`execution_log`/`claude_session_id`/
> `tool_calls` already exist). A bare-string old-image body leaves the columns null (graceful
> mixed-fleet degrade). **Residual:** `_write_terminal_and_gate` terminals (backend
> timeout/budget/crash) and standalone-scheduler RETRY-001 FAILED writes still land bare — so the
> Aug-14 initiative must not assume *every* FAILED row carries telemetry. Base-image rebuild + cold
> recreate for the agent half (#1809). Tests: `tests/unit/test_1853_error_telemetry_salvage.py`.

> **Updated 2026-06-27 (#792, SUB-003 switch-retry):** `execute_task` now retries the
> triggering execution **once** after a successful SUB-003 subscription auto-switch, so a
> one-shot trigger (manual `…/schedules/{id}/trigger`, webhook, MCP `trigger_agent_schedule`)
> recovers instead of landing FAILED (interactive chat already retried client-side; recurring
> cron recovers next tick). Because `agent_post_with_retry` **returns** the response (never
> `raise_for_status`), a 429/auth is interceptable **pre-raise**, adjacent to the #678 502 block,
> and falls through to the single shared success-parse path by reassigning `response` — no
> duplication. New module helpers: `classify_switch_failure(response)` maps the response to the
> full SUB-003 surface (`429`→rate_limit; `503/401/403/402` or `is_auth_failure` body→auth; else
> None); `_extract_agent_error(response, fallback)` is the shared body→`(msg, metadata, execution_log)`
> extractor (the 3rd element is the #1853 transcript) reused by the `except httpx.HTTPError`
> handler; `_salvage_attempt_cost(metadata)` feeds the #678
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
> double-consumes a half-open probe an upstream `acquire()` gate already admitted
> — its fast-fail reason is built by `_circuit_breaker_error(transport_open,
> dispatch_open)` (#1557), which names the breaker that fired (transport →
> *unreachable*, dispatch → *auth-dead*) instead of a blanket "agent is unhealthy";
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

> **Updated 2026-09-03 (#2391):** `execute_task` is no longer a `reject`-only producer. `build_pull_queue_payload` returns a `PersistentTaskPayload` when — and only when — `pull_pilot.pull_owns_dispatch(agent, triggered_by)` is true (a `PULL_MODE_PILOT_AGENTS` agent on `schedule` / `webhook` / `reminder`), which selects `overflow_policy="queue_persistent"`; the row is enqueued, `execute_task` returns `TaskExecutionStatus.QUEUED` before any activity, agent call or dispatch marker, and the agent's own worker claims it. Everything else keeps `"reject"` byte-for-byte, so scheduled capacity semantics are unchanged for every non-pilot agent. Two hard preconditions: an existing `execution_id` (the enqueue is a CAS RUNNING→QUEUED on that row) and `slot_already_held=False` (queueing under a held slot would leak it for the lease TTL). #1083 fire-and-forget cannot stack on it — a queued row is never dispatched, so no 202 can arrive.

> **Updated 2026-04-26 (#428):** Slot acquisition/release now goes through [`CapacityManager`](capacity-management.md) (`acquire(overflow_policy="reject")` + `release()`) rather than calling `SlotService` directly. The `slot_already_held` parameter still applies — routers pre-acquire via `CapacityManager` and pass `slot_already_held=True` so the service's `finally` block remains the single release point.

## Overview
Service that encapsulates the task-execution lifecycle (execution record, slot management, activity tracking, agent HTTP call with retry, credential sanitization, response persistence). Used by most — but not all — execution paths.

> **Sync-chat sibling (#1483).** `routers/chat.py`'s `/chat` path does NOT go through `execute_task`: sync-chat has its own `chat_sessions` persistence + collaboration-activity completion + `mode="chat"` prompt. That divergent applier is now `chat_execution_service.run_chat_turn` (extracted from the router, **declared transitional**). The `/task` split delegates its sync/async paths to **this** service's `execute_task` / `apply_result` — the split adds no second terminal applier, keeping the pull-migration single-applier seams (`apply_result`/`_write_terminal_and_gate`/#1083 callback) byte-untouched. Converging `run_chat_turn` onto `execute_task` is a tracked follow-up (a genuine behavior change, out of #1483's scope).

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

#### agent_post_with_retry() (line 522)

Moved from `routers/chat.py`. Module-level async function. Used by:
- `TaskExecutionService.execute_task()` internally — three `/api/task` call sites: first attempt (`:1414`), the #678 reader-race retry (`:1500`), the #792 switch-retry (`:1596`)
- `chat_execution_service.run_chat_turn` for the `/chat` endpoint (`:636`; the async `/task` wrapper `_run_async_task_with_persistence` delegates to `execute_task`, #95)

```python
async def agent_post_with_retry(
    agent_name: str,
    endpoint: str,
    payload: dict,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    timeout: float = 600.0,
    execution_id: Optional[str] = None,   # #2433: the schedule_executions row this call serves
) -> httpx.Response:
```

Exponential backoff: delay = `retry_delay * (2 ** attempt)`. Handles `httpx.ConnectError` for agent servers still booting.

**Backend agent-call limiter (#904 RC-1).** Each attempt is gated by `acquire_agent_call_slot` (`services/agent_call_limiter.py`): a per-agent semaphore (cap = the effective `max_parallel_tasks`, fallback 3, frozen at first access) then a global one (`BACKEND_AGENT_CALL_LIMIT`, default 8 per uvicorn worker). The queue wait is bounded by `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S` (default **3600**; `0` = wait forever, opt-in) — on timeout `BackendAgentCallBudgetExhausted` is raised and the callers' existing except branches write FAILED without firing SUB-003 (no Claude work started). The one-shot `> 5s` queue-wait warning fires on **both** acquire branches (before #2433 it fired only on the opt-in `0` branch, so the default configuration parked calls in silence). Both knobs are forwarded in `docker-compose.yml`, `docker-compose.prod.yml` and `docker-compose.hosted.yml` and documented in `.env.example` (#2433 — they were dev-compose-only, so production could not raise the cap: the #1039 packaging-gap class).

**In-flight proof-of-life (#2433).** When `execution_id` is given, the whole retry loop runs inside `track_inflight_dispatch(execution_id, agent_name, http_timeout=timeout)` (`:582`), so the queue wait, the connect-retry sleeps and the POST itself are all owned by a live dispatcher as far as the cleanup watchdog is concerned:

- **Registry.** The in-process `_INFLIGHT` entry (`InflightEntry{phase: parked|calling, parked_since, cancel_requested, deadline}`) is exact for the worker that owns the coroutine; ONE refresher task per process pipelines the cross-worker marker `execution:inflight:{execution_id}` (60s TTL, 15s tick, only for entries older than a 5s grace so a fast acquire never touches Redis) and is the marker's **sole writer/deleter** (an unregister only queues the DEL). The marker is liveness, not state — a dead worker stops refreshing and the row is orphaned as before (the #408 dead-coroutine class is unchanged). A Redis failure is negative-cached for 30s and logged once per episode; every Redis touch is fail-open. An entry past its deadline (`registered_at + queue-wait bound + http_timeout + 60s`) is invisible, so a leaked entry can never keep a `running` row alive forever.
- **Verdicts.** `inflight_verdicts(ids)` (one `MGET`) answers `alive` / `absent` / `unknown` — `unknown` only when an *established* client raised (slow Redis), `absent` when no client exists at all (the in-process registry is then the whole truth). `cleanup_service` withholds recovery on `alive`, and on `unknown` only for rows younger than `inflight_max_age_seconds()` (queue-wait bound + widest HTTP timeout + slack). Watchdog side: [cleanup-service.md](cleanup-service.md).
- **Re-anchor at grant.** `acquire_agent_call_slot(agent_name, *, execution_id, on_granted)` (`:585`) records the `parked → calling` phase flip; when the park reached `DISPATCH_RESTAMP_THRESHOLD_SECONDS` (5s) it awaits `on_granted(parked_seconds)` = the nested `_on_dispatch_granted` (`:560`): `db.restamp_execution_dispatch(execution_id)` — CAS on `status == RUNNING AND lease_expires_at IS NULL`, `started_at = now`, `queued_at = COALESCE(queued_at, old started_at)` (the drained-backlog shape, so the wait stays visible in the row) — then `slot_service.renew_slot(agent_name, execution_id)`, **both via `asyncio.to_thread`** (#2435 review: the re-stamp is a sync sqlite write and it fires while both semaphores are held, at the one moment the queue is by definition congested — precisely the event-loop stall this limiter exists to bound) (see [capacity-management.md](capacity-management.md)), and one INFO line naming the park. Every age check — the registry-blind Phase-1 `mark_stale_executions_failed`, canary E-01, the watchdog's 60s grace, `duration_ms` — and the slot TTL were measured from admission, so without this a `park + run > timeout+300` was bulk-FAILed **mid-run** with its slot TTL-reclaimed. The refresher additionally renews the slot every tick while the entry is `parked`. A raising `on_granted` never blocks the dispatch (logged, swallowed). Step 3b's `mark_execution_dispatched` deliberately stays *before* the park (E-05 needs the sentinel on rows >60s).
- **Cancelled while parked.** At grant the limiter publishes the `parked → calling` transition and reads the cross-worker key `execution:cancel:{execution_id}` in ONE pipeline (in that order — see [chat-turn-cancellation.md](chat-turn-cancellation.md) for why the ordering is what makes a remote cancel safe), gated on the entry being older than the marker grace, checks the in-process `cancel_requested` flag, and raises `BackendAgentCallCancelled` (a subclass of `BackendAgentCallBudgetExhausted`) instead of POSTing. `chat_execution_service.terminate_execution` sets that flag through `_cancel_inflight_if_parked` (`cancel_inflight(eid, agent_name=name)` locally, else `request_cross_worker_cancel(eid, agent_name=name)` — both scoped to the agent the caller is authorised on, plus a row-level `agent_name` belt) and finalizes the row CANCELLED right there — `release_if_matches`, CAS write, activity close, a `cancelled_while_parked` activity. The dispatcher's own terminal write in the `except BackendAgentCallBudgetExhausted` branch is **CANCELLED too, not FAILED** (`_write_terminal_and_gate(status=CANCELLED)`), so the row reads the same whichever writer wins the CAS; the loser's lost-CAS branch closes the activity in the standing state. A `calling` phase goes to the agent as before. In depth: [chat-turn-cancellation.md](chat-turn-cancellation.md).

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
4     POST to agent /api/task with retry         1414   agent_post_with_retry(..., execution_id=execution_id)
4a      register in-flight for the WHOLE call    582    track_inflight_dispatch() — _INFLIGHT + execution:inflight:{id} marker (#2433)
4b      park in the agent-call semaphore         585    acquire_agent_call_slot(execution_id=…, on_granted=…) (#904)
4c      grant after a park ≥5s → re-anchor       560    _on_dispatch_granted → db.restamp_execution_dispatch() + slot_service.renew_slot() (#2433)
4d      cancel landed while parked → no POST     1766   BackendAgentCallCancelled → CANCELLED result (#2433)
5     Sanitize response + execution log          267    sanitize_execution_log(), sanitize_response()
6     Update execution record with result        283    db.update_execution_status()
7     Complete activity                          297    activity_service.complete_activity()
8     Release slot (if acquired, in finally)     412    slot_service.release_slot()
```

> **Step 3b**: Sets `claude_session_id='dispatched'` before the agent HTTP call. This prevents the cleanup service's no-session check from falsely marking long-running executions as "Silent launch failure". Only executions that never reach dispatch (backend crash before step 3b) will be caught by the 60-second no-session cleanup.
>
> **Parallel codepath (#686, 2026-05-11):** The `/chat` endpoint in `src/backend/routers/chat.py` (lines 313-321) now performs the equivalent inline dispatched-mark via `db.mark_execution_dispatched(task_execution_id)` immediately before its agent HTTP call — parallel of #279. On success it also computes the real `claude_session_id` from `response_data.get("session_id")` (falling back to `session_data` then `metadata`) and passes it to `db.update_execution_status()`, replacing the 'dispatched' sentinel with the actual Claude session UUID (#686 UC1 closes the observability gap). Net effect: the no-session sweep protection now applies to interactive chat executions too, even though `/chat` does not go through `TaskExecutionService.execute_task()`.

> **Steps 4a–4d (#2433)**: between Step 3b and the agent receiving the turn sits the backend agent-call semaphore — a queue the agent has never heard of. The whole `agent_post_with_retry` call is registered as an in-flight dispatch (4a) so the watchdog reads it *alive*; a park ≥5s re-stamps `started_at` and renews the slot lease at grant (4c) so the park never spends the run's own budget; a cancel that landed during the park is honoured at grant (4d) and never reaches the agent. Mechanics under [agent_post_with_retry()](#agent_post_with_retry-line-522).

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
| Re-anchor a parked row at dispatch (#2433) | `db.restamp_execution_dispatch()` | `src/backend/db/schedules/executions.py:292` (facade `database.py:1542`) | CAS on `status == RUNNING AND lease_expires_at IS NULL`: `started_at = now`, `queued_at = COALESCE(queued_at, old started_at)`; `False` for terminal / pull-leased / unknown rows |

### Redis Operations

| Operation | Service | Key Pattern |
|-----------|---------|-------------|
| Acquire slot | `SlotService.acquire_slot()` | `agent:slots:{name}` (ZSET), `agent:slot:{name}:{exec_id}` (HASH) |
| Release slot | `SlotService.release_slot()` | Same keys, ZREM + DELETE |
| Renew slot lease (#2433) | `SlotService.renew_slot()` — sync, via `asyncio.to_thread` at grant and from the limiter refresher's worker thread | `ZADD XX CH` score=now on the ZSET + `EXPIRE` of the HASH to its stored `timeout_seconds + 300`, together; never resurrects a released member |
| In-flight marker (#2433) | `agent_call_limiter` refresher (sole writer/deleter) | `execution:inflight:{execution_id}` — STRING JSON `{agent, phase, since, pid}`, 60s TTL, refreshed every 15s |
| Cross-worker cancel flag (#2433) | `agent_call_limiter.request_cross_worker_cancel()` (set) / `acquire_agent_call_slot` at grant (read) | `execution:cancel:{execution_id}` — TTL = queue-wait bound + 60s |

Slot TTL: Dynamic (agent timeout + 5 min buffer), set at admission — re-anchored at dispatch after a park ≥5s and every 15s while parked (`renew_slot`, #2433). See capacity-management.md / parallel-capacity.md for details.

## Side Effects

### Activity Tracking

| Event | Type | When |
|-------|------|------|
| Execution start | `ActivityType.CHAT_START` | After slot acquired (line 203) |
| Execution success | `complete_activity(status="completed")` | After response persisted (line 297) |
| Execution failure | `complete_activity(status="failed")` | On any exception (lines 332, 374, 398) |
| Terminal applier close | `complete_activity(status=activity_state_for_terminal(envelope.status))` | `apply_result` won-CAS failure branch + the SUCCESS-lost-CAS-to-cancel reconcile branch — a CANCELLED terminal closes the dispatch activity as `cancelled`, not `failed` (#1332) |
| **Every** CAS-won terminal (#1804) | `activity_service.close_execution_activity(execution_id, terminal_status, …)` | The close is a property of **winning the terminal CAS**, not of holding the `activity_id` local. See [activity-stream.md → The Close Contract](activity-stream.md#the-close-contract-1804) for the owner, the lattice, and the full writer list |

#### `_write_terminal_and_gate` — both CAS outcomes close (#1804)

This writer handles the terminals that never reach `apply_result` (timeout,
budget exhausted, unexpected exception, and the inline circuit-open/capacity/
ephemeral fast-fails). It gated the close on `won` and did nothing on a loss —
while the SUCCESS applier had reconciled its own lost CAS since #1332. Same
file, two appliers, one of them handling it. Now:

```python
if won:
    await activity_service.close_execution_activity(
        execution_id, status, error=error, activity_id=activity_id)
elif execution_id:
    # The row holds someone else's terminal (a user cancel, or a
    # watchdog/lease-reaper recovery). Close the activity in THAT state.
    reconciled = db.get_execution(execution_id)
    await activity_service.close_execution_activity(
        execution_id,
        reconciled.status if reconciled else TaskExecutionStatus.FAILED,
        error=f"superseded by {…}", activity_id=activity_id)
```

The `activity_status` parameter is **gone** from the signature — the state is
derived from `status` through the shared #1332 mapping, so the two can no longer
drift. All four call sites passed `FAILED`/`ActivityState.FAILED`, so this is a
no-op for behaviour and one fewer thing to keep in sync.

#### Backend shutdown (`except asyncio.CancelledError`) — #1804

`execute_task`'s cancellation handler (and its twin in
`routers/internal._execute_task_internal_background`) writes FAILED so the
cleanup sweep can't inflate the execution's duration (#767) — then left the
activity open for the 120-minute activity backstop to inflate *its* duration
instead. Worse: the row is now `failed`, so startup recovery (which scans
`running`) skips it forever and the activity orphans permanently. Both handlers
now close on the CAS-won branch. This is the issue's own reproduction step:
restart the backend mid-run under `--reload` and watch the Timeline.

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
| Backend call budget exhausted (#904) | `BackendAgentCallBudgetExhausted` after `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S` (default 3600) in the agent-call semaphore → `_write_terminal_and_gate(FAILED)`, `status=FAILED, error="Backend call budget exhausted…"` (no Claude work started; SUB-003 never fires; the outer `finally` releases the slot) | `/chat`: 503 via `_finalize_budget_exhausted` | FAILED result (no `error_code`) |
| Cancelled while parked (#2433) | `BackendAgentCallCancelled` (subclass) at semaphore grant — in-process `cancel_requested` or `execution:cancel:{id}` — the branch writes **CANCELLED** through `_write_terminal_and_gate` (wins if the terminate path has not written yet, loses to its identical CANCELLED otherwise; the #1804 lost branch closes the activity as `cancelled`) and returns `status=CANCELLED` | `/chat`: **409** via `_finalize_budget_exhausted` (CANCELLED row + `cancelled` activities; a real exhaustion keeps 503/FAILED) | CANCELLED result |
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

> **Status Enums (#92)**: Execution statuses use `TaskExecutionStatus` (`running/success/failed/cancelled/skipped`). Activity statuses use `ActivityState` (`started/completed/failed`/`cancelled` since #1332). Both are defined in `models.py`; the terminal→activity mapping is `models.activity_state_for_terminal`, and the close itself is a CAS returning `ActivityCloseOutcome` (#1804).

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
  +-- 4. agent_post_with_retry(agent_name, "/api/task", payload, execution_id=execution_id)
  |      |
  |      +-- track_inflight_dispatch(execution_id) ...... #2433: _INFLIGHT entry + execution:inflight:{id}
  |      |     |                                          marker (60s TTL / 15s refresher) for the WHOLE call
  |      |     +-- acquire_agent_call_slot(execution_id, on_granted) ... #904 per-agent + global semaphore
  |      |     |     +-- parked >= 5s --> on_granted: db.restamp_execution_dispatch() + slot_service.renew_slot()
  |      |     |     +-- cancel flag / execution:cancel:{id} at grant --> BackendAgentCallCancelled (no POST)
  |      |     |     +-- queue timeout (3600s) --> BackendAgentCallBudgetExhausted --> FAILED (no SUB-003)
  |      |     |
  |      |     +-- Retries: 3 attempts, exponential backoff (1s, 2s, 4s) — entry kept across the sleeps
  |      |     |
  |      |     +-- httpx.ConnectError --> retry or fail
  |      |     +-- httpx.TimeoutException --> terminate_execution_on_agent() --> fail (#61)
  |      |     +-- httpx.HTTPError --> fail
  |      |
  |      +-- [FINALLY] unregister_inflight() --> refresher DELs the marker
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

6. **Action** (#2433): Set `BACKEND_AGENT_CALL_LIMIT=2`, trigger four long-running tasks on one agent (`max_parallel_tasks` ≥ 4), wait past the 60s watchdog grace and one 5-min sweep
   **Expected**: The two parked rows are never orphaned (`dispatch_inflight_skipped` in the cleanup report, zero `orphan_recovered`); all four finish `success`; `active_slots` never drops below the count actually running
   **Verify**: Backend log shows `parked …ms in the backend call queue — re-anchoring at dispatch`; the parked rows carry `queued_at` = admission and `started_at` = grant; Redis `execution:inflight:{id}` exists (TTL ≤ 60) while a call is in flight and is gone afterwards

7. **Action** (#2433): While a task is parked (log line `Agent-call queue wait > 5s`), terminate it via the Executions UI / `POST .../executions/{id}/terminate`
   **Expected**: `{"status": "cancelled_while_parked"}` without any agent call; the row is `cancelled`, its slot released
   **Verify**: No `/api/task` reaches the agent for that id; backend log shows `cancelled while queued … — not dispatching` at grant

### Unit tests (#2433)

| File | Pins |
|------|------|
| `tests/unit/test_2433_dispatch_wiring.py` | `agent_post_with_retry` registers the in-flight entry for the whole call (phase `calling` during the POST, unregistered after); no `execution_id` → nothing registered; a park at grant calls `db.restamp_execution_dispatch` + `renew_slot` exactly once; a cancel while parked raises `BackendAgentCallCancelled` and never POSTs; `terminate_execution` cancels a locally-parked and an other-worker-parked row without touching the agent (`release_if_matches` + CANCELLED CAS), and falls through to the agent proxy when not parked or already `calling` |
| `tests/unit/test_2433_limiter_inflight.py` | registry lifecycle; one pipelined marker write per tick (fakeredis, 60s TTL, grace); slot renewal only while `parked`; refresher-flushed deletes; Redis `None`/raise never reaches the caller, negative-cached and logged once; tri-state `inflight_verdicts` incl. the MagicMock-leak guard; deadline invisibility; `on_granted` only past the threshold and a raising one never blocks dispatch; cross-worker cancel key honoured at grant; the `> 5s` warning on the default branch; docstring default = code default |
| `tests/unit/test_2433_db_restamp.py` | `restamp_execution_dispatch` on the real schema (SQLite; PostgreSQL when `TEST_POSTGRES_URL` is set): moves `started_at`, keeps the admission instant in `queued_at` (an existing one is preserved), refuses terminal / pull-leased / unknown rows |
| `tests/unit/test_2433_limiter_packaging.py` | `BACKEND_AGENT_CALL_LIMIT` / `BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S` forwarded with equal defaults in dev, prod and hosted compose; documented in `.env.example`; the module docstring's default matches `os.getenv(..., "3600")` |

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
- [task-completion-events.md](task-completion-events.md) -- #1578 system-emitted `agent.task.completed`/`failed` at this service's CAS-won terminals (`apply_result` ×2 + `_write_terminal_and_gate`)
- [capacity-management.md](capacity-management.md) -- #2433 `SlotService.renew_slot` beside acquire/release: why the slot lease is re-anchored at dispatch after a park
- [cleanup-service.md](cleanup-service.md) -- #2433 watchdog side: the tri-state `inflight_verdicts` read (`alive`/`absent`/`unknown`), `dispatch_inflight_skipped`, the honest orphan string
- [chat-turn-cancellation.md](chat-turn-cancellation.md) -- #2433 parked-cancel arm of `terminate_execution` (`_cancel_inflight_if_parked` → `BackendAgentCallCancelled` at grant)

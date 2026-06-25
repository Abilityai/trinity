# Feature: Sequential Agent Loops (#740)

## Overview
Server-managed sequential bounded repetition of an agent task. Caller fires `run_agent_loop` once, gets back a `loop_id`, and can disconnect — the loop runs in-process in the backend, dispatching each iteration through the standard task execution path. Companions `get_loop_status` and `stop_loop` cover observability and graceful termination.

Complements `chat_with_agent` (single turn) and `fan_out` (parallel batch) with the third execution pattern: sequential, ordered, optionally chained.

## User Story
As an agent orchestrator, I want to run a task N times in order, optionally chaining each iteration's response into the next, with an early-exit signal — so iterative refinement, agentic retry loops, and bounded polling work without me holding an HTTP connection open past the 60-second MCP timeout.

## Entry Points
- **API**: `POST /api/agents/{name}/loops`, `GET /api/agents/{name}/loops`, `GET /api/loops/{id}`, `POST /api/loops/{id}/stop`
- **MCP tools**: `run_agent_loop`, `get_loop_status`, `stop_loop`
- **Web UI (Phase 2, #1106)**: the **Loops** tab on the Agent Detail page (`AgentDetail.vue`, between Schedules and Playbooks).

Phase 1 shipped headless (API/MCP only); iterations also appear in the standard execution timeline tagged with `loop_id`, rendered as the distinct **Loops** analytics bucket and fuchsia trigger chip (#1150) rather than folding into Scheduled.

## Frontend Layer (Phase 2, #1106)
- **Tab**: `src/frontend/src/views/AgentDetail.vue` adds `{ id: 'loops', label: 'Loops' }` and mounts `<LoopsPanel :agent-name :agent-status />`.
- **Component**: `src/frontend/src/components/LoopsPanel.vue` — collapsible Run-loop form (message template w/ `{{run}}`/`{{previous_response}}` helper text, `max_runs`, `stop_signal`, `delay_seconds`, `timeout_per_run`, `max_duration_seconds`, `model` via `ModelSelector`, `allowed_tools` picker), loop list with status badge + `runs_completed/max_runs` + `stop_reason`, expandable per-run table (#/status/cost/duration/response), last full response via `renderMarkdown`, and a Stop control reflecting `stopping`→`stopped`. The Run-loop button is gated on `agentStatus === 'running'`.
- **Store**: `src/frontend/src/stores/loops.js` — agent-scoped Pinia store on the shared `api.js` client (Invariant #7). `setAgent(name)`/`clear()` bind the store to the mounted agent; `handleWebSocketEvent` filters fleet-wide `loop_run_completed`/`loop_completed` events by that agent and targeted-refreshes only the affected loop; a 12s backstop poll runs while any loop is `queued`/`running` to recover a missed terminal event. `expandedLoopId` lives in the store so it survives the `v-if` tab remount.
- **WS wiring**: `src/frontend/src/utils/websocket.js` routes the `data.type`-keyed loop events to `loopsStore.handleWebSocketEvent` in the `default:` branch.
- **e2e**: `src/frontend/e2e/loops-panel.spec.js` (@interactive — needs a live stack + running agent).

## MCP Layer

### Tool registration
- `src/mcp-server/src/server.ts:218` — `createLoopTools(client, requireApiKey)`

### Tool definitions
- `src/mcp-server/src/tools/loops.ts`
- Permission model identical to `chat_with_agent`: owner/admin/shared on the agent, or explicit `agent_permissions` for agent-scoped MCP keys. Backend enforces — MCP tools surface a cleaner message for unscoped keys.
- `run_agent_loop` accepts `message`, `max_runs` (1–100, required), optional `stop_signal`, `delay_seconds` (0–3600), `timeout_per_run` (10–7200), `max_duration_seconds` (1–604800 = 7d; optional wall-clock deadline, #1156), `model`, `allowed_tools`. `agent_name` is required for user-scoped keys and defaults to the bound agent for agent-scoped keys.

### Client methods
- `src/mcp-server/src/client.ts` — `startAgentLoop`, `getLoopStatus`, `stopAgentLoop`.

## Backend Layer

### Router
- `src/backend/routers/loops.py` — two routers exported (agent-scoped + loop-scoped) and both mounted in `main.py`.
- Request validation via `StartLoopRequest` Pydantic model (`max_runs` 1–100, `message` 1–100_000 chars, `stop_signal` ≤200 chars and stripped — blank → `None` → fixed mode; `max_duration_seconds` 1–604800).
- 202 Accepted on start; 404 on unknown loop; 403 if caller is not the initiator and lacks agent access.
- **400** when `max_duration_seconds` is smaller than the effective per-run timeout (`timeout_per_run`, else the agent's `execution_timeout_seconds`) — a deadline that can't fit one run is rejected rather than silently never firing (#1156).
- `GET /api/loops/{id}` returns `max_duration_seconds` plus a computed `elapsed_seconds` (from `started_at`).

### Service
- `src/backend/services/loop_service.py` — `LoopService.start_loop()` creates the `agent_loops` row and spawns an `asyncio.Task` via `_run()`. One in-process handle per active loop (`_handles: dict[str, _LoopHandle]`) tracks the cooperative stop flag.
- **Wall-clock deadline (#1156):** when `max_duration_seconds` is set, the runner checks `now - started_at` only at iteration boundaries (before the next run, and before/after the inter-run delay — which is itself capped to the remaining budget). An in-flight run is never killed mid-turn, so total overshoot is bounded by one `timeout_per_run`; on expiry the loop exits `completed` / `stop_reason="deadline_exceeded"`. Complements the `max_runs` count cap with a time cap.
- Iteration body:
  1. Cooperative stop check (`handle.should_stop`); then the deadline check above.
  2. Template substitution: `{{run}}` → 1-indexed; `{{previous_response}}` → trailing 2000 chars of last response (empty on iteration 1).
  3. Insert `agent_loop_runs` row in `running` status.
  4. `await task_execution_service.execute_task(triggered_by="loop", loop_id=...)`.
  5. Finalize the run row with `cost`, `duration_ms`, `execution_id`.
  6. Broadcast `loop_run_completed` event.
  7. Stop-signal substring check; on match → exit with `stop_reason="stop_signal_matched"`.
  8. Optional `delay_seconds` sleep before next iteration.
- Terminal states + reasons:
  - `completed` / `max_runs_reached`
  - `completed` / `stop_signal_matched`
  - `completed` / `deadline_exceeded` (`max_duration_seconds` wall-clock budget reached, #1156)
  - `stopped` / `user_stopped` (via `stop_loop`)
  - `failed` / `error` (any iteration's `TaskExecutionResult.status != "success"` or an unhandled exception)
  - `interrupted` / `interrupted` (backend restart, swept by cleanup-service)
- `loop_completed` event broadcast on every terminal transition.

### DB layer
- `src/backend/db/loops.py` — `LoopOperations`:
  - `create_loop`, `get_loop`, `mark_loop_running`, `update_loop_progress`, `finalize_loop`, `list_loops_for_agent`, `list_non_terminal_loops`, `mark_orphans_interrupted`.
  - `start_loop_run`, `attach_execution_to_run`, `finalize_loop_run`, `list_runs`.
- Facade: `src/backend/database.py` exposes all of the above on `db`.

### Schema + migration
- `src/backend/db/schema.py` / `db/tables.py` — `agent_loops`, `agent_loop_runs`, plus `loop_id TEXT` column on `schedule_executions` + index `idx_executions_loop`. `agent_loops.max_duration_seconds INTEGER` (NULL = no deadline) added for #1156.
- **Dual-track migration (Invariant #9)** for `max_duration_seconds`: SQLite `_migrate_agent_loops_max_duration` in `db/migrations.py` (`_safe_add_column`) **and** Alembic revision `src/backend/migrations/versions/0005_agent_loops_max_duration.py` (`ADD COLUMN IF NOT EXISTS`, chained after `0004_agent_ownership_voice_name`) for the Postgres backend.
- `src/backend/db/migrations.py` — `_migrate_agent_loops_tables` (idempotent `CREATE TABLE IF NOT EXISTS` + `_safe_add_column` for the existing executions table).

### Execution dispatch
- `src/backend/services/task_execution_service.py:246` — `loop_id` parameter added to `execute_task()` and forwarded into `db.create_task_execution`, which writes the new `loop_id` column on `schedule_executions`. Every iteration shows up as a normal execution row tagged with its parent loop.

### Restart recovery
- `src/backend/services/cleanup_service.py` — `_cleanup_loop()` startup hook calls `db.mark_orphan_loops_interrupted()`. Any non-terminal `agent_loops` rows from a prior process flip to `interrupted`; loops do not auto-resume.

## WebSocket Events
- `loop_run_completed` per iteration: `{type, loop_id, agent_name, run_number, execution_id, cost, duration_ms, timestamp}`.
- `loop_completed` on terminal transition: `{type, loop_id, agent_name, status, stop_reason, runs_completed, timestamp}`.
- Both flow through the existing `manager.broadcast()` → Redis Streams event bus (RELIABILITY-003).

## Side Effects
- Each iteration creates one `schedule_executions` row with `triggered_by="loop"`.
- The iteration goes through `capacity_manager.admit()` — loops share the agent's `max_parallel_tasks` budget with other traffic.
- Per-iteration `cost` accumulates in `agent_loops.last_response` is the latest response; per-run `cost` lives on `agent_loop_runs`.

## Error Handling
| Case | Behavior |
|---|---|
| Iteration raises Python exception | `agent_loop_runs.status='failed'`, `agent_loops.status='failed'`, `stop_reason='error'`, loop terminates |
| Iteration returns `TaskExecutionResult.status != "success"` | Same as above; `agent_loops.error` carries the iteration number + task error |
| Stop requested while iteration in flight | Current iteration completes; loop exits with `stop_reason="user_stopped"` |
| Wall-clock deadline reached (`max_duration_seconds`) | Detected at an iteration boundary; loop exits `completed` / `stop_reason="deadline_exceeded"`. An in-flight run is never killed mid-turn (overshoot ≤ one `timeout_per_run`) |
| `max_duration_seconds` < effective per-run timeout | Rejected at create with **400** (can't fit one run) |
| Backend restart mid-loop | On next boot, cleanup-service flips to `interrupted` |
| Stop on already-terminal loop | Returns `already_done` (no-op) |
| Stop on unknown loop | Returns `not_found` (router returns 404 separately) |

## Security Considerations
- Standard agent-access check on start (`get_authorized_agent`).
- Loop-scoped endpoints (`/api/loops/{id}/...`) verify that the caller is the initiator OR has access to the underlying agent (owner/admin/shared via `db.can_user_access_agent`).
- No sensitive data in WS events — `cost`, `duration_ms`, `run_number`, `execution_id` only.
- `max_runs` capped at 100; `delay_seconds` at 3600; `timeout_per_run` at 7200; `max_duration_seconds` at 604800 (7d) to bound resource consumption.

## Testing
**Prerequisites**: backend running; an agent the caller can access.

**Test Steps**:
1. `POST /api/agents/{name}/loops` with `{"message": "step {{run}}", "max_runs": 3}` — returns 202 + `loop_id`.
2. `GET /api/loops/{loop_id}` immediately — `status="running"` or `"queued"`.
3. After ~3 iterations: `status="completed"`, `stop_reason="max_runs_reached"`, `runs_completed=3`, `runs[]` has 3 entries.
4. Repeat with `stop_signal="[[DONE]]"` and a message that includes the sentinel — verify `stop_reason="stop_signal_matched"` and `runs_completed < max_runs`.
5. Start a longer loop, call `POST /api/loops/{loop_id}/stop` — verify `status="stopped"`, `stop_reason="user_stopped"`.

**Edge Cases**:
- `max_runs=0` → 422.
- `max_runs=101` → 422.
- `max_duration_seconds` below the effective per-run timeout → 400; start a loop with a tight `max_duration_seconds` and verify it stops `completed` / `deadline_exceeded` before `max_runs`.
- Loop on a non-accessible agent → 403.
- Stop on already-completed loop → `{"status": "already_done"}`.
- Backend restart mid-loop → next `GET /api/loops/{loop_id}` shows `status="interrupted"`.

**Unit tests**: `tests/unit/test_loop_service.py` covers fixed/until modes, template substitution, graceful stop, failure paths, restart recovery, `get_status`, and the #1156 wall-clock deadline (boundary check, delay capped to remaining budget, `deadline_exceeded`). `tests/unit/test_loops_router_validation.py` covers the create-time `max_duration_seconds` validation (400 below the effective per-run timeout).

**Status**: ✅ Phase 1 (backend/MCP) + Phase 2 (web UI, #1106) shipped; wall-clock deadline (`max_duration_seconds`, #1156) added.

## Related Flows
- **Upstream**: `task-execution-service.md` — each iteration dispatches through `TaskExecutionService`.
- **Sibling**: `fan-out.md` — parallel batch counterpart to this sequential primitive.
- **Downstream**: `execution-list-page.md` / `execution-detail-page.md` — iterations show up tagged with `loop_id`.

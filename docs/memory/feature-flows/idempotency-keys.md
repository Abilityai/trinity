# Feature: Idempotency Keys at Trigger Boundaries (RELIABILITY-006)

> **Updated 2026-06-02 (#525, PR #1019):** New cross-cutting primitive — Architectural Invariant #18. Every producer boundary that creates an execution accepts an optional `Idempotency-Key` header; the same `(scope, key)` within 24h yields exactly one execution. Duplicates short-circuit with the original response + `X-Idempotent-Replay: true`; an in-flight duplicate returns 409. The layer is **fail-open** — a dedup-layer error never blocks a real execution.
>
> **Note:** this is unrelated to the "idempotent retry" behavior of the dispatch circuit breaker (#526) and the in-line reader-race auto-retry (#678). Those reuse an `execution_id`; this feature dedups at the *producer boundary* before any execution exists.
>
> **Updated 2026-06-04 (#1051):** `chat_with_agent` was decomposed into `_admit_chat_request` / `_prepare_chat_execution` / `_run_chat_and_finalize`. The idempotency gate now lives in `_admit_chat_request`, and the dispatch-breaker-open `/chat` deny path **also releases the claim** (`fail(idem)`), joining the capacity-full path. `routers/chat.py` line anchors below refreshed for the new layout.

## Overview
A `(scope, idempotency_key)` claim is taken atomically before an execution is dispatched at every trigger boundary. A first-seen key proceeds and dispatches; a duplicate within the 24h TTL short-circuits and replays the original result instead of dispatching a second execution. This closes the producer-boundary dedup gap that the unified execution funnel made more acute: webhook re-deliveries, MCP client transport retries, and scheduler→backend network blips no longer create phantom executions.

## User Story
As a Trinity platform operator, I want a transient retry of the same trigger (a webhook re-delivery, an MCP SDK retry, a scheduler resend after a 5xx) to resolve to one execution rather than two, so that duplicate work, double charges, and confusing duplicate timeline rows are eliminated — without ever blocking a legitimately new request if the dedup layer itself errors.

## Entry Points (Wired Boundaries)
Enforcement lives at the **router** layer, not solely in `TaskExecutionService`, because the "single funnel" is not actually single — sync `/chat` runs an inline path and `/api/webhooks/{token}` creates no execution at all.

| Boundary | Scope | Key source | File |
|----------|-------|-----------|------|
| `POST /api/agents/{name}/chat` | `agent:{name}` | optional `Idempotency-Key` header | `routers/chat.py:148` |
| `POST /api/agents/{name}/task` | `agent:{name}` | optional `Idempotency-Key` header | `routers/chat.py:1259` |
| `POST /api/internal/execute-task` | `agent:{name}` | header (set by scheduler) | `routers/internal.py:251` |
| `POST /api/webhooks/{token}` | `webhook:{token}` | header **or auto-derived** `(token, body_hash)` | `routers/webhooks.py:248` |
| `POST /api/agents/{name}/fan-out` | `agent:{name}` | optional `Idempotency-Key` header | `routers/fan_out.py:134` |
| Scheduler dispatch | `agent:{name}` | `sched:{execution_id}` (deterministic) | `src/scheduler/service.py:1042` |
| MCP `chat_with_agent` / `fan_out` | `agent:{name}` | `mcp:{sha256(args)}` (deterministic) | `src/mcp-server/src/tools/chat.ts:301,518` |

**Invariant #18 contract:** *Any new trigger type must accept an idempotency key before merge.* The dedup layer is fail-open, so the cost of wiring it is one `begin`/`complete`/`fail` triple.

## Backend Layer

### Three-layer split (Invariant #1)
- **DB layer**: `db/idempotency.py` — `IdempotencyOperations` (no HTTP, no key-derivation logic).
- **Service layer**: `services/idempotency_service.py` — key derivation + `begin`/`complete`/`fail` orchestration over the DB layer.
- **Router layer**: each boundary calls `begin()` → dispatch → `complete()` or `fail()`.
- **Facade**: `database.py` exposes `idempotency_claim` / `idempotency_attach_execution` / `idempotency_complete` / `idempotency_release` / `idempotency_purge_expired` (`database.py:2064-2080`), backed by `IdempotencyOperations` constructed in `__init__` (`database.py:297`).

### DB layer — `db/idempotency.py`
The table's `PRIMARY KEY (scope, idempotency_key)` **is** the atomic claim. Atomicity relies on SQLite database-level write locking, which holds across processes (multiple uvicorn workers + the standalone scheduler share one DB file).

- `claim(scope, key, ttl_hours=24)` — `db/idempotency.py:34`. First deletes any expired row for the key (`created_at < cutoff`), then `INSERT`s an `in_flight` row. On success returns `{state: "new"}`. On `sqlite3.IntegrityError` (lost the race / genuine duplicate) it reads the surviving row and returns its `{state, execution_id, snapshot}` — `state` is `in_flight` or `completed`. State constants live at `db/idempotency.py:26-28` (`STATE_NEW`, `STATE_IN_FLIGHT`, `STATE_COMPLETED`).
- `attach_execution(scope, key, execution_id)` — `db/idempotency.py:87`. Best-effort `UPDATE` recording the dispatched `execution_id` on an in-flight claim, so an in-flight 409 can hand back a pollable id.
- `complete(scope, key, execution_id, snapshot)` — `db/idempotency.py:96`. Sets `status='completed'`, `COALESCE`s the execution_id, JSON-encodes and stores `response_snapshot` for replay.
- `release(scope, key)` — `db/idempotency.py:126`. Deletes the row **only when still `in_flight`** so a failed first attempt can retry; never removes a `completed` row (which must stay to keep replaying the original result).
- `purge_expired(ttl_hours=24)` — `db/idempotency.py:139`. Deletes rows past the TTL; returns the row count.

Time math uses `iso_cutoff(hours)` and `utc_now_iso()` from `utils/helpers.py` (Invariant #16 — ISO-Z TEXT columns).

### Service layer — `services/idempotency_service.py`
- **Scope derivation**: `make_agent_scope(agent_name)` → `agent:{name}` (`idempotency_service.py:45`); `make_webhook_scope(token)` → `webhook:{token}` (`idempotency_service.py:50`).
- **Key derivation**:
  - `derive_webhook_key(token, body)` — `idempotency_service.py:55`. `auto:{sha256(token + b"\x00" + body)}`. Header-independent so a naive sender that retries the same POST resolves to the same key; distinct bodies get distinct keys.
  - `derive_schedule_key(execution_id)` — `idempotency_service.py:69`. `sched:{execution_id}`. The scheduler creates one execution_id per fire and reuses it across an HTTP-level resend of the same dispatch (the #525 network-blip case); intentional #271 retries create a fresh execution_id → fresh key → not suppressed.
- **Lifecycle** (returns / consumes the `IdempotencyDecision` dataclass at `idempotency_service.py:29`):
  - `begin(scope, key)` — `idempotency_service.py:84`. Falsy key → no-op decision (`enabled=False`, full back-compat). Wraps `db.idempotency_claim` in a try/except that logs and returns a no-dedup decision on any error (**fail-open**). Maps `new` → proceed, `in_flight` → replay+409, `completed` → replay+snapshot. An unknown state degrades to no-dedup rather than wedging the caller.
  - `attach_execution(decision, execution_id)` — `idempotency_service.py:112`. No-op on replay / disabled / missing id.
  - `complete(decision, execution_id, snapshot)` — `idempotency_service.py:122`. Finalizes a fresh claim; no-op on replay / disabled.
  - `fail(decision)` — `idempotency_service.py:132`. Releases a fresh in-flight claim so a failed first attempt can retry; no-op on replay / disabled.

### Router flow (representative — `/chat`, `routers/chat.py`)
1. `begin(make_agent_scope(name), idempotency_key)` at `chat.py:148` (inside `_admit_chat_request`) — gated before consuming a capacity slot.
2. **Replay path** (`idem.replay`, `chat.py:151`): writes a `EXECUTION / idempotent_replay` platform-audit event; if `idem.in_flight`, raises `409 {error: "request_in_progress", execution_id}` (`chat.py:169`); otherwise returns `idem.snapshot` (or a minimal `{execution.task_execution_id}`) with header `X-Idempotent-Replay: true` (`chat.py:181`).
3. **Fresh path**: dispatch through `CapacityManager`. `attach_execution(idem, task_execution_id)` at `chat.py:361` (inside `_prepare_chat_execution`). On a final response, `complete(idem, task_execution_id, response_data)` at `chat.py:755` (inside `_run_chat_and_finalize`). On an upfront rejection where **nothing was dispatched** — capacity-full (`chat.py:264`) **or** dispatch-breaker-open (`chat.py:216`, #526, added by #1051) — `fail(idem)` releases the claim so the caller can retry with the same key once capacity frees or the breaker recovers.

The `/task` boundary mirrors this (`chat.py:1005-1045` replay block, `complete` at `chat.py:1246`/`1282`/`1424`/`1499`, `fail` at `chat.py:1223`/`1331`). Post-dispatch failures intentionally leave the claim in place — a duplicate within the TTL gets a 409 with the original `execution_id` to poll.

### Webhook boundary — `routers/webhooks.py`
The trigger creates no execution (it fires the scheduler fire-and-forget). The key is `idempotency_key or derive_webhook_key(token, raw_body)` (`webhooks.py:306`); `begin()` at `webhooks.py:309` uses the `webhook:{token}` scope. Replay returns `202` with the stored ack snapshot + `X-Idempotent-Replay: true` (`webhooks.py:332-336`), or `409` if in-flight. On any dispatch error (scheduler 4xx/5xx/unreachable) the claim is released via `fail(idem)` (`webhooks.py:369`, `webhooks.py:373`) so a legitimate re-delivery isn't stuck behind a wedged 409. On success the ack is stored with `complete(idem, None, trigger_payload)` (`webhooks.py:415`) — no execution_id, since the webhook is fire-and-forget into the scheduler.

### Internal boundary — `routers/internal.py`
`POST /api/internal/execute-task` (`internal.py:251`) is the scheduler→backend path. It reads the `Idempotency-Key` header, `begin()`s with the `agent:{name}` scope (`internal.py:288`), `attach_execution(idem, request.execution_id)` (`internal.py:315`), and `complete`/`fail`s around dispatch (`internal.py:347`/`374`/`379`).

### Fan-out boundary — `routers/fan_out.py`
Idempotency covers the whole batch — a duplicate replays the original `FanOutResponse` rather than re-dispatching every sub-task. `begin()` at `fan_out.py:163`, replay returns the stored `idem.snapshot` with `X-Idempotent-Replay: true` (`fan_out.py:185`) or 409 (`fan_out.py:181`), `fail` at `fan_out.py:215`, `complete(idem, result.fan_out_id, response.model_dump())` at `fan_out.py:241`.

## Scheduler Layer
`src/scheduler/service.py:1042` sets `headers["Idempotency-Key"] = f"sched:{execution_id}"` when dispatching to the backend internal endpoint. The execution_id is created once per fire and reused across an HTTP-level resend of the same dispatch, so a transient backend 5xx + resend resolves to the same key and short-circuits the duplicate. Intentional #271 retries create a fresh execution_id → fresh key → not suppressed.

## MCP Layer (Invariant #13 — third surface in sync)
- `src/mcp-server/src/tools/chat.ts:20` — `deriveMcpIdempotencyKey(parts)` returns `mcp:{sha256(parts.join(" "))}`.
- `chat_with_agent` derives the key over `[caller, agent, "chat"|"task", model, "sync"|"async", message]` (`chat.ts:301`) and passes it into `apiClient.task(...)` / `apiClient.chat(...)`.
- `fan_out` derives the key over `[caller, agent, "fan_out", model, JSON.stringify(tasks)]` (`chat.ts:518`) and passes it into `apiClient.fanOut(...)`.
- `src/mcp-server/src/client.ts` forwards the key as the `Idempotency-Key` header on the `chat` / `task` / `fanOut` methods (`client.ts:485,498-501`, `client.ts:647,656-658`, `client.ts:762,787-789`). A transport-level retry of a byte-identical MCP call within 24h dedupes.

## Database

### Table — `idempotency_keys`
DDL in `db/schema.py:1098` (fresh installs); migration `_migrate_idempotency_keys_table` in `db/migrations.py:2186` (registered at `migrations.py:2372`, existing installs). Index `idx_idempotency_created` on `created_at` (`schema.py:1305`, `migrations.py:2210`).

```sql
CREATE TABLE idempotency_keys (
    scope TEXT NOT NULL,            -- "agent:{name}" | "webhook:{token}"
    idempotency_key TEXT NOT NULL,  -- caller-supplied or derived
    execution_id TEXT,              -- nullable (webhook short-circuit has none)
    status TEXT NOT NULL,           -- 'in_flight' | 'completed'
    response_snapshot TEXT,         -- JSON of the original response, for replay
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
```

### Lifecycle of a row
`claim` (INSERT `in_flight`) → optional `attach_execution` (set `execution_id`) → `complete` (status→`completed`, store `response_snapshot`) **or** `release` (DELETE the `in_flight` row so a failed first attempt can retry — never deletes a `completed` row). A row older than the 24h TTL is treated as expired and re-claimed as new.

## Side Effects
- **Audit log**: every replay hit writes a platform-audit `EXECUTION / idempotent_replay` event with `idempotency_key`, `execution_id`, and `in_flight` flag (chat/task/webhook/fan-out replay blocks). See [audit-trail.md](audit-trail.md).
- **Cleanup**: the [Cleanup Service](cleanup-service.md) purges rows past their 24h TTL each cycle via `db.idempotency_purge_expired(ttl_hours=24)` (`services/cleanup_service.py:513`), reported as `idempotency_keys_purged` (`cleanup_service.py:167,195`).
- **No WebSocket events** — the dedup layer is a request-path gate, not a broadcast source.

## Error Handling
| Case | Result |
|------|--------|
| No `Idempotency-Key` (and no auto-derivable key) | Dedup disabled; request proceeds normally (back-compat) |
| First-seen key | Claim taken `in_flight`; request dispatches |
| Duplicate, prior claim still running | `409` (`request_in_progress` for chat/task, `detail` string for webhook), with original `execution_id` where known |
| Duplicate, prior claim completed | `200`/`202` replay of stored `response_snapshot` + `X-Idempotent-Replay: true` |
| Upfront at-capacity rejection (chat/task) | Claim released (`fail`) so caller can retry once capacity frees |
| Dispatch error (webhook → scheduler) | Claim released (`fail`) so a legitimate re-delivery can retry |
| Dedup-layer DB error | **Fail-open** — `begin` logs a warning and returns a no-dedup decision; the real execution always proceeds |

## Testing
### Prerequisites
- Backend + scheduler + Redis running; an agent created and running.

### Test Steps
1. **First chat with a key**: `POST /api/agents/{name}/chat` with `Idempotency-Key: t1`.
   **Expected**: 200, execution dispatched, no `X-Idempotent-Replay` header.
   **Verify**: one row in `schedule_executions`; one `completed` row in `idempotency_keys` with `scope='agent:{name}'`.
2. **Replay with the same key**: repeat the identical request after it completes.
   **Expected**: 200 with header `X-Idempotent-Replay: true`, the original response body; **no** new execution row.
3. **In-flight duplicate**: fire two requests with the same key concurrently (long-running task).
   **Expected**: the loser gets `409 request_in_progress` with the in-flight `execution_id`.
4. **Webhook auto-derive**: `POST /api/webhooks/{token}` twice with an identical body and no header.
   **Expected**: first fires the schedule (202); second returns 202 + `X-Idempotent-Replay: true` and does **not** fire again.
5. **No key (back-compat)**: chat with no `Idempotency-Key`.
   **Expected**: normal dispatch; **no** `idempotency_keys` row created.
6. **TTL purge**: age a row past 24h (or wait), run a cleanup cycle.
   **Verify**: cleanup report shows `idempotency_keys_purged > 0`; row gone.

## Related Flows
- [Webhook Triggers](webhook-triggers.md) — the boundary that auto-derives `(token, body_hash)`.
- [Fan-Out](fan-out.md) — batch-level idempotency over a fan-out operation.
- [Execution Queue](execution-queue.md) / [Capacity Management](capacity-management.md) — the dispatch path downstream of the gate.
- [Scheduling](scheduling.md) / [Scheduler Service](scheduler-service.md) — the `sched:{execution_id}` producer.
- [Cleanup Service](cleanup-service.md) — purges keys past the 24h TTL.
- [Task Execution Service](task-execution-service.md) — the unified funnel the gate sits in front of.

## Change History
| Date | ID | Change |
|------|-----|--------|
| 2026-06-02 | RELIABILITY-006 (#525), PR #1019 (commit 1bb8f271) | Initial implementation — `idempotency_keys` table + migration, `db/idempotency.py`, `services/idempotency_service.py`, wiring at `/chat`, `/task`, `/api/internal/execute-task`, `/api/webhooks/{token}`, `/api/agents/{name}/fan-out`, the scheduler, and MCP `chat_with_agent`/`fan_out`; 24h purge in the cleanup service. Established Architectural Invariant #18. |

# Feature: Agent Self-Reminders (#1296)

## Overview
Durable one-shot deferred self-trigger — the **time-deferred sibling of loops** (`run_agent_loop`, #740). While running, an agent schedules a future re-invocation of itself with a message it picks ("remind me to check this PR in 2h"). When the timer fires, Trinity dispatches a **normal execution of that same agent** carrying the reminder message, through the standard `capacity_manager` admit/slot path (`triggered_by="reminder"`, shares `max_parallel_tasks`). The agent lists and cancels pending reminders.

A loop runs iterations back-to-back *now*; a reminder fires *once, later*. Unlike loops (an in-process, non-durable `asyncio.Task` in the backend), reminders must survive a restart, so they follow the **cron/retry durability model**: a DB row is the source of truth and the single-instance standalone scheduler re-arms an APScheduler `DateTrigger` from the DB.

## User Story
As an autonomous agent, I want to schedule a one-shot future re-invocation of myself with a message, so a follow-up ("check this PR in 2h", "review tomorrow 9am") reliably fires later — surviving a backend/scheduler restart — without me holding anything open or polling.

## Entry Points
- **API**: `POST /api/agents/{name}/reminders`, `GET /api/agents/{name}/reminders`, `POST /api/agents/{name}/reminders/{id}/cancel`
- **MCP tools**: `set_reminder`, `list_reminders`, `cancel_reminder`
- No web UI in v1 (backend-only scheduling primitive). Fired reminders appear in the standard execution timeline tagged `triggered_by="reminder"`, rendered as the distinct **Reminders** analytics bucket (#1107) rather than folding into Scheduled.

## The two design forks (decided)
- **Storage = a dedicated `agent_reminders` table**, NOT a nullable `fire_at` on the cron `agent_schedules` table. `agent_schedules.cron_expression` is `NOT NULL` and every consumer (`_add_job`→`CronTrigger`, the sync loop, `fire_missed_schedules`, the Schedules UI) assumes recurring cron; a `cron IS NULL ⇒ one-shot` sentinel would mean auditing every consumer (high blast radius) and re-introduce the Schedules-tab pollution the issue calls "leaky". Reminder executions still land in `schedule_executions` (via the standard dispatch), so Executions/Overview visibility is preserved regardless of storage.
- **Fire home = the standalone `src/scheduler/` container** (single-instance), NOT the `--workers 2` backend (an in-backend timer double-fires without a leader lock). The scheduler already owns the one-shot machinery (RETRY-001 `DateTrigger` + boot recovery + a 60s sync loop); reminders are a near-clone of the retry path.

## MCP Layer
- **Registration**: `src/mcp-server/src/server.ts` — `createReminderTools(client, requireApiKey)`.
- **Tool definitions**: `src/mcp-server/src/tools/reminders.ts` (mirrors `loops.ts` 1:1). Reuses the inline `getClient` + `resolveAgentName` self-scoping (an agent-scoped key defaults to its bound name). Zod carries the abuse bounds: `message.max(4000)`, `delay_seconds.min(60).max(2592000)`, `fire_at` ISO string. Idempotency is enforced server-side over the raw input, so a naive retry dedupes without a client-supplied key.
- **Client methods**: `src/mcp-server/src/client.ts` — `setReminder` / `listReminders` / `cancelReminder` (thin `request` wrappers).
- No agent-server mirror (backend-only scheduling primitive, no `docker/base-image` change).

## Backend Layer (create/list/cancel)

### Router — `src/backend/routers/reminders.py`
- Three agent-nested endpoints (registered in `main.py`, no route-ordering trap).
- **Self-only auth (AC #5)** — `_self_gate`: `_reject_connector_principal(current_user)` then, for an agent-scoped caller, `current_user.agent_name == name` (else 403). This is the exact reports/heartbeat self-gate on top of `AuthorizedAgent` — `AuthorizedAgent` only proves the *owner* can access the path agent; the extra check stops a sibling-spoof. Connector keys are double-fenced: OFF the connector auth-entry allowlist (`dependencies.py` — a connector key never reaches the router) AND `_reject_connector_principal` (explicit). Ghost keys are 403'd (reminders deliberately OUT of the #69 `_EPHEMERAL_ALLOWED_ROUTES` fence — a pending reminder can outlive a discarded ghost).
- **Create idempotency (Invariant #18)**: scope `make_agent_scope(name)`; the key is a caller `Idempotency-Key` header if present, else `idempotency_service.derive_reminder_key(name, message, raw_fire_spec)` over the **raw** input (`delay_seconds=N` / `fire_at=<string>` literal, NOT the resolved instant — a `delay_seconds` retry resolves `now+delay` differently each call, so a resolved-instant key would defeat dedup). On a completed replay whose stored reminder is still `pending`/`firing` → return it with `X-Idempotent-Replay: true`; a terminal (cancelled/fired) or missing stored reminder → proceed as a **fresh create** and `upgrade_snapshot` (so cancel-then-recreate-identical yields a fresh pending reminder). In-flight replay → 409. `idempotency_service.fail(idem)` is called ONLY in the pre-insert `except HTTPException` (bounds fail before the row exists) — never after the insert (a post-insert release would let a retry double-insert).
- **Tenant-scoped by-id (security F2)**: `cancel` maps `db.cancel_reminder(name, id)` outcomes → `cancelled`/`already_cancelled` 200, `conflict` 409, `not_found` 404. A foreign id → uniform 404 (no 200/404 oracle).

### Service — `src/backend/services/reminder_service.py`
Thin create orchestration (Invariant #1 — idempotency + response headers stay in the router). `create_reminder`:
1. Resolve the fire instant: `delay_seconds` → `now + delta`; else `parse_iso_timestamp(fire_at)`. Written via `to_utc_iso` (ISO-Z).
2. **Min/max window** against the *resolved* instant: `< REMINDER_MIN_DELAY_SECONDS` (60, ≥ the 60s reload interval) → 400; `> REMINDER_MAX_DELAY_SECONDS` (30d, < the 180-day soft-delete name reservation) → 400.
3. **Timeout clamp** to the agent cap (`db.get_execution_timeout`, #929 parity) → 400 `reminder_timeout_exceeds_agent_cap`.
4. **Pending cap** (`MAX_PENDING_REMINDERS_PER_AGENT`, 25, real DB count) → 429.
5. **Durable daily cap** (`MAX_REMINDERS_PER_AGENT_PER_DAY`, 100, rolling-24h `count_reminders_created_since`) → 429. The non-fail-open backstop against self-perpetuation (a reminder can itself call `set_reminder`).
6. Provenance (`owner_id`/`created_by_email` = the resolved user, `source_agent_name`/`source_mcp_key_id`) → `db.insert_reminder`.

Router-level flood guard: `rate_limiter.enforce("agent_reminder:{name}", 30/60)` (fail-open).

### DB — `src/backend/db/reminders.py` (`RemindersOperations`)
SQLAlchemy Core over `agent_reminders` (Invariant #2). Every by-id op tenant-scoped (`agent_name` in the predicate). `insert_reminder`, `list_reminders(agent_name, status)`, `get_reminder(agent_name, id)`, `count_pending_reminders`, `count_reminders_created_since`, `cancel_reminder` (CAS `pending → cancelled`, returns `cancelled`/`already_cancelled`/`conflict`/`not_found`), plus retention `count_agent_reminders_candidates` + `prune_agent_reminders`. Delegated through `database.py`.

### Models — `src/backend/models.py`
`ReminderCreate` (`@model_validator`: `fire_at` XOR `delay_seconds`; `fire_at` ISO-validated **by delegating to the same `parse_iso_timestamp` the service uses, so validator-acceptance and parseability cannot diverge (#1831)**; `message` ≤4000 → 422; `raw_fire_spec()` for the idempotency key), `Reminder`, `ReminderSummary`. Env-tunable bound constants live here.

## Scheduler Layer — `src/scheduler/` (arm / fire / reconcile / recover)
The scheduler reads/writes `agent_reminders` directly through its own dual-backend DB layer (`get_connection()` + `_PgConn`/`_PgCursor` shims — same split as `agent_schedules`).

### DB — `src/scheduler/database.py` (all writes `conn.commit()` — load-bearing)
`get_active_reminders()` reads `pending` ∪ `firing` JOINed to `agent_ownership`, filtering `deleted_at IS NULL` (a soft-deleted agent's reminder never fires into a nonexistent container) AND `autonomy_enabled = 1` (disabling autonomy holds pending reminders; they resume, past-due-fire, when re-enabled). `claim_reminder_firing` is the **single-fire CAS** — `UPDATE … SET status='firing', firing_at=?, fire_attempts=fire_attempts+1 WHERE id=? AND status='pending'`; committed; `rowcount>0`. The mutated predicate is in the outer WHERE, so a losing concurrent updater's CAS is a no-op (learnings #1081 #70). Plus `mark_reminder_fired` (`firing→fired`), `release_reminder_to_pending` (`firing→pending`), `mark_reminder_failed` (`firing→failed`), `set_reminder_execution`, `get_reminder_by_id`. `_row_to_reminder` parses `fire_at`/`firing_at` via `parse_scheduler_ts` → **naive UTC**, so a `fire_at < datetime.utcnow()` past-due compare never hits the offset-aware-vs-naive `TypeError` (#1472/#1474); APScheduler runs `timezone=pytz.UTC`, so a naive-UTC `DateTrigger` is interpreted as UTC.

### Service — `src/scheduler/service.py`
- `_schedule_reminder_job(reminder, run_at)` — `add_job(self._execute_reminder, DateTrigger(run_date=run_at), id=f"reminder_{id}", replace_existing=True)`. Deterministic id + `replace_existing` ⇒ re-arming is idempotent.
- `_execute_reminder(...)` — the fire handler (at-least-once):
  1. **Claim** (`claim_reminder_firing`, CAS `pending→firing`, increments `fire_attempts`). `False` → won by another fire / cancelled / already firing → skip (single-fire gate; cancel-safe).
  2. **Create the execution row up-front** via `create_execution(schedule_id="__manual__", triggered_by="reminder")` (real id, RUNNING; `__manual__` is PG-safe — no FK, the manual/loop path already inserts it) → `set_reminder_execution`.
  3. **Dispatch** `_call_backend_execute_task(..., execution_id=<real id>)` — the real id auto-stamps `Idempotency-Key: sched:{id}` and `_poll_and_finalize` polls the real row. No `_call_backend_execute_task` change needed.
  4. **Outcome** (Codex C2 — never blind-FAILED): a dispatch **TimeoutException** = outcome-unknown → `mark_reminder_fired` (assume dispatched; the execution row is NOT force-FAILED — the poll finalizes it, and retrying would double-execute on a fresh id). A **clean pre-start failure** (non-200 503-warmup/5xx or connection error — task never started) marks the attempt's execution row FAILED (status-guarded, `service.py:989` pattern), then bounded retry: `fire_attempts ≥ MAX_REMINDER_FIRE_ATTEMPTS` (default 3) → `mark_reminder_failed` (terminal, visible in `list`); else `release_reminder_to_pending` → the reconcile re-arms. **This is what makes AC #3 hold** — a reminder due during a backend restart (503) retries instead of being permanently consumed.
- `_reconcile_reminders()` — **fail-open** (its whole body wrapped; a not-yet-migrated `agent_reminders` table or any error is a logged no-op, never a boot crash-loop). Reads `get_active_reminders()`; for each `pending` with no live `reminder_{id}` job → arm (`fire_at < now` → `now+5s`, past-due reconcile mirroring `_recover_pending_retries`); for each stale `firing` (old `firing_at`, no live job = crash mid-fire) → FAIL an orphan RUNNING execution row (status-guarded) then bounded release/fail. Wired into `initialize()` (boot recovery, after `_recover_pending_retries`, own try), `_sync_schedules()` (its **own** try/except, NOT under the cron-sync try — Codex C5, so a cron error doesn't starve reminder pickup), and `reload_schedules()` (the full-reload path only rebuilds cron/process jobs — Codex C6). Latency ≤ one `schedule_reload_interval` (≤60s).

## Single-fire + delivery semantics
| Layer | Mechanism | Protects against |
|---|---|---|
| Topology | Fires only from the single-instance scheduler | Cross-worker double-fire |
| DB CAS claim | `claim_reminder_firing` (`WHERE status='pending'`, **committed**, mutated predicate in outer WHERE) | Reconcile racing an armed job; a cancelled-but-fired reminder; a future multi-instance scheduler |
| Per-attempt idempotency | `Idempotency-Key: sched:{execution_id}` (fresh id per attempt) | HTTP-layer resend of the same attempt within 24h |

Delivery is **at-least-once, bounded, observable**. The `firing` intermediate makes the fire atomic (single-fire) while letting a fire that didn't land be retried. Accepted tradeoff: a scheduler crash *after* the backend started an attempt but *before* `mark_reminder_fired` → the reclaim retries with a fresh id → the task may run twice (rare; bounded by attempts; strictly better than at-most-once silent-drop). The TimeoutException path deliberately does NOT retry (outcome unknown → assume dispatched).

## Data + migration (Invariant #3 — 5-track)
`agent_reminders` table, dual-track migration: `db/schema.py` (TABLES + 2 indexes) + `db/migrations.py` (`_migrate_agent_reminders_table`) + Alembic `0028_agent_reminders` (off head `0027_users_github_pat`) + `db/tables.py` MetaData + `db/agent_cleanup.py` `AGENT_REFS` CASCADE. The `AGENT_REFS` entry is CI-blocking (`test_agent_cleanup_parity`) AND load-bearing: `cascade_rename` re-keys a renamed agent's reminders (else they fire against a nonexistent agent), `cascade_delete` (#834 purge) wipes them, and L-03's orphan scan covers the table. `source_agent_name` (initiator provenance) is intentionally NOT registered (audit-only, mirrors `agent_loops.source_agent_name`). Status machine: `pending → firing → fired` / `firing → pending|failed` / `pending → cancelled`.

## Retention (#1296 / #1638–#1644 discipline)
`agent_reminders_retention_days` (default 90, `0`=off). `cleanup_service._sweep_agent_reminders_retention` DELETEs terminal (`fired`/`cancelled`/`failed`) rows past the window (`pending`/`firing` never deleted), chunked, gated through the **#1644 blast-radius guard** (`_guard_allows` + `_after_guarded_prune`, default `MAX_ROWS_PER_SWEEP` floor). Registered in `RETENTION_OPS_KEYS` (surfaced at `GET /api/settings/retention`, protected from `/ops/reset`, logged at boot); NOT a community-floor key. Wide/safe default per the #1638 floor rule.

## The new `triggered_by="reminder"` — all three constants
`_TRIGGER_BUCKETS` (`db/schedules.py`, first-class "Reminders" bucket + `_BUCKET_ORDER`), `_AUTONOMOUS_TRIGGERS` (`task_execution_service.py` — a reminder fires unwatched, so a FAILED reminder earns an operator alert), `_VALID_TRIGGERS` (`routers/executions.py` — the fleet Executions `?triggered_by=` filter). Deliberately NOT in `ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` (#1083) — reminders use the safe synchronous scheduler dispatch, no fire-and-forget callback in v1.

## Tests
- **Backend** (`tests/unit/test_1296_reminders.py`, 33): migration + live-`select()` tables.py accessor guard, the three trigger constants, `ReminderCreate` XOR, `derive_reminder_key` raw-input stability, db round-trip / tenant-scope / cancel CAS states, retention (terminal pruned, pending+firing kept), `reminder_service` bounds (min/max window, timeout>cap, pending 429, daily 429), router self-gate ATTACK (sibling 403, connector 403), tenant-scope 404, idempotency edges (dup replay, in-flight 409, cancel-then-recreate-fresh), cancel outcomes, list default-pending.
- **Scheduler** (`tests/scheduler_tests/test_1296_reminders.py`, 17 — run directly, NOT in the unit sweep): committed single-fire CAS + multi-connection contention (exactly one wins, Codex C8), `_execute_reminder` outcomes (claim-loss / success + real id / timeout=assume-dispatched-no-force-FAILED / clean-failure-retry / bounded-failed), `_reconcile` (arm-once, past-due→now+5s, stale-firing reclaim, Z-suffix no-raise, missing-table no-op, soft-delete/autonomy-off filtered), reload path (Codex C6).

## Rollout
Additive + inert: empty table ⇒ `get_active_reminders` returns `[]` ⇒ no jobs armed ⇒ zero behavior change. No feature flag (the capability only exists once an agent calls `set_reminder`). Ship backend + scheduler together (an old scheduler image never fires — fails safe, durable rows wait; a new scheduler image tolerates the not-yet-migrated table via the fail-open reconcile).

## Related
- [run-agent-loop.md](run-agent-loop.md) — the immediate/sequential sibling (#740)
- [scheduler-service.md](scheduler-service.md) — the standalone scheduler + RETRY-001 machinery this clones
- [idempotency-keys.md](idempotency-keys.md) — the trigger-boundary dedup (Invariant #18)
- [cleanup-service.md](cleanup-service.md) — the retention sweep host

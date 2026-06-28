# Feature Flow: Correlated-Failure / Thundering-Herd Controls (#1085)

> **Status**: Shipped 2026-06-28. Agent-side jitter ships unflagged; backend
> controls (rate caps + shared-cause pause) are **default-OFF** behind one
> master flag `REDELIVERY_GOVERNOR_ENABLED`. **No DB schema change** — all state
> is Redis. Everything **fail-open**.

## Problem

The #1083 fire-and-forget callback path is a **live re-delivery primitive**.
After a routine backend restart, every agent that completed a turn mid-restart
re-sends its persisted terminal envelope (`result_callback.resend_pending_results`
startup sweep) **plus** any in-flight callback retries — all POSTing
`/api/agents/{name}/executions/{id}/result` with **pure exponential backoff, no
jitter**. With a fleet of ~N agents this is a synchronized thundering herd
against one endpoint. Worse, if the failures share a cause (Claude-API outage,
an expired platform key, a bad skill pushed fleet-wide), all N agents retry the
*same doomed work* in lockstep, amplifying the outage instead of riding it out.

Pull mode (Epic #1045/#1081) will make re-delivery a first-class, at-least-once
mechanism — so these controls are built now against the live callback path, as
**reusable primitives** pull consumes unchanged.

## Goal

Three controls, composable and independently safe:

1. **Jitter** every re-poll/reconnect/retry so a fleet desynchronises.
2. **Rate caps** (per-agent + fleet-wide) bound how fast re-deliveries are
   admitted — overflow is *throttled, never dropped*.
3. A **shared-cause pause** that halts re-delivery fleet-wide while a correlated
   fault is in progress, then auto-recovers.

## Part A — Jitter (agent-side, `docker/base-image/agent_server/services/result_callback.py`)

- **`_deliver` backoff → decorrelated jitter.** `_jittered_backoff(prev_sleep)`
  returns `min(_BACKOFF_CAP, uniform(_BACKOFF_BASE, max(base, prev)*3))` (the AWS
  "decorrelated jitter" recipe). It **self-paces** (the ceiling grows with the
  previous sleep) *and* **spreads** (uniform), unlike pure exponential (lockstep:
  every agent on the identical schedule) or full jitter (`uniform(0, cap)`, which
  discards the floor and can hot-loop). The deadline clamp is unchanged.
- **`Retry-After` floor.** On a transient (incl. the Part B/C 503) the response's
  `Retry-After` is parsed (`_parse_retry_after`, integer-seconds form) and used as
  a floor on the next sleep — so a throttled agent actually waits the hinted time.
- **Startup-sweep spread (highest-value).** `resend_pending_results` does a
  one-shot `sleep(uniform(0, _SWEEP_INITIAL_JITTER_SECONDS=60))` so ~N restarting
  agents smear the t≈0 burst over a minute, plus a small per-envelope
  `sleep(uniform(0, _SWEEP_PER_ENVELOPE_JITTER_SECONDS=5))`.
- **Backend loop jitter** (`src/backend/main.py`): the capacity-maintenance loop
  period is `60 + uniform(0, 15)` and its startup stagger `15 + uniform(0, 2)` so
  replicas don't realign their sweeps after a coordinated restart.
- **Parity:** the jitter helper is **duplicated** agent-side, not vendored.
  Invariant #5 governs mirrored *API/policy* contracts; the backend never
  inspects the agent's backoff, so this is utility math, not a contract.

## Part B — Re-delivery rate caps (backend, `routers/agents.py`)

In `agent_execution_result`, after the fail-closed gates (auth → ownership 404 →
**replay-ACK** → **marker-409** → body-413) and **before** `apply_result`:

```
auth → 404 → replay-ACK(200) → marker-409 → 413 → [governor gate] → apply_result
```

Order is load-bearing: an already-final row must still **ACK `{replayed:true}`**
while the fleet is throttled (replays are never starved), and a non-async row must
still be permanently **409**'d — only a legit accepted async terminal is ever
throttled.

The gate (flag-gated) calls `services/rate_limiter.check` (NOT `enforce`) on two
keys and raises **503 + Retry-After** on either over-limit:

| Key | Default cap |
|-----|-------------|
| `redelivery:fleet` | `REDELIVERY_FLEET_LIMIT`/`_WINDOW_SECONDS` = 600 / 60s (~10/s) |
| `redelivery:agent:{name}` | `REDELIVERY_AGENT_LIMIT`/`_WINDOW_SECONDS` = 20 / 60s |

`Retry-After = max(fleet.retry_after, per_agent.retry_after, 1)`.

**Never-drop guarantee.** `rate_limiter.check` is fail-open (in-process fallback
on a Redis outage). **503 ∉ `result_callback._PERMANENT_STATUSES`**, so a
throttled callback stays persisted on disk and retries on its jittered backoff;
the startup sweep + lease reaper remain the backstops. (Cross-checked by a test.)

## Part C — Shared-cause pause (`src/backend/services/redelivery_governor.py`)

A leaf service over the shared fail-open breaker Redis client
(`redis_breaker_util.get_breaker_redis`), singleton `get_redelivery_governor()`,
transport-agnostic so pull's future `/api/internal/tasks/{id}/result` reuses it.

### Record (distinct-agent detector)

Hooked in `task_execution_service.apply_result` on the **CAS-`won` failure
branch** (so a replayed/late callback never double-counts) and gated on the
master flag: when the terminal's `error_code.value ∈ {auth, billing}`, call
`record_terminal_failure(agent_name, code)`.

> **Enum gotcha:** the recorder compares `envelope.error_code.value`, NOT
> `error_code in (AUTH, BILLING)`. `TaskExecutionErrorCode` is a `@dataclass`-
> decorated fieldless str-Enum, whose generated zero-field `__eq__` makes **any
> two members compare `==`**. (The adjacent #526 AUTH-breaker line shares this
> latent quirk — flagged, but left out of scope.) Value-compare is correct
> regardless.

State is a Redis ZSET `governor:corr_failures` (member=`agent_name`, score=now),
updated in one pipeline: `ZREMRANGEBYSCORE` (trim the window) → `ZADD` (a repeat
failure just refreshes the same member's score) → `ZCARD` → `EXPIRE`. **`ZCARD` is
the distinct-agent count** — a fleet cause is *many different* agents failing, so
one crash-looping agent (one member, refreshed) can never arm the pause.

### Pause flag + auto-expiry

On `ZCARD ≥ CORRELATED_FAILURE_THRESHOLD` (default 20) within
`CORRELATED_FAILURE_WINDOW_SECONDS` (120s): `SET governor:pause 1 EX
CORRELATED_PAUSE_TTL_SECONDS` (300s). A persisting storm re-arms (refreshing the
TTL). **There is no explicit unpause** — the TTL lapse *is* the recovery, so
there is no stuck-pause failure mode. `is_paused()` is **fail-open** (Redis
unreachable → `False`).

### Read points (all flag-gated)

| Read point | Behavior while paused |
|---|---|
| Callback endpoint (`routers/agents.py`) | 503 + jittered `Retry-After` |
| Lease reaper (`cleanup_service._sweep_stale_slots`) | early-return — keep async rows RUNNING (not FAILED→`LEASE_EXPIRED`) so a throttled-then-resumed callback still lands |
| Capacity drain (`capacity_manager.run_maintenance`) | skip `drain_orphans_all()` + `_backstop_open_breaker_backlog()`; keep the 24h `expire_stale()` |

**TTL-vs-lease bound.** `CORRELATED_PAUSE_TTL_SECONDS` (300) stays under the lease
window (`timeout + SLOT_TTL_BUFFER`, buffer=300), so a held row is not failed
during the pause; and a genuinely-late SUCCESS still corrects any reaper FAIL via
the `apply_result` CAS (SUCCESS wins over a non-cancelled terminal).

### BILLING populated (producer change)

`result_callback._STATUS_MAP` now maps agent `429 → ("billing", "rate_limit")`
(the enum existed but was never set), so a fleet-wide Claude-API 429 storm arms
the detector alongside AUTH. `terminal_reason` stays `rate_limit`, so the
cancel-relabel guard (`_is_auth_or_rate`, and its backend mirror) still treats it
as auth/rate — an auth/rate terminal is never reclassified as a clean
cancellation.

## Config (`src/backend/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `REDELIVERY_GOVERNOR_ENABLED` | `false` | Master switch — gates Parts B+C. Inert until ON; a flip back is the whole rollback. |
| `REDELIVERY_FLEET_LIMIT` / `_WINDOW_SECONDS` | `600` / `60` | Fleet re-delivery cap (~10/s). |
| `REDELIVERY_AGENT_LIMIT` / `_WINDOW_SECONDS` | `20` / `60` | Per-agent re-delivery cap. |
| `CORRELATED_FAILURE_THRESHOLD` | `20` | Distinct agents failing AUTH/BILLING to arm the pause. |
| `CORRELATED_FAILURE_WINDOW_SECONDS` | `120` | Rolling distinct-agent window. |
| `CORRELATED_PAUSE_TTL_SECONDS` | `300` | Pause flag TTL (auto-expiry; < lease window). |
| `REDELIVERY_PAUSE_RETRY_AFTER_SECONDS` | `30` | Retry-After hint on a 503 (jittered ±50%). |

Surfaced as `redelivery_governor_enabled` in `GET /api/settings/feature-flags`
for operator observability during soak (mirrors `mcp_agent_chat_pull_enabled`;
not a UI surface).

## Fail-open matrix

| Component | Redis trouble degrades to |
|---|---|
| `rate_limiter.check` (caps) | in-process per-worker fallback → allow |
| `redelivery_governor.is_paused` / `should_hold_reaper` | `False` (never pause) |
| `redelivery_governor.record_terminal_failure` | no-op (no detection, no arm) |
| recorder hook in `apply_result` | wrapped in try/except → never blocks a terminal |

Nothing here can block or drop a terminal — the controls only ever *reduce*
pressure.

## Redis keys

| Key | Type | Purpose |
|---|---|---|
| `ratelimit:redelivery:fleet` | ZSET | fleet re-delivery sliding window (rate_limiter) |
| `ratelimit:redelivery:agent:{name}` | ZSET | per-agent re-delivery sliding window |
| `governor:corr_failures` | ZSET | distinct agents failing AUTH/BILLING in the window |
| `governor:pause` | STRING (TTL) | fleet pause flag, auto-expiring |

## Tests (`tests/unit/`)

- `test_1085_jitter_spread.py` — `_jittered_backoff` band + variance + upward
  self-pace; `Retry-After` floor + deadline clamp; sweep applies initial +
  per-envelope sleeps.
- `test_1085_redelivery_caps.py` — callback 503 + Retry-After on fleet/per-agent
  cap; fail-open admits; replay-ACK while capped; flag-off bypass; cross-check
  503 ∉ `_PERMANENT_STATUSES`.
- `test_1085_correlated_pause.py` — distinct-agent arming; same-agent = 1
  distinct; non-correlated codes ignored; auto-expiry; fail-open; recorder gated
  on CAS `won` + flag; 429→BILLING map; reaper + drain hold-off; callback 503
  while paused.
- `test_1085_restart_storm_sim.py` (**in-suite soak gate**) — N concurrent
  callbacks → caps admit at most `limit` (rest throttled, never dropped),
  per-agent cap isolates a crash-looper; jitter spreads N restart arrivals across
  the window; decorrelated backoff desynchronises retriers.

## Manual soak procedure

On a sibling stack (`-p trinity-1085-test`, remapped ports — never against a
running dev stack):

1. Set `REDELIVERY_GOVERNOR_ENABLED=true` + `DISPATCH_ASYNC=true` and boot a
   fleet with schedules/webhooks producing async turns.
2. Force a shared cause (e.g. revoke the platform key so turns return AUTH, or
   drive 429s) so many agents fail at once.
3. `redis-cli ZCARD governor:corr_failures` — watch it climb to the threshold;
   `redis-cli GET governor:pause` flips to `1` with a TTL (`TTL governor:pause`).
4. Confirm callback POSTs get **503** (agent logs show the jittered backoff +
   `Retry-After` floor), the reaper does **not** FAIL async rows during the
   pause, and the capacity drain holds.
5. Restore the cause; after the pause TTL lapses, confirm the fleet drains
   without a synchronized retry spike (arrivals spread, no endpoint storm).

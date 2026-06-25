# Feature Flow: Subscription Auto-Switch (SUB-003)

> **Requirement**: `docs/requirements/SUB-003-subscription-auto-switch.md`
> **Issue**: #153, threshold + scope update #441, hot-reload #1089, classifier unify #1088
> **Status**: Implemented (2026-03-21), updated 2026-04-25 (#441), 2026-06-13 (#1089 — switch hot-reloads instead of recreating the container), 2026-06-21 (#1088 — auth classifier extracted to a shared module)

## Overview

Automatically switches an agent to a different subscription on the first
subscription failure — either a rate-limit (429) **or** an auth-class
failure (401/403/credit balance/expired token). Default ON (opt-out via
system setting `auto_switch_subscriptions`).

## Flow

```
Agent container detects rate limit OR auth failure → returns 429/503 to backend
    ↓
Backend catches the failure in:
  - TaskExecutionService.execute_task()  [schedules, MCP, agent-to-agent, async]
  - chat_with_agent()                     [interactive chat sync path]
    ↓
Classify:
  - 429 → handle_subscription_failure(..., failure_kind="rate_limit")
  - 503 OR is_auth_failure(error_msg) → handle_subscription_failure(..., failure_kind="auth")
    ↓
Check: setting enabled? → No → return None
    ↓ Yes
Check: agent has subscription? → No → return None
    ↓ Yes
Record failure event, get count (informational; no threshold gate)
    ↓
Find best alternative subscription (fewest agents, not rate-limited in last 2h)
    ↓
No alternative? → return None (log warning)
    ↓ Found
Switch: DB update + token HOT-RELOAD (not container recreate, #1089) + log activity + send notification
    ↓
Return switch result → caller surfaces 429/503 with auto_switch info + retry hint
```

## Token Application: Hot-Reload, not Recreate (#1089)

The switch step (and the manual `PUT /api/subscriptions/agents/{name}` sub→sub
path, and the `POST /api/subscriptions` key-rollover upsert) applies the new
token via `_hot_reload_subscription_token(agent_name)` — a POST to the
agent-server `POST /api/credentials/reload-token` that mutates the running
container's `CLAUDE_CODE_OAUTH_TOKEN` env. The **next** Claude subprocess uses
the new token while **in-flight** turns finish on the old one, so a rotation no
longer kills every parallel execution (#1037). Falls back to the previous
`_restart_agent` recreate on a 404 (old base image), transport failure, or a
missing token. Durability across a plain restart is handled by the
`/var/lib/trinity/oauth-token` writable-layer override that `startup.sh` reads
before launching the agent server. The override is created **atomically at mode
`0600`** via `os.open(..., O_CREAT, 0o600)` — not `write_text()`+`chmod()`, which
would leave the token file briefly world-readable under the process umask between
create and chmod. Canonical home: architecture.md
§"Subscription Token Rotation via Hot-Reload".

## Trigger Surface

| Layer | Signal | Failure kind |
|-------|--------|--------------|
| HTTP 429 from agent | rate-limit reached | `rate_limit` |
| HTTP 503 from agent | auth failure (#285 detection) | `auth` |
| Error message matches `AUTH_INDICATORS` | credit balance / expired token / unauthorized / etc. | `auth` |

`AUTH_INDICATORS` (canonical list in
`src/backend/services/failure_classifier.py::is_auth_failure`, #1088):
`credit balance`, `unauthorized`, `authentication`, `credentials`,
`forbidden`, `401`, `403`, `oauth`, `token expired`, `not authenticated`.
`is_auth_failure` also short-circuits to `False` on any
`NON_AUTH_KILL_MARKERS` substring (SIGKILL/SIGTERM/SIGINT, shell-encoded
137/143/130, OOM/memory-cgroup) so an externally-killed subprocess never
trips SUB-003 (#904). `subscription_auto_switch.py` re-exports
`is_auth_failure` unchanged so existing importers and their test patch
targets keep working.

The scheduler runs in a separate container and cannot import from
`backend.services`, so the classifier is vendored byte-identically at
`src/scheduler/failure_classifier.py` (the scheduler uses it for
**log-labelling only** — it picks the `logger.error` wording, never gates
a switch). Byte-identity between the canonical copy and the mirror is
enforced by `tests/unit/test_904_sigkill_no_false_auth.py::TestBackendSchedulerParity`
— edit the backend copy and regenerate the mirror; do not hand-sync.

## Files

| Layer | File | Purpose |
|-------|------|---------|
| DB | `src/backend/db/subscriptions.py` | Rate-limit event CRUD, best-alternative selection |
| DB | `src/backend/db/migrations.py` | `subscription_rate_limit_events` table |
| DB | `src/backend/database.py` | Delegation methods |
| Service | `src/backend/services/subscription_auto_switch.py` | Orchestration: detect, switch, log, notify. Re-exports `is_auth_failure` from `failure_classifier` (#1088) |
| Classifier | `src/backend/services/failure_classifier.py` | **Canonical** auth-class classifier (#1088): `is_auth_failure`, `AUTH_INDICATORS`, `NON_AUTH_KILL_MARKERS` |
| Classifier | `src/scheduler/failure_classifier.py` | Byte-identical vendored mirror for the separate scheduler container (#1088) |
| Router | `src/backend/routers/subscriptions.py` | Setting GET/PUT endpoints |
| Service | `src/backend/services/task_execution_service.py` | 429 interception for all execution paths (schedules, MCP, agent-to-agent) |
| Router | `src/backend/routers/chat.py` | 429 interception in chat proxy + background tasks |
| Frontend | `src/frontend/src/views/Settings.vue` | Toggle in Subscriptions section |
| Tests | `tests/test_subscription_auto_switch.py` | Smoke tests |
| Tests | `tests/unit/test_904_sigkill_no_false_auth.py` | #904 SIGKILL/OOM no-false-AUTH coverage + `TestBackendSchedulerParity` byte-identity guard on the canonical↔mirror classifier (#1088) |
| Tests | `tests/unit/test_subscription_auto_switch_pingpong.py` | Unit regression for #444 ping-pong prevention; `TestRateLimitAging` (#476) pins 2h-window correctness; `TestHotReloadSwitch` + `TestKeyRolloverFanOut` (#1089) pin the hot-reload helper, auto-switch wire-in, and key-rollover fan-out |
| Tests | `tests/unit/test_subscription_reassign_hotreload.py` | #1089 — manual sub→sub hot-reload under the lock (no `container_stop`), mode-change still recreates, register/upsert key-rollover fan-out, and the admin-only gate on `register_subscription` (non-admin → 403 before any create or fan-out) |
| Tests | `tests/unit/test_reload_token_endpoint.py` | #1089 — agent-server `POST /api/credentials/reload-token`: sets env, atomically writes the `/var/lib/trinity/oauth-token` override at `0600`, no `.env` write, `remove_api_key` pops `ANTHROPIC_API_KEY`, empty token → 400 |
| Tests | `tests/unit/test_subscription_auto_switch_no_cred_import.py` | Chain-level regression for #606 — pins `_restart_agent → start_agent_internal → inject_assigned_credentials` reaches the `lifecycle.py:155` `subscription_mode` short-circuit and never re-enters file-based credential import |
| Tests | `tests/unit/test_iso_cutoff.py` | Format parity between `iso_cutoff(N)` and `utc_now_iso()` (#476) |
| Util | `src/backend/utils/helpers.py::iso_cutoff` | Canonical cutoff helper for ISO-Z TEXT comparisons (#476) |
| Spec | `docs/requirements/SUB-003-subscription-auto-switch.md` | Full requirements |

## Database

### subscription_rate_limit_events

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| agent_name | TEXT | Agent that hit the limit |
| subscription_id | TEXT FK | Subscription that was rate-limited |
| error_message | TEXT | Error details |
| occurred_at | TEXT | ISO timestamp |

### System Setting

| Key | Default | Description |
|-----|---------|-------------|
| `auto_switch_subscriptions` | `"true"` (#441) | Enable/disable auto-switch |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/subscriptions/settings/auto-switch` | Get setting state |
| PUT | `/api/subscriptions/settings/auto-switch?enabled=true` | Toggle setting |

## Selection Strategy

1. Exclude current subscription
2. Order by agent_count ascending (load-balance)
3. Skip any subscription with rate-limit events in last 2 hours
4. Return first viable candidate, or None

## 2h Window Correctness (Issue #476)

The "last 2 hours" filter in `is_subscription_rate_limited()` and
`record_rate_limit_event()` now uses `iso_cutoff(2)` passed as a bound
parameter — not SQLite's `datetime('now', '-2 hours')`. The two functions
produce different string formats (`T` separator + `Z` suffix vs. space
separator, no suffix); lexicographic compare on the old form tripped at
position 10 (`T` (0x54) > space (0x20)), making every event with today's
date pass the filter regardless of clock time. Net effect before the fix:
events didn't age out until UTC midnight, and a single 429 early in the day
marked a subscription as rate-limited for the rest of the UTC day, draining
viable alternatives within minutes of the first real outage.

Same correction applied to the 24h cleanup cutoff and the parallel
`db/dashboard_history.py` / `db/schedules.py` stats queries that shared the
pattern.

## Cleanup Wiring

`cleanup_old_rate_limit_events()` deletes events with `occurred_at <
iso_cutoff(24)`. It is invoked hourly from `CleanupService._run_cleanup_inner`
(phase 6, every 12th cycle at the 5-min loop interval). Prior to #476 it had
zero production callers — the mis-comparison made the table look empty
anyway, so the omission was silent.

## Edge Cases

- **All subscriptions exhausted**: No switch, error surfaces as normal 429/503. `_perform_auto_switch` does **not** clear rate-limit events for the old subscription — those events are the signal that keeps `is_subscription_rate_limited()` truthful, so the just-drained sub is not offered as a candidate on the next cycle (issue #444).
- **API key agents**: Auto-switch only applies to subscription-based agents
- **Flip-flopping** (#441 update): the 2h skip-list (`is_subscription_rate_limited` ∧ `select_best_alternative_subscription`) is now the only thrash guard. Pre-#441 the threshold also required 2 consecutive 429s before switching, but that gated user-visible failures unnecessarily — the skip-list alone is sufficient because a just-drained sub stays flagged for 2h post-switch.
- **Concurrent switches** (#799/#1089): a per-agent `agent_switch_lock` serializes the assign+apply window so a manual `PUT /api/subscriptions/agents/{name}` reassignment can't interleave with a concurrent auto-switch. The `old_sub_id` snapshot is taken **inside** that lock, immediately before the DB assign — a concurrent switch therefore can't change the agent's subscription between the read and the assign (TOCTOU). Without this, a sub→sub swap could be mis-classified as an auth-mode change (or vice-versa) and routed into a needless container recreate instead of a hot-reload.
- **Cleanup**: Records older than 24h are pruned hourly by `CleanupService` (phase 6, #476); the 2h "is rate-limited" window drives candidate filtering independently of cleanup

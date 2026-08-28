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

**Shadow-proofing (#2114).** Post-#1999 the spawn env re-reads `.env` at every
spawn, so a stale `.env`-resident `ANTHROPIC_API_KEY` — which Claude Code
prefers over `CLAUDE_CODE_OAUTH_TOKEN` — silently shadowed the subscription
token, and SUB-003 mis-attributed the resulting identical auth failures to
each healthy subscription in turn (2h skip-list poisoning, "no viable
alternative"). Three coordinated pieces:

- `_hot_reload_subscription_token` sends `remove_api_key=True` **for Claude
  runtimes** (container label, claude-code default); non-Claude runtimes keep
  `False` — a legacy subscription row on a Gemini/Codex agent must not strip a
  `.env` key its own scripts may use.
- The endpoint's `remove_api_key=True` force-unsets **both** API-key spellings
  (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` — one shared
  `SUBSCRIPTION_SHADOW_KEYS` constant in `execution_env.py`) and returns
  `env_shadow`: names (never values) of force-unset keys the current `.env`
  still carries. The backend logs a WARNING naming agent + keys when
  non-empty — the durable operator signal at switch time.
- **Restart durability** is the agent server's own job: at boot,
  `arm_subscription_auth_guard()` arms the same force-unset overrides when the
  container baseline carries a truthy `CLAUDE_CODE_OAUTH_TOKEN` on a Claude
  runtime (the rotated override-file token is exported by `startup.sh` before
  the server launches, so it is always baseline). Without this, every plain
  stop/start re-opened the shadow until the next switch.

Observability: `env_drift_report` marks force-unset keys
`suppressed_for_spawn` (including keys absent from `.env`), so the drift
surface cannot show all-green over an active suppression; `build_execution_env`
logs a per-key memoized WARNING (names only) when a force-unset swallows a
value `.env` supplied, re-arming if the key is removed and re-added.

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
| Tests | `tests/unit/test_reload_token_endpoint.py` | #1089 — agent-server `POST /api/credentials/reload-token`: sets env, atomically writes the `/var/lib/trinity/oauth-token` override at `0600`, no `.env` write, `remove_api_key` pops both shadow keys + arms force-unset overrides + reports `env_shadow` names-only (#2114), empty token → 400 |
| Agent | `docker/base-image/agent_server/services/execution_env.py` | #2114 — `arm_subscription_auth_guard()` (boot-time force-unset on baseline-token Claude agents), `SUBSCRIPTION_SHADOW_KEYS`, per-key memoized suppression WARNING, `env_drift_report` `suppressed_for_spawn` marker |
| Tests | `tests/unit/test_2114_subscription_shadow_guard.py` | #2114 — arm trigger matrix (empty-token / non-Claude / terminal / platform-key / .env-managed all inert), override-layer semantics, drift-marker + memo observability, stale-.env-OAuth residual pinned |
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

## Selection Strategy (#2409)

1. **Filter (db):** exclude the current subscription; drop any subscription with a failure row in the last 2 hours (rate-limit OR auth, #2352); order `agent_count ASC, name ASC` — `db.list_viable_alternative_subscriptions`. Filter only, never a ranking.
2. **Rank (service):** read the survivors' cached provider snapshots in ONE `MGET` (never a probe) and sort furthest-from-the-nearest-wall first — the fuller of the 5h/7d windows — in 10-point bands with `agent_count` as the in-band tiebreak; a FRESH provider refusal is dropped; no usable reading ⇒ today's order — `services.subscription_auto_switch.select_best_alternative_subscription`
3. Return the first ranked candidate with its `why` (surfaced on the switch), or None — no survivors, or every survivor currently refused by the provider

## 2h Window Correctness (Issue #476)

The "last 2 hours" filter in `has_recent_subscription_failures()` (pre-#2352:
`is_subscription_rate_limited()`) and
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

- **All subscriptions exhausted**: No switch, error surfaces as normal 429/503. `_perform_auto_switch` does **not** clear rate-limit events for the old subscription — those events are the signal that keeps `has_recent_subscription_failures()` truthful, so the just-drained sub is not offered as a candidate on the next cycle (issue #444).
- **API key agents**: Auto-switch only applies to subscription-based agents
- **Flip-flopping** (#441 update): the 2h skip-list (`has_recent_subscription_failures` ∧ `select_best_alternative_subscription`) is now the only thrash guard. Pre-#441 the threshold also required 2 consecutive 429s before switching, but that gated user-visible failures unnecessarily — the skip-list alone is sufficient because a just-drained sub stays flagged for 2h post-switch.
- **Concurrent switches** (#799/#1089): a per-agent `agent_switch_lock` serializes the assign+apply window so a manual `PUT /api/subscriptions/agents/{name}` reassignment can't interleave with a concurrent auto-switch. The `old_sub_id` snapshot is taken **inside** that lock, immediately before the DB assign — a concurrent switch therefore can't change the agent's subscription between the read and the assign (TOCTOU). Without this, a sub→sub swap could be mis-classified as an auth-mode change (or vice-versa) and routed into a needless container recreate instead of a hot-reload.
- **Cleanup**: Records older than 24h are pruned hourly by `CleanupService` (phase 6, #476); the 2h "is rate-limited" window drives candidate filtering independently of cleanup
- **`.env`-resident `ANTHROPIC_API_KEY` on a subscription agent (#2114)**: suppressed from every spawn (see Shadow-proofing above). A *funded* key that was silently billing instead of the subscription flips to subscription auth after the base-image rebuild — matches the declared assignment; the managed path to API-key auth remains clear-subscription (which recreates). Known residual, deliberately unfixed: a stale `.env` `CLAUDE_CODE_OAUTH_TOKEN` still beats the rotated baseline token after a restart (different fix shape — precedence, not unset; pinned by `test_2114_subscription_shadow_guard.py::TestKnownResidual`)

## #471 (2026-08-19) — record-before-gate + failure_kind persisted

Two producer changes in `handle_subscription_failure`:

1. **The failure event is recorded BEFORE the `auto_switch_subscriptions` enabled gate.** The old order returned at the gate before `record_rate_limit_event`, so an operator who disabled auto-switch — exactly the population depending on manual visibility — got a permanently-zero pressure count on every #471 surface. Recording now happens against the pre-lock `sub_at_entry` snapshot (MORE correct than the old under-lock re-read: the failure genuinely happened on that subscription, and a stale failure — agent already switched — used to record nothing at all). Recording is a single INSERT and does not need the #799 lock, which protects the read→decide→assign window. Switch-suppression behavior is unchanged; pinned by `test_setting_disabled_blocks_switch` (pin deliberately flipped in #471) and `TestRecordBeforeGate`.
2. **`failure_kind` ("rate_limit" | "auth") is now PERSISTED** on `subscription_rate_limit_events` (the writer carried the param since #441/#792 but the table conflated broken-token auth failures with genuine quota 429s — and five observability consumers read this stream as "429 pressure"). NULL = pre-#471 row, bucketed as `unknown`; the 24h sweep retires those within a day. Dual-track migration: SQLite `rate_limit_events_failure_kind` + Alembic `0040_rl_events_failure_kind`.

`is_subscription_rate_limited` (2h) is unchanged and remains the machinery's own skip-list predicate — #471's `rate_limited_now` reuses it rather than defining a second window (the #2157 one-gate rule).

## #2352 (2026-08-20) — the skip-list predicate got its own name

Reusing one predicate for both jobs was the defect. `is_subscription_rate_limited` served the *display*
question ("is this throttled right now") and the *candidate-skip* question ("did this fail recently for
any reason"), and those want different answers about an auth failure: the badge must not call a dead
token a rate limit, while auto-switch must absolutely still refuse to move an agent onto it.

So the predicate SPLIT rather than narrowed:

| Predicate | Counts | Consumers |
|---|---|---|
| `is_subscription_rate_limited` | `failure_kind = 'rate_limit'` only (NULL excluded) | `decorate_usage`, `pressure_states`, `get_subscription_usage` — every badge/tile |
| `has_recent_subscription_failures` | every kind, NULL included | `list_viable_alternative_subscriptions`, `list_assignable_subscriptions` (db filters; the services rank — #2409) |

The kind-blind half is byte-for-byte the pre-split behaviour, so **nothing about switch safety changed**.
Narrowing the shared predicate in place would have passed every display assertion and quietly started
offering auth-failing subscriptions as switch candidates — an outage dressed as a remedy, and the #444
class by a new route. `tests/unit/test_2352_subscription_failure_kind_predicates.py` pins both halves and
the disagreement between them.

`_perform_auto_switch` still does not clear the old subscription's events — the note below now reads
against `has_recent_subscription_failures`, which is the predicate that keeps the just-drained (or
just-rejected) sub off the candidate list.

## #2409 (2026-08-27) — the selector ranks by cached headroom

**Before:** `db.select_best_alternative_subscription` returned the FIRST survivor of the 2h failure filter in `agent_count ASC` order and read no headroom, so an agent could be moved onto a subscription at 99% of its weekly window — and an *unused dead-token* subscription (no agents ⇒ no failure rows) sorted first. The switch reported success and the agent hit the wall again shortly after; nothing surfaced that the destination was a bad choice.

**Now — filter in the db, rank in the service, never a probe:**

1. `db.list_viable_alternative_subscriptions(current)` — every other subscription with no failure row in the last 2h (any kind, #2352), `agent_count ASC, name ASC`. Filter only; the name tiebreak makes the fallback order deterministic (SQLite leaves ties unspecified).
2. `services.subscription_auto_switch.select_best_alternative_subscription(current)` — runs under the per-agent switch lock via `asyncio.to_thread` (both reads are blocking): one `MGET` over `subscription:headroom:{id}` for the survivors (`subscription_headroom_service.cached_headroom_readings` — tri-state on Redis, never `get_headroom`/`_locked_probe`), then `rank_subscriptions`. Only survivors are ever read: a candidate the filter dropped is never looked at.
3. Ranking key `(tier, band, agent_count, primary, other, name)`, stable sort:
   - **measured** — fresh (≤2h `MAX_READING_AGE_SECONDS`), provider serving, weekly figure present. `primary` = the **fuller** of the two windows (the nearest wall: the #792 retry re-issues the turn on the destination immediately, so 7d 20% / 5h 98% fails within the minute; a lexicographic 7d-then-5h key never reaches its second term because 1-decimal utilization never ties). The 5h figure counts only while display-fresh (≤30 min — it can fully reset inside 2h); the 7d figure is a lower bound for the whole bound (monotonic within the provider's fixed window). Banded to 10 points so `agent_count` — the only key that moves as switches land — still spreads a storm within a band.
   - **unknown** — no snapshot, stale, transport error, 5h-only, non-finite figure, or a STALE refusal → exactly today's order (`agent_count ASC, name ASC`).
   - **refused** — a FRESH (≤30 min, the LIMIT-badge bound) probe 429, blocking window status, or rejected token → dropped. The one deviation from the issue's literal AC #3, recorded on the issue: moving an agent onto a subscription the provider is refusing is a guaranteed failed turn plus a failure row, and with several such subscriptions the agent walks through all of them (#444's class as a walk). The only case that now yields no target where it previously did is "every survivor is currently refused by the provider".
4. Any failure of the ranking half (Redis down, an import that resolved to the wrong module, a bug) → the db's order **plus a WARNING**; when every survivor is unknown and ambient refresh is OFF the log says the ranker is inert (an inert ranker must not look like a working one). The ranking never gates: the survivor set is untouched except for fresh refusals.
5. `_perform_auto_switch(..., destination_headroom=why)` surfaces the pick: `destination_headroom` (tier, both windows' utilization + reset, reading age, candidate count, auto-refresh flag) on the activity `details`, the notification `metadata` and the result, plus one clause in the notification text — *"It had the most headroom of the 3 alternatives (45% of its weekly limit and 8% of its 5-hour limit used)."* or *"No fresh headroom reading was available for it (ambient headroom refresh is off); it was chosen by load-balance order."* On a two-subscription install the ranking cannot change the pick, so the explanation IS the value there.

**Shared gate.** `classify_headroom` (ent#434) and the ranker consume ONE usability gate, `headroom_reading` (fresh? probe answered? window shape?) — the classifier became six lines of policy over it with byte-identical verdicts, pinned by a differential test against a frozen copy of the pre-#2409 function over the full age × status × window × threshold product. The ranker itself is threshold-free: the alert threshold is an operator knob that must not steer where agents land, and it is `0` when alerts are off. `MAX_READING_AGE_SECONDS` moved into the headroom service (`subscription_headroom_alerts` re-exports it) and is pinned above the sampler cadence, because a bound under it reads most candidates as unknown on an unwatched instance.

**New-agent auto-assign (#74) rides the same ranker:** `db.list_assignable_subscriptions()` → `services.subscription_service.select_subscription_for_new_agent()` — rank, then the first candidate whose token still decrypts (#340), so the common case costs one decrypt. `get_least_used_subscription` is gone; `database` is resolved at call time so the creation harnesses' per-test stubs are honoured.

**Tests:** `tests/unit/test_2409_headroom_ranked_switch.py` — gate table, frozen-oracle classifier parity, ranker tables (incl. the storm/band case and the reviewer's 7d 20% / 5h 98% example), MGET single-call, tri-state Redis, no-probe guard, poisoned-import fail-open beside a positive real-import proof, wiring off the loop, notification clause, new-agent assignment. The #444 / #2352 db-level pins moved to the list form unchanged in substance.

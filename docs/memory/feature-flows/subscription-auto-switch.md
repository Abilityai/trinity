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
  - model rejection (is_model_rejection, #3012) → NO switch, error_code MODEL_UNSUPPORTED
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
| Pull terminal `error_code=billing` (#2643) | a pull-owned turn the provider refused on quota | `rate_limit` |
| Pull terminal `error_code=auth` (#2643) | a pull-owned turn the provider refused on credentials | `auth` |

**Never a trigger (#3012):** Claude Code refusing the model — a CLI too old for the id
(`claude_code_version_too_old`) or an id the API does not know (`model_not_found`). No
subscription can fix it. The agent answers **400** with `detail.error_code: "model_unsupported"`
and the API's sentence; images older than #3012 answer 503 with
`[claude-code:unrecognized_model]`, so `failure_classifier.is_model_rejection` (markers in
`MODEL_REJECTION_MARKERS`, pinned to the producer by `test_3012_model_rejection.py`) is checked
first at every site above: `classify_switch_failure`, the TES except-handler, `/chat`
`_apply_sub003_autoswitch`, the pull `_switch_failure_kind`, and the #1083 callback (an
`auth` code carrying the marker becomes `model_unsupported`, so the AUTH breaker does not count
it). `is_auth_failure` itself returns False on a model rejection. The Workspace answers
`model_unsupported` (the agent's default was refused) or `invalid_model` (the client's pick,
which self-heals).

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
| Service | `src/backend/services/subscription_headroom_service.py` | `cached_headroom_readings` (#2409 ranker input), `recovery_verdict` (#2638 per-candidate skip-list override on positive evidence) |
| Envelope | `src/backend/services/execution_envelope.py` | `TaskExecutionResult.subscription_switch` — the switch dict stamped on every terminal return (#2638) |
| Portal | `src/backend/client_portal/service.py` | AUTH/BILLING branch consults `subscription_switch` before refusing: `auth_switched`/503/retryable vs `_usage_limit_detail` naming `earliest_known_reset` (#2638) |
| Router | `src/backend/routers/chat.py` | 429 interception in chat proxy + background tasks |
| Frontend | `src/frontend/src/components/settings/SubscriptionsPanel.vue` | Auto-switch, headroom and (#2638) API-key-fallback toggles in Settings → Subscriptions (moved out of `views/Settings.vue` in #471) |
| Frontend | `src/frontend/src/stores/subscriptions.js` | `fetchApiKeyFallback` / `setApiKeyFallback` (#2638) beside the auto-switch setting calls |
| Tests | `tests/test_subscription_auto_switch.py` | Smoke tests |
| Tests | `tests/unit/test_2638_subscription_switch_on_turn.py` | #2638 — `recovery_verdict` table, readmission through the real selector, pre-dispatch switch contracts, API-key-fallback setting semantics, `_with_switch` AST guard, four end-to-end 429 turns |
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
| `subscription_api_key_fallback` | `"true"` (#2638) | Last-resort fallback to the platform API key when the switcher declines; inert without a stored key |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/subscriptions/settings/auto-switch` | Get setting state |
| PUT | `/api/subscriptions/settings/auto-switch?enabled=true` | Toggle setting |
| GET | `/api/subscriptions/settings/api-key-fallback` | `{enabled, key_configured}` — fails open to enabled (#2638) |
| PUT | `/api/subscriptions/settings/api-key-fallback?enabled=true` | Toggle the API-key fallback (#2638) |

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

## #2638 (2026-09-09) — the turn COMPLETES on another subscription

**Before:** a Workspace message to an agent whose subscription was rate-limited failed outright — "The agent has reached its usage limit and can't respond right now. Please try again later." — the message was lost to a FAILED execution, and the client was told the failure was **not retryable**. Every mechanism above was working; four gaps between them left the turn failing anyway.

### 1. The 2h skip-list is now overridable — on evidence, per candidate

`list_viable_alternative_subscriptions` drops any subscription with **any** failure event in a flat 2h window. That window is a proxy for "the provider is still refusing it", and on a two-subscription install a single stale event is the difference between a completed turn and a user watching their message die. #2320's own evidence was exactly this: *"no viable alternative — the whole pool was exhausted"*, with an alternative sitting right there.

The exclusion is now overridden per candidate by `subscription_headroom_service.recovery_verdict`, on **positive evidence only**:

| verdict | evidence |
|---|---|
| `serving_now` | a FRESH reading says the provider is not refusing this token. Ground truth about now beats an inference from past failures (#447's rule), and it is the same evidence `rank_subscriptions` already trusts in the other direction when it drops a refusing candidate. |
| `window_reset` | no fresh reading, but a BLOCKED window's own reset instant has elapsed **and predates the failure**. The window the subscription failed in has rolled over, so the event is about a quota that no longer exists. |
| `None` | everything else. |

Three properties keep #444's ping-pong closed:

- **Absence of evidence readmits nothing.** #444 was caused by *forgetting* a failure; nothing here forgets one.
- **A fresh refusal does not fall through to the instant arm.** The strongest signal available points the other way, so the weaker one is not consulted.
- **The failure must PREDATE the reset.** Without that ordering, a subscription that 429'd a minute *after* its window rolled over — i.e. one that is exhausted again — would be readmitted on a reset it had already consumed.

Instants are read from an **aged** snapshot (`RECOVERY_INSTANT_MAX_AGE_SECONDS`, bounded by the snapshot's own 7-day Redis TTL) on the asymmetry this codebase already states (#447/#2396): a utilisation *number* decays, an *instant* does not.

Two interactions worth stating because both would make the feature inert:

- **The fail-open path readmits nobody.** `select_best_alternative_subscription`'s `except` degrades the whole ranking half to the pre-#2409 load-balance order, and that branch is precisely where the evidence could not be read — so the skip-list stands. (It also now returns `None` on an empty survivor set instead of indexing into it; the old early return moved inside the `try`.)
- **A `window_reset` candidate is handed to the ranker as UNKNOWN.** Its reading's `blocked` flag describes the window that just rolled over, and `rank_subscriptions` drops a blocked candidate as `refused` — so the readmission would be thrown straight back out in exactly the case it exists for. The number is stale and the flag is about a quota that no longer exists, so `unknown` is the honest tier; it still ranks, after any measured candidate, in load-balance order. (`serving_now` readmissions are untouched — their reading is fresh and says the provider is serving.)

db layer: `list_recently_failed_alternatives` (the filter's complement) and `last_failure_at_by_subscription` (kind-blind, like the predicate it sits beside). Both share `_list_all_alternatives` with the filter, so "the other subscriptions" is one query asked twice with different predicates rather than two queries that can disagree about which rows exist.

### 2. The switch can happen BEFORE the first dispatch

SUB-003 has always been reactive: dispatch → refused → switch → re-issue once. That works, but the first message after a subscription hits its wall always burns a failed attempt — and on the Workspace that attempt is a person watching their message fail.

`ensure_serviceable_subscription(agent)` runs immediately before the first POST, from `_call_agent_with_retries`. It switches when the assigned subscription is **already known not to serve**:

- a fresh cached provider reading that is `refusing` (`provider_refusing`), or
- a 429 in the platform's own 2h window (`recent_rate_limit`).

The second uses `is_subscription_rate_limited` — the 429-only DISPLAY predicate (#2352) — deliberately, **not** the kind-blind candidate-skip one: an auth failure is a credential problem a different subscription may share (a `.env` shadow is per-agent, not per-subscription), and quota exhaustion is the case a switch actually fixes.

**The provider reading is bounded at DISPLAY freshness, not at the selection bound.** `cached_headroom_readings`' default is `MAX_READING_AGE_SECONDS` (≥2h) — calibrated for *ranking* candidates, where a stale reading beats none. This call is a different job: it decides whether a provider verdict may **overrule** the 2h event predicate, and a reading as old as the window it overrules cannot. Unbounded, a two-hour-old "serving" snapshot suppresses a five-minute-old 429 and pins the agent on a subscription that is refusing it right now — the #447 rule ("a probe is ground truth about NOW") applied to a probe that is no longer about now. It asks for `FRESHNESS_SECONDS`, the same bound `_headroom_indicates_healthy` uses for the same judgement and the one the file already declares for the mirror case (`REFUSAL_FRESHNESS_SECONDS = FRESHNESS_SECONDS`). Caught in re-review of #2638; the argument is pinned as an argument, not only as behaviour, because omitting it *is* the bug and a behavioural test alone would pass again the day the default moves.

**The two are read three-state, never OR'd.** A fresh reading ENDS the question in both directions: `refusing` → switch, *not* refusing → dispatch, and only the absence of a usable reading falls through to the 2h event. `fresh_refusing OR db_events` is the shape `resolve_rate_limited_now` exists to replace (#447) and it makes the two doors disagree — `recovery_verdict` readmits a subscription the provider is demonstrably serving while the evacuate door would keep moving agents off it on every dispatch, one hot-reload and one high-priority notification per turn, and with two such subscriptions a flap turn after turn. The db predicate is an inference from past failures; a probe is ground truth about now. Caught in review on #2638; pinned by `TestTheRefusalPredicateIsThreeState`, whose last case asserts the two doors agree on one reading rather than testing each in isolation.

Contracts:

- **It never raises and never blocks.** Every "cannot tell" path returns `None`; a pre-flight optimisation must not be able to fail a turn that would otherwise have run, and #792 remains the backstop for everything it declines to do.
- **It records no failure event.** Nothing failed — that is the point — and a synthetic event would poison the very skip-list that decides where the agent may move next.
- **With no alternative it dispatches anyway.** Refusing would turn a probably-failing turn into a certainly-failing one; the provider's answer is better evidence than ours.
- **It performs the SAME switch** (`_perform_auto_switch`), so the activity, the notification and the hot-reload the Settings usage cards and Dashboard pressure badges read happen whichever path fired. `pre_dispatch=True` changes only the wording (`_failure_phrase`): "switched after a rate-limit error" for a turn that never ran sends an operator looking for a failed execution that does not exist.
- **It spends the turn's one remediation.** The pre-dispatch arm sets `subscription_switch_attempted`, the same one-shot flag #792's switch-retry and the API-key fallback ride. Without it a turn moved before its first attempt and refused again would switch a SECOND time, re-issue, and burn a further rate-limit event — churning towards a third never-used subscription, which is the cascade the flag was introduced to stop. The except handler reads the same flag, so it also stops recording a second failure event; that is the rule already stated at its other read site, not a new one.

### 3. Last resort: the platform API key

When the switcher declines — every subscription exhausted, refused or skip-listed with no recovery evidence — `fallback_to_api_key` keeps the agent working instead of failing the message: clear the subscription assignment, set `use_platform_api_key`, **restart**. A restart rather than a hot-reload because the reload endpoint pushes an OAuth token and the change needed here is the opposite one (`ANTHROPIC_API_KEY` set, `CLAUDE_CODE_OAUTH_TOKEN` dropped), which `lifecycle`'s own auth block already derives correctly from DB state — including #2114's shadowing guard, so nothing here reasons about that.

It **clears** rather than remembering-and-restoring: a hidden "go back when the window resets" would be a second, invisible scheduler competing with the operator's own assignment. The notification says what happened; reassignment is a deliberate act.

Narrow by construction: only after the switcher declined, only for a Claude runtime, only with a key actually configured, only with the setting on, and it rides the existing one-shot `subscription_switch_attempted` budget so a turn gets at most one remediation.

**Setting** `subscription_api_key_fallback`, default ON — `GET`/`PUT /api/subscriptions/settings/api-key-fallback`, rendered in Settings → Subscriptions beside the auto-switch and headroom toggles. The GET also returns `key_configured`: with the setting on and no key stored the fallback is enabled and inert, and a control that showed only "on" would be describing a remedy that cannot run. The read fails **open** (enabled) — the failure it guards is a user's turn dying with a usable key sitting in settings.

### 4. The client is told what changed

`TaskExecutionResult.subscription_switch` carries the switch dict (in-memory, like `dispatched_async`), stamped at `execute_task`'s return sites by `_with_switch` — one place that knows both the result and the attempt state, rather than a parameter threaded through five constructors that would be `None` on half of them. **Every** terminal return, not most: two were missed in review (`BackendAgentCallBudgetExhausted` and the generic `except Exception`), and both are reachable after a pre-dispatch switch, so the portal would have said "not retryable" while the agent sat on a fresh subscription. The only unwrapped returns are the two that precede any dispatch — `admission_denied` and `breaker_denied` — and an AST guard (`TestEveryTerminalCarriesTheSwitch`) names them, so a return site added later has to be justified rather than silently dropping the switch.

**A subscription usage limit is `BILLING`, and until #2638's review nothing ever produced that code.** The agent surfaces a Claude usage limit as **429**, not 503, and `_handle_http_error` classified only 503 → `AUTH`. `TaskExecutionErrorCode.BILLING` had no assignment site anywhere in `src/backend`: it existed in the enum, in comments, and in the portal's own gate tuple, and was never set. A Workspace turn is `triggered_by="public"`, which is not async-eligible, so it takes exactly that sync path — which is why the client-facing half of this work was inert for the symptom that motivated it, and fired only when a failure happened to arrive as 503. A 429 now sets `BILLING`. Safe by construction downstream: the dispatch breaker counts `auth` only (#526 D10), and the #1085 shared-cause governor DOES count `billing` — which is what it was written for ("a fleet-wide Claude-API 429 storm"), has never been reachable from the sync path before, and is behind a default-OFF flag.

The portal's AUTH/BILLING branch consults it **before** refusing:

- **switched** → `503`, `category="auth_switched"`, `retryable=True`, naming the new subscription (or the platform API key). #2320's `retryable=False` rested on "re-sending re-fails", which holds only while nothing changed underneath — and a switch is exactly something changing underneath.
- **not switched** → still `502` / `retryable=False`, but the body now names the earliest reset instant the sampler already caches (`earliest_known_reset` → `_usage_limit_detail`). "Please try again later" is true and nearly useless: the person cannot tell whether later means ten minutes or two days, so they either give up or re-send in a loop that cannot succeed. Degrades to the original sentence whenever the instant is unknown or unreadable — a fabricated time would be worse than a vague one, and this runs where things are already going wrong, so it must not be able to raise.

### Not covered

Nothing on the trigger side any more: #2643 (section below) gave `pull_coordination_service.apply_task_result` the SUB-003 hook on its CAS-won branch, so a pull-dispatched terminal triggers the same switch as a push one (inert today only because no agent is piloted onto the pull path). What is still deliberately outside SUB-003 is the re-delivery governor's treatment of `billing` as a correlated code — a fleet-wide throttling decision, not a per-agent switch. Also outside it: the #2572 adoption of **credential-less** agents at subscription-registration and instance-key-deletion time. That is SUB-002's ([subscription-management.md](subscription-management.md)) — SUB-003 is failure-driven and its precondition *the agent already has a subscription* is unchanged, which is precisely why a second trigger was needed rather than a relaxation of this one.

**Tests:** `tests/unit/test_2638_subscription_switch_on_turn.py` — `recovery_verdict` as a pure table (including the failure-after-reset ordering and the fail-closed unreadable-instant cases), readmission through the real selector, the fail-open path readmitting nobody, the pre-dispatch switch's five contracts, `earliest_known_reset`, the fallback's setting semantics, the three-state refusal predicate (including its agreement with `recovery_verdict` on one reading, and the fail-closed unreadable-snapshot case), the `_with_switch` AST guard, and four end-to-end turns through the real `execute_task` + real switcher: 429 → completes on a never-failed alternative, 429 → completes on a **readmitted** one, the honest negative (nothing to switch to ⇒ still FAILED, asserting `error_code == BILLING` — compared by `.value`/`.name`, since #1085's fieldless-dataclass quirk makes `BILLING == AUTH` True), and a pre-dispatch switch spending the turn's single remediation rather than switching twice. Not an integration test against a live instance: a real 429 cannot be provoked from a provider on demand, so the seam actually under test — refusal in, completed turn plus switch out — is exercised where it can be deterministic.

## #2643 (2026-09-09) — the pull sink is a trigger surface too

`pull_coordination_service.apply_task_result` is a CAS-won terminal writer
and had every other terminal hook — the #1578 completion event, the #1804
activity close — but no SUB-003 hook. So a pull-dispatched turn that the
provider refused recorded **no** `subscription_rate_limit_events` row (no
skip-list entry, no usage card, no pressure badge) and left the agent pinned
to the subscription that had just refused it. Everything the push path gained
in #441 / #471 / #792 — and everything #2638 added on top — was unreachable
from a pull-dispatched turn.

Inert today: the pull path is gated on `PULL_MODE_PILOT_AGENTS` and no agent
is piloted. It mattered as a prerequisite on the pull-mode default-ON gate
list — piloting a subscription-backed agent would have silently removed
SUB-003 from that agent, with nothing saying so.

Three properties are load-bearing:

* **The vocabularies do not line up.** The worker's typed `error_code` calls
  the quota class `billing` (`result_callback._STATUS_MAP` maps an agent 429
  to it); this subsystem calls it `rate_limit`. `_switch_failure_kind` is the
  map, and it is an **allowlist** — an `error_code` it has not heard of
  switches nothing, because the inverse ("switch unless the code looks
  benign") would churn an agent through every subscription it owns the first
  time a worker reports an unfamiliar crash class.
* **A SUCCESS terminal is excluded, on the merits.** The gate is
  `error_code` rather than the FAILED/CANCELLED split — a worker that labels a
  quota refusal `cancelled` still refused for a quota reason — but a turn the
  provider *served* is evidence the subscription works, so a stray `error_code`
  riding a success must not move the agent off it. (It is also the only branch
  where the sink never binds `err_text`, so an ungated hook would raise
  `NameError` and turn a committed, billed terminal into a 500.)
* **CAS-won branch only**, beside the two hooks it sits with. A replayed
  terminal short-circuits above the write and a late one loses the CAS, so
  neither can spend a second switch (the #1083 rule). Once past that gate
  `handle_subscription_failure` owns the rest: it records the event
  unconditionally, then takes the #799 per-agent `agent_switch_lock` and
  re-reads under it, so two failures racing on one agent still switch once.
* **It re-delivers nothing.** Re-delivery is the lease reaper's decision
  (#1081 Phase 3) and the #1085 governor's correlated-cause pause still gates
  it. The switch only puts the agent somewhere the NEXT attempt can succeed;
  whether that attempt happens at all is not this hook's call. Concretely: a
  switched agent's re-delivery is expected to succeed **if** the reaper
  re-queues the row (under `MAX_REDELIVERY`) and the governor is not paused —
  and a `billing` terminal is exactly the class the governor counts, so a
  fleet-wide quota event can legitimately hold the re-delivery back even
  after a successful switch. The switch is not wasted in that case: it is the
  next scheduled or claimed turn that benefits.

The sink is synchronous and its caller is async, so the call goes through
`subscription_auto_switch.spawn_subscription_failure` — the same wrapper shape
as `spawn_task_terminal_event` (#1578) and `spawn_close_execution_activity`
(#1804): one coroutine, a strong reference held until it finishes, a raising
switch logged and swallowed, and no-running-loop treated as a skip with the
coroutine closed. It runs after a committed, billed terminal and must never be
able to turn one into a 500 on the result endpoint.

**Still not wired on the pull sink** (out of scope here, named so it is not
mistaken for done): the #526 AUTH dispatch breaker and the #1085 governor's
`record_terminal_failure`. `apply_result` has both; `apply_task_result` has
neither, and neither is a SUB-003 concern.


## #3470 (2026-10-09) — the turn WALKS the pool; the API key is a rung, not a reassignment

Reported from the Workspace: a turn on a rate-limited subscription failed with
*"The agent has reached its usage limit"* while other subscriptions had
headroom. Every remediation above was bounded to **one** switch (or one key
fallback) and **one** re-issue per turn, and three surfaces then handed the
retry back to the person (`/chat`'s *"auto-switched … Please retry"*, the
Workspace's *"Send that again"*, the `_usage_limit_detail` 502 over a pool that
was never fully walked). Part 2, confirmed on a live install: `fallback_to_api_key`
cleared the assignment, set `use_platform_api_key` and **restarted** the
container; a key with no credit then failed every turn in seconds and nothing
could move the agent back — a few-hour limit became a permanent outage.

### 1. Credential trial, then one commit

```
attempt on the baseline ──429/auth──▶ advance_subscription_walk
                                        │ record the refusal where it RAN
                                        │ next rung: another subscription …
                                        │           … then the platform API key
                                        ▼
re-issue the SAME execution_id with  auth_override={oauth_token | api_key}
                                        │  (the agent applies it to THAT spawn;
                                        │   header X-Trinity-Auth-Override echoes it)
                        served ─────────┴──▶ commit_subscription_walk
                                               subscription: assign + hot-reload + ONE notification
                                               api_key:      assignment untouched; transition notice
```

`subscription_auto_switch.SubscriptionWalk` / `Rung` / `advance_subscription_walk`
are the shared decision core; `task_execution_service._call_agent_with_retries`
and `chat_execution_service._walk_chat_dispatch` are the two dispatch loops
(the twins share the core rather than copying it — learnings 2026-09). A rung is
a **trial**: nothing in the DB or the container changes until a trial serves,
and then the serving subscription is committed once (`_perform_auto_switch`,
under the #799 lock, re-checking the assignment is still the one the turn
started on — a concurrent commit wins). A key that served changes **no**
assignment (Part 2 AC#3 — per-turn routing); "Switched to the platform API
key" / "Returned to subscription" are transition notifications deduped by
`agent:api_key_route:{name}` (informational only — EXEMPT in the #1560
registry). The row's SUB-004 `subscription_id` is re-pointed to the serving
subscription (`db.set_execution_subscription`), or to NULL for a key-served turn.

**Attribution is exact by construction.** The failure event goes to the
credential the walk SENT (`walk.ran_on()`), never to whatever the assignment
reads after the fact — the #799 cascade (turn 2 blaming turn 1's destination)
cannot happen. A refusal that arrives while a concurrent turn has already moved
the assignment is a `concurrent_switch` rung: re-run on the new baseline, no
second switch (AC#7).

**Liveness is the walk's own property.** Every rung grows
`tried_subscription_ids` or sets `tried_api_key`; a pick already tried ⇒
exhausted; a hard cap of `len(subscriptions) + 2` rungs computed once. The
dispatcher adds the only other bound — the turn's remaining wall clock (#2789,
still the only *ceiling*) over a floor of settle + HTTP slack + 30 s
(`_SWITCH_RETRY_MIN_REMAINING_S`): a re-issue with `max(1, …)` seconds of
agent budget is a guaranteed, billed, mis-blamed TIMEOUT. A budget-stopped turn
still records its refusal (#471).

### 2. The candidate set

`select_best_alternative_subscription(current, exclude_ids=…, last_resort_min_age_seconds=…)`:
the walk excludes every subscription this TURN ran on. When nothing viable or
readmitted (#2638) remains, the **last-resort rung** tries the skip-listed
candidates (`db.list_recently_failed_alternatives`), still RANKED so a fresh
provider refusal keeps excluding one (the skip-list is an inference from past
events; live evidence is not overridden). Interactive triggers
(`pull_pilot.INTERACTIVE_TRIGGERS`) take any untried candidate; autonomous
triggers only one whose last failure is ≥ 30 min old
(`LAST_RESORT_MIN_AGE_AUTONOMOUS_SECONDS`) — a schedule firing every five
minutes must not re-probe a refused subscription on every run. Because a trial
moves no assignment, #444's thrash (assignment flips + a notification per flip)
cannot recur; the cost of a last-resort probe is one zero-token 429. A blocked
window whose `resets_at` has already elapsed ranks UNKNOWN rather than REFUSED
(the `_readmit_recovered` rule), and `earliest_known_reset` never reports a
past instant. Legacy callers (`None`) keep the skip-list absolute — the #444
pingpong contract is untouched.

### 3. The platform key is a rung

`api_key_rung(agent)` offers the key only when: the fallback setting is on; the
agent is a Claude runtime; `use_platform_api_key` is not explicitly False (an
operator's "never bill the key" is a hard rule — the column defaults to True);
a key is configured; and the key is not **recently refused**
(`platform_api_key:refused`, kind-aware: auth/billing ⇒ 2h, a 429 ⇒ 5 min — a
burst is not "no credit", and a fleet-wide 2h skip for it would remove the last
rung from every agent on one spike). A key refusal is one more exhausted
candidate; the turn then fails honestly with the earliest reset across EVERY
credential tried. When the assigned subscription is known-refused at dispatch
and no alternative exists, `ensure_serviceable_subscription` answers
`{"route": "api_key"}` and the FIRST attempt runs on the key by override — no
doomed attempt, no commit. `fallback_to_api_key` survives as a credential-free
"is the rung available?" answer and changes nothing.

### 4. Old agent images: legacy mode

An image that predates `auth_override` ignores the field and runs the attempt
on its baseline; the response then lacks `X-Trinity-Auth-Override` and the walk
flips to **legacy mode**: the rung is applied the #792 way (`apply_rung_legacy`
→ `_perform_auto_switch`, assign + hot-reload) and the turn re-issued once more
— capped at ONE restart-based rung (`restart_result != "hot_reloaded"`: a
recreate kills the agent's concurrent work and takes longer than the settle +
connect backoff) and with **no** key rung (the restart-based key path IS the
stranding bug this replaces). No regression against #2638 on subscriptions.

### 5. What the person and the operator see

- **Workspace** (`client_portal/service.py` AUTH/BILLING ladder): a turn whose
  budget ran out before a re-issue (`budget_exhausted`) keeps the retryable
  "send that again" copy — a fresh send gets a fresh budget. Everything else is
  the honest usage-limit 502 (`_usage_limit_detail(agent, subscription_ids=tried)`,
  earliest reset across the walk).
- **`/chat`** (`_apply_sub003_autoswitch(…, walk=)`): exhausted ⇒ 429/503 with
  the usage-limit copy, the reset, the trail and `auto_switch.attempts`; budget
  ⇒ the last-resort "Please retry". Without a walk the pre-#3470 behaviour is
  byte-for-byte unchanged (test_2889 / test_3012 stub the module with two names).
- **Executions view**: the trail is appended to the FAILED row's `error` —
  `Tried: A (rate limit) -> B (auth) -> platform API key (billing); every
  candidate refused.` — no schema change; the structured list rides
  `TaskExecutionResult.subscription_switch.attempts` and the `auto_retry` audit
  rows. One "No subscription could serve a turn" alert per agent per hour
  (`agent:pool_exhausted_notice:`, EXEMPT).

### Agent side

`ParallelTaskRequest` / `ChatRequest` gain `auth_override: AuthOverride`
(`oauth_token` XOR `api_key`). `execution_env.auth_override_env_layers` turns it
into the top env layer of ONE spawn — `build_execution_env(extra, drop=…)`
gained `drop`, because the oauth route must take `ANTHROPIC_API_KEY` away from
that spawn (Claude Code prefers the key) and a mapping cannot express an
absence. Both spawn sites (`headless_executor`, `claude_code`) apply it; the
router stamps `X-Trinity-Auth-Override` on success AND on the HTTPException,
stages the value for log redaction (`credential_sanitizer.add_credential_value`),
and drops the field for a runtime without `supports_auth_override`. Nothing is
persisted: a `docker restart` reverts to the DB baseline by construction.

### Review hardening (pre-landing)

- **The commit is a compare-and-set.** The #799 lock is process-local and prod
  runs `--workers 2`, so `commit_subscription_walk` writes the assignment with
  `db.assign_subscription_to_agent(…, expected_subscription_id=<the turn's
  origin>)`: the first worker wins, the second reads `False` and skips its
  hot-reload and notification. `apply_rung_legacy` keeps the plain write (it
  runs before any re-issue, under the lock, as #792 did).
- **A container-fault 503 never walks.** `classify_switch_failure` reads a bare
  503 as `auth` (#285); the agent also answers 503 for *"Claude Code is not
  available in this container"* / *"Permission bypass failed"*. Looping a
  one-shot remediation multiplies every false-positive trigger by the pool
  size, so `failure_classifier.is_container_fault` excludes those texts (pinned
  against the agent source like #3012's markers).
- **A key-only agent has no pool**: `advance_subscription_walk` stops with
  `no_subscription` before recording anything (the pre-#3470 early return), so
  the baseline key is never re-tried as a "rung" and the caller keeps its own
  wording.
- **Async dispatch (#1083) drops a pre-dispatch key route** before sending —
  the agent's 202 branch runs before it reads the override, and the trail must
  not claim a re-issue that never happened. The walk is inline-only.
- The legacy re-issue obeys the same budget floor; an undecryptable token
  re-selects (bounded) instead of falling straight to the key; the hot path pays
  ONE db read until the first refusal (name and rung cap are lazy);
  `Rung.credential` is `repr=False` and the agent's `AuthOverride` fields are
  `SecretStr`, so no repr, validation echo or assertion message prints a secret.

### Not covered

Pull pilots: `public` ∈ `PULL_REACHABLE_NON_AUTONOMOUS`, so a Workspace turn on
a piloted agent is claimed from the durable queue and the pull sink only runs
the one-shot `spawn_subscription_failure` with **no re-delivery**. Follow-up
filed with the PR. Multi-worker (`--workers 2`): the process-local #799 lock
serialises commits within a worker; two workers committing different serving
subscriptions is last-write-wins with two hot-reloads and two notifications,
never a restart or a wedge (the module comment's standing limitation).

**Tests:** `tests/unit/test_3470_subscription_walk.py` — the state machine
(2-of-3 ⇒ the third; exhausted; budget stop still records; already-tried pick ⇒
exhausted; rung cap; concurrent switch ⇒ re-run; disabled), the last-resort rung
(interactive vs autonomous age gate; legacy callers absolute; fresh refusal still
excludes; elapsed `resets_at` ranks UNKNOWN), the key rung (ordering; refusal ⇒
honest failure with the assignment untouched and no restart; refused-key skip;
kind-aware TTLs; explicit opt-out; pre-dispatch route), commit + transition
notifications, legacy mode (header absent ⇒ one restart-based rung, no key),
`earliest_known_reset` dropping past instants, four end-to-end turns through the
real `execute_task` + real walk, the `/chat` re-issue, the portal ladder, and the
agent's env layers / header / redaction. `test_792` / `test_2789` are re-seamed
on `advance_subscription_walk`; `test_2638`'s one-remediation test became "the
pre-dispatch switch feeds the walk and the origin is never retried".

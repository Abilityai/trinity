# Feature: Subscription Usage Tracking

## Overview
Per-subscription rolling token and cost usage across two time windows (5h and 7d), plus — since #471 — failure-event counters and **live provider headroom** (actual 5h/7d utilization % + reset times). **Dedup rule (#471, verified live):** a modern turn writes BOTH a `schedule_executions` row and (for `/chat` + persisted `/task`) a cost-bearing `chat_messages` row, so `schedule_executions` is the SOLE source for cost / context-estimate / turn count; `chat_messages` contributes only `output_tokens` (which executions don't carry). Summing both — the pre-#471 behavior — counted every chat turn's cost twice.

## User Story
As a platform admin, I want to see aggregate usage, live headroom, and failure pressure per Claude Max/Pro subscription so that I can understand load distribution, spot which agent burns the quota, and see how much is left before limits hit.

## Entry Points
- **UI**: Settings → Integrations → Claude Subscriptions (`components/settings/SubscriptionsPanel.vue`, extracted from Settings.vue in #471) — Pressure column per row; expanded row shows the usage/headroom block, per-agent breakdown (lazy), and the Refresh (probe) button. Dashboard pressure badges: `GET /api/agents/subscription-pressure` (see below).
- **API**: `GET /api/subscriptions/{subscription_id}/usage` (extended), `GET /{id}/usage/breakdown`, `POST /{id}/usage/refresh`, `GET|PUT /api/subscriptions/settings/headroom-auto-refresh`. Accepts subscription UUID or name as the path parameter.
- **UI (Dashboard Grid)**: the **Subscription pressure** tile (ent#259, `components/tiles/SubscriptionPressureTile.vue`) — one row per SUBSCRIPTION, the inverse unit of the per-agent chip below. Composes `GET /api/subscriptions` + `/{id}/usage` per subscription in `stores/subscriptions.js::fetchPressureData` on the Grid's 60s batch poll; **no endpoint of its own** (operator ruling, ent#259 2026-08-19). Admin-only, since the payload carries per-subscription spend. Its display rules — a real utilization % only while `headroomIsFresh`, "429s" from the `rate_limit` kind only, a rejected provider token shown as its own state ranked above a limit (#2353), `input_tokens` kept off the row face as context occupancy — are pinned in `utils/subscriptionPressureTile.js`; see [dashboard-grid-view.md](dashboard-grid-view.md) § Subscription pressure.

## #471 — Live headroom (provider truth)

**Facts established 2026-08-19 against a real stored `sk-ant-oat01-` setup token:**
- `GET https://api.anthropic.com/api/oauth/usage` → **403 `permission_error` (missing `user:profile` scope)** — that endpoint requires an interactive-login token and is DEAD for the tokens Trinity stores (the mechanism behind closed PR #2170). Do not resurrect it.
- `POST /v1/messages` under the same token returns the full `anthropic-ratelimit-unified-*` header set: `{5h,7d}-utilization` (fraction 0..1), `-reset` (unix), per-window `-status`, `representative-claim`, overage status.

**`services/subscription_headroom_service.py`** reads those headers via a **micro-ping probe** (`max_tokens=1` Haiku message, ~a dozen tokens of the subscription's own quota, visible in the Anthropic console — release-noted):
- **Click** (`POST /{id}/usage/refresh`, admin): one probe, floored ≥60s apart per subscription; works Redis-down via a per-worker in-process floor.
- **Ambient** (default ON, `subscription_headroom_auto_refresh` system setting): refresh when the cached snapshot is older than `SUBSCRIPTION_HEADROOM_REFRESH_SECONDS` (900), demand-driven by reads — an unwatched instance probes nothing. **Fail-CLOSED when Redis is unreachable**: `_read_snapshot` returns `(redis_ok, snapshot)` and ambient probing requires an ANSWERED cache read — a client object existing proves nothing about the server (learnings 2026-08-19).
- **Batch path never blocks**: `get_headroom(wait=False)` (used by `pressure_states` behind the dashboard poll) spawns the refresh as a strong-ref background task and serves the stale snapshot with its honest age.
- Snapshot in Redis `subscription:headroom:{id}` (7d TTL, best-effort DEL on subscription delete); probe single-flighted (`SingleFlightLock` #1920). A probe 429 updates the snapshot's `status` only — never `subscription_rate_limit_events` (platform-caused, not agent work). 401/403 → `status: invalid_token`. Missing header family on a 200 → throttled WARNING (provider drift is loud) + degrade to observed.
- **Contract (the #2170 inversion)**: every `/usage` response carries `source: "anthropic"|"observed"` + `headroom.snapshot_age_seconds`; the DB-derived windows/counters are ALWAYS populated — the observed arm is load-bearing, provider data is enrichment. `rate_limited_now` has ONE derivation (`decorate_usage`): the 2h `is_subscription_rate_limited` predicate OR a fresh (≤2× refresh interval) provider verdict. **#2352 scoped that predicate to `failure_kind = 'rate_limit'`** — it counted every kind, so an auth failure set the flag and a dead token was reported as quota exhaustion. NULL kinds are excluded (unknown is never promoted to "429"). The kind-blind semantics live on under their own name, `has_recent_subscription_failures`, for the auto-switch/assignment candidate skip — see [subscription-auto-switch.md](subscription-auto-switch.md).

**Failure counters (#471)**: `failure_events_24h` + `failure_events_by_kind` (`rate_limit` | `auth` | `unknown` for pre-#471 NULL rows) from `subscription_rate_limit_events`, which now persists `failure_kind` (dual-track migration `rate_limit_events_failure_kind` / Alembic `0040`). Events are recorded BEFORE the auto-switch enabled gate (see subscription-auto-switch.md) so opted-out operators keep observability.

**Fleet pressure**: `GET /api/agents/subscription-pressure` (registered before `/{agent_name}`, Invariant #4) — per accessible agent (`visible_agent_names`, the shared pure-DB ent#384 helper): `auth_mode` (the `AgentAuthStatus` vocabulary via the shared `derive_auth_mode`), `subscription_name`, `failure_events_24h`, its auth-kind slice `auth_failures_24h` (#2352), `rate_limited_now`, the probe's own `token_status`, `utilization_5h_pct` + `headroom_source`. Explicit `response_model` (ent#334). The two #2352 fields exist because after the predicate split a dead-token subscription is no longer `rate_limited_now`, and a bare total cannot tell the badge whether to say "limit", "429s" or "auth" — `pressureBadge` degrades to the pre-#2352 wording when they are absent. Feeds the AgentTile chip + AgentListPanel badge via the sync-health two-store poll discipline (`fleetGrid.js` + `agents.js`); shared badge predicate in `utils/subscriptionPressure.js` (vitest-covered).

## Backend Layer

### Endpoint
- `src/backend/routers/subscriptions.py:118` - `get_subscription_usage()`
  - Admin-only (`require_admin`)
  - Resolves path param by UUID first, falls back to name lookup
  - Returns `SubscriptionUsage` Pydantic model

```
GET /api/subscriptions/{subscription_id}/usage
Authorization: Bearer <admin_token>
```

Response shape:
```json
{
  "subscription_id": "<uuid>",
  "window_5h": {
    "input_tokens": 12000,
    "output_tokens": 4500,
    "cost_usd": 0.18,
    "message_count": 37
  },
  "window_7d": {
    "input_tokens": 210000,
    "output_tokens": 75000,
    "cost_usd": 3.12,
    "message_count": 512
  },
  "agents": ["agent-a", "agent-b"]
}
```

### Resolution Logic
1. Try `db.get_subscription(subscription_id)` by UUID
2. If not found, try `db.get_subscription_by_name(subscription_id)`
3. If still not found, raise 404
4. Call `db.get_subscription_usage(subscription.id)` with the resolved UUID

### Business Logic
- `agents` field reflects **current** assignments from `agent_ownership`, not historical data
- Usage windows query historical records via the snapshotted `subscription_id` column — correct even when agents switch subscriptions between queries

## Data Layer

### Schema Changes (migration #31: `subscription_usage_tracking`)
- `src/backend/db/schema.py`
- `src/backend/db/migrations.py:900` - `_migrate_subscription_usage_tracking()`

New columns added via `ALTER TABLE`:

| Table | Column | Type | Purpose |
|-------|--------|------|---------|
| `chat_messages` | `subscription_id` | TEXT | Subscription active when message was recorded |
| `chat_messages` | `output_tokens` | INTEGER | Output token count from agent response metadata |
| `chat_sessions` | `subscription_id` | TEXT | Subscription active when session was created |
| `schedule_executions` | `subscription_id` | TEXT | Subscription active when execution was created |

New indexes:
- `idx_chat_messages_subscription ON chat_messages(subscription_id, timestamp)`
- `idx_executions_subscription ON schedule_executions(subscription_id, started_at)`

### Usage Query
- `src/backend/db/subscriptions.py:606` - `SubscriptionOperations.get_subscription_usage()`
- `src/backend/database.py:1137` - delegation wrapper

Two windows computed by `_query_window(cutoff)`:

**Chat messages** (assistant role only):
```sql
SELECT
    COALESCE(SUM(context_used), 0)   AS input_tokens,
    COALESCE(SUM(output_tokens), 0)  AS output_tokens,
    COALESCE(SUM(cost), 0.0)         AS cost_usd,
    COUNT(*)                          AS message_count
FROM chat_messages
WHERE subscription_id = ?
  AND role = 'assistant'
  AND timestamp >= ?
```

**Schedule executions** (terminal states only, no separate output_tokens):
```sql
SELECT
    COALESCE(SUM(context_used), 0) AS input_tokens,
    COALESCE(SUM(cost), 0.0)       AS cost_usd,
    COUNT(*)                        AS exec_count
FROM schedule_executions
WHERE subscription_id = ?
  AND started_at >= ?
  AND status NOT IN ('running', 'pending')
```

Totals are summed: `input_tokens = chat.input_tokens + exec.input_tokens`, `message_count = chat.message_count + exec.exec_count`.

### Pydantic Models
- `src/backend/db_models.py:672` - `SubscriptionUsageWindow` (input_tokens, output_tokens, cost_usd, message_count)
- `src/backend/db_models.py:680` - `SubscriptionUsage` (subscription_id, window_5h, window_7d, agents)

## Subscription ID Snapshot Strategy

The `subscription_id` is written at **record creation time**, not resolved at query time. This is intentional: an agent's subscription may be reassigned dynamically (SUB-003 auto-switch), so querying `agent_ownership.subscription_id` at read time would misattribute historical usage.

### Chat path (`src/backend/routers/chat.py`)
1. At request start, call `db.get_agent_subscription_id(agent_name)` — a lightweight single-column read
2. Store result as `_exec_subscription_id` / `_chat_subscription_id`
3. Pass `subscription_id` to `db.create_task_execution()` (execution record)
4. Pass `subscription_id` to `db.get_or_create_chat_session()` (session record — new sessions only)
5. Pass `subscription_id` and `output_tokens=metadata.get("output_tokens")` to `db.add_chat_message()` (assistant message record)

### Task execution service path (`src/backend/services/task_execution_service.py:178`)
When `execution_id` is not pre-provided by the caller:
1. Use caller-supplied `subscription_id` if present, otherwise call `db.get_agent_subscription_id(agent_name)` (best-effort, swallows exceptions)
2. Pass `subscription_id` to `db.create_task_execution()`

### Schedule execution path (`src/backend/db/schedules.py`)
- `create_task_execution()` at line ~450 accepts `subscription_id` parameter
- `create_schedule_execution()` at line ~520 accepts `subscription_id` parameter
- Both write it directly to `schedule_executions.subscription_id`

## Database Operations Summary

| Operation | File | Function | What changes |
|-----------|------|----------|--------------|
| Read subscription ID for agent | `db/subscriptions.py:450` | `get_agent_subscription_id()` | SELECT from `agent_ownership` |
| Write to chat_messages | `db/chat.py:105` | `add_chat_message()` | INSERT with `subscription_id`, `output_tokens` |
| Write to chat_sessions | `db/chat.py:60` | `get_or_create_chat_session()` | INSERT with `subscription_id` (new sessions) |
| Write to schedule_executions | `db/schedules.py:450` | `create_task_execution()` | INSERT with `subscription_id` |
| Write to schedule_executions | `db/schedules.py:520` | `create_schedule_execution()` | INSERT with `subscription_id` |
| Query usage windows | `db/subscriptions.py:606` | `get_subscription_usage()` | SELECT aggregates from both tables |

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Subscription not found by ID or name | 404 | "Subscription not found" |
| Non-admin caller | 403 | "Admin access required" |
| Usage query exception | 500 | "Failed to retrieve usage data" |
| `get_agent_subscription_id()` failure at record time | — | Swallowed silently; `subscription_id` stored as NULL |

## Side Effects
None. This is a read-only analytics endpoint. No WebSocket broadcasts, no activity tracking.

## Testing

### Prerequisites
- Backend running
- Admin token obtained via `POST /api/token`
- At least one subscription registered and assigned to an agent
- Agent has sent at least one chat message or completed at least one execution

### Test Steps

1. **Action**: `GET /api/subscriptions/{id}/usage` with admin token
   **Expected**: 200 with `window_5h` and `window_7d` populated
   **Verify**: `message_count` matches number of assistant messages attributed to subscription

2. **Action**: `GET /api/subscriptions/{name}/usage` using subscription name instead of UUID
   **Expected**: Same 200 response (name resolution fallback)

3. **Action**: `GET /api/subscriptions/nonexistent/usage`
   **Expected**: 404 "Subscription not found"

4. **Action**: Same request with non-admin token
   **Expected**: 403 "Admin access required"

5. **Action**: Check `chat_messages` table after a chat interaction
   **Expected**: `subscription_id` column populated on assistant message row; `output_tokens` populated with value from agent metadata

6. **Action**: Reassign agent to a different subscription, send another message
   **Expected**: New message row has new `subscription_id`; old messages retain original `subscription_id`

## Related Flows
- [subscription-management.md](feature-flows/subscription-management.md) - Subscription CRUD and agent assignment
- [subscription-auto-switch.md](feature-flows/subscription-auto-switch.md) - SUB-003 automatic subscription switching on rate-limit
- [subscription-credential-health.md](feature-flows/subscription-credential-health.md) - Credential health monitoring
- [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) - Chat flow that writes messages
- [task-execution-service.md](feature-flows/task-execution-service.md) - Execution lifecycle that writes execution records

---

## Recovery: how a `LIMIT` badge clears (#447)

Three defects sat on top of each other, and only the third is what an operator
sees. Fixed together because fixing any one alone leaves the surface still lying.

### 1. `rate_limited_now` was an OR, so ground truth could not win

```
before:  rate_limited_now = db_2h_predicate OR fresh_provider_verdict
after:   fresh says limited -> True
         fresh says allowed -> False          <-- the missing arm
         no usable verdict  -> db_2h_predicate
```

The db half is *"a failure row exists in the last 2 hours"*, and **nothing clears
a failure row on success** — `clear_rate_limit_events` has had zero production
callers since #444 removed the one call, because clearing was destroying
auto-switch's detection signal. So the db half only decays with the clock, and
being OR'd it outranked a fresh probe reporting `allowed · 32% used · resets
19:10`. Observed live: two subscriptions wearing `LIMIT` while every agent
assigned to them answered normally.

`_headroom_indicates_healthy` is deliberately **not** the negation of
`_headroom_indicates_limited`. "Not limited" is also true for a stale snapshot, a
rejected token and a transport error — none of which are evidence the
subscription is usable — so all three fall through to the db predicate instead of
clearing it. Only a fresh `ok` snapshot with at least one window, every reported
window **non-blocking**, is positive proof of headroom.

#### 1a. What "non-blocking" means — the window-status vocabulary (#2396)

Both predicates judge a window against ONE named constant,
`NON_BLOCKING_WINDOW_STATUSES = {"allowed", "allowed_warning"}`. It is an
**allowlist of statuses meaning "requests are being served"**, never a blocklist
of blockers — the provider does not publish this vocabulary, so an unrecognised
status must still read as blocking. (The inverse mistake, a deny-check that
silently admits every future value, is the #848 lesson in `learnings.md`; here
the safe default runs the other way.)

It shipped as the bare literal `("allowed",)` in one predicate and
`(None, "allowed")` in the other — two homes, one vocabulary — and the provider's
own near-the-limit tier `allowed_warning` was in neither. So a **healthy**
subscription approaching its weekly window was reported rate-limited on every
surface that reads `resolve_rate_limited_now`. Observed live on a running
instance: a stored snapshot recording `seven_day: 90.0 / allowed_warning` with
`overage_status: allowed` sat beside 47 successful executions in the same two
hours and **zero** `subscription_rate_limit_events` — the provider said allowed
and meant it.

`allowed_warning` also counts as **positive proof of headroom**, which is a
decision rather than a side effect: the quota was reached, the answer was "yes",
and the request was served. The states this predicate excludes are stale /
rejected-token / transport-error, none of which is evidence of usability — a
served request plainly is. Excluding it would keep a stale `LIMIT` badge on a
working subscription for the db predicate's full two hours, i.e. the #447 bug
returning in a narrower window. `abilityai/trinity-enterprise#434` consumes this
predicate as its "is this reading assessable" gate, so the rule is settled here
rather than re-derived there.

**The window arm is the weakest of three independent detectors**, which is what
bounds the blast radius of getting the vocabulary wrong: a real HTTP 429 sets the
top-level snapshot `status` and is checked BEFORE any window is inspected, and
the 2h db predicate is a third path that this change does not touch.

**Scope:** this is the DISPLAY predicate. Candidate selection reads the
kind-blind `has_recent_subscription_failures` (#2352's split), so a just-recovered
subscription is still skipped as a *switch target* until its failures age out —
#444's ping-pong cannot return through this door.

### 2. Nothing asked the provider again

The fresh verdict arm above is only reachable if a probe actually runs. Ambient
refresh is **demand-driven** — it fires when someone reads the dashboard or the
usage endpoint — so an unwatched instance never re-checked at all, and a watched
one waited out the general 15-minute cadence.

`subscription_recovery_service` (backend lifespan, staggered +13s) sweeps every
`SUBSCRIPTION_RECOVERY_PROBE_SECONDS` (default 300) and probes **only**
subscriptions currently presented as limited — the union of "last snapshot said
`rate_limited`" and "the db predicate says so", i.e. exactly the set wearing a
badge. Normally that set is empty and the cycle costs nothing.

Properties worth keeping:

| Property | Why |
|---|---|
| Reuses the `max_tokens=1` Haiku probe | ~a dozen tokens of the operator's own quota per limited subscription |
| Cannot feed itself | `_probe` writes a 429 to the **snapshot only**, never `subscription_rate_limit_events` — so re-probing can never manufacture the db row that keeps it limited |
| Gated on `subscription_headroom_auto_refresh` | That Settings toggle already answers *may Trinity probe on its own?*; a second knob would split one question in two |
| Leader-locked, fail-**open** | `subscription:recovery:leader`. A duplicated probe wastes a dozen tokens; failing closed would silently stop recovery detection — the mode the operator cannot see |
| Probe itself fail-**closed** on Redis | Without a readable cache the result could not be stored for anyone to see, so it would be quota spent for nothing (the ambient path's rule) |

### 3. The row never said when the limit comes back

`resets_at` was on the wire from #471 and rendered nowhere but a hover.
`resetReading()` reads `headroom.*.resets_at` **directly**, bypassing
`windowReadings` and therefore its freshness gate — the same asymmetry
`headroomStatus` documents (#2353): a *number* decays, an *instant* does not.

That gate is exactly what hid it. A 429 probe sets `status: 'rate_limited'`, so
`decorate_usage` never promotes `source` to `anthropic`, so `headroomIsFresh` is
false, so `windowReadings` returns `null` — while `headroom.five_hour.resets_at`
sits populated in the same object.

Binding window, in order: `representative_claim` (live-verified populated — and
both windows can report the *same* utilization, so a "fullest wins" tiebreak has
no answer there and would pick by array order) → any window the provider marks
other than `allowed` → the fullest window carrying a reset.

Honest states, none of them blank:

- `resets 19:10` — a real instant
- `reset due` — the instant has passed; the window should have rolled, but only
  another probe confirms it
- `reset unknown` — limited with no reset in the payload; the row already links
  to Settings → Integrations
- *nothing at all* — a rejected token has no quota clock, and the remedy is a
  person, not a wait

Placement is decided in the pure module, so the SFC is unchanged: the reset takes
whichever text slot is free — joining the headline on the primary line when there
are no bars, leading the second line when there are, because the fixed-width bars
cannot take more without overflowing a row that clips silently.

### Row alignment

The two window groups are fixed-width (label `min-width: 16px`, bar `30px`,
percentage `min-width: 32px` right-aligned, tabular numerals) so they form
**columns** down the tile. Without it `0%` / `6%` / `24%` each had a different
advance and pushed the `7d` group left by a different amount per row — the bars
were always 30px, but landing at three x positions they read as three lengths.
`min-width`, not `width`: an overage plan reporting >100% must overflow the cell
rather than be clipped, the same report-honestly / clamp-only-geometry rule
`barWidthPct` follows.

---

## Warning before the wall (ent#434)

#471 made the weekly reading real and #447 made it self-refreshing for a
subscription believed limited. Neither tells the operator anything *before* the
limit is reached. ent#434 adds the alert, and the shape it ended up with is
mostly a consequence of measuring the provider first.

### The window is fixed-with-reset, and that deleted most of the design

`subscription_headroom_history` on a live instance shows `seven_day_resets_at`
holding **constant** at one midnight-UTC instant across five days of probes
while utilization climbed 36 → 90, then **stepping exactly +7 days**. Under a
rolling window with continuous usage it would have advanced continuously. So
the weekly window accumulates and zeroes on a schedule, and utilization is
monotonic non-decreasing inside a window.

(`dashboard-grid-view.md` described these as rolling windows. That claim is
corrected there; it predates anyone reading the history table.)

Two consequences, each of which removes work the issue asked for:

1. A **hysteresis floor is dead code** — utilization does not fall inside a
   window, so the only real re-arm is the reset.
2. `resets_at` **is** the window's identity, so putting it in the alert id makes
   the id the entire state machine:

   ```
   sub-headroom-{sid}-{reset-day}-{tier}
   ```

   `create_item` maps `item["id"]` onto `request_id`, `UNIQUE(agent_name,
   request_id)` with ON CONFLICT DO NOTHING. Same window ⇒ same id ⇒ one row
   however many cycles re-emit it. A reset mints a new id, so the alert re-arms
   by construction. Cross-worker and cross-restart dedup come free, with **no
   durable memo** to leak, race, or clean up on `delete_subscription`.

   The id is quantised to the **day**, not the instant — under the measured
   semantics that is one episode per window, and if some future plan did behave
   as rolling it degrades to one alert per day rather than one per probe.

### The threshold fires; the projection decides urgency

There is no configurable "alert if the reset is less than N hours away", and
deliberately so — no such number is right for every operator. The window length
is known and `resets_at` says how much is left, so the operator's own pace is
already derivable:

```
projected_end = utilization_pct / fraction_of_window_elapsed
```

75% three days in projects to 175% and is an emergency; 75% with hours left
projects to ~77% and is a normal week. The alert is **never withheld** at the
threshold (operator ruling) — the projection sets `priority`: `low` when the
window will finish under 100%, `high` when it will not. An unknowable
projection is treated as not-on-pace, because a missing `resets_at` is not
evidence of an emergency.

### Three states, because two cannot express "we could not tell"

`services/subscription_headroom_service.py::classify_headroom` →
`saturated | has_headroom | unassessable`.

`_headroom_indicates_healthy` is **not** usable as the assessability gate, and
#2396's docstring wrongly said ent#434 would consume it as one. That predicate
returns `False` for a stale snapshot, a rejected token, a transport error
**and** a genuinely saturated subscription — so the most saturated subscription
in a fleet is indistinguishable from an unreachable one, and the fleet
escalation would be blocked by exactly the condition it exists to report. The
docstring is corrected; the body is untouched, because `resolve_rate_limited_now`
consumes it and a behavioural edit there moves every `LIMIT` badge.

The classifier keys on **utilization, not window status**. `allowed_warning` is
deliberately non-blocking (#2396) — correct for the badge, and catastrophic
here: a status-driven classifier would file a live 90% warning-tier reading as
`has_headroom`. Status still contributes, as the *stronger* signal: a blocking
7d status or a probe 429 is `saturated` whether or not a number came with it,
and such a reading counts toward the fleet claim but raises no
percentage-crossing alert, because there is no percentage to name.

### Where it runs

Inside the existing `subscription_recovery_service` sweep — **not** a second
loop. A second loop would mean a second leader lease over the same probe
budget, and the only thing between two leases and a double probe is a 60s
per-worker floor, which is not a coordination primitive.

Per subscription, per cycle, in two sibling `try/except` blocks:

1. `recover_probe(sid)` — #447, unchanged, **first**.
2. `ensure_reading(sid, max_age=SAMPLE_INTERVAL_SECONDS)` then
   `classify_headroom(...)`.

The ordering is load-bearing. `_probe_floor_ok` bounds probes at 60s per
subscription, so whichever consumer probes first floors the other out. This way
a believed-limited subscription is refreshed by the path that wants the tight
cadence and `ensure_reading` serves that zero-age snapshot for free; reversed,
`recover_probe` would answer `"floored"` and #447 would quietly stop working.
The sibling `try/except` is the same argument in the failure direction: an
alert-path bug must not take down the only mechanism that clears a stale
`LIMIT` badge.

The sweep gained bounded concurrency and a between-chunk lease refresh. It now
touches every subscription rather than the normally-empty believed-limited set,
so `N × 15s` serial probes could otherwise outlive the lease TTL and let a
sibling worker probe concurrently.

### Fleet escalation, and the claim it does NOT make

Every subscription saturated ⇒ one `high` item. It requires at least **two**
subscriptions (with one, "this subscription is full" and "every subscription is
full" are the same fact), and a single `unassessable` member blocks the claim
and is named instead — a positive fleet-wide claim needs positive evidence from
every member. When it fires, the per-subscription alerts are suppressed for
that cycle; the fleet item names them all.

The denominator is built from the subscription **roster**, never from the
sweep's results: a member whose sampling raised carries `classification: None`
by design (the swallow is what protects #447) and a mid-cycle lease yield
`break`s with a short result list, so filtering results on truthiness dropped
exactly the members the ent#100 rule requires to block the claim. A
3-subscription instance emitted *"All 2 registered subscriptions are at or past
75%"* with the per-subscription alerts suppressed by the early return, making
the false claim the only emission. `fleet_verdict` was always correct; the
caller had narrowed its input.

It says *what was measured*, not "auto-switch has nowhere to go". That sentence
would be underived: `select_best_alternative_subscription` filters on recent
failures and reads **no headroom at all**, so it will happily relocate agents
onto a 99% subscription. Teaching it about headroom is
`abilityai/trinity#2409`, which consumes this classifier.

### Cadence, cost, and the toggle

`SAMPLE_INTERVAL_SECONDS` (3600, floored at `REFRESH_SECONDS`) is a
**constant, not an operator knob** — the operator cannot reason about the right
value, and a mutable constant gating provider spend is the #1638 shape. It reads
no env var at all: neither compose uses `env_file`, so an unforwarded read would
be permanently inert while still *looking* configurable, which is the state that
invites a later "packaging fix" creating the knob. The one genuine lever is
`SUBSCRIPTION_SWEEP_CONCURRENCY` (probes per sweep chunk, fleet-size dependent),
forwarded in both composes and documented in `.env.example`. At
hourly this is ~24 probes/day/subscription of `max_tokens=1` Haiku, which is
**4× less than a watched instance already spends** through the 900s ambient
refresh.

It rides the existing `subscription_headroom_auto_refresh` toggle rather than
adding a second switch (operator decision). That toggle's label and caption
claimed probing happened *"only while a dashboard is open"* — false since #447
and more so now — and both the UI copy and the route docstring are corrected
here. The threshold itself is the on/off control: `0` disables (the
`operator_queue_retention_days` idiom), default 75, range 50–99, on
`PUT /api/subscriptions/settings/headroom-alert-threshold` with a named 422 and
a 422 block on the generic settings catch-all. The escalation tier is
**derived** (`max(threshold, 90)`) — two independently-settable thresholds are
an oscillator, and `validate_ops_setting` is per-key so it could not express the
cross-field invariant.

`GET /api/subscriptions/settings/headroom-auto-refresh` carries the honest
status: `active` plus an `inactive_reason` of `no_subscriptions` /
`threshold_disabled` / `auto_refresh_off` / `redis_unavailable` /
`count_unavailable`, so "no alerts" is distinguishable from "not checking"
(#2217's lesson). An unreadable subscription list is its **own** reason — it
previously fell through every arm (`None == 0` is False) and returned
`active: true`, claiming health from a failure in the one function whose whole
job is naming why it is off.

**It is rendered.** The threshold input and the status line live in
`SubscriptionsPanel.vue` beside the auto-refresh toggle, over the `BaseInput` /
`BaseButton` / `InlineError` primitives. The backend shipped first with no
client at all, which left AC #1's "Settings-surfaced value" API-only and AC #4's
"reports itself inactive **on the Settings surface**" reaching nobody — the
exact #2217 failure this design cites throughout. Decidable rules (validation
copy, the changed-check, the status line) live in
`utils/headroomAlertSettings.js` rather than the SFC: vitest runs
`environment: 'node'` with no mount harness, so a rule expressed inside a
component is one no test can reach (the ent#392 precedent).

### Residual

`create_item` has no UPDATE path, so a warning row keeps the number it was
raised with — a 75% alert still reads 75% when the subscription later sits at
92%. Same residual `retention_guard` documents for its own alarm. The
escalation is a separate id with a self-contained body, so the newer figure
arrives as a second item rather than an edit.

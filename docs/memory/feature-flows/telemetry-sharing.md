# Feature Flow: Opt-in Usage Sharing (Tier-2 telemetry — ent#12, ent#437)

> **Status**: ✅ Implemented — ent#12 (consent + egress + backfill, v0.8.5) and ent#437 PR1 (reachable ask, share id, enforced schema v2, outcome mix, send log, delivery that survives a missing receiver), and the benchmark read wired to the live receiver (ent#190, receiver live since 2026-09-04); the heartbeat's cadence anchored on the persisted stamp instead of process age (#2618).
> **Requirements**: `docs/memory/requirements/lifecycle-observability.md` §45.1 (ent#12) and §45.2 (ent#437). Payload contract: `docs/PRODUCT_EVENTS.md` (Tier-2 section). Public user docs: `docs/user-docs/operations/telemetry.md`, `docs/user-docs/faq/security.md`.
> **Open-core**: OSS-core by decision (both issues). Only the reciprocity **benchmark view** is entitlement-gated (`telemetry`; its read of the hosted service lives in the private module and is documented there), and the hosted receiver (ent#190, live since 2026-09-04) is not in this repo.

## The promise, in one paragraph

Nothing leaves the box until an admin turns sharing on. When they do, Trinity sends **coarse counts and enums only** — release version, platform, edition, install lane, feature list, agent and execution counts, the activation funnel, and an *outcome mix* of how runs ended — keyed by a random **share id** that is minted on consent, discarded on revoke, and is never the install id. The payload is inspectable **before** consent (the preview) and **after** (the last five send attempts), it is validated against a documented schema before every send, and turning sharing off stops egress at the next heartbeat. With sharing on, exactly one other request leaves, under the same two gates: the gated benchmark view asks the hosted service for this instance's standing, carrying only the share id.

## Surfaces

| Surface | Where | Who | What it does |
|---|---|---|---|
| **First-run overlay, `sharing` step** (ent#581; was the ent#437 Finish setup card) | Dashboard overlay, `components/onboarding/steps/StepSharing.vue` | verified admin | The consent ask as one step of the setup sequence: **Share anonymous usage** / **Don't ask again** (both write the server marker); the overlay's Skip replaces **Not now**. The sign-in-email nudge is the sibling `email` step. |
| **Usage sharing panel** (ent#12) | Settings → General, `components/settings/TelemetrySharingPanel.vue` | admin | The reversible toggle, the backfill window choice, the exact payload preview, the share id and its rule, **Recent sends**. |
| **Feature flags** | `GET /api/settings/feature-flags` | any authenticated user | Four booleans: `telemetry_sharing_enabled` / `_hard_disabled` / `_dismissed` / `_first_value`. The card decides from these alone. |

## Backend flow

```
Dashboard load ──▶ stores/sessions.loadFeatureFlags()  (cached, `once`)
                       │  four bools ─▶ firstRunSteps.js `sharing` step applies(...) (FirstRunOverlay)
                       │                  false ⇒ nothing rendered, ZERO telemetry queries
                       ▼ true
                   stores/telemetrySharing.load({preview:false}) ─▶ GET /api/settings/telemetry-sharing?preview=0
                       │                                              (admin + human-only; status off the event loop)
                       ▼ expand "See what would be sent"
                   load({preview:true}) ─▶ GET …/telemetry-sharing ─▶ asyncio.to_thread(build_aggregate_payload)
                       │
        Share ─────────┼──▶ PUT …/telemetry-sharing {enabled:true, backfill_days:30}
                       │      set_consent: claim sharing_id + dismissed_at (insert_setting_if_absent), audit, spawn_share(backfill=True)
        Skip ──────────┼──▶ overlay skip, localStorage `trinity_first_run_skipped` (no request)
        Don't ask ─────┴──▶ POST …/telemetry-sharing/ask/dismiss  (admin + human-only, audit, idempotent)

heartbeat (wake every 10 min + ≤10 min jitter, sleep-first, every worker — #2618)
          ── gates: TELEMETRY_SHARING_ENABLED/DO_NOT_TRACK AND stored consent  (before any stamp read or Redis touch)
          ── is_share_due(last_shared_at, interval): due unless the stamp is provably inside the interval
          ── _retry_throttled(recent_sends): after 5 consecutive failures, ≤1 attempt per interval/2
                                             │ due
                                             ▼
                                         _claim_tick()  telemetry_share:tick  SET NX EX interval/2  (a fresh lock per claim;
                                             │ won (or Redis down)                 released only if the receiver did not acknowledge)
                                             ▼
share_now ── gates: TELEMETRY_SHARING_ENABLED/DO_NOT_TRACK AND stored consent
          ── _resolve_window: backfill owed until first 2xx, then cumulative since last_shared_at
          ── get_or_mint_sharing_id (self-heals a deleted id)
          ── to_thread(build_aggregate_payload) ─▶ validate_payload(PAYLOAD_SCHEMA_V2)  ✗ ⇒ refused, ERROR log, recorded, NOT sent
          ── httpx POST TELEMETRY_SHARING_URL (10s, no credential)
          ── _record_send({sent_at, ok, http_status|error class, backfill, window_days, payload})  last 5
          ── 2xx ⇒ last_shared_at (+ backfill_delivered_at on a backfill)
```

**Files**: `services/telemetry_sharing_service.py` (everything above), `routers/settings.py` (the three routes + the flags spread), `db/schedules/stats.py::count_terminal_executions_by_status` / `first_autonomous_success_at` (+ facade delegations in `database.py`), `utils/app_version.py` (release version), `stores/telemetrySharing.js`, `stores/sessions.js`, `components/onboarding/firstRunSteps.js` (the `sharing` step's predicate, pure), `components/onboarding/telemetryConsent.js` (copy variant + the per-browser keys, pure), `components/onboarding/steps/StepSharing.vue`, `components/settings/TelemetrySharingPanel.vue`; the benchmark card (ent#190): `components/settings/ActivationFunnelPanel.vue` (fetch, once per mount), `components/settings/FleetBenchmarkCard.vue` (render), `components/settings/benchmarkFormat.js` (every decision, pure); the funnel footer (ent#545): `components/settings/funnelFormat.js` (the install-id footer's three states, pure), `services/operator_intake_service.py::get_installation_id` (the non-minting read the enterprise funnel GET uses).

## The payload (schema v2)

See `docs/PRODUCT_EVENTS.md` for the annotated document. What matters structurally:

- `sharing_id` — UUID4, **never `installation_id`**. The validator bans that key outright (`BANNED_KEYS`) and the builder never calls `get_or_create_installation_id`. Why: `installation_id` rides beside the operator's email and company in the ent#38 intake POST, so a share keyed on it is linkable to a person. Before consent the preview shows a fixed placeholder id so that *looking* mints no identity. The same rule now holds for the install id itself (ent#545): `operator_intake_service` has two accessors over it — `get_installation_id` (one read, `None` until minted; the enterprise activation-funnel GET uses it, so opening Settings → Activation no longer creates the identity row) and `get_or_create_installation_id` (the writers' accessor: the consent POST, the product-event emit, the canary label), whose mint is a write-once claim through `insert_setting_if_absent` like the share id below, so two workers cannot land two ids.
- `instance.trinity_version` — the **release** version (`utils/app_version.resolve_release_version`), never a commit SHA: adoption timing would re-join this stream to the presence/intake streams on a small fleet.
- `instance.install_source` — #2380's provenance, coerced to a short safe token or `unknown`.
- `outcomes.by_trigger` — projected from `db.get_fleet_execution_timeline(None, "trigger", hours)` + `db.shape_execution_timeline` (which folds through `_TRIGGER_BUCKETS` with `Other`), then mapped label → **telemetry-owned wire key** (`chat | mcp | channel | public | schedule | loop | reminder | room | operator_queue | agent | voice | other`). `cost` / `context_used` are dropped. UI labels never reach the wire, so a renamed or added product bucket lands in `other` instead of halting egress under the fail-closed validator; a parity test pins every `_BUCKET_ORDER` label to a key.
- `outcomes.by_status` — terminal rows grouped by raw status (`success | failed | error | cancelled | skipped | other`), the reader Trinity's own reliability classes needed (`success`/`failed` alone hid cancelled/skipped).
- `outcomes.provider_failures` — `rate_limit` / `auth` counts summed over `subscription_rate_limit_events`. That reader's cutoff is unconditional, so an all-time backfill (`window_days=0`) asks it for a window wider than the table's retention rather than `0`, which would read "since now".
- `activation_funnel` — derived from `_FUNNEL_STEPS` (five; `setup_step_intro` is a render beacon, not a funnel step), never hand-typed.

**Enforcement**: `validate_payload` walks a nested allow-list (`PAYLOAD_SCHEMA_V2`: exact key sets, types, documented vocabularies, UUID shape) and `share_now` **refuses to send** on any violation — fail-closed egress, logged at ERROR and recorded in the send log as `error: "schema"`.

## The identity rule

| Event | `telemetry_sharing_id` | `telemetry_sharing_dismissed_at` |
|---|---|---|
| consent off → on | minted (write-once claim; `sharing_id_rotated: true` in the audit) | stamped if absent — a consented install is never asked again |
| consent on → on | unchanged | unchanged |
| consent on → off | **deleted** (revoke = forget locally) | unchanged |
| re-consent | minted again — a different id | unchanged |
| manual `DELETE /api/settings/telemetry_sharing_id` while on | re-minted at the next send (self-heal; a fresh id links to nothing) | — |
| "Don't ask again" | — | stamped if absent (first stamp wins) |

Both keys are written with `db.insert_setting_if_absent` (#2380's write-once primitive), so two workers or a double-clicked consent cannot persist one id and send another. Anything already sent stays with the receiver: the client sends no deletion on revoke (a fresh id links to nothing), and the receiver's forget-me deletion (ent#190) is operator-initiated, keyed on the id the admin reads off this panel — the panel copy says so.

## The ask: snooze-first, warm once

The first-run overlay's `sharing` step (`components/onboarding/firstRunSteps.js`, ent#581) applies when: admin ∧ ¬hard-disabled ∧ ¬enabled ∧ ¬dismissed (server). The overlay itself opens only once flags, profile and first-run state have loaded (`isFirstRunOverlayVisible`), and a skipped step does not re-open it.

- **Skip** (the overlay footer; it replaced **Not now**) = a per-browser skip in `localStorage['trinity_first_run_skipped']`. No request. A pre-ent#581 snooze (`trinity_telemetry_ask_snoozed_until`, 14 days) is honoured as a skip of this step while it lasts. A one-shot cold ask at the coldest moment is ent#12's own "pure opt-in gets almost no data" trap; the snooze was the operator override on the plan (trail #35).
- **Warm copy** = `telemetryConsent.consentVariant` picks value-framed copy **once per browser** after the install's first SUCCESS execution with an autonomous trigger (`schedule`/`webhook`). Since ent#581 it changes only the copy of a step that is shown; it no longer re-opens a skipped ask. The milestone is **derived on read and memoised** (`telemetry_sharing_first_value_at`, via `db.first_autonomous_success_at()` — one `LIMIT 1` read until it exists, a settings read forever after), deliberately *not* a hook in the dispatch terminal: `task_execution_service.py` is a code-health hotspot under #2314.
- **Cost**: the step reads the flags document the Dashboard already awaits and calls the admin status route only when it will render; the preview loads on expand (`?preview=0` first). Steady state after consent, dismissal or hard-disable: zero telemetry queries per Dashboard load.
- The chassis is the first-run overlay (`FirstRunOverlay.vue`): one step per open item, so the Dashboard grows no stacked nudge; the email nudge is the sibling `email` step, and its #2381 `localStorage` dismissal counts as a skip of it.

## Delivery that survives a missing receiver

The hosted receiver (ent#190) did not exist when this shipped and went live on 2026-09-04. The three properties below made the gap honest rather than silent, and they still guard any outage:

1. **Recent sends** keeps the last five attempts — successes and failures, with the HTTP status or the exception *class* (never `str(e)`) — behind the admin panel. A 404 from the default URL is worded as a 404 at the default address; from an overridden `TELEMETRY_SHARING_URL` as that receiver answering 404 (`receiver_hint`). The log does not yet record which host answered (#2571).
2. **Backfill until delivered**: the consent-time backfill is retried by every heartbeat until the first 2xx (`telemetry_sharing_backfill_delivered_at`), so the disclosed history is not lost for the cohort that consents before the receiver exists. After that, heartbeats cover everything since `last_shared_at` (cumulative, in whole days — the window floors, so the minutes a send lands past the exact interval are not counted: a ≤20-minute boundary effect per day against a 45-day benchmark window, kept over a ceiling that would halve the receiver's per-day rate). Note for upgrades: an install that consented under ent#12 has no delivered marker, so its first heartbeat after this deploy re-sends one backfill window.
3. **One send per interval fleet-wide, decided from the stamp (#2618)**: every wake (10 min + ≤10 min jitter, sleep-first so boot is never a burst) reads `last_shared_at`, and a send is due unless that stamp is *provably inside* the interval — empty, unparseable and future stamps are due, so an owed backfill retries at the first wake and a corrupt stamp self-heals on the next 2xx. A restart therefore never resets the cadence; before #2618 the loop slept the whole interval from process start, so an install that restarted daily shared its consent-time backfill and never again. `_claim_tick` takes `telemetry_share:tick` via a **fresh** `SingleFlightLock` per claim (the lock is single-use: a reused one answers True forever) with TTL = half the interval, keyed to the send cadence, never the wake. It is **released only when the receiver did not acknowledge** — so a failed send is retried at the next wake by either worker (or at marker expiry, if Redis was unavailable at release) — and kept after an acknowledged one; `share_now` reports an acknowledged send as True even when the local stamp write fails, because with the stamp unwritable the marker is what stops a sibling re-sending an accepted snapshot. After five consecutive failures (the whole send log), attempts fall to at most one per half-interval, measured from the persisted log, so a dead receiver or an air gap costs twice a day rather than every wake and recovers within half an interval. Fail-open: Redis down ⇒ both workers may send, today's behaviour. A cancellation mid-send (a reload, a SIGTERM; the heartbeat is stopped in lifespan shutdown) also releases, so a dead process's marker never blocks the next boot's first wake. Two consequences worth knowing: if the stamp cannot be written after an acknowledged send, the marker alone paces sends — one per half-interval, twice the normal rate — until it can, and the WARNING says so; and a failure *before* the POST (a settings read, the id claim, the aggregate build) is recorded in the send log like any failed attempt, so the panel shows it and the cap applies to it too.

## Settings keys and their guards

All under the `telemetry_sharing_` prefix: `enabled`, `consent_at`, `backfill_days`, `last_shared_at` (ent#12) plus `id`, `dismissed_at`, `first_value_at`, `backfill_delivered_at`, `recent_sends` (ent#437). The generic `PUT /api/settings/{key}` refuses the whole family (the dedicated routes are the only writers); the generic `DELETE` stays admin-gated and **open** for it by design — it is the reset path, and every deletion moves toward off / ask again / re-mint — or, for `last_shared_at`, one re-share: since #2618 an empty stamp is due at the next wake, so that DELETE costs one bounded heartbeat within 10–20 minutes, consent still gating. Reset the ask on a dev box: `DELETE /api/settings/telemetry_sharing_dismissed_at`.

## Security properties

- Consent, dismissal and the status read are `assert_admin` + `reject_agent_principal` (an agent-scoped key resolves to its owner carrying the owner's role — trinity-ops-agent#232; `assert_admin` rejects agents since #1890, the explicit gate is the belt the PUT has always worn).
- The flags are booleans only; the status read (share id, last payloads) is human-only.
- The share id is logged as an 8-char prefix; audit rows carry `sharing_id_rotated` as a bool, never the id.
- Every reader in the builder is fenced and coerced, so a stubbed or failing source degrades a field, never the payload, and a Mock can never reach the validator (learnings 2026-08-03); the builder runs off the event loop in both callers.
- The one thing the payload cannot promise alone: unlinkability also depends on the receiver keeping this stream apart from the identified intake record — a contract item recorded on ent#190 (enforced receiver-side since 2026-09-04) and ent#466.

## The benchmark read (ent#190)

The gated benchmark view (private module, `telemetry` entitlement) asks the hosted service for this instance's standing and answers five statuses — `not_sharing`, `pending`, `not_enough_data`, `ready`, `unavailable` — with `message` as the one home of the operator prose and `reason` naming the class. The OSS side only renders: `components/settings/FleetBenchmarkCard.vue` composes `LoadFailed` / `SkeletonLoader` and takes every decision from the pure `benchmarkFormat.js` (branch per status, tint from the effective `sharing_enabled`, the three-row metrics table, a legacy pre-bump shape still rendered through the message branch; units pinned to the receiver's contract — the success rate is a 0..1 fraction, percentiles are 0..100 mid-rank — and a rate outside its range renders as a dash rather than a clamped or multiplied number). `ActivationFunnelPanel.vue` fetches it **once per mount and independently of the funnel** — each call is an outbound request carrying the share id, so the window selector never re-fires it, and a slow receiver never hides the funnel. The read itself (gates, non-minting id read, URL derivation, bounded transport, memo) is the private module's mechanism and is documented in the private repo.

## The funnel footer (ent#545)

The activation panel's footer prints the install id the enterprise funnel GET answers, and since ent#545 that field is `null` until a writer has minted it. `funnelFormat.js::installIdFooter` decides three honest states and the panel renders them through interpolation: `minted` (a non-blank string → `Install <id>`), `not_minted` (exactly `null` → "No install id yet — one is minted when this instance opts in to security & product updates (Settings → General)", the one writer an operator can reach, since the wizard's first product event only fires on an empty fleet or `?onboarding=1`), and `unavailable` (an absent key or a malformed value — not evidence about minting, so never rendered as that claim). A pre-bump enterprise backend still answers a string and lands in `minted`, so the panel renders across the two-repo landing window. The read itself, its sentinels and the static guard are the private module's mechanism (`tests/test_545_funnel_never_mints.py` there).

## Testing

- `tests/unit/test_ent437_telemetry_consent.py` — schema v2 shape and the `installation_id` ban; wire-key projection with `Other`, cost dropped; `by_status` folding; provider failures and the all-time window; a fully stubbed `db` still validates; validator rejections (parametrised); parity tests (`_BUCKET_ORDER` ↔ wire keys, funnel ⊆ allow-list); `share_now` refuse/record/deliver paths incl. the 404 hint; send-log bound + corrupt row; backfill-until-delivered; id lifecycle + self-heal; dismissed first-wins; first-value memo; flags hidden-direction; tick marker via fakeredis; router gates, lazy preview, audit shape; the version resolver's import safety.
- `tests/unit/test_ent12_telemetry_sharing.py` — the ent#12 gates and PII assertions, updated to the v2 keyed shape.
- `tests/unit/test_2618_heartbeat_dueness.py` — the scheduler layer (#2618): the pure dueness matrix (empty / unparseable / future / exact / offset / naive stamps, a 1h interval); one wake on a store without sleeping (gates before any stamp read or Redis touch, positive control); three boots over one store through `_loop` with a fake clock (nothing inside the window, one send past it, silence after another restart); sleep-first on an ordered sleep/send timeline; the marker on fakeredis (TTL keyed to the cadence at 1h and 24h, a fresh SETNX per claim, released on the store after a failure and kept after an acknowledgement even when the stamp write raises, release touching only this tick's lock, fail-open, released on cancellation mid-send); retry at the next wake with a 2xx-only stamp; an unreadable store never sends; the persisted retry cap (rule, before-the-claim, and ten wakes against a dead receiver end to end); a tick that raises does not end the loop; the module never again says "never released".
- `src/frontend/tests/unit/telemetryConsent.spec.js` — the visibility matrix, the warm-once rule, the copy's claims (says anonymous / off by default / reversible / last 30 days; never "traceable" or "secure"), the receiver wording, snooze persistence under blocked storage.
- `tests/unit/test_operator_intake.py` — the ent#545 read twin: `None` on a fresh store with a store that refuses every write, the stored id verbatim, blank / non-string as not minted, a read failure propagated rather than rendered as "not minted", writer and reader as one key, and the racing claim (a worker whose claim loses returns the winner's id, never an upsert).
- `src/frontend/tests/unit/funnelFormat.spec.js` — the footer's three states (`minted` / `not_minted` for exactly `null` / `unavailable` for everything else), no blank leading phrase for any wire value, copy free of JS-isms, and a source-structure guard that the panel routes through the pure function and passes the wire value through untouched.
- `src/frontend/tests/unit/benchmarkFormat.spec.js` — the fleet-benchmark card's decisions: the branch per status (including the legacy pre-bump shape), the tint, metric formatting and ordinals, null-safe rows, the participants and based-on lines, the reason detail.
- Not asserted: the rendered card in both themes and the real clock's cadence (dueness itself is asserted in the #2618 file; the 2026-09-09 fix was also watched live: an instance 45h past its last share sent within one wake of the reload) — eyeballed on the local stack (sink recipe: `TELEMETRY_SHARING_URL=http://host.docker.internal:8787/v1/telemetry-share` + a local POST sink; consent fires an immediate backfill send).

## Deferred (recorded)

Feature-usage / click-through coverage (PR2, child issue); the error-class taxonomy (ent#418); an edition-differentiated ask (ent#496, unblocked by ent#190); the send log recording its destination host (#2571); fleet-composition tallies in the benchmark card once a `ready` answer is observable; `main.py` adopting `utils/app_version.py` (debt inbox `2026-09-03-main-version-resolver-adopt-util`).

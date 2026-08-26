# Feature Flow: Dashboard Grid View (magnetic tile canvas)

> **Last Updated**: 2026-08-20 (#2352/#2353: a rejected token is not a rate limit)
> **Status**: Implemented — third dashboard mode, not default
> **Issue**: trinity-enterprise#47 (design of record embedded in the issue)
> **Requirements**: `docs/memory/requirements/core-agent.md` §9.8, §9.12 (info tiles)

## Overview

One of the Dashboard's three view modes alongside **Timeline** (waterfall
activity) and **List** (the retired Agents page consolidated in
trinity-enterprise#260 — see
[dashboard-list-view.md](dashboard-list-view.md)). The legacy **Graph** mode
(Vue Flow topology) was decommissioned in #1689. Grid is a **magnetic tile
canvas**: richer 384×216 landscape agent tiles
that snap to a sparse, **unbounded** integer lattice (negative coordinates
included) the operator arranges freely — islands, gaps, parked loners — with
iPhone-style drag and live snap preview, on a pan/zoom dotted-canvas.

- Mode toggle: `Timeline / Grid / List` in the Dashboard header; selection
  persists to `localStorage['trinity-dashboard-view']`. **Timeline stays the
  default** for users with no saved preference (and a stale mode — `'graph'`,
  or `'list'` on an older bundle — degrades to it via the
  `VIEW_MODES.includes()` guard).
- **No Vue Flow dependency** in this mode, and **no new backend endpoints**.

## Components & Data Flow

```
views/Dashboard.vue          mode toggle, grid pane (v-if), Tidy up / Reset pills,
  │                          "N working now" header stat, empty/error/skeleton states
  ├─ components/FleetGrid.vue    pan/zoom viewport, lattice, drag physics, sockets,
  │    │                         cell shading, keyboard reorder, zoom controls + legend,
  │    │                         viewport culling, shared 1s tick for tile timers
  │    └─ components/AgentTile.vue   five-zone tile (see below); composes
  │           AgentAvatar / RuntimeBadge / RunningStateToggle / AutonomyToggle
  ├─ stores/fleetGrid.js     per-user layout (localStorage v1, self-healing),
  │                          lazy analytics hydration queue (concurrency 4,
  │                          stale-while-revalidate over executions-store cache),
  │                          batch chip data (sync-health + operator-queue pending)
  │                          on a 60s visibility-aware poll, active only while mounted
  ├─ stores/network.js       agents list, contextStats / executionStats / slotStats
  │                          (15s shared poll), NEW: viewMode ('grid'|'timeline'; graph decommissioned #1689),
  │                          circuitBreakers map, WS-driven workingState map
  ├─ stores/executions.js    fetchAgentAnalytics(name, '14d') — existing
  │                          `${name}:${window}` cache (#1107)
  └─ utils/gridLayout.js     pure lattice math: cell geometry, spiral
                             nearest-free-cell, normalize (self-healing), tidy, bbox
```

### Data sources (all existing)

| Tile element | Source |
|---|---|
| Identity, status, runtime, repo, autonomy | `GET /api/agents` (network store) |
| Activity·14d chart + Context·7d chart | `GET /api/agents/{name}/analytics?window=14d` (#1107) — one fetch feeds both; last 7 timeline entries drive the context line |
| Live context %, Active/Idle/Offline | fleet `GET /api/agents/context-stats` (15s poll) |
| Success meter, tasks/cost/last-run, schedules chip | fleet `GET /api/agents/execution-stats` (15s poll) |
| ⚡ circuit open chip | `GET /api/agents/slots` → `circuit_breakers` map (#526) |
| ⟳ sync failing / git ✓ chips | `GET /api/agents/sync-health` (#389, batch) |
| ⚠ needs response / approval pending chip | `GET /api/operator-queue?status=pending` (batch, grouped per agent) |
| ▶ working + elapsed timer | WS `agent_activity` events → `workingState` map, reconciled by the context-stats poll; fallback `activityState === 'active'` |

### Trigger-bucket collapse (tile scale)

The backend's #1107 buckets collapse to three groups: **Scheduled** ←
Scheduled · **Manual·MCP** ← Chat/Tasks, MCP, Loops, Agent-to-agent, Other ·
**External** ← Channels, Public, Voice. A board-level legend (bottom-right)
explains the colors once — never repeated per tile.

## Layout model

- Layout = per-user map `agent → {c, r}`; localStorage key
  `trinity-grid-layout-v1`; server-side per-user storage is a follow-up.
- **Self-healing** (`normalizeLayout`): new agents take the first free cell
  near the origin (spiral search); deleted agents leave their gap; an invalid
  or colliding saved position resolves to the nearest free cell.
- **Filters never destroy layout**: persisting merges the active layout over
  the full saved map, so agents hidden by an owner/tag filter keep their
  saved cells (filtering is indistinguishable from deletion client-side, so
  absence is never treated as deletion). The `/` type-to-filter (ent#261)
  rides this same absence-as-filtering path: matching tiles stay at their
  lattice cells (gaps preserved — no tidy/re-layout on filter), and a
  zero-match keeps FleetGrid mounted under the chassis query-empty overlay,
  so pan/zoom and layout state survive transient zero-matches while typing.
- **Tidy up** compacts row-by-row (3 columns) preserving reading order,
  anchored at the layout's own top-left, clamped to the coordinate bound.
  **Reset** restores the deterministic default (system agent first,
  reading-order grid) and drops stale saved entries.

## Interaction constants (design of record)

Cell 384×216, gaps 34/18 (column gap exceeds the 26px avatar overhang);
zoom 0.25–1.6× around the cursor; drag follows 1:1 (screen deltas ÷ zoom)
with ±3° velocity tilt; displace/reflow 280ms `cubic-bezier(.3,.7,.25,1)`;
overshoot snap 420ms `cubic-bezier(.22,1.35,.32,1)`; lock-ring pulse 450ms.
Drop on an occupied cell **swaps** (live preview while hovering; a swap never
disturbs a third tile). Keyboard: focused tile moves with arrow keys (through
a neighbor = swap). `prefers-reduced-motion` disables tilt/spring/pulse —
drops become instant placement. Multi-touch is discriminated by pointer id
(one drag at a time; a second touch cannot start a pan mid-drag).

## Performance contract (#47 acceptance criteria)

1. **Non-blocking first paint** — tiles render immediately from the agents
   list the Dashboard already holds; the chart zones show the scanline
   loading motion while analytics stream in (see "Chart loading motion"
   below). Nothing awaits the full set.
2. **Lazy, capped hydration** — a tile asks the fleetGrid store to hydrate
   only when near the viewport (culled tiles render a light placeholder and
   fetch nothing); fetches run through a 4-slot queue into the executions
   store's `(agent, window)` cache; stale entries (>5 min) serve instantly
   and refresh in the background.
3. **Batch endpoints over per-agent loops** for chip data; the 60s poll is
   visibility-aware (skips when `document.hidden`) and tears down when the
   Grid unmounts (mode switch is `v-if`).

## Chart loading motion (trinity-enterprise#245)

The tile's two chart zones use **`components/ScanlineReveal.vue`** — the
app's default data-loading motion (design-system.md §6: beam sweep while
loading, one 550ms `clip-path` wipe-in when data arrives) — instead of the
retired flat pulse skeleton. This is the **reference adoption** of the
primitive; new data-loading surfaces reach for it rather than inventing
spinners/skeletons (fleet-wide adoption pass: trinity-enterprise#253).

- One `ScanlineReveal` per chart box, both driven by a single
  `chartsLoading = !analytics && analyticsPending` flag, so the beams
  phase-sync. `:reveal="!!analytics"` — a data-less terminal (per-tile fetch
  error) snaps instead of playing the arrival pass; zero-run stub baselines
  are an answer and do reveal.
- The phase rules (cache hits mount straight to `loaded`, rising edges
  re-enter `loading`, background refresh never animates) live in the pure
  `utils/scanlinePhase.js`, unit-tested in `tests/unit/scanlinePhase.spec.js`.
  The store contract already guarantees the refresh half: `fleetGrid.hydrate`
  only sets `'loading'` when there is no cached payload.
- The grid overrides the primitive's token defaults with its own palette
  (`.t-charts .mini .scanline` → `--scan-core: var(--gv-blue)`,
  `--scan-track: var(--gv-bar-track)`), so both themes come from FleetGrid's
  existing `--gv-*` definitions.
- `prefers-reduced-motion`: static track, instant reveal (JS matchMedia skip
  + CSS belt). E2e guards in `e2e/dashboard-grid-view.spec.js` assert no
  element retains a `clip-path` after settle (the stuck-reveal bug class)
  and cover the reduced-motion path.

- **No-blink reveal**: during the arrival pass the dimmed track is wiped OUT
  behind the beam (the complementary `clip-path` of the content wipe), so
  each pixel shows either the track or the final content on its final
  background — the track's unmount at `loaded` is visually a no-op.

**Adoption is deliberately Dashboard-only for now** (product decision,
2026-08-08): Agent Detail keeps its existing loading states. Rolling the
primitive across further surfaces (Overview trend charts included) is
trinity-enterprise#253's charter.
4. **A slow or failed per-agent fetch degrades that one tile only.**

## Info tiles (widget chassis ent#325 · data tiles ent#100, ent#96)

A **second occupant type** shares the lattice with agent tiles under `widget:<id>`
keys in the same layout map, so drag / swap / tidy / keyboard / culling apply with
no second code path. The chassis itself — registry, key namespace, layout v2
migration, prefs-as-override-map — is documented under **#2126** and deliberately
not restated here; what follows is only what a data tile adds.

```
components/tiles/catalog.js            side-effect registration into GRID_WIDGETS
  └─ components/tiles/RecentFailuresTile.vue      (ent#100, default-on)
        ├─ components/InfoTile.vue                shell: scope/title/stamp/state/footer link
        ├─ components/tiles/parts/TileRowList.vue bounded [lead][primary][meta] + optional sub-line
        └─ utils/executionFailure.js              PURE: state machine, code marker, trigger label
```

**Props the chassis passes** (`FleetGrid.vue`): `:agents` = the **unfiltered**
roster (`orgAgents || agents`), and `:now` = the shared 1s tick, **only** when the
catalog entry declares `wantsTick: true`.

- Unfiltered because an info tile is *fleet*-scope: `props.agents` is the ent#261
  `visibleAgents` seam, narrowed live per keystroke by the `/` filter, so binding
  it would degrade every non-matching row's display label to a raw slug as the
  operator types — on a default-on tile. ent#305 built this seam for the same
  reason.
- `wantsTick` because binding the tick unconditionally forces a child update once
  per second, forever, for every tile on a canvas that also drags at 60fps; epic
  ent#94 queues eight tiles and most render no clock.
- `InfoTile` sets `inheritAttrs: false`, so a prop a tile does not declare cannot
  fall through onto `<article>` as a literal DOM attribute.

### Executions (ent#96) — the tile that needed a second dimension

```
components/tiles/ExecutionsTile.vue          (default-on, wantsTick: false)
  ├─ components/InfoTile.vue                 shell
  └─ utils/executionsTile.js                 PURE: stack order, columns, legend, headline, state
```

| Tile element | Source |
|---|---|
| 24 hourly columns, stacked by trigger | `GET /api/executions/timeline?group_by=hour&hours=24&split=trigger` |
| Stack + legend order | the SAME response's `trigger_order` (backend `_BUCKET_ORDER`) |
| Headline (runs · ok% · failed) | summed from the same buckets — never a second query |
| Live running / queued chips | `GET /api/executions/stats?hours=24` (those two fields are live, not windowed) |

**Why `split` rather than N calls.** The chart's axis is hours and its stack is
triggers; `/timeline` grouped one dimension at a time, so the tile would have
needed one request per bucket name — and then a column whose segments came from
ten responses could disagree with its own total. The endpoint folds the second
dimension server-side and **re-sums each bucket's totals from the split rows**,
so that disagreement is unrepresentable. Gap-filled hours carry `by_trigger: {}`,
never a missing key, so the renderer never has to tell "no runs" from "no field".

**Why failures are a rail, not a segment.** AC2 asks for failures to be visible
and never hidden inside totals. As a stack segment, "Failed" would have to be
subtracted from its trigger's segment to keep the column equal to runs — which
silently redefines every other segment as "runs that succeeded". The rail sits
below the stack on its own scale: the column still totals runs, failures are
visible, and the hover breakdown still names which trigger failed.

**Order comes from the server.** A bucket present in the data but missing from
`trigger_order` is appended rather than dropped — otherwise it would count toward
a column total while missing from its stack, which is the same "the chart does not
add up to itself" failure in the other direction.

### Recent failures (ent#100) — data sources (all existing)

| Tile element | Source |
|---|---|
| Failure rows (agent · trigger · age · message) | `GET /api/executions?status=failed&hours=24&limit=4` — **returns a BARE JSON ARRAY**, unlike every other fetcher in this store |
| "N in 24h" header stamp | `GET /api/executions/stats?hours=24` → `failed_count` |
| Agent display label | the `:agents` roster (lookup table, never a data source) |
| Row age | the shared 1s tick, corrected by the server clock offset read from the response's HTTP `Date` header (`utils/timestamps.js::serverSkewMs` — no backend change) |

Both GETs ride `stores/fleetGrid.js::refreshBatchData()`, the one 60s
visibility-aware poll, **gated on the tile being enabled**; a
`visibilitychange → visible` listener refreshes immediately on return-to-tab
rather than waiting out the interval (an event listener, not a second timer).
The tile itself never fetches — culling *unmounts* tiles, so an `onMounted`
fetch would re-issue on every pan.

Each GET owns its own `{data, loaded, error}` triple. Sharing one pair
manufactures a false green in whichever direction is left unguarded.

### The green empty state needs positive evidence

"No failures in 24h ✓" is a positive claim about the fleet on the fleet's own
monitoring surface. Three independent faults would otherwise produce it, and all
three are closed in `utils/executionFailure.js::failuresTileState` (a pure
function, because the unit suite is node-environment and cannot mount):

1. **a failed rows GET** — principle 15 / #1926: an empty state needs a fetch
   that succeeded and returned zero, never `list.length === 0`;
2. **a failed `/stats` GET** — the 24h total is a second request, so its failure
   means *unknown*, not zero; it is never inferred from `rows.length`, because a
   bounded page cannot yield a 24h total;
3. **an unenumerable fleet** — `accessible_agent_names` →
   `docker_service.list_all_agents_fast()`, which returns `[]` when the Docker
   client is None **and on any exception** (throttled warning). For a non-admin
   every fleet accessor then early-returns zeros at **HTTP 200**: the fetch
   "succeeds", `loaded` is honestly true, and a green all-clear is invented by an
   infrastructure fault. The same fault empties the Grid's own roster, so a
   non-empty roster is the enumerability signal available with no backend change.
   (The durable fix is ent#384's — resolve the accessible set from
   `db.get_all_agent_metadata()` — which is a backend change.)

A **fourth** route lives one layer up, in the store rather than the state
machine, and is closed there: `GET /api/executions` answers a bare array, so a
200 whose body is *not* an array is a fault — coercing it to `[]` while flipping
`loaded` true and `error` false would hand `failuresTileState` the byte-identical
shape of a healthy empty fleet, invisibly to the pure function's exhaustive
sweep. `fetchRecentFailures` treats it as a failed cycle instead, matching the
`/stats` fetcher, which already degrades an unreadable `failed_count` to `null`
(*unknown*, never zero). Pinned by `tests/unit/fleetGridFailuresFetch.spec.js`.

Note the asymmetry: a *refresh* failure over already-loaded rows stays `ready`
(stale-while-revalidate). Only the CONFIRMATION requires everything green on this
cycle.

`/stats` counts `status IN ('failed','error')` while the list endpoint filters
ONE status, so a fleet whose only recent failures are legacy `'error'` rows would
otherwise render "3 in 24h" beside a green ✓; that case renders an explanatory
line instead of the row list.

### Error-code taxonomy: read, never guessed (named AC deviation)

`schedule_executions` has no `error_code` column — the code lives on
`TerminalEnvelope` in memory and is discarded at the terminal write, leaving
`error_summary = SUBSTR(error, 1, 200)`. `failureCodeFromSummary` reads an
**anchored, lower-case, charset-bounded** `[code]` prefix when the platform
actually emitted one and returns `null` otherwise. It is deliberately not a
classifier: `services/failure_classifier.py` is a byte-identity-mirrored pair
with `src/scheduler/failure_classifier.py`, and a third copy in JavaScript
guessing from a truncation would be a new unenforced mirror producing labels
nobody can trust. The only writer of that marker today is
`pull_coordination_service.py`, dark until a pull pilot — so the chip is absent
on every current install and the freed width goes to the real message. Persisting
`error_code` is a follow-up; the chip then appears with no UI churn.

`error_summary` is agent/LLM-authored, prompt-injectable text: rendered as text
interpolation and bound attributes only, never `v-html`.

### Subscription pressure (ent#259) — the first admin-only tile

"How much is left on any of my subscriptions?" answered from the board. The
**inverse unit** of #471's per-agent pressure chip: that chip says whether *this
agent's* funding is strained and structurally cannot say *which subscription* is
the bottleneck, because agents share a subscription and burn one 5h window
between them — reading it off the board otherwise means grouping every agent
chip into buckets by eye.

```
components/tiles/SubscriptionPressureTile.vue     (adminOnly, default-on, wantsTick: false)
  ├─ components/InfoTile.vue                      shell
  ├─ components/tiles/parts/TileRowList.vue       one row per subscription
  ├─ utils/subscriptionPressureTile.js            ALL decisions (pure, node-env testable)
  └─ stores/subscriptions.js::fetchPressureData   roster + usage on the 60s batch poll
```

**Zero backend change**, per the operator ruling on ent#259 (2026-08-19): "a
small build on `GET /api/agents/subscription-pressure` + the extended
`GET /api/subscriptions/{id}/usage` once #471 merges". The roster comes from
`GET /api/subscriptions` and each row's figures from `/{id}/usage` — a fan-out
of one request per subscription, which at the real fleet size (one per Claude
account) is fewer requests than `fetchOpQueuePending` already makes on the same
poll. A batched endpoint was designed and **rejected**: it optimizes ~0.5 q/s,
and `assert_admin` rejects agent principals (#1890) so it would not even be
reusable by ent#351's agent-facing tools.

**First `adminOnly: true` tile, and not a style choice** — every endpoint it
reads is admin-gated (the payload carries per-subscription spend) and its footer
link goes to Settings, whose route is `requiresAdmin`. The flag also does the
gating for free: the widget key is absent for non-admins, so the batch fetch
never fires and there is no 403 loop. Because `isAdmin` is only confirmed once
`fetchUserProfile()` lands, the store also **watches the key appearing** and
re-polls — otherwise a cold load skips the fetch and the tile sits blank until
the next 60s tick. `fetchSubscriptionPressure()` (the per-agent chip feed) stays
**ungated**: chips and list badges must keep working while this tile is off.

**What the row may claim** — the honesty rules, all pinned in the pure module:

| Shown | Rule |
|---|---|
| `5h ▰▰▰ 97%   7d ▰▰ 88%` | **Both** rolling limits, each as a small 30×4px bar **plus** the number, coloured by its own band — green < 60, amber < 85, red ≥ 85 (`utilizationLevel`). The bar is a second glanceable channel, never the only one: colour also rides the number, the row carries a severity chip, and each window is `role="img"` with an `aria-label` spelling the reading out. Fill width is `fill` (clamped 0–100 by `barWidthPct`), NOT `pct` — an overage plan reports >100% and an unclamped `width: 137%` overruns its track and shoves the row out of a body that is `overflow: hidden`; the *number* stays unclamped so the overage is still reported. Track width is fixed so the two windows align into columns down the tile. Two windows rather than one because either can be the wall you hit: a subscription routinely sits at 20% of its 5h while its weekly is nearly gone, and one number cannot say which. Rendered ONLY when `source=anthropic` and the snapshot is ≤30 min old (`headroomIsFresh`); #471 established the number is real (`anthropic-ratelimit-unified-*` headers), so ent#259's "never a fake-precise X% left" is about FRAMING — utilization *consumed*, source-labelled — not about hiding a genuine reading. Labelled `5h`/`7d` with a visible `resets_at`. These were previously described here as **rolling** windows; ent#434 measured them and they are **fixed windows with a scheduled reset** — on a live instance `seven_day_resets_at` held constant at one midnight-UTC instant across five days of probes while utilization climbed 36→90, then stepped exactly +7 days. So the number is consumption accumulated *within* the current window, not a rolling average, and it drops to near-zero at the reset rather than decaying. The `5h`/`7d` labels stay in preference to "daily"/"weekly" because the reset is not calendar-aligned to the reader's timezone. An absent window is omitted rather than shown as `0%` |
| `near` chip | A window in the **red** band — **or the provider's own `allowed_warning` status, which can fire below that band and is better evidence precisely because it is theirs (#2396)** — raises severity to `warn` even with a spotless failure history — it is the only signal that exists *before* the first 429. Without it a subscription at 93% of its weekly ranks below one whose usage read merely failed, and gets pushed into the overflow. The chip distinguishes `429s` (already happened) from `near` (has not yet) |
| `3× 429` | the `rate_limit` **kind only**, never `failure_events_24h`, which also counts `auth` and pre-#471 `unknown` rows. Migration 0040 split them at the data layer; reporting the total under a "429" label would undo that fix in the UI |
| `rate-limited` | the backend's one-gate `rate_limited_now` only — a 24h count alone never claims "limited now". **Since #2352 that flag means real 429s**: its event half is scoped to `failure_kind = 'rate_limit'`, so an auth failure no longer renders here (it used to, via a kind-blind 2h predicate — a dead token read as quota exhaustion on every surface). **Nor does the provider's warning tier (#2396):** `allowed_warning` was read as a hard limit by the backend predicate, so a subscription that was merely *near* its weekly window wore a red `limit` chip while the provider was still serving it. It now scores `warn`/`near` |
| `token invalid` | `headroom.status == "invalid_token"` — the provider REFUSED the credential. It outranks `rate-limited` (#2353): a probe that could not authenticate learned nothing about the quota, so a limit claim there is unfounded, not merely less useful. It is also the only state on this list a person can act on immediately, and before #2353 it was reachable only by hovering. Read regardless of snapshot age — the deliberate inverse of the freshness gate above, because a *number* decays and "this credential was refused" does not |
| `auth` chip | a rejected token gets its OWN severity, ranked ABOVE `crit` in the row sort: the row needing a person sorts above the row needing a wait. The warn tier gains the same word for an auth-only 24h history (`warnReason: "auth"`, strict equality against the total so the per-agent chip — which sees only a total and an auth count — cannot disagree) — without it, an auth-only history would land in `warn` after the predicate split and render as `429s`, trading one wrong label for another |
| `no provider data` | `headroom.status == "error"` (transport failure, non-200, or a 200 carrying no unified headers). Ranked below the failure states — a failed probe says nothing — but ABOVE `ok`, which would assert health on no evidence |
| `nearing limit` | the provider's `allowed_warning` tier or the red band (#2396). A forecast, not a failure, so it ranks below everything that has actually gone wrong and above `ok`, which the row is not. **Scope, honestly:** the SFC binds `{{ row.meta }}` behind `v-else` on `v-if="row.windows"`, so on the tile this wording is reached only by a fresh warning-tier snapshot carrying a status but no utilization figure — the bars are the reading otherwise. It exists so `pressureHeadline` cannot say `ok` for a row this module scores `warn`; the operator-visible half of the fix is the lead chip going red `limit` → amber `near` |
| `≈$3.12 · 1.2k out` | output tokens and cost are real. **`input_tokens` is deliberately absent from the row face**: it is `SUM(schedule_executions.context_used)` — context-window *occupancy* per run, not tokens consumed — the same ruling architecture.md already made for the sibling ent#101 tile against `/executions/timeline`. It appears in the tooltip, labelled an estimate |
| `unavailable` | a failed per-subscription usage read renders as unavailable — never zeroes, never a healthy-looking row |

`showsReset` carries a third arm for the same reason (#2396): a provider-warned row at 78%
used to be (wrongly) `crit`, and `crit` is what put the reset on the row. Demoting it to
`warn` without that arm would have silently removed *when the limit clears* — the one
actionable fact — from the row whose entire message is "this is about to matter", and only
below the 85% band where the provider is the sole source of the warning.

Cost is always `≈`: a subscription is a flat fee, so the figure is
API-equivalent, not a bill. The tooltip states that 7d *contains* the 5h window
so the two are never read as additive, and surfaces
`headroom.status == "error"` as the reason the percentages are missing.
`invalid_token` was in this tooltip from the start — described as "the most
actionable state the payload carries and otherwise indistinguishable from 'no
provider data'" — and #2353 is that observation cashed out: it was ALSO
indistinguishable from real throttling, which is worse, so the state now leads
the row face and the tooltip keeps the remedy.

**Empty vs failed vs stale**, the three states this surface must not blur: the
empty branch requires a fetch that SUCCEEDED and returned zero (`listLoaded &&
!listError`), never `rows.length === 0`; a wrong-shaped 200 is a FAULT, not an
empty fleet (`stores/subscriptions.js` guards `Array.isArray`, mirroring the
ent#100 fetchers) — laundering it would tell an operator their subscriptions are
*not configured*, a different and much worse claim than "could not read them";
and a poll that fails *after* a good one keeps its rows and marks the stamp
`stale` rather than replacing real data with an error panel.

**Overflow is disclosed, not clipped.** Past the fixed track count a row is
clipped by `InfoTile`'s `overflow: hidden` with no scroll or ellipsis, so the
pure function returns `visibleRows`/`totalRows`, the last track becomes a
`+N more` link, and the stamp reads `3 of 9`. Sort is a **total** order
(severity → 429 volume → utilization → name) so equal rows cannot reshuffle
between polls, and the utilization term uses the **fullest** window — ranking on
the 5h alone buries a subscription whose weekly is the one about to run out.

Reading the dashboard drives #471's ambient headroom refresh — floored
server-side at one probe per 15 min per subscription. That is the intended
demand-driven design (an unwatched instance probes nothing); an open dashboard
keeping headroom warm is the point of the tile. The trend line is **ent#433's**
(headroom history): this tile ships point-in-time only, by explicit coordination.

### Chassis rules a list tile must honour

Centralised in `TileRowList.vue` rather than repeated per tile, because each is
dashboard-breaking when forgotten:

- **`.nodrag` on every interactive child** — `FleetGrid.onTilePointerDown` checks
  `e.target.closest('.nodrag')`; without it, clicking a row starts a tile drag.
- **No internal scroll, ever** — `FleetGrid.onWheel` calls `preventDefault()`
  unconditionally and zooms, so a nested `overflow-y` is unusable. Bounded data
  is bounded by construction (server `limit`) with the total stated.
- **Fixed row tracks + `nowrap` ellipsis** — `InfoTile`'s body is
  `overflow: hidden`, so an overrun neither scrolls nor ellipsizes: the last row
  silently vanishes, and the node-environment suite structurally cannot see it.
  Only the identity column may consume slack; meta columns and any chip are
  fixed-width with their own ellipsis, so the agent name is not what loses.
- **Route names must exist** — a `RouterLink` to an unknown route throws during
  render, aborts Vue's update for the whole tree and freezes the dashboard.

## Failure modes & edge cases

- Corrupt/unavailable localStorage → default layout, session-local.
- Agent deleted/renamed mid-session → self-healing pass; a mid-drag removal
  cancels the drag cleanly.
- Stopped agent → Offline state, context chart flattens to a dash.
- Analytics fetch error with no cache → charts degrade to flat/na quietly.
- WS `workingState` entries leaked by missed end events are reconciled by
  the 15s context-stats poll (entries younger than one poll period are
  spared to avoid a stale response evicting a fresh start).

## Testing

`src/frontend/e2e/dashboard-grid-view.spec.js` — mode toggle + tile render
(@smoke), mode persistence across reload, drag-to-cell with socket preview +
layout persistence, tidy/reset, and Timeline coexistence (@smoke; Graph mode decommissioned #1689).

Unit (node environment — pure modules and static source guards, no mounting):
`tests/unit/executionFailure.spec.js` (the ent#100 state machine, including an
exhaustive sweep proving no fault combination reaches the green ✓),
`tests/unit/timestamps.spec.js` (`formatCompactAge` boundaries + negative-age
floor, `serverSkewMs`), `tests/unit/gridTokens.spec.js` (now AUTO-DISCOVERING
every tile recursively, `parts/` included — it was a hand-written two-entry list,
so a new tile was unguarded until someone remembered it), and
`tests/unit/gridTileLinks.spec.js` (now brace-balanced over `link-to=`, `:to=`
and bare `to:` object literals across `components/tiles/**` — it previously
matched only a literal `link-to` ATTRIBUTE, so every per-row link was invisible
to the guard whose failure mode is a frozen dashboard).

## Out of scope (tracked follow-ups)

Fleet KPI strip; "Needs your attention" + live-activity right rail;
server-side per-user layout storage; the widget-chassis documentation itself
(#2126 — the `widget:*` occupant type, what `InfoTile` deliberately lacks, the
org-overlay interlock, the layout v1→v2 copy-migration, prefs-as-override-map);
persisting `error_code` so the failure chip carries the platform taxonomy;
WS-driven early refresh for the failures tile; the sibling **Next schedules**
tile (ent#99), held.

## Org overlay — department zones + reporting lines (trinity-enterprise#305)

Organizational layer over the same lattice; OSS-core. Full requirement:
`docs/memory/requirements/core-agent.md` § Grid Org Overlay.

**Data model (namespaced tags, no schema change).** Department =
`dept-<name>` tag; reporting line = `reports-to-<agent>` tag on the REPORT
agent (direction = which row carries the tag). Prefix constants live twice by
contract: `src/backend/db/tags.py` (`ORG_TAG_PREFIXES`) and
`src/frontend/src/utils/gridOrg.js` — keep in sync.

**Frontend flow.** `utils/gridOrg.js` (pure: parsing, `orgMeta` bootstrap
gate, `computeZones` hulls + `ZONE_CHROME` budget, `computeEdges` with
arrowheads, `arrangeByDept`, `tidyByDept`, `newcomerOrigin`, `deptSlot` hash
palette, `isOrgTag`) → `composables/useOrgOverlay.js` (all org state +
gestures: connect-port drag with live pill, drop-to-assign re-validated at
drop, zone-header block move with rAF throttling + drop-time re-validation,
canvas toast with Undo, New-department assign mode) → `FleetGrid.vue`
(template layers: zones under wires under tiles; canvas-space panels) +
`AgentTile.vue` (dept ribbon via `--gv-dept-N` slot vars, light 600 / dark
400). Tag writes go through the **network store** (atomic `PUT /tags`
set-list; refetch-on-failure; `{previous, next}` returned for Undo).

**Backend flow.** `routers/tags.py`: org namespaces are human-only
(`_guard_org_namespace` rejects agent principals — #1578 pattern; the
set-list guard checks the DELTA) and every mutation broadcasts
`agent_tags_changed` (network store patches by name — cross-browser
convergence without a roster poll). Rename: `metadata.py:rename_agent` calls
`db/tags.py:rename_reports_to_refs` in the SAME transaction
(delete-colliding-then-update; the `(agent_name, tag)` PK would otherwise
abort the whole rename). Hard purge: `agent_cleanup.py:cascade_delete` calls
`delete_reports_to_refs` (dangling refs must not re-attach to a reused name).
Soft delete keeps refs; `computeEdges` skips unplaced endpoints.

**Guardrails.** Zones derived (hull model) — never constrain the lattice;
bootstrap fallback (first plain tag = dept while zero `dept-*` exist
fleet-wide) renders READ-ONLY zones; `zoneAt` resolves overlap by smallest
hull; roster changes cancel all in-flight org gestures; spacing contract
(chrome 22/10/34/10 ≤ gaps 40/50) pinned by unit test.

**Testing.** `src/frontend/tests/unit/gridOrg.spec.js` +
`gridLayout.spec.js` (vitest, `npm run test:unit`, wired into
frontend-build.yml); `tests/unit/test_305_org_tag_integrity.py` (rename
collision, purge sweep, namespace guard); e2e smoke in
`src/frontend/e2e/grid-org-overlay.spec.js`.

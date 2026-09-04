# Feature Flow: Dashboard List View (Agents-page consolidation)

> **Last Updated**: 2026-09-03 (#2358: one column sizing context; label leads, slug follows; badge policy)
> **Status**: Implemented — third dashboard mode
> **Issue**: trinity-enterprise#260
> **Requirements**: `docs/memory/requirements/core-agent.md` §9.9 (and §9.8 for the mode set)
> **Supersedes**: [agents-page-ui-improvements.md](agents-page-ui-improvements.md) (the standalone `/agents` page is retired; its list lives on here)

## Overview

The Dashboard's third view mode (Timeline / Grid / **List**; Timeline stays the
default). The standalone Agents page (`views/Agents.vue`) is deleted; its row
list — filters, sort, selection + bulk tag ops, toggles, stats, three
responsive layouts — is extracted into `components/AgentListPanel.vue` and
mounted through the existing view-mode machinery, so mode persistence
(`localStorage['trinity-dashboard-view']`), the `VIEW_MODES.includes()`
degrade guard, and the chassis header (tag/owner filters, time range,
refresh) all come free.

**Zero backend changes.** The fleet payload (`GET /api/agents`) already
carries `tags`, `read_only_enabled`, and `display_label` per row, so both of
the old page's per-agent N+1 mount loops (`fetchAllAgentTags`,
`fetchAllReadOnlyStates`) were **deleted, not migrated**. This is also more
correct: the per-agent read-only GET 404'd on stopped containers and the page
coerced that to `false`; the payload field is backend truth.

## Components & Data Flow

```
stores/network.js
 ├─ VIEW_MODES ['grid','timeline','list'] · setViewMode(mode, {persist=true})
 │     persist:false skips ONLY the localStorage write (the ?view= deep-link)
 ├─ visibleAgents = computed(agents ∘ owner-filter)   ←— ent#261 seam
 │     `agents` is already server-side tag-filtered by fetchAgents.
 │     Feeds the GRID and LIST panes ONLY (Dashboard :agents prop).
 │     The timeline/node paths deliberately do NOT read it — sibling ent#261
 │     switches ReplayTimeline's :agents prop to this computed itself.
 └─ fetchAgents → agents.value = data ; agentsStore.agents = [...data]
       (write-through: keeps #1643 displayNameForSlug tab titles warm with
        zero extra HTTP; cycle-free — agents.js no longer imports this store.
        GATED on no active quick-tag filter — `params.tags` narrows the
        response server-side, and a filtered subset must not clobber the
        full-fleet list agentsStore consumers read; shallow copy so the two
        stores share rows but never the array)

views/Dashboard.vue (chassis)
 ├─ header: mode toggle ['timeline','grid','list'] (second home of VIEW_MODES)
 │          + Create Agent button + CreateAgentModal (all modes; close →
 │            fetchAgents, since the WS agent_created event can lag; label
 │            icon-only below `md` so the fixed controls cluster leaves the
 │            #1830 stats ladder its 71px agents-only floor at 640px)
 ├─ watch(route.query.view, {immediate}) → setViewMode(v, {persist:false})
 │          → router.replace strips `view` (spreads the rest — ?onboarding=1
 │            survives), .catch(() => {}) for redundant-navigation rejections
 ├─ pane v-if list → 4-state ladder in a flex-1 min-h-0 wrapper:
 │     1 isFleetLoading && agents.length===0  → SkeletonLoader variant="rows"
 │     2 fleetLoadError && agents.length===0  → error + Retry (refreshAll)
 │     3 visibleAgents.length===0             → true-empty "Get started"
 │                                              → openOnboarding() (grid-identical)
 │     4 else → AgentListPanel
 └─ refreshAll(): list branch calls listPanelRef.refresh() (sync health only)

components/AgentListPanel.vue  (props-driven; root owns scroll: h-full
 │                              overflow-y-auto + the old page shell's padding,
 │                              so the half-out-of-card avatar never clips)
 ├─ props: agents (= visibleAgents), availableTags (chassis /api/tags fetch)
 ├─ emits: tags-changed (BEFORE awaiting the fleet refresh — an emit after an
 │         await inside a torn-down component is dropped),
 │         clear-chassis-filters (Clear-all clears the chassis tag/owner
 │         layers too — the old button cleared all four filters)
 ├─ expose: refresh() (= fetchSyncHealth)
 ├─ networkStore: contextStats/executionStats/slotStats · toggleAgentRunning /
 │               toggleAutonomy (+ isTogglingRunning) — result-object
 │               {success,error} toasts (the store returns, never throws)
 ├─ agentsStore: sortBy/setSortBy · syncHealth/fetchSyncHealth
 │               (mount fetch + 60s visibility-aware interval, dies on unmount)
 ├─ utils/agentSort.js: pure sortAgents(list, sortBy, executionStats) —
 │               full-list contract (partitions + re-pins system rows first),
 │               #1642 display-name sort, zero-task-to-bottom tiebreak
 └─ local: name/status filters (NEW trinity-dashboard-list-filter-* keys)
           · selection + bulk tag ops (raw axios, existing endpoints)
           · sticky bulk toolbar (sticky top-0 inside the panel's scroll)
           · toasts · data-agent="<slug>" row hooks for e2e

lg row anatomy (#2358) — ONE sizing context:
  list container  flex flex-col gap-y-1.5
                  lg:grid lg:grid-cols-[auto auto auto 1fr 46px 22rem 180px auto auto auto]
                  lg:gap-x-4                      ← the template is declared HERE, once
   ├─ header  hidden lg:grid lg:grid-cols-subgrid lg:col-span-full items-center py-2
   │            data-testid="list-header"; cells 1-3 / 9 / 10 are spacers; the five
   │            labelled cells carry data-col="name|status|controls|success|stats"
   └─ row ×N  … lg:grid lg:grid-cols-subgrid lg:col-span-full lg:items-center
                lg:gap-y-1 lg:py-3                ← still the visual box (bg/rounded/
                                                     hover/border-l, avatar's abs parent)
         ├─ <div class="hidden lg:contents">      ← one breakpoint switch, adds no box
         │     9 row-1 cells (same data-col hooks as the header)
         │     + secondary line  lg:row-start-2 lg:col-start-4 lg:col-end-10
         │           flex-nowrap min-w-0 overflow-hidden, meta ink on the container
         │           slug(code, select-all, truncate max-w-1/2) · pressure · runtime · tags · +N
         │     + CapacityMeter  lg:col-start-10 lg:row-start-1 lg:row-span-2 lg:mr-4
         ├─ md layout   (display:none at lg — never a grid item)
         └─ base layout (display:none at lg)
  Neither the header nor the row carries ANY horizontal padding at lg — not even
  an explicit `lg:px-0`, which the structural guard rejects along with the rest
  of `lg:p[xlr]-`. Edge insets are ITEM MARGINS (checkbox lg:ml-8, meter
  lg:mr-4), identical on the header and every row.

router: /agents ─fn-redirect(query-preserving, view:'list')→ /
        (exact segment — /agents/:name and deeper untouched)
NavBar: Agents entry removed; Dashboard active on '/' || isAgentSection
```

## Key Decisions

- **D1 — data spine**: networkStore feeds everything (single `/api/agents`
  fetch, chassis-coherent filters, WS-live rows); agentsStore composed in for
  exactly `sortBy` + `syncHealth`. The fetch write-through keeps
  `agentsStore.agents` warm for #1643 tab titles and the WS merge target
  (gated: a quick-tag-filtered fetch never clobbers the full-fleet list).
- **D2 — redirect-with-intent**: `/agents` → `/?view=list`, applied via an
  immediate route **watch** (an onMounted-only read would no-op when
  navigating to `/?view=list` on an already-mounted Dashboard), then stripped.
  **Non-persisting** — a stale bookmark is not a preference statement; AC-4
  protects the *selected* view. `?view=` is thereby a general non-persisting
  deep-link for all modes.
- **D7 — pane states**: the chassis owns the loading/error/true-empty ladder
  (no false "No agents yet" during a cold 20s fleet fetch); filtered-empty
  ("No matching agents" + Clear all) lives inside the panel.
- **D8 — ent#261 seam**: the visible-set predicate lives in ONE place
  (`visibleAgents` in network.js), now consumed by ALL THREE panes — ent#261
  layered the type-to-filter query into the seam (split as
  `ownerFilteredAgents` ∘ query) and switched the timeline's `:agents` prop
  onto it. Node rebuilds read the pre-query `ownerFilteredAgents`, never the
  seam.
- **ent#261 composition (chassis query ∘ panel filters)**: the chassis `/`
  type-to-filter AND-stacks on top of the panel's own persisted name/status
  filters (`displayAgents` filters the already-query-narrowed `:agents`
  prop). Honesty rules: the panel's "N/M" count badge is **suppressed while
  the chassis query is active** (the pill already shows "X of Y match"
  against a different denominator — two disagreeing counters must never
  render simultaneously); the chassis query-empty overlay *precedes* the
  panel (zero query matches → panel mounts with zero rows under the overlay,
  never the onboarding CTA — `!filterActive` guard on the true-empty branch);
  the panel's own filtered-empty ("No matching agents") handles its own layer
  when the panel filters, not the query, narrow to zero.
- **D11 — fresh filter keys**: `trinity-dashboard-list-filter-name`/`-status`
  are a clean break from `trinity-agents-filter-*` — a filter persisted on
  the dead page can never silently narrow the new tab. Old keys (incl. the
  legacy tag-key migration block) are simply no longer read.
- **System-row Run guard (item B)**: the list hides the Run toggle on system
  rows, matching the grid tile — two tabs of one widget must agree. Stopping
  `trinity-system` remains available on its AgentDetail page. **At `md`, hidden
  means RESERVED (#2358)**: the three toggles sit BEFORE the `flex-1` success
  bar on line 2, so a `v-if` that dropped one pulled the success bar ~298px
  left on exactly the system and shared rows (measured), with no header there
  to line up against; `invisible` keeps the box, takes no clicks, and is sized
  by the toggle itself so it cannot drift from a hand-written rem. The meter
  and task counts never moved — they sit after the `flex-1`, which absorbs the
  freed space. **At `base` the Run toggle stays DROPPED (`v-if`)**, the
  opposite call for the opposite geometry: its group is `flex-shrink-0` behind
  a `flex-1` name, so the chevron is already flush right on every row (measured
  at 390px: same x present, absent or reserved). A reservation would align
  nothing and would cost the system row the toggle's ~94px — enough to truncate
  a labelled `trinity-system` at the narrowest supported width (name 221px →
  119px, measured), which is what AC #6 forbids.
- **Backend-authoritative visibility (item A)**: the old page's client-side
  admin gate on the system agent is dropped — `get_accessible_agents` already
  scopes the fleet list server-side, exactly as grid/timeline render it.

- **D12 — one column sizing context, via CSS subgrid (#2358)**: the header
  and each row used to be two INDEPENDENT grids sharing a copy of the same
  template string, so every `auto` track (checkbox, dots, Exec/Sched, arrow)
  resolved against its own content and the `1fr` name track absorbed the
  difference — a row reading `14/16` put Controls/Success at a different x than
  a row reading `0`, and neither matched the header. Two further drifts rode
  along: header spacers were not the row cells' widths (`w-3`/`w-6` vs
  `w-2.5`/`w-4`), and the row grid was 10px narrower than the header because the
  `CapacityMeter` sat OUTSIDE it as a flex sibling. The template is now declared
  **once** on the list container and the header and every row are
  `lg:grid-cols-subgrid lg:col-span-full` items of it, with the meter joining as
  track 10 so both share a right edge. Three rules keep it true: **no horizontal
  padding on any subgrid item** (insets are ordinary item margins — `lg:ml-8` on
  the checkbox cell, `lg:mr-4` on the meter — so nothing depends on the Grid L2
  §7.1 padding-inside-edge-tracks corner); **definite placement in BOTH axes**
  for the two items that are not on row 1 (secondary line
  `lg:row-start-2 lg:col-start-4 lg:col-end-10`, meter
  `lg:col-start-10 lg:row-start-1 lg:row-span-2`) — sparse auto-placement would
  otherwise land the meter in rows 2–3 and hang an implicit third row below the
  line; and the `lg` cells are grouped under one `hidden lg:contents` wrapper,
  which adds no box. Only the `lg` layout is a grid; `md`/`base` are
  `display:none` siblings and never become grid items. Pinned by structural
  guards in `tests/unit/agentName.spec.js` and by the e2e column-alignment test.
- **D13 — the label leads, the slug follows, on the line that already exists
  (#2358)**: a labelled row used to render the label ALONE with the slug
  `title`-only — two naming conventions in one list, and nothing connecting
  `Delivery Operations Manager` to the `delivery-ops` that URLs, MCP keys and
  containers are keyed on (§1.3.1 FR-4). The slug now renders as real,
  selectable `<code>` (`select-all`, so one click takes the whole hyphenated
  slug) on the row's EXISTING always-rendered secondary line — the `lg`/`md`
  tags row and the `base` meta line — never on a new line, so a labelled row is
  exactly as tall as an unlabelled one (contract: layout stability). Resolution
  goes through one pure helper, `utils/agentName.js::agentNameParts(agent)` →
  `{primary, secondary}`, where `secondary` is either `null` or exactly
  `agent.name`; there are no `display_label || name` chains at any call site
  (guarded). Same treatment on the Grid tile's `.t-repo` line
  (dashboard-grid-view.md). `agentNameTooltip` stays as a belt for a truncated
  label — it is no longer the slug's only home. **md density change**: the md
  tags row is now always rendered (`min-h-[1.375rem]`, matching `lg`) because it
  hosts the slug there, so a TAGLESS md row grows ~30px; that also removes a
  latent md jitter where rows with tags were taller than rows without.
- **D14 — badge policy, and the rule that keeps the secondary line from
  becoming a junk drawer (#2358)**: the name cell carries **exception markers
  only** — SYSTEM, GHOST, Shared. The #471 subscription-pressure badge (present
  on essentially every row of a shared-subscription fleet) moves to the `lg`
  secondary line, FIRST in it (a problem signal escalates to the front — the
  grid tile's chip-strip precedent), severity colour kept, predicate untouched.
  The runtime badge renders **only for a non-default runtime**
  (`utils/agentRuntime.js::showsRuntimeBadgeInList`, default `claude-code`) on
  the `lg`/`md` secondary line and never at `base`: relocating a badge that is
  on every row is not a reduction, so on a homogeneous fleet it disappears
  entirely and on a mixed fleet it marks the exceptions. The rule is
  platform-anchored in a pure util rather than derived from fleet majority,
  which would silently flip badges as the fleet changes. **Secondary-line
  contract** (`lg`/`md`): fixed order `slug · pressure · runtime · tags · +N`;
  `flex-nowrap min-w-0 overflow-hidden`; slug `truncate max-w-[50%]`; badges
  `flex-shrink-0`; tags keep their counted `+N`. A future badge goes here in
  that order or it does not go in the row.

## Teardown state loss (by design)

The list pane is `v-if`-mounted, so switching modes destroys the panel.
**Survives** a mode switch: name/status filters (localStorage), `sortBy`
(agentsStore, session-lived). **Does not survive**: row selection, open
bulk-op dropdowns, in-flight toast. Bulk tag ops emit `tags-changed` *before*
awaiting the fleet refresh so the chassis tag counts stay correct even when
the user switches modes mid-operation.

## Testing

- `e2e/dashboard-list-view.spec.js` — system-row render (`data-agent` hooks;
  deliberately no toggle-visibility assertions — CI's only agent is the
  guarded system agent), AC-4 persistence, AC-2 redirect (param stripped,
  saved mode untouched), `?onboarding=1` passthrough, name-filter +
  filtered-empty recovery, three-mode round-trip.
- Adjusted: `smoke.spec.js` (nav link gone; redirect assertion),
  `browser-tab-titles.spec.js` (Agents hop removed; redirect title),
  `dashboard-grid-view.spec.js` (comment), `dashboard-stats-overflow.spec.js`
  (`list` added to MODES — the third toggle + Create button widen the controls
  cluster in every mode), `navbar-overflow.spec.js` (test 3's 640px squeeze is
  conditional on measured overflow — the 4-link OSS bar may fit where 5 links
  overflowed).
- `tests/unit/agentName.spec.js` (#2358) — `agentDisplayName` /
  `hasDistinctLabel` / `agentNameParts` / `agentNameTooltip` / `agentOptionLabel`
  over the full edge set (blank + whitespace + non-string labels, bare-string and
  null inputs), the load-bearing property that `secondary` is either `null` or
  exactly `agent.name`, plus three **structural** guards read from the SFC source
  (`node:fs`, the `gridTileLinks.spec.js` pattern — vitest is `environment: 'node'`
  with no mount harness): no `display_label` `||`/`??`/ternary chain in
  `AgentListPanel.vue` or `AgentTile.vue`; the meter's and secondary line's
  definite placement classes; exactly one `lg:grid-cols-[` template with
  `lg:grid-cols-subgrid` on both the header and the row.
- `tests/unit/agentRuntime.spec.js` (#2358) — `isDefaultRuntime` /
  `showsRuntimeBadgeInList` over present/absent/blank/foreign runtimes.
- `e2e/dashboard-list-view.spec.js` (#2358, serial block with an `afterEach`
  label restore) — per-`data-col` header/row bounding-box equality at 1280 and
  1440 with the meter proven to sit in grid rows 1–2; a labelled agent rendering
  BOTH names with a selectable slug and the SYSTEM badge intact; md/base degrade
  plus row-height parity between the labelled and unlabelled states.
- Backend untouched (this flow has no backend surface). Frontend unit tests run
  under vitest (`npm run test:unit`, `environment: 'node'` — pure modules and
  source-level guards only; component behaviour is covered by Playwright).

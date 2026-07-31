# Feature Flow: Dashboard List View (Agents-page consolidation)

> **Last Updated**: 2026-07-31 (initial implementation)
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
  `trinity-system` remains available on its AgentDetail page.
- **Backend-authoritative visibility (item A)**: the old page's client-side
  admin gate on the system agent is dropped — `get_accessible_agents` already
  scopes the fleet list server-side, exactly as grid/timeline render it.

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
- No frontend unit-test infra exists (Playwright-only); backend untouched.

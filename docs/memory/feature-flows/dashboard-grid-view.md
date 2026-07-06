# Feature Flow: Dashboard Grid View (magnetic tile canvas)

> **Last Updated**: 2026-07-06 (initial implementation)
> **Status**: Implemented — third dashboard mode, not default
> **Issue**: trinity-enterprise#47 (design of record embedded in the issue)
> **Requirements**: `docs/memory/requirements/core-agent.md` §9.8

## Overview

The Dashboard's third view mode alongside **Graph** (Vue Flow topology) and
**Timeline** (waterfall activity). Grid is a **magnetic tile canvas**: richer
384×216 landscape agent tiles that snap to a sparse, **unbounded** integer
lattice (negative coordinates included) the operator arranges freely —
islands, gaps, parked loners — with iPhone-style drag and live snap preview,
on the same pan/zoom dotted-canvas language as the graph view.

- Mode toggle: `Grid / Graph / Timeline` in the Dashboard header; selection
  persists to `localStorage['trinity-dashboard-view']`. **Timeline stays the
  default** for users with no saved preference.
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
  │                          (15s shared poll), NEW: viewMode ('grid'|'graph'|'timeline'),
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
  absence is never treated as deletion).
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
   list the Dashboard already holds; per-section skeletons while analytics
   stream in. Nothing awaits the full set.
2. **Lazy, capped hydration** — a tile asks the fleetGrid store to hydrate
   only when near the viewport (culled tiles render a light placeholder and
   fetch nothing); fetches run through a 4-slot queue into the executions
   store's `(agent, window)` cache; stale entries (>5 min) serve instantly
   and refresh in the background.
3. **Batch endpoints over per-agent loops** for chip data; the 60s poll is
   visibility-aware (skips when `document.hidden`) and tears down when the
   Grid unmounts (mode switch is `v-if`).
4. **A slow or failed per-agent fetch degrades that one tile only.**

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
layout persistence, tidy/reset, and Graph/Timeline coexistence (@smoke).

## Out of scope (tracked follow-ups)

Fleet KPI strip; "Needs your attention" + live-activity right rail;
server-side per-user layout storage.

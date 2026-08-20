/**
 * Grid info-tile catalog (trinity-enterprise#325).
 *
 * Imported for its SIDE EFFECT by `FleetGrid.vue`: each entry registers into
 * the `GRID_WIDGETS` registry in `utils/gridWidgets.js`. That module stays
 * free of `.vue` imports so it remains unit-testable under vitest's node
 * environment — this file is where the component references live instead.
 *
 * Adding a tile (epic #94 sub-issues #95-#101, #259) is one block here plus
 * the component. Components are imported eagerly, not lazily: a tile that
 * fails to resolve should break the build rather than render as a silent
 * blank cell on an operator's dashboard.
 */
import { registerWidget } from '@/utils/gridWidgets'
import FleetSummaryTile from './FleetSummaryTile.vue'
import RecentFailuresTile from './RecentFailuresTile.vue'
import ExecutionsTile from './ExecutionsTile.vue'
import SubscriptionPressureTile from './SubscriptionPressureTile.vue'

registerWidget({
  id: 'fleet-summary',
  title: 'Fleet summary',
  scope: 'Fleet',
  component: FleetSummaryTile,
  adminOnly: false,
  defaultOn: true,
  cells: { w: 1, h: 1 },
})

registerWidget({
  id: 'recent-failures',
  title: 'Recent failures',
  component: RecentFailuresTile,
  adminOnly: false,
  defaultOn: true,
  // Its age column is recomputed off the Grid's shared 1s tick, so it opts in.
  wantsTick: true,
  cells: { w: 1, h: 1 },
})

registerWidget({
  id: 'executions',
  title: 'Executions',
  component: ExecutionsTile,
  adminOnly: false,
  defaultOn: true,
  // No clock on its face — the chart is a 24h window refreshed by the store's
  // batch poll, so it does not opt into the 1s tick.
  wantsTick: false,
  cells: { w: 1, h: 1 },
})

registerWidget({
  id: 'subscription-pressure',
  title: 'Subscription pressure',
  component: SubscriptionPressureTile,
  // The FIRST admin-only tile, and not a stylistic choice: every endpoint it
  // reads (`/api/subscriptions`, `/{id}/usage`) is admin-gated because the
  // payload carries per-subscription SPEND, and its footer link goes to
  // Settings, whose route is `requiresAdmin`. Shown to a non-admin it would be
  // a permanently-erroring tile pointing at a page they cannot open. The flag
  // also does the gating work for free: the widget key is absent for
  // non-admins, so the batch poll's fetch never fires and there is no 403 loop.
  adminOnly: true,
  // Explicit, because the registry default is `false` and this tile's whole
  // premise is answering the question WITHOUT opening Settings — shipping it
  // off-by-default would mean opening a menu to avoid opening a page. Matches
  // all three siblings; `adminOnly` already bounds who ever sees it.
  defaultOn: true,
  // Reset times render as absolute clock times ("resets 14:05"), not a live
  // countdown, so the tile needs no per-second tick.
  wantsTick: false,
  cells: { w: 1, h: 1 },
})

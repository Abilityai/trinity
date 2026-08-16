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

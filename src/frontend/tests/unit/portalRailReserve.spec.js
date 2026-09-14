import { describe, it, expect } from 'vitest'
import { railColumnReservedFor, railVisibleFor } from '../../src/components/portal/portalRail.js'

/**
 * #2711 — the rail column is reserved while the stage loads.
 *
 * The contract's layout-stability rule: loading and loaded share one footprint,
 * nothing shifts on arrival. The rail's CONTENT needs the roster, but its WIDTH
 * does not — it comes from persisted state and is known at first paint — so the
 * column is held open empty rather than appearing later and taking the
 * conversation column's width with it.
 */
describe('railColumnReservedFor', () => {
  it('reserves on a 1:1 conversation route while the stage loads', () => {
    expect(railColumnReservedFor({ stageState: 'loading' })).toBe(true)
  })

  it('stops reserving once the stage reaches any settled verdict', () => {
    // Not just `ready`: an empty or failed stage renders no rail, and holding a
    // gap open next to a failure would be inventing a column.
    for (const stageState of ['ready', 'empty', 'failed']) {
      expect(railColumnReservedFor({ stageState }), stageState).toBe(false)
    }
  })

  it('never reserves on an agent page, which carries no rail at all', () => {
    expect(railColumnReservedFor({ agentPage: 'scout', stageState: 'loading' })).toBe(false)
  })

  it('never reserves on a room route', () => {
    // A room's rail depends on `roomsAvailable`, which arrives ON the roster
    // payload (#2128) — reserving on a capability we have not been told about
    // would trade this shift for the opposite one on an install without rooms.
    expect(railColumnReservedFor({ roomId: 'r1', stageState: 'loading' })).toBe(false)
  })

  it('defaults to reserving when called with nothing, because loading is the default', () => {
    expect(railColumnReservedFor()).toBe(true)
  })
})

describe('reserve and visible are complementary, never both', () => {
  const routes = [
    { name: '1:1 loading',   args: { stageState: 'loading', activeAgent: 'scout' } },
    { name: '1:1 ready',     args: { stageState: 'ready', activeAgent: 'scout' } },
    { name: 'agent page',    args: { stageState: 'loading', agentPage: 'scout' } },
    { name: 'room loading',  args: { stageState: 'loading', roomId: 'r1', roomsAvailable: true } },
    { name: 'unreachable',   args: { stageState: 'ready', activeAgent: 'ghost', unreachable: true } },
  ]

  it('never claims the column twice for the same state', () => {
    // The wrapper renders on `railHasColumn || railColumnReserved`; if both were
    // true at once nothing would break, but the reserve would be silently
    // meaningless — and if BOTH are false on a 1:1 route the column is gone,
    // which is the shift this fixes.
    for (const { name, args } of routes) {
      const reserved = railColumnReservedFor(args)
      const visible = railVisibleFor(args)
      expect(reserved && visible, `${name}: reserved and visible at once`).toBe(false)
    }
  })

  it('keeps a 1:1 conversation column continuously occupied across the transition', () => {
    // The property that actually matters: loading → ready must not pass through
    // a state where neither holds the column.
    const loading = { stageState: 'loading', activeAgent: 'scout' }
    const ready = { stageState: 'ready', activeAgent: 'scout' }
    expect(railColumnReservedFor(loading) || railVisibleFor(loading)).toBe(true)
    expect(railColumnReservedFor(ready) || railVisibleFor(ready)).toBe(true)
  })

  it('leaves an agent page unoccupied throughout, as before', () => {
    for (const stageState of ['loading', 'ready']) {
      const args = { agentPage: 'scout', stageState }
      expect(railColumnReservedFor(args) || railVisibleFor(args), stageState).toBe(false)
    }
  })
})

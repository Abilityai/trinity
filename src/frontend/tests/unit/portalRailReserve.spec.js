import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { railColumnReservedFor, railVisibleFor, railSizedOpen } from '../../src/components/portal/portalRail.js'

/**
 * #2711 — the rail column is reserved while the stage loads.
 *
 * @source-text-pin: the last describe is a spelling guard — `route.value`
 *   must never appear in Portal.vue, because a `useRoute()` getter that reads
 *   `.value` throws inside a watcher and Vue swallows it (the page renders,
 *   the watcher is dead, every e2e passes). A mount proves the page renders;
 *   only the text proves the spelling is absent. The reserve/visible rules
 *   above are executed directly.
 *
 * The contract's layout-stability rule: loading and loaded share one footprint,
 * nothing shifts on arrival. The rail's CONTENT needs the roster, but its WIDTH
 * does not — it comes from persisted state and is known at first paint — so the
 * column is held open empty rather than appearing later and taking the
 * conversation column's width with it.
 */
// #3060 — the reservation was sized as COLLAPSED even for a rail the person
// left open: `useColumnResize` was told the rail is open only when it is
// VISIBLE, and it is never visible while the stage loads. So a persisted-open
// rail reserved 48px, then jumped to its open width (384px by default) the
// moment the stage was ready, shoving the conversation's right edge 336px in
// one frame. The reserved column must take the width the rail will have.
describe('railSizedOpen (#3060)', () => {
  it('a persisted-open rail is sized open while its column is reserved', () => {
    expect(railSizedOpen({ open: true, visible: false, reserved: true })).toBe(true)
  })
  it('and open once it is visible, as before', () => {
    expect(railSizedOpen({ open: true, visible: true, reserved: false })).toBe(true)
  })
  it('a collapsed rail is collapsed in every phase', () => {
    for (const phase of [{ visible: false, reserved: true }, { visible: true, reserved: false }]) {
      expect(railSizedOpen({ open: false, ...phase })).toBe(false)
    }
  })
  it('with no column at all (agent page, room load, failed stage) it is not open', () => {
    expect(railSizedOpen({ open: true, visible: false, reserved: false })).toBe(false)
  })
  it('reads booleans strictly — a truthy non-boolean never opens it', () => {
    expect(railSizedOpen({ open: 'true', visible: 1, reserved: 1 })).toBe(false)
  })
})

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

describe('the shell reads the route the way the rest of the file does', () => {
  // Not style. `useRoute()` returns a reactive OBJECT, not a ref, so a
  // `route.value.x` getter throws — and Vue routes a watch-getter error to its
  // error handler instead of aborting setup, so the page still renders, every
  // e2e still passes, and the watcher is simply dead. That shipped here: the
  // reset watcher added with the reservation threw on every Workspace load and
  // nothing failed. One spelling in one file, so pin it.
  const SHELL = readFileSync(join(process.cwd(), 'src/views/Portal.vue'), 'utf8')

  it('never reaches through `.value` on the route object', () => {
    const offenders = SHELL.split('\n')
      .map((line, i) => [i + 1, line])
      .filter(([, line]) => /\broute\.value\b/.test(line) && !line.trim().startsWith('//'))
      .map(([n, line]) => `${n}: ${line.trim().slice(0, 80)}`)
    expect(offenders, 'useRoute() is a reactive object — read `route.x`, not `route.value.x`').toEqual([])
  })

  it('still reads the route somewhere, so the guard is not vacuous', () => {
    expect(SHELL).toMatch(/\broute\.(fullPath|params|query|path)\b/)
  })
})

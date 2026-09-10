/**
 * #2617 — the rail's maximum comes from the viewport, not from a constant.
 *
 * `RAIL_MAX = 560` was applied unconditionally, so the wider the display the
 * SMALLER the share a person could give the column: ~22% of a 2560px screen.
 * The operator's report is the whole specification — *"it does not go bigger
 * than like 20% or something. I should be able to make it whatever I want, say
 * half the screen width."*
 *
 * The bound that replaces it was already in the file: `CONVERSATION_MIN`, the
 * readable floor `fitsThreeColumns` already refuses to break. A column may take
 * everything left after its neighbour and that floor — which is generous on a
 * wide screen and unchanged on a narrow one, because it is the same rule at
 * both ends.
 *
 * The subtle half is AC 4, and it has its own section below: a width arranged
 * on a big monitor must survive a stint on a laptop. That needs the DESIRED
 * width and the width the layout can currently give to be two different
 * numbers, which is what `railOpenWidth` vs `effectiveRail` is.
 */
import { describe, it, expect } from 'vitest'

import {
  railMaxFor,
  sidebarMaxFor,
  clampRail,
  clampSidebar,
  fitsThreeColumns,
  readStored,
  writeStored,
  RAIL_MIN,
  RAIL_DEFAULT,
  SIDEBAR_MIN,
  SIDEBAR_DEFAULT,
  CONVERSATION_MIN,
} from '@/composables/useColumnResize'

const LAPTOP = 1280
const DESK = 1440
const WIDE = 2560

function fakeStorage(seed = {}) {
  const map = { ...seed }
  return {
    getItem: (k) => (k in map ? map[k] : null),
    setItem: (k, v) => { map[k] = String(v) },
  }
}

describe('#2617 — the reported case', () => {
  it('lets the rail take at least half a wide screen', () => {
    const max = railMaxFor(WIDE, SIDEBAR_DEFAULT)
    expect(max).toBeGreaterThanOrEqual(WIDE / 2)
    // For the record, since "20% or something" is what was reported:
    expect(max / WIDE).toBeGreaterThan(0.5)
  })

  it('is bigger on a bigger screen — the property a constant cannot have', () => {
    const laptop = railMaxFor(LAPTOP, SIDEBAR_DEFAULT)
    const desk = railMaxFor(DESK, SIDEBAR_DEFAULT)
    const wide = railMaxFor(WIDE, SIDEBAR_DEFAULT)
    expect(desk).toBeGreaterThan(laptop)
    expect(wide).toBeGreaterThan(desk)
  })

  it('is exactly the space left after the sidebar and the conversation floor', () => {
    for (const vp of [LAPTOP, DESK, WIDE, 3840]) {
      expect(railMaxFor(vp, SIDEBAR_DEFAULT)).toBe(vp - SIDEBAR_DEFAULT - CONVERSATION_MIN)
    }
  })

  it('shrinks as its neighbour grows, so the two cannot both claim the space', () => {
    expect(railMaxFor(WIDE, 400)).toBe(railMaxFor(WIDE, SIDEBAR_DEFAULT) - (400 - SIDEBAR_DEFAULT))
  })
})

describe('#2617 — narrow windows are unchanged', () => {
  it('never offers a maximum below the column minimum', () => {
    // A ceiling under the floor would INVERT the clamp — `min(max, max(min,px))`
    // pins the column to the maximum and quietly breaks the floor. On a viewport
    // with no room for three columns the auto-collapse rule applies instead.
    expect(railMaxFor(700, SIDEBAR_DEFAULT)).toBe(RAIL_MIN)
    expect(railMaxFor(1, 9999)).toBe(RAIL_MIN)
    expect(sidebarMaxFor(700, RAIL_DEFAULT)).toBe(SIDEBAR_MIN)
  })

  it('an unmeasured viewport yields the minimum, never a number derived from zero', () => {
    for (const vp of [0, null, undefined, NaN, -100, 'wide']) {
      expect(railMaxFor(vp, SIDEBAR_DEFAULT), String(vp)).toBe(RAIL_MIN)
    }
  })

  it('a clamped drag can never break the conversation floor', () => {
    // The property the old constant was standing in for, now exact at every
    // width rather than approximately right at one.
    for (const vp of [1024, LAPTOP, DESK, 1920, WIDE]) {
      const max = railMaxFor(vp, SIDEBAR_DEFAULT)
      const dragged = clampRail(99999, max)
      expect(dragged).toBe(max)
      // At the very narrow end the rail is floored at RAIL_MIN and the
      // auto-collapse rule — not the clamp — is what protects the floor.
      if (max > RAIL_MIN) {
        expect(fitsThreeColumns(vp, SIDEBAR_DEFAULT, dragged), `vp=${vp}`).toBe(true)
      }
    }
  })

  it('the same holds for the sidebar (AC 7 — reviewed, not left behind)', () => {
    for (const vp of [LAPTOP, DESK, WIDE]) {
      const max = sidebarMaxFor(vp, RAIL_DEFAULT)
      expect(clampSidebar(99999, max)).toBe(max)
      expect(fitsThreeColumns(vp, max, RAIL_DEFAULT), `vp=${vp}`).toBe(true)
    }
  })
})

describe('#2617 — a stored width survives a change of screen (AC 4)', () => {
  it('persistence keeps the width the person asked for', () => {
    // Not clamped to the current viewport on the way in or out. Clamping here
    // would write the laptop's ceiling over their choice on the first commit —
    // "silently discarded" wearing a clamp.
    const s = fakeStorage()
    writeStored(s, 'sam', { sidebar: SIDEBAR_DEFAULT, rail: 1400 })
    expect(readStored(s, 'sam').rail).toBe(1400)
  })

  it('a wide-monitor width is clamped for DISPLAY on a laptop, and comes back', () => {
    const desired = 1400   // arranged on the 2560px screen
    const onLaptop = Math.min(desired, railMaxFor(LAPTOP, SIDEBAR_DEFAULT))
    const backOnWide = Math.min(desired, railMaxFor(WIDE, SIDEBAR_DEFAULT))

    expect(onLaptop).toBeLessThan(desired)         // clamped down, not overflowing
    expect(onLaptop).toBe(LAPTOP - SIDEBAR_DEFAULT - CONVERSATION_MIN)
    expect(backOnWide).toBe(desired)               // and restored, not discarded
  })

  it('still floors a hand-edited value that is far too small', () => {
    const s = fakeStorage({
      'trinity-workspace-columns:sam': JSON.stringify({ sidebar: 5, rail: 9 }),
    })
    expect(readStored(s, 'sam')).toEqual({ sidebar: SIDEBAR_MIN, rail: RAIL_MIN })
  })
})

describe('#2617 — the composable exposes the derived ceilings', () => {
  it('offers them where a handle can read them, and not as a stale constant', async () => {
    const src = (await import('fs')).readFileSync(
      (await import('url')).fileURLToPath(
        new URL('../../src/composables/useColumnResize.js', import.meta.url),
      ), 'utf8',
    )
    // `limits.rail.max` is gone rather than left holding an old number.
    expect(src).not.toMatch(/export const RAIL_MAX/)
    expect(src).not.toMatch(/export const SIDEBAR_MAX/)
    expect(src).toMatch(/rail: \{ min: RAIL_MIN, default: RAIL_DEFAULT \}/)
    // Top-level computeds, because a ref nested in a plain object is not
    // auto-unwrapped in a template and would have rendered [object Object]
    // into `aria-valuemax`.
    expect(src).toMatch(/^\s*railMax,$/m)
    expect(src).toMatch(/^\s*sidebarMax,$/m)
  })

  it('the handles read the derived value, so End and aria-valuemax follow it', async () => {
    const shell = (await import('fs')).readFileSync(
      (await import('url')).fileURLToPath(
        new URL('../../src/views/Portal.vue', import.meta.url),
      ), 'utf8',
    )
    expect(shell).toMatch(/:max="columns\.railMax\.value"/)
    expect(shell).toMatch(/:max="columns\.sidebarMax\.value"/)
    expect(shell).not.toMatch(/limits\.(rail|sidebar)\.max/)
    // ...and `:value` is the EFFECTIVE width, so `aria-valuenow` cannot exceed
    // the `aria-valuemax` beside it and a drag starts where the handle is. The
    // behavioural half of this is in the driving block below; this pins the
    // PAIRING, which lives in the template and nowhere else.
    expect(shell).toMatch(/:value="columns\.effectiveSidebar\.value"/)
    expect(shell).toMatch(/:value="columns\.effectiveRail\.value"/)
    expect(shell).not.toMatch(/:value="columns\.(sidebar|railOpenWidth)\.value"/)
  })

  it('the viewport is recorded even while the rail is collapsed', async () => {
    // Otherwise reopening after a window resize would clamp against a stale
    // ceiling — the maxima depend on a number `enforceFit` is the only writer of.
    const src = (await import('fs')).readFileSync(
      (await import('url')).fileURLToPath(
        new URL('../../src/composables/useColumnResize.js', import.meta.url),
      ), 'utf8',
    )
    const fn = src.slice(src.indexOf('function enforceFit'), src.indexOf('function refreshIdentity'))
    expect(fn.indexOf('viewportWidth.value = vw')).toBeLessThan(fn.indexOf("if (!railOpen?.value) return"))
  })

  it('auto-collapse tests the EFFECTIVE width, not the desired one', async () => {
    // Testing the desired width would collapse a rail that fits, purely because
    // the person once arranged a wider one on a bigger screen.
    const src = (await import('fs')).readFileSync(
      (await import('url')).fileURLToPath(
        new URL('../../src/composables/useColumnResize.js', import.meta.url),
      ), 'utf8',
    )
    expect(src).toMatch(/fitsThreeColumns\(vw, effectiveSidebar\.value, effectiveRail\.value\)/)
  })
})

// ---------------------------------------------------------------------------
// The composable itself, driven — not scanned
// ---------------------------------------------------------------------------
//
// Every assertion above this line is either a pure helper or a regex over the
// source. That is why the collapsed-rail ceiling shipped wrong: the AC-4 case
// recomputed its own expectation from `railMaxFor` rather than reading what
// `useColumnResize` actually derives, so the composable's own wiring was never
// executed. It can be: `effectScope` gives it a reactive owner, and the two
// lifecycle hooks are the only thing it wants a component for.

describe('#2617 — driving useColumnResize', () => {
  async function drive({ viewport = LAPTOP, railOpen = true, stored = null } = {}) {
    const { effectScope, ref } = await import('vue')
    const { useColumnResize } = await import('@/composables/useColumnResize')

    const prevWindow = globalThis.window
    const store = fakeStorage(
      stored ? { 'trinity-workspace-columns:anon': JSON.stringify(stored) } : {},
    )
    globalThis.window = {
      innerWidth: viewport,
      localStorage: store,
      addEventListener() {},
      removeEventListener() {},
    }
    // `onMounted` outside a component is a no-op with a dev warning; the hooks
    // only install the resize listener, which these tests drive by hand.
    const warn = console.warn
    console.warn = () => {}

    const open = ref(railOpen)
    const scope = effectScope()
    let columns
    try {
      columns = scope.run(() => useColumnResize({ railOpen: open, setRailOpen: (v) => { open.value = v } }))
    } finally {
      console.warn = warn
      globalThis.window = prevWindow
    }
    return { columns, open, scope, store }
  }

  it('bills the sidebar for the COLLAPSED strip when the rail is closed', async () => {
    // The finding: `sidebarMax` read `effectiveRail`, which carries no
    // `railOpen` term, so a closed rail was charged at its full open width.
    const { columns } = await drive({ viewport: LAPTOP, railOpen: false })
    expect(columns.railWidth.value).toBe(48)
    expect(columns.sidebarMax.value).toBe(LAPTOP - 48 - CONVERSATION_MIN) // 752
  })

  it('does not regress the narrow window below the constant it replaces', async () => {
    // 416 was the number this shipped with — under `SIDEBAR_MAX = 480`, so a
    // laptop came out WORSE than before #2617. AC 3 is that it does not.
    const { columns } = await drive({ viewport: LAPTOP, railOpen: false })
    expect(columns.sidebarMax.value).toBeGreaterThanOrEqual(480)
  })

  it('bills the sidebar for the rail\'s real width when it is open', async () => {
    const { columns } = await drive({ viewport: LAPTOP, railOpen: true })
    expect(columns.railWidth.value).toBe(RAIL_DEFAULT)
    expect(columns.sidebarMax.value).toBe(LAPTOP - RAIL_DEFAULT - CONVERSATION_MIN)
  })

  it('collapsing the rail widens the sidebar\'s ceiling, reopening narrows it', async () => {
    const { columns, open } = await drive({ viewport: LAPTOP, railOpen: true })
    const openCeiling = columns.sidebarMax.value
    open.value = false
    expect(columns.sidebarMax.value).toBeGreaterThan(openCeiling)
    open.value = true
    expect(columns.sidebarMax.value).toBe(openCeiling)
  })

  it('never lets the three columns exceed the viewport, either way round', async () => {
    for (const railOpen of [true, false]) {
      const { columns } = await drive({ viewport: LAPTOP, railOpen })
      const total = columns.sidebarMax.value + columns.railWidth.value + CONVERSATION_MIN
      expect(total).toBeLessThanOrEqual(LAPTOP)
    }
  })

  it('keeps the desired width while showing the clamped one (AC 4)', async () => {
    // Arranged on a wide monitor, viewed on a laptop.
    const { columns } = await drive({
      viewport: LAPTOP, railOpen: true, stored: { sidebar: 700, rail: 900 },
    })
    expect(columns.sidebar.value).toBe(700)              // the desire survives
    expect(columns.railOpenWidth.value).toBe(900)
    expect(columns.effectiveRail.value).toBe(columns.railMax.value)
    expect(columns.effectiveSidebar.value).toBe(columns.sidebarMax.value)
    expect(columns.effectiveSidebar.value).toBeLessThan(700)
  })

  it('reports a width the handle can legally announce (aria-valuenow <= max)', async () => {
    // The second finding: the handles bound `:value` to the DESIRE and `:max`
    // to the derived ceiling, so `aria-valuenow` could exceed `aria-valuemax`
    // and `startValue` began every drag at a position the clamp discards.
    const { columns } = await drive({
      viewport: LAPTOP, railOpen: true, stored: { sidebar: 700, rail: 900 },
    })
    expect(columns.effectiveSidebar.value).toBeLessThanOrEqual(columns.sidebarMax.value)
    expect(columns.effectiveRail.value).toBeLessThanOrEqual(columns.railMax.value)
  })

  it('a drag from a clamped position moves immediately; the desire would not', async () => {
    // Arranged on a 2560px screen, viewed on a 1600px one, so the rail's
    // desire is far above its ceiling and the ceiling is above its floor.
    const { columns } = await drive({
      viewport: 1600, railOpen: true, stored: { sidebar: SIDEBAR_DEFAULT, rail: 2000 },
    })
    expect(columns.railOpenWidth.value).toBe(2000)
    expect(columns.effectiveRail.value).toBe(columns.railMax.value)
    expect(columns.effectiveRail.value).toBeGreaterThan(RAIL_MIN)

    // What ColumnResizeHandle does with whatever `:value` it is given:
    // startValue = props.value, then every move is clamped.
    const drag = (startValue, delta) =>
      Math.min(columns.railMax.value, Math.max(RAIL_MIN, startValue - delta))

    // Bound to the EFFECTIVE width — the handle tracks the pointer from pixel one.
    expect(drag(columns.effectiveRail.value, 1)).toBe(columns.effectiveRail.value - 1)

    // Bound to the DESIRE, as it shipped: the handle is inert for the whole
    // distance between the desire and the ceiling.
    const dead = columns.railOpenWidth.value - columns.railMax.value
    expect(dead).toBeGreaterThan(300)
    expect(drag(columns.railOpenWidth.value, 1)).toBe(columns.railMax.value)
    expect(drag(columns.railOpenWidth.value, dead)).toBe(columns.railMax.value)
  })

  it('records the viewport on resize even while the rail is collapsed', async () => {
    const { columns } = await drive({ viewport: LAPTOP, railOpen: false })
    const before = columns.sidebarMax.value
    columns.enforceFit(WIDE)
    expect(columns.viewportWidth.value).toBe(WIDE)
    expect(columns.sidebarMax.value).toBeGreaterThan(before)
  })

  it('collapses the rail when the window can no longer fit three columns', async () => {
    const { columns, open } = await drive({ viewport: DESK, railOpen: true })
    expect(open.value).toBe(true)
    columns.enforceFit(600)
    expect(open.value).toBe(false)
  })
})

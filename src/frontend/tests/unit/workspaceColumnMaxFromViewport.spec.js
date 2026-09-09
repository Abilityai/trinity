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

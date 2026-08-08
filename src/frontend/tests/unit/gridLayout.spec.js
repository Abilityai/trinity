import { describe, it, expect } from 'vitest'
import { normalizeLayout, tidyLayout, nearestFreeCell } from '@/utils/gridLayout'

describe('normalizeLayout originFor (zone-aware newcomer placement)', () => {
  it('newcomers search from their per-agent origin when provided', () => {
    const saved = { a: { c: 0, r: 0 } }
    const { layout } = normalizeLayout(saved, ['a', 'new'], () => ({ c: 5, r: 5 }))
    expect(layout.a).toEqual({ c: 0, r: 0 })
    expect(layout.new).toEqual({ c: 5, r: 5 })
  })

  it('an occupied origin resolves to the nearest free cell around it', () => {
    const saved = { a: { c: 5, r: 5 } }
    const { layout } = normalizeLayout(saved, ['a', 'new'], () => ({ c: 5, r: 5 }))
    expect(layout.new).not.toEqual({ c: 5, r: 5 })
    // Adjacent ring around the requested origin, not the board origin.
    expect(Math.abs(layout.new.c - 5)).toBeLessThanOrEqual(1)
    expect(Math.abs(layout.new.r - 5)).toBeLessThanOrEqual(1)
  })

  it('an invalid origin falls back to the board origin', () => {
    const { layout } = normalizeLayout({}, ['new'], () => ({ c: NaN, r: 2 }))
    expect(layout.new).toEqual({ c: 0, r: 0 })
  })

  it('without originFor, behavior is unchanged (newcomers near the origin)', () => {
    const { layout, changed } = normalizeLayout({ a: { c: 0, r: 0 } }, ['a', 'new'])
    expect(changed).toBe(true)
    expect(Math.max(Math.abs(layout.new.c), Math.abs(layout.new.r))).toBeLessThanOrEqual(1)
  })
})

describe('existing lattice invariants (regression)', () => {
  it('nearestFreeCell returns the origin itself when free', () => {
    expect(nearestFreeCell({}, 3, 4)).toEqual({ c: 3, r: 4 })
  })

  it('tidyLayout compacts into 3 columns preserving reading order', () => {
    const layout = {
      a: { c: 0, r: 0 },
      b: { c: 2, r: 0 },
      c: { c: 0, r: 2 },
      d: { c: 1, r: 2 },
    }
    const out = tidyLayout(layout)
    expect(out.a).toEqual({ c: 0, r: 0 })
    expect(out.b).toEqual({ c: 1, r: 0 })
    expect(out.c).toEqual({ c: 2, r: 0 })
    expect(out.d).toEqual({ c: 0, r: 1 })
  })
})
